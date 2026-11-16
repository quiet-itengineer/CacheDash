"""
CacheDash alerts: watches the cache continuously (whether or not anyone has the
dashboard open), records incidents in alerts.db, and can notify a chat webhook
(Google Chat, Slack, Teams) or an email address.

Rules (thresholds can be changed in alerts.json next to this file):

  cache_full        critical  Requests rejected with HTTP 503 because the cache is full
  young_purge       warning   Content less than a day old is being evicted (cache too small)
  nearly_full       warning   Cache size is close to its limit, or the disk is nearly full
  traffic_drop      critical  Traffic to devices collapsed suddenly (devices likely fell back to the internet)
  link_saturated    warning   The cache's network link is close to its maximum speed
  service_down      critical  The caching service isn't running / isn't active
  not_registered    critical  The cache isn't registered with Apple (devices can't find it)
  error_burst       warning   Many errors in the AssetCache log in a short time
"""

import json
import os
import re
import smtplib
import sqlite3
import socket
import statistics
import subprocess
import threading
import time
import urllib.request
from collections import deque
from email.message import EmailMessage

import incident
import logs

DEFAULTS = {
    "checkEverySeconds": 15,
    "dropMinMbps": 100,          # traffic_drop only considered when the recent baseline was at least this busy
    "dropPercent": 75,           # ...and traffic fell by at least this much
    "dropMinClients": 10,        # ...or this many devices were downloading and most vanished
    "linkSaturationPercent": 85,
    "linkMbps": None,            # detected from the network interface; set to override
    "nearlyFullPercent": 95,
    "minFreeGB": 20,
    "errorsPerMinute": 30,
    "cpuPercent": 80,            # whole-Mac CPU, averaged over 2 minutes
    "notifyOnResolve": True,
    "notifyInfo": False,         # also notify when a traffic drop turns out to be normal (downloads finished)
    "webhook": "",               # Google Chat / Slack / Teams incoming-webhook URL
    "email": {"to": [], "from": "", "host": "", "port": 25, "starttls": False, "username": "", "password": ""},
    "dashboardUrl": "",          # link included in notifications, e.g. http://10.0.1.15:8027
}

RULES = {
    "cache_full":     ("critical", "Cache full: devices are being turned away"),
    "young_purge":    ("warning",  "Cache is evicting brand-new content"),
    "nearly_full":    ("warning",  "Cache is nearly full"),
    "traffic_drop":   ("critical", "Traffic to devices dropped suddenly"),
    "link_saturated": ("warning",  "Network link is near its limit"),
    "service_down":   ("critical", "Caching service is not running"),
    "not_registered": ("critical", "Cache is not registered with Apple"),
    "error_burst":    ("warning",  "Burst of errors in the caching log"),
    "cpu_high":       ("warning",  "CPU usage is high"),
    "memory_pressure": ("warning", "Memory is under pressure"),
    "service_restarted": ("critical", "Caching service crashed or restarted"),
    "mac_rebooted":   ("critical", "Caching Mac restarted"),
}

RE_ERR = re.compile(r"\b(error|failed|failure|timed out|timeout|refused|reset by peer|unavailable|503|too many)\b", re.I)


def load_config(path):
    cfg = json.loads(json.dumps(DEFAULTS))
    if path and os.path.exists(path):
        with open(path) as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def detect_link_mbps():
    """Speed of the default-route interface, from `ifconfig` media line (e.g. 10GbaseT -> 10000)."""
    try:
        out = subprocess.run(["/sbin/route", "-n", "get", "default"], capture_output=True, text=True, timeout=5).stdout
        iface = re.search(r"interface:\s*(\S+)", out).group(1)
        media = subprocess.run(["/sbin/ifconfig", iface], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"media:.*?\((\d+(?:\.\d+)?)(G?)base", media, re.I)
        if m:
            v = float(m.group(1))
            return int(v * 1000 if m.group(2).upper() == "G" else v), iface
        return None, iface
    except Exception:
        return None, None


class AlertEngine:
    def __init__(self, db_path, config_path, store=None, status=None, clients=None, server_name=""):
        self.config_path = config_path
        self.cfg = load_config(config_path)
        self.store, self.status, self.clients = store, status, clients
        self.server_name = server_name or socket.gethostname().split(".")[0]
        self.lock = threading.Lock()
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, rule TEXT, severity TEXT, title TEXT, message TEXT,
            started REAL, ended REAL, peak TEXT, details TEXT)""")
        self.db.execute("CREATE INDEX IF NOT EXISTS alerts_started ON alerts(started)")
        # One row per check: traffic, connections and system counters, used to explain incidents.
        self.db.execute("CREATE TABLE IF NOT EXISTS samples (t REAL PRIMARY KEY, j TEXT)")
        # Acknowledged alerts and muted alert types (shared by every browser)
        if "acked" not in [c[1] for c in self.db.execute("PRAGMA table_info(alerts)")]:
            self.db.execute("ALTER TABLE alerts ADD COLUMN acked REAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS mutes (rule TEXT PRIMARY KEY, until REAL, created REAL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        self.prev_sample = None
        self.db.commit()
        # re-open anything left active by a previous run so it can resolve normally
        self.open = {}
        for row in self.db.execute("SELECT id, rule, started, details FROM alerts WHERE ended IS NULL"):
            try:
                det = json.loads(row[3] or "{}")
            except ValueError:
                det = {}
            self.open[row[1]] = {"id": row[0], "started": row[2], "since_ok": None, "det": det}
        self.hist = deque(maxlen=400)       # (t, out_bps, clients, upstream_bps)
        self.errors = deque(maxlen=5000)    # (t, message)
        self.link_mbps, self.iface = (self.cfg["linkMbps"], None) if self.cfg.get("linkMbps") else detect_link_mbps()
        self.notify_state = {"last": None, "error": None}
        self.last_check = None
        self.err_counts, self.err_recent = {}, deque(maxlen=3)
        self._last_completed, self._last_purge = None, 0
        self.probe = incident.Probe(iface=self.iface, port_getter=self._cache_port)
        threading.Thread(target=self._loop, daemon=True).start()

    def _cache_port(self):
        try:
            st = self.status.get() if self.status else None
            return (st or {}).get("result", {}).get("Port")
        except Exception:
            return None

    # -- inputs ---------------------------------------------------------------
    def note_log_line(self, msg, t=None):
        """Called by the client monitor for every AssetCache log line."""
        if RE_ERR.search(msg or ""):
            self.errors.append((t or time.time(), msg[:300]))
            cat = incident.categorize(msg)
            self.err_counts[cat] = self.err_counts.get(cat, 0) + 1
            self.err_recent.append(msg[:300])

    # -- evaluation -------------------------------------------------------------
    def _metrics_window(self, seconds):
        if not self.store:
            return []
        self.store.refresh()
        cut = time.time() - seconds
        return [r for r in self.store._rows if r["t"] >= cut]

    def evaluate(self):
        now = time.time()
        c = self.cfg
        found = {}   # rule -> (message, peak, details)

        # Metrics.db rules
        rows10 = self._metrics_window(600)
        rej = sum(r.get("requestsRejectedForNoSpace") or 0 for r in rows10)
        if rej > 0:
            found["cache_full"] = (f"{rej:,} requests were rejected (HTTP 503) in the last 10 minutes because the cache has no free space. "
                                   "Rejected devices download from the internet instead and may skip the cache for up to 24 hours.",
                                   f"{rej:,} rejected", {"rejected": rej})
        rows30 = self._metrics_window(1800)
        young = sum(r.get("bytesPurgedYoungerThan1Day") or 0 for r in rows30)
        if young > 0:
            found["young_purge"] = (f"{young/1e9:.1f} GB of content less than a day old was evicted in the last 30 minutes. "
                                    "The cache is too small for current demand, so content is being downloaded more than once.",
                                    f"{young/1e9:.1f} GB", {"bytes": young})

        # Live traffic (nettop via client monitor)
        live = self.clients.live if self.clients else None
        if live and live.get("available"):
            out_bps = (live.get("totalOut") or 0) * 8
            up_bps = ((live.get("upstream") or {}).get("rateIn") or 0) * 8
            n = sum(1 for x in live.get("clients", []) if (x.get("rateOut") or 0) > 1000)
            self.hist.append((now, out_bps, n, up_bps))
            base = [h for h in self.hist if now - 900 <= h[0] <= now - 180]
            cur = [h for h in self.hist if h[0] >= now - 90]
            if len(base) >= 8 and len(cur) >= 3:
                b_rate = statistics.median(h[1] for h in base)
                b_n = statistics.median(h[2] for h in base)
                c_rate = sum(h[1] for h in cur) / len(cur)
                c_n = sum(h[2] for h in cur) / len(cur)
                drop = 1 - (c_rate / b_rate) if b_rate else 0
                ndrop = 1 - (c_n / b_n) if b_n else 0
                busy = b_rate >= c["dropMinMbps"] * 1e6 or b_n >= c["dropMinClients"]
                o = self.open.get("traffic_drop")
                if o:
                    # Stay open until traffic gets back to half of what it was before the drop (max 1 hour).
                    before = (o.get("det") or {}).get("fromMbps", 0) * 1e6
                    if c_rate < 0.5 * before and now - o["started"] < 3600:
                        found["traffic_drop"] = None
                elif busy and (drop * 100 >= c["dropPercent"] or (b_n >= c["dropMinClients"] and ndrop * 100 >= c["dropPercent"])):
                    found["traffic_drop"] = (
                        f"Traffic to devices fell from about {b_rate/1e6:,.0f} Mbps ({b_n:.0f} devices) to {c_rate/1e6:,.0f} Mbps "
                        f"({c_n:.0f} devices) within a few minutes. If a rollout is still running, devices have probably "
                        "stopped using the cache and are downloading from the internet.",
                        f"{b_rate/1e6:,.0f} → {c_rate/1e6:,.0f} Mbps", {"fromMbps": b_rate / 1e6, "toMbps": c_rate / 1e6,
                                                                       "fromDevices": b_n, "toDevices": c_n})
            if self.link_mbps:
                recent = [h for h in self.hist if h[0] >= now - 120]
                if len(recent) >= 5:
                    load = sum(h[1] + h[3] for h in recent) / len(recent)
                    pct = load / (self.link_mbps * 1e6) * 100
                    if pct >= c["linkSaturationPercent"]:
                        found["link_saturated"] = (f"The {self.iface or 'network'} link has averaged {pct:.0f}% of its {self.link_mbps:,} Mbps "
                                                   "for the last 2 minutes. Downloads will slow and devices may time out.",
                                                   f"{pct:.0f}%", {"percent": pct, "linkMbps": self.link_mbps})
        elif live and "isn't running" in (live.get("error") or ""):
            found["service_down"] = ("The AssetCache process isn't running. No device can use the cache until it starts again.",
                                     "not running", {})

        smp = self._record_sample(now, live)
        self._resource_rules(now, smp, found)

        # Service status (AssetCacheManagerUtil)
        st = self.status.get() if self.status else None
        if st and st.get("ok") and st.get("result"):
            r = st["result"]
            active = r.get("Active", r.get("Activated"))
            if active is False or (r.get("CacheStatus") and r.get("CacheStatus") != "OK") or \
                    (r.get("StartupStatus") and r.get("StartupStatus") not in ("OK", "PENDING")):
                found["service_down"] = (f"Caching service reports Active={active}, CacheStatus={r.get('CacheStatus')}, "
                                         f"StartupStatus={r.get('StartupStatus')}.", str(r.get("CacheStatus")), {})
            reg = r.get("RegistrationStatus")
            if active and reg is not None and reg != 1:
                found["not_registered"] = (f"RegistrationStatus is {reg} (expected 1){': ' + str(r.get('RegistrationError')) if r.get('RegistrationError') else ''}. "
                                           "Devices can't discover an unregistered cache.", str(reg), {})
            used, limit, free = float(r.get("CacheUsed") or 0), float(r.get("CacheLimit") or 0), float(r.get("CacheFree") or 0)
            msgs = []
            if limit and used / limit * 100 >= c["nearlyFullPercent"]:
                msgs.append(f"the cache is using {used/1e9:,.0f} GB of its {limit/1e9:,.0f} GB limit ({used/limit*100:.0f}%)")
            if free and free < c["minFreeGB"] * 1e9:
                msgs.append(f"only {free/1e9:.0f} GB is free on the cache volume")
            if msgs:
                found["nearly_full"] = ("Space is running out: " + "; ".join(msgs) + ". Raise the cache limit or add storage.",
                                        f"{used/limit*100:.0f}%" if limit else f"{free/1e9:.0f} GB free", {})

        # Error burst in the log
        errs = [e for e in self.errors if e[0] >= now - 60]
        if len(errs) >= c["errorsPerMinute"]:
            groups = logs.group([{"t": t, "msg": m} for t, m in errs], top=3)
            top = groups[0] if groups else None
            found["error_burst"] = (f"{len(errs)} error lines in the AssetCache log in the last minute. "
                                    + (f"Most common ({top['count']}×): {top['example'].rstrip('.')}." if top else f"Example: {errs[-1][1].rstrip('.')}.")
                                    + " Open the log viewer to see every line.",
                                    f"{len(errs)}/min", {"count": len(errs), "sample": [e[1] for e in errs[-5:]],
                                                         "patterns": [{"pattern": g["pattern"], "count": g["count"], "example": g["example"]} for g in groups]})

        # Explain traffic drops: is it a real problem or did downloads simply finish?
        if found.get("traffic_drop") and "traffic_drop" not in self.open:
            msg, peak, det = found["traffic_drop"]
            rep = self.analyze(now)
            v = rep["verdict"]
            det = {**det, "verdict": v, "checks": rep["checks"], "t0": rep["t0"]}
            found["traffic_drop"] = (v["headline"] + " " + msg.split(". If a rollout")[0] + ".", peak, det)
        self._reanalyze(now)
        self._apply(found, now)
        self.last_check = now

    # -- incident forensics ---------------------------------------------------------
    def _record_sample(self, now, live):
        s = {}
        if live and live.get("available"):
            s.update({"outMbps": (live.get("totalOut") or 0) * 8 / 1e6,
                      "upMbps": ((live.get("upstream") or {}).get("rateIn") or 0) * 8 / 1e6,
                      "devices": sum(1 for x in live.get("clients", []) if (x.get("rateOut") or 0) > 1000),
                      "conns": live.get("conns"), "upConns": (live.get("upstream") or {}).get("conns"),
                      "newConns": live.get("newConns")})
        if self.clients:
            s["err"], self.err_counts = self.err_counts, {}
            s["errSample"] = list(self.err_recent)
            self.err_recent.clear()
            done = self.clients.parser.stats.get("completed", 0)
            s["completed"] = done - self._last_completed if self._last_completed is not None else 0
            self._last_completed = done
        try:
            s.update(self.probe.sample())
        except Exception as e:
            s["probeError"] = str(e)
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO samples (t, j) VALUES (?, ?)", (now, json.dumps(s)))
            if now - self._last_purge > 3600:
                self.db.execute("DELETE FROM samples WHERE t < ?", (now - 14 * 86400,))
                self._last_purge = now
            self.db.commit()
        return s

    # -- CPU, memory, crashes, reboots ---------------------------------------------
    def _point_event(self, rule, msg, peak, det, now):
        """Something that happened at one moment (a crash, a reboot): recorded and notified once."""
        sev, title = RULES[rule]
        with self.lock:
            self.db.execute("INSERT INTO alerts (rule, severity, title, message, started, ended, peak, details) VALUES (?,?,?,?,?,?,?,?)",
                            (rule, sev, title, msg, now, now, peak, json.dumps(det)))
            self.db.commit()
        if not self.is_muted(rule):
            self._notify(f"🔴 {title}", msg, sev)

    def _meta(self, k, v=None):
        with self.lock:
            if v is None:
                r = self.db.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
                return r[0] if r else None
            self.db.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (k, str(v)))
            self.db.commit()

    def _resource_rules(self, now, s, found):
        c, prev = self.cfg, self.prev_sample
        self.prev_sample = s
        if not s:
            return
        # Whole-Mac CPU (2-minute average)
        with self.lock:
            rows = self.db.execute("SELECT j FROM samples WHERE t >= ?", (now - 120,)).fetchall()
        recent = []
        for (j,) in rows:
            try:
                recent.append(json.loads(j))
            except ValueError:
                pass
        cpus = [r["cpuPct"] for r in recent if r.get("cpuPct") is not None]
        if len(cpus) >= 3 and sum(cpus) / len(cpus) >= c["cpuPercent"]:
            avg = sum(cpus) / len(cpus)
            ac = min(100, (s.get("cpu") or 0) / max(1, s.get("cores") or 1))
            topp = (s.get("topProcs") or [{}])[0]
            found["cpu_high"] = (f"The Mac's CPU has averaged {avg:.0f}% for 2 minutes. The caching service (AssetCache) is using about {ac:.0f}% of the Mac"
                                 + (f"; the busiest process is {topp.get('name')} at {topp.get('cpu', 0):.0f}%" if topp.get("name") else "") + ".",
                                 f"{avg:.0f}%", {"avg": avg, "assetcache": ac, "top": s.get("topProcs")})
        # Memory pressure
        if s.get("memPressure") in ("warning", "critical"):
            found["memory_pressure"] = (f"macOS reports {s['memPressure']} memory pressure: {s.get('memPct', 0):.0f}% of memory in use"
                                        + (f", {s['swapUsedMB']/1024:.1f} GB swapped to disk" if s.get("swapUsedMB") else "")
                                        + f". AssetCache is using {(s.get('rssMB') or 0)/1024:.1f} GB.",
                                        s["memPressure"], {"pct": s.get("memPct"), "swapMB": s.get("swapUsedMB")})
        # Caching service crashed / restarted (process ID changed or disappeared and came back)
        if prev and prev.get("pid") and s.get("pid") and prev["pid"] != s["pid"]:
            crash = [x for x in (s.get("crashes") or []) if x["kind"] == "AssetCache crash" and now - x["t"] < 900]
            self._point_event("service_restarted",
                              f"The AssetCache process changed from PID {prev['pid']} to {s['pid']}"
                              + (f". A crash report was written: {crash[0]['file']}" if crash else ". No crash report was found, so it was probably restarted rather than crashed")
                              + f". Just before, the Mac was at {prev.get('cpuPct', 0):.0f}% CPU and {prev.get('memPct', 0):.0f}% memory, and AssetCache used {(prev.get('rssMB') or 0)/1024:.1f} GB.",
                              "crash" if crash else "restart",
                              {"before": {k: prev.get(k) for k in ("cpuPct", "memPct", "memPressure", "rssMB", "cpu", "fds", "swapUsedMB")},
                               "crashReport": crash[0] if crash else None}, now)
        # The whole Mac restarted (boot time changed since we last looked)
        if s.get("bootTime"):
            last = self._meta("bootTime")
            if last and int(last) != s["bootTime"]:
                panic = [x for x in (s.get("crashes") or []) if x["kind"] == "kernel panic" and x["t"] >= int(last)]
                self._point_event("mac_rebooted",
                                  f"The caching Mac started up at {time.strftime('%b %d %I:%M %p', time.localtime(s['bootTime']))}"
                                  + (f" after a kernel panic ({panic[0]['file']})" if panic else "") + ". The cache was unavailable while it restarted.",
                                  "panic" if panic else "restart", {"bootTime": s["bootTime"], "panic": panic[0] if panic else None}, now)
            if last != str(s["bootTime"]):
                self._meta("bootTime", s["bootTime"])

    def system(self, start=None, end=None):
        """CPU/memory series for the dashboard (averaged into at most ~400 points) plus the latest details."""
        now = time.time()
        end = end or now
        start = start or end - 3600
        bucket = max(15, int((end - start) / 400 // 15 * 15) or 15)
        with self.lock:
            rows = self.db.execute("SELECT t, j FROM samples WHERE t BETWEEN ? AND ? ORDER BY t", (start, end)).fetchall()
            last = self.db.execute("SELECT t, j FROM samples ORDER BY t DESC LIMIT 1").fetchone()
            events = self.db.execute("""SELECT rule, started, message FROM alerts WHERE rule IN ('service_restarted','mac_rebooted')
                                         AND started BETWEEN ? AND ? ORDER BY started DESC""", (start, end)).fetchall()
        agg = {}
        for t, j in rows:
            try:
                s = json.loads(j)
            except ValueError:
                continue
            b = int(t // bucket * bucket)
            a = agg.setdefault(b, {"n": 0, "cpu": 0, "cpuMax": 0, "mem": 0, "ac": 0, "acMem": 0, "swap": 0, "nc": 0, "nm": 0})
            a["n"] += 1
            if s.get("cpuPct") is not None:
                a["cpu"] += s["cpuPct"]; a["nc"] += 1; a["cpuMax"] = max(a["cpuMax"], s["cpuPct"])
                a["ac"] += min(100, (s.get("cpu") or 0) / max(1, s.get("cores") or 1))
            if s.get("memPct") is not None:
                a["mem"] += s["memPct"]; a["nm"] += 1
                a["acMem"] += (s.get("rssMB") or 0)
                a["swap"] += (s.get("swapUsedMB") or 0)
        series = [{"t": b, "cpu": a["cpu"] / a["nc"] if a["nc"] else None, "cpuMax": a["cpuMax"] if a["nc"] else None,
                   "acCpu": a["ac"] / a["nc"] if a["nc"] else None,
                   "mem": a["mem"] / a["nm"] if a["nm"] else None, "acMemMB": a["acMem"] / a["nm"] if a["nm"] else None,
                   "swapMB": a["swap"] / a["nm"] if a["nm"] else None} for b, a in sorted(agg.items())]
        latest = None
        if last:
            try:
                latest = {"t": last[0], **json.loads(last[1])}
            except ValueError:
                pass
        return {"start": start, "end": end, "bucket": bucket, "series": series, "latest": latest,
                "events": [{"rule": r, "t": t, "message": m} for r, t, m in events],
                "cpuThreshold": self.cfg["cpuPercent"]}

    def analyze(self, t_alert):
        with self.lock:
            rows = self.db.execute("SELECT t, j FROM samples WHERE t BETWEEN ? AND ?", (t_alert - 1800, t_alert + 900)).fetchall()
        samples = []
        for t, j in rows:
            try:
                samples.append({"t": t, **json.loads(j)})
            except ValueError:
                pass
        metrics = [r for r in (self.store._rows if self.store else []) if t_alert - 1800 <= r["t"] <= t_alert + 900]
        rep = incident.analyze(samples, t_alert, metrics, None, self.link_mbps)
        if self.clients:
            try:
                dep = self.clients.departed(rep["t0"])
                rep = incident.analyze(samples, t_alert, metrics, dep, self.link_mbps)
            except Exception:
                pass
        return rep

    def _reanalyze(self, now):
        """Three minutes after a traffic drop there's more 'after' data: confirm or correct the verdict."""
        o = self.open.get("traffic_drop")
        if not o or o.get("final") or now - o["started"] < 180:
            return
        rep = self.analyze(o["started"])
        v, old = rep["verdict"], (o.get("det") or {}).get("verdict") or {}
        o["final"] = True
        det = {**(o.get("det") or {}), "verdict": v, "checks": rep["checks"], "t0": rep["t0"], "final": True}
        o["det"] = det
        with self.lock:
            self.db.execute("UPDATE alerts SET severity = ?, title = ?, details = ? WHERE id = ?",
                            (v["severity"], v["title"], json.dumps(det), o["id"]))
            if v["severity"] == "info" and not o.get("info"):
                self.db.execute("UPDATE alerts SET ended = started WHERE id = ?", (o["id"],))
            self.db.commit()
        if v.get("kind") != old.get("kind") and not self.is_muted("traffic_drop"):
            was_paged = old.get("severity") in ("critical", "warning")
            if v["severity"] == "info" and was_paged:
                self._notify("ℹ️ Update: " + v["title"], v["headline"], "info", force=True)
            elif v["severity"] != "info":
                self._notify(f"{'🔴' if v['severity'] == 'critical' else '🟠'} {'Update: ' if was_paged else ''}{v['title']}", v["headline"], v["severity"])
        o["info"] = v["severity"] == "info"

    def _apply(self, found, now):
        with self.lock:
            for rule, val in found.items():
                if rule in self.open:
                    self.open[rule]["since_ok"] = None
                    if val:
                        msg, peak, det = val
                        self.db.execute("UPDATE alerts SET message = ?, details = ? WHERE id = ?",
                                        (msg, json.dumps(det), self.open[rule]["id"]))
                    continue
                if not val:
                    continue
                sev, title = RULES[rule]
                msg, peak, det = val
                v = (det or {}).get("verdict") or {}
                sev, title = v.get("severity", sev), v.get("title", title)
                info = sev == "info"
                cur = self.db.execute("INSERT INTO alerts (rule, severity, title, message, started, ended, peak, details) VALUES (?,?,?,?,?,?,?,?)",
                                      (rule, sev, title, msg, now, now if info else None, peak, json.dumps(det)))
                # info events are recorded as a moment in time, but stay "open" in memory so they don't repeat
                self.open[rule] = {"id": cur.lastrowid, "started": now, "since_ok": None, "det": det, "info": info}
                if not self.is_muted(rule):
                    self._notify(f"{'🔴' if sev == 'critical' else '🟠' if sev == 'warning' else 'ℹ️'} {title}", msg, sev)
            for rule in list(self.open):
                if rule in found:
                    continue
                o = self.open[rule]
                o["since_ok"] = o["since_ok"] or now
                if now - o["since_ok"] >= 120:   # must stay clear for 2 minutes before resolving
                    ended = max(o["since_ok"], o["started"])
                    del self.open[rule]
                    if o.get("info"):
                        continue          # already recorded as a moment; nothing to resolve
                    self.db.execute("UPDATE alerts SET ended = ? WHERE id = ?", (ended, o["id"]))
                    if self.cfg.get("notifyOnResolve"):
                        mins = max(1, (ended - o["started"]) / 60)
                        if not self.is_muted(rule):
                            self._notify(f"✅ Resolved: {RULES[rule][1]}", f"Lasted about {mins:.0f} minute{'' if round(mins) == 1 else 's'}.", "resolved")
            self.db.execute("DELETE FROM alerts WHERE started < ?", (now - 180 * 86400,))
            self.db.commit()

    def _loop(self):
        time.sleep(20)   # let the monitors take their first samples
        while True:
            try:
                self.evaluate()
            except Exception as e:
                self.notify_state["error"] = f"check failed: {e}"
            time.sleep(self.cfg["checkEverySeconds"])

    # -- notifications -------------------------------------------------------------
    def _notify(self, title, message, severity, force=False):
        if severity == "info" and not (force or self.cfg.get("notifyInfo")):
            return
        threading.Thread(target=self._send, args=(title, message, severity), daemon=True).start()

    def _send(self, title, message, severity):
        text = f"*CacheDash · {self.server_name}*\n{title}\n{message}"
        if self.cfg.get("dashboardUrl"):
            text += f"\n{self.cfg['dashboardUrl']}"
        errs = []
        if self.cfg.get("webhook"):
            try:
                req = urllib.request.Request(self.cfg["webhook"], data=json.dumps({"text": text}).encode(),
                                             headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=15).read()
            except Exception as e:
                errs.append(f"webhook: {e}")
        em = self.cfg.get("email") or {}
        if em.get("to") and em.get("host"):
            try:
                m = EmailMessage()
                m["Subject"] = f"[CacheDash {self.server_name}] {title}"
                m["From"] = em.get("from") or f"cachedash@{socket.gethostname()}"
                m["To"] = ", ".join(em["to"] if isinstance(em["to"], list) else [em["to"]])
                m.set_content(text.replace("*", ""))
                with smtplib.SMTP(em["host"], int(em.get("port") or 25), timeout=20) as s:
                    if em.get("starttls"):
                        s.starttls()
                    if em.get("username"):
                        s.login(em["username"], em.get("password", ""))
                    s.send_message(m)
            except Exception as e:
                errs.append(f"email: {e}")
        self.notify_state = {"last": time.time(), "error": "; ".join(errs) or None}

    def test(self):
        self._send("🧪 Test alert", "If you can read this, CacheDash notifications are working.", "test")
        return self.notify_state

    # -- acknowledge & mute ---------------------------------------------------------
    def is_muted(self, rule, now=None):
        row = self.db.execute("SELECT until FROM mutes WHERE rule = ?", (rule,)).fetchone()
        return bool(row) and (row[0] is None or row[0] > (now or time.time()))

    def ack(self, alert_id, acked=True):
        with self.lock:
            self.db.execute("UPDATE alerts SET acked = ? WHERE id = ?", (time.time() if acked else None, int(alert_id)))
            self.db.commit()
        return {"ok": True}

    def mute(self, rule, days=None):
        """Stop banner, bell and notifications for one alert type. days=None means until unmuted.
        The alerts are still detected and kept in the history (marked muted)."""
        if rule not in RULES:
            return {"error": f"unknown alert type {rule}"}
        until = time.time() + float(days) * 86400 if days else None
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO mutes (rule, until, created) VALUES (?, ?, ?)", (rule, until, time.time()))
            self.db.commit()
        return {"ok": True, "rule": rule, "until": until}

    def unmute(self, rule):
        with self.lock:
            self.db.execute("DELETE FROM mutes WHERE rule = ?", (rule,))
            self.db.commit()
        return {"ok": True}

    def mutes(self):
        now = time.time()
        with self.lock:
            self.db.execute("DELETE FROM mutes WHERE until IS NOT NULL AND until <= ?", (now,))
            rows = self.db.execute("SELECT rule, until, created FROM mutes ORDER BY created").fetchall()
        return [{"rule": r, "title": RULES.get(r, ("", r))[1], "until": u, "since": c} for r, u, c in rows]

    # -- API -----------------------------------------------------------------------
    def report(self, alert_id):
        with self.lock:
            row = self.db.execute("SELECT id, rule, severity, title, message, started, ended, peak, details FROM alerts WHERE id = ?",
                                  (alert_id,)).fetchone()
        if not row:
            return {"error": "no such alert"}
        e = dict(zip(["id", "rule", "severity", "title", "message", "started", "ended", "peak", "details"], row))
        try:
            e["details"] = json.loads(e["details"] or "{}")
        except ValueError:
            e["details"] = {}
        rep = self.analyze(e["started"])
        if e["rule"] != "traffic_drop":
            # other alerts: still show the surrounding measurements, but keep the alert's own explanation
            rep["verdict"] = {"kind": e["rule"], "headline": e["message"], "severity": e["severity"], "title": e["title"]}
        rep["alert"] = e
        rep["linkMbps"] = self.link_mbps
        return rep

    def snapshot(self, start=None, end=None):
        now = time.time()
        end = end or now
        start = start or end - 7 * 86400
        with self.lock:
            rows = self.db.execute("""SELECT id, rule, severity, title, message, started, ended, peak, details, acked FROM alerts
                                      WHERE started <= ? AND (ended IS NULL OR ended >= ?) ORDER BY started DESC LIMIT 500""",
                                   (end, start)).fetchall()
        cols = ["id", "rule", "severity", "title", "message", "started", "ended", "peak", "details", "acked"]
        muted = {m["rule"] for m in self.mutes()}
        events = []
        for r in rows:
            e = dict(zip(cols, r))
            try:
                e["details"] = json.loads(e["details"] or "{}")
            except ValueError:
                e["details"] = {}
            e["active"] = e["ended"] is None
            e["muted"] = e["rule"] in muted
            events.append(e)
        c = self.cfg
        with self.lock:   # active alerts are always "right now", whatever window was asked for
            act_rows = self.db.execute("""SELECT id, rule, severity, title, message, started, ended, peak, details, acked
                                          FROM alerts WHERE ended IS NULL ORDER BY started DESC""").fetchall()
        active = []
        for r in act_rows:
            e = dict(zip(cols, r))
            try:
                e["details"] = json.loads(e["details"] or "{}")
            except ValueError:
                e["details"] = {}
            e["active"] = True
            e["muted"] = e["rule"] in muted
            active.append(e)
        return {
            "events": events,
            "active": active,
            "mutes": self.mutes(),
            "checkedAt": self.last_check,
            "linkMbps": self.link_mbps, "interface": self.iface,
            "rules": {k: {"severity": v[0], "title": v[1]} for k, v in RULES.items()},
            "thresholds": {k: c[k] for k in ("dropMinMbps", "dropPercent", "dropMinClients", "linkSaturationPercent",
                                              "nearlyFullPercent", "minFreeGB", "errorsPerMinute", "cpuPercent")},
            "notify": {"webhook": bool(c.get("webhook")), "email": bool((c.get("email") or {}).get("to")),
                       **self.notify_state},
        }
