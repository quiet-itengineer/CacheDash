"""
CacheDash incident forensics.

Probe   - every check (15 s) records what the Mac itself can tell us about the
          caching service: connections, new connections, TCP listen-queue
          overflows (the classic "connection limit reached" signal), open files
          vs the per-process limit, service restarts, CPU, network-interface
          errors and link state.
analyze - when traffic to devices collapses, looks at those samples plus
          Metrics.db, the AssetCache log and per-device download records, and
          decides *why*: downloads finished normally, connection limit, cache
          full, service restart, network problem, CPU maxed, downloads failing,
          or unclear. Each verdict comes with the evidence that was checked.
"""

import os
import re
import statistics
import subprocess
import time

SEVERITY = {
    "finished": "info", "finished_probably": "info",
    "unclear": "warning",
}
TITLES = {
    "finished": "Downloads finished: traffic dropped normally",
    "finished_probably": "Traffic dropped as downloads ended (no problems found)",
    "cache_full": "Traffic dropped: cache ran out of space",
    "service_restart": "Traffic dropped: caching service restarted",
    "connection_limit": "Traffic dropped: connection limit reached",
    "network": "Traffic dropped: network problem on the cache",
    "cpu": "Traffic dropped: cache CPU maxed out",
    "abandoned": "Traffic dropped: device downloads were failing",
    "unclear": "Traffic dropped suddenly (cause unclear)",
}

RE_CAT = [
    ("timeout", re.compile(r"timed? ?out|timeout", re.I)),
    ("reset", re.compile(r"reset by peer|connection reset|broken pipe|ECONNRESET|EPIPE", re.I)),
    ("refused", re.compile(r"refused|too many|EMFILE|ENFILE|no buffer space|ENOBUFS|backlog", re.I)),
    ("http503", re.compile(r"\b503\b|unavailable|no space", re.I)),
]


def categorize(msg):
    for name, rx in RE_CAT:
        if rx.search(msg or ""):
            return name
    return "other"


def _run(args, timeout=8):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


class Probe:
    """Cheap, read-only system measurements of the caching service (macOS)."""

    def __init__(self, iface=None, port_getter=None):
        self.iface = iface
        self.port_getter = port_getter or (lambda: None)
        self.cores = os.cpu_count() or 1
        self.maxfiles = None
        out = _run(["/usr/sbin/sysctl", "-n", "kern.maxfilesperproc"])
        if out.strip().isdigit():
            self.maxfiles = int(out.strip())
        self._fds = (None, 0)
        self._slow = {}        # name -> (value, fetched_at) for things sampled less often
        mem = _run(["/usr/sbin/sysctl", "-n", "hw.memsize"]).strip()
        self.memtotal = int(mem) if mem.isdigit() else None

    def _every(self, name, seconds, fn):
        v = self._slow.get(name)
        if not v or time.time() - v[1] > seconds:
            try:
                v = (fn(), time.time())
            except Exception:
                v = (None, time.time())
            self._slow[name] = v
        return v[0]

    def system(self):
        """Whole-Mac CPU, load, memory, swap, pressure, thermal state, boot time, busiest processes, crash reports."""
        s = {}
        top = _run(["/usr/bin/top", "-l", "2", "-n", "0", "-s", "1"], timeout=12)
        cpu = re.findall(r"CPU usage:\s*([\d.]+)% user,\s*([\d.]+)% sys,\s*([\d.]+)% idle", top)
        if cpu:
            u, sy, idle = map(float, cpu[-1])
            s.update({"cpuPct": round(100 - idle, 1), "cpuUser": u, "cpuSys": sy})
        la = re.findall(r"Load Avg:\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)", top)
        if la:
            s["load"] = [float(x) for x in la[-1]]
        vm = _run(["/usr/bin/vm_stat"])
        ps_m = re.search(r"page size of (\d+) bytes", vm)
        if ps_m and self.memtotal:
            pg = int(ps_m.group(1))
            def pages(label):
                m = re.search(rf"{label}:\s+(\d+)", vm)
                return int(m.group(1)) if m else 0
            # Activity Monitor-style "Memory Used" = app (active) + wired + compressed
            used = (pages("Pages active") + pages("Pages wired down") + pages("Pages occupied by compressor")) * pg
            used = min(used, self.memtotal)
            s.update({"memUsed": used, "memTotal": self.memtotal, "memPct": round(used / self.memtotal * 100, 1),
                      "memCompressed": pages("Pages occupied by compressor") * pg})
        lvl = _run(["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"]).strip()
        if lvl.isdigit():
            s["memPressure"] = {1: "normal", 2: "warning", 4: "critical"}.get(int(lvl), lvl)
        sw = re.search(r"used = ([\d.]+)M", _run(["/usr/sbin/sysctl", "-n", "vm.swapusage"]))
        if sw:
            s["swapUsedMB"] = float(sw.group(1))
        bt = re.search(r"sec = (\d+)", _run(["/usr/sbin/sysctl", "-n", "kern.boottime"]))
        if bt:
            s["bootTime"] = int(bt.group(1))
        me = _run(["/bin/ps", "-o", "%cpu=,rss=", "-p", str(os.getpid())]).split()
        if len(me) >= 2:
            try:
                s["selfCpu"], s["selfRssMB"] = float(me[0]), int(me[1]) / 1024
            except ValueError:
                pass
        s["thermal"] = self._every("thermal", 60, self._thermal)
        s["topProcs"] = self._every("top", 60, self._top_procs)
        s["crashes"] = self._every("crashes", 300, self._crash_reports)
        return s

    @staticmethod
    def _thermal():
        out = _run(["/usr/bin/pmset", "-g", "therm"])
        lim = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", out)
        if lim and int(lim.group(1)) < 100:
            return f"Throttled: CPU limited to {lim.group(1)}% by heat"
        warn = re.search(r"(?:thermal|performance) warning level[^\d\n]*(\d+)", out, re.I)
        if warn and int(warn.group(1)) > 0:
            return f"Thermal warning level {warn.group(1)}"
        return "Normal" if out else None

    @staticmethod
    def _top_procs():
        rows = []
        for line in _run(["/bin/ps", "-Ao", "pid=,pcpu=,rss=,comm=", "-r"]).splitlines()[:6]:
            parts = line.split(None, 3)
            if len(parts) == 4:
                try:
                    rows.append({"pid": int(parts[0]), "cpu": float(parts[1]), "rssMB": int(parts[2]) / 1024,
                                 "name": parts[3].rsplit("/", 1)[-1]})
                except ValueError:
                    pass
        return rows

    @staticmethod
    def _crash_reports():
        out = []
        for d in ("/Library/Logs/DiagnosticReports", "/Library/Logs/DiagnosticReports/Retired"):
            try:
                for f in os.listdir(d):
                    low = f.lower()
                    if low.startswith("assetcache") or "panic" in low or low.startswith("cachedash") or low.startswith("python"):
                        p = os.path.join(d, f)
                        mt = os.path.getmtime(p)
                        if time.time() - mt < 14 * 86400:
                            out.append({"file": f, "path": p, "t": mt,
                                        "kind": "kernel panic" if "panic" in low else "AssetCache crash" if low.startswith("assetcache") else "CacheDash crash"})
            except OSError:
                pass
        return sorted(out, key=lambda x: -x["t"])[:20]

    def pid(self):
        p = _run(["/usr/bin/pgrep", "-x", "AssetCache"]).split()
        return int(p[0]) if p else None

    def sample(self):
        s = {"cores": self.cores, "maxfiles": self.maxfiles}
        try:
            s.update(self.system())
        except Exception as e:
            s["systemError"] = str(e)
        pid = self.pid()
        s["pid"] = pid
        if pid:
            ps = _run(["/bin/ps", "-o", "%cpu=,rss=", "-p", str(pid)]).split()
            if len(ps) >= 2:
                try:
                    s["cpu"] = float(ps[0])
                    s["rssMB"] = int(ps[1]) / 1024
                except ValueError:
                    pass
            # open files: lsof is slower, so only once a minute
            if time.time() - self._fds[1] > 60:
                out = _run(["/usr/sbin/lsof", "-n", "-P", "-p", str(pid)], timeout=15)
                self._fds = (max(0, len(out.splitlines()) - 1) if out else None, time.time())
            s["fds"] = self._fds[0]
        # TCP listen queue overflows (system-wide counter): increments when a
        # server can't accept connections fast enough / its backlog is full.
        m = re.search(r"(\d+)\s+listen queue overflows?", _run(["/usr/sbin/netstat", "-s", "-p", "tcp"]))
        if m:
            s["listenOverflows"] = int(m.group(1))
        port = self.port_getter()
        if port:
            for line in _run(["/usr/sbin/netstat", "-Lan", "-p", "tcp"]).splitlines():
                mm = re.match(r"\s*(\d+)/(\d+)/(\d+)\s+(\S+)", line)
                if mm and mm.group(4).endswith(f".{port}"):
                    s["qlen"], s["maxqlen"] = int(mm.group(1)), int(mm.group(3))
                    break
        if self.iface:
            rows = _run(["/usr/sbin/netstat", "-I", self.iface, "-b", "-n"]).splitlines()
            if rows:
                head = rows[0].split()
                for r in rows[1:]:
                    if "<Link#" in r:
                        cols = r.split()
                        if len(cols) == len(head):
                            d = dict(zip(head, cols))
                            try:
                                s["ierrs"], s["oerrs"] = int(d.get("Ierrs", 0)), int(d.get("Oerrs", 0))
                            except ValueError:
                                pass
                        break
            st = re.search(r"status:\s*(\w+)", _run(["/sbin/ifconfig", self.iface]))
            if st:
                s["linkUp"] = st.group(1) == "active"
        return s


# ------------------------------------------------------------------ analysis --

def _cap(x):
    return x[:1].upper() + x[1:] if x else x


def _between(samples, a, b):
    return [s for s in samples if a <= s["t"] < b]


def _vals(samples, key):
    return [s[key] for s in samples if s.get(key) is not None]


def analyze(samples, t_alert, metrics_rows=(), departed=None, link_mbps=None):
    """Explain a traffic drop. samples: list of probe+traffic dicts with 't'."""
    S = sorted((s for s in samples if t_alert - 1500 <= s["t"] <= t_alert + 900), key=lambda s: s["t"])
    report = {"t0": t_alert, "checks": [], "series": [], "departed": [], "logLines": []}
    if len(S) < 4:
        report["verdict"] = {"kind": "unclear", "headline": "Not enough monitoring data around this time to explain the drop.",
                             "severity": "warning", "title": TITLES["unclear"]}
        return report

    # When exactly did it fall? Largest drop vs the previous few samples, shortly before the alert.
    best, t0 = 0, t_alert - 90
    for i, s in enumerate(S):
        if not (t_alert - 420 <= s["t"] <= t_alert):
            continue
        prev = [p.get("outMbps", 0) for p in S[max(0, i - 5):i]]
        if prev:
            d = statistics.median(prev) - s.get("outMbps", 0)
            if d > best:
                best, t0 = d, s["t"]
    report["t0"] = t0
    before = _between(S, t0 - 600, t0)
    after = _between(S, t0, t0 + 240)
    around = _between(S, t0 - 300, t0 + 240)
    checks = report["checks"]

    def check(key, label, state, detail):
        checks.append({"key": key, "label": label, "state": state, "detail": detail})

    def avg(xs):
        return sum(xs) / len(xs) if xs else 0

    out_b, out_a = avg(_vals(before, "outMbps")), avg(_vals(after, "outMbps"))
    dev_b, dev_a = avg(_vals(before, "devices")), avg(_vals(after, "devices"))
    report["summary"] = {"fromMbps": out_b, "toMbps": out_a, "fromDevices": dev_b, "toDevices": dev_a,
                         "peakConns": max(_vals(before, "conns") or [0])}

    # 1. Cache full (Metrics.db)
    rows = [r for r in metrics_rows if t0 - 600 <= r["t"] <= t0 + 300]
    rej = sum(r.get("requestsRejectedForNoSpace") or 0 for r in rows)
    if rows:
        check("cache_full", "Cache out of space (HTTP 503 rejections)", "found" if rej else "clear",
              f"{rej:,} requests rejected for lack of space" if rej else "No requests were rejected for lack of space")
    else:
        check("cache_full", "Cache out of space (HTTP 503 rejections)", "nodata", "No Metrics.db rows for this time")

    # 2. Service restart
    pids = [s.get("pid") for s in around]
    seen = {p for p in pids if p}
    restarted = bool(seen) and (any(p is None for p in pids) or len(seen) > 1)
    if seen:
        check("restart", "Caching service stopped or restarted", "found" if restarted else "clear",
              "The AssetCache process ID changed or disappeared during this window" if restarted
              else f"Same AssetCache process the whole time (PID {next((p for p in pids if p), '?')})")
    else:
        check("restart", "Caching service stopped or restarted", "nodata", "Not measured")

    # 3. Connection limit: listen-queue overflows, backlog full, open-file limit, connection plateau
    ov = _vals(around, "listenOverflows")
    ov_delta = (max(ov) - min(ov)) if len(ov) >= 2 else None
    q = [(s["qlen"], s["maxqlen"]) for s in around if s.get("maxqlen")]
    backlog_full = any(ql >= 0.9 * mx for ql, mx in q) if q else None
    fds = _vals(around, "fds")
    maxfiles = next((s.get("maxfiles") for s in around if s.get("maxfiles")), None)
    fd_full = (max(fds) >= 0.9 * maxfiles) if (fds and maxfiles) else None
    conns = _vals(before, "conns")
    plateau = False
    if len(conns) >= 6 and max(conns) >= 50:
        top = max(conns)
        plateau = sum(1 for c in conns if c >= 0.97 * top) >= 4
    conn_found = bool(ov_delta) or bool(backlog_full) or bool(fd_full)
    if ov_delta is None and backlog_full is None and fd_full is None:
        check("conn_limit", "Connection limit reached", "nodata", "Listen-queue and open-file counters weren't available")
    else:
        bits = []
        if ov_delta is not None:
            bits.append(f"{ov_delta:,} TCP listen-queue overflows (connection attempts dropped)" if ov_delta else "no listen-queue overflows")
        if q:
            bits.append(f"accept backlog peaked at {max(a for a, _ in q)}/{q[0][1]}")
        if fds:
            bits.append(f"{max(fds):,} open files" + (f" of {maxfiles:,} allowed" if maxfiles else ""))
        if plateau:
            bits.append(f"connections flat-lined at about {max(conns):,} before the drop")
        check("conn_limit", "Connection limit reached", "found" if conn_found else ("warn" if plateau else "clear"), _cap("; ".join(bits)))

    # 4. Network interface
    ie = [(s.get("ierrs", 0) or 0) + (s.get("oerrs", 0) or 0) for s in around if "ierrs" in s]
    nic_err = (max(ie) - min(ie)) if len(ie) >= 2 else None
    link_down = any(s.get("linkUp") is False for s in around)
    sat = None
    if link_mbps:
        peaks = [(s.get("outMbps", 0) + s.get("upMbps", 0)) / link_mbps for s in before]
        sat = max(peaks) if peaks else None
    net_found = link_down or (nic_err or 0) > 100 or (sat or 0) >= 0.9
    if nic_err is None and sat is None and not link_down:
        check("network", "Network link problem", "nodata", "Interface counters weren't available")
    else:
        bits = []
        if link_down:
            bits.append("the link went down")
        if nic_err is not None:
            bits.append(f"{nic_err:,} interface errors" if nic_err else "no interface errors")
        if sat is not None:
            bits.append(f"peak link use {sat*100:.0f}% of {link_mbps:,} Mbps")
        check("network", "Network link problem", "found" if net_found else "clear", _cap("; ".join(bits)))

    # 5. CPU
    cpu = _vals(before + after, "cpu")
    cores = next((s.get("cores") for s in around if s.get("cores")), 1)
    cpu_max = max(cpu) if cpu else None
    sys_cpu = _vals(before, "cpuPct")
    sys_max = max(sys_cpu) if sys_cpu else None
    cpu_found = (cpu_max is not None and cpu_max >= 90 * cores) or (sys_max is not None and sys_max >= 90)
    if cpu_max is None:
        check("cpu", "Cache CPU maxed out", "nodata", "Not measured")
    else:
        check("cpu", "Cache CPU maxed out", "found" if cpu_found else ("warn" if (sys_max or 0) >= 80 else "clear"),
              f"AssetCache peaked at {cpu_max:.0f}% CPU ({cpu_max/cores:.0f}% of {cores} cores)"
              + (f"; the whole Mac peaked at {sys_max:.0f}% CPU" if sys_max is not None else ""))

    # 6. Errors in the log
    def errsum(ss, cat=None):
        return sum((s.get("err") or {}).get(cat, 0) if cat else sum((s.get("err") or {}).values()) for s in ss)
    base_ss = _between(S, t0 - 1500, t0 - 300)
    base_rate = errsum(base_ss) / max(1, len(base_ss))
    win_ss = _between(S, t0 - 180, t0 + 180)
    win_rate = errsum(win_ss) / max(1, len(win_ss))
    cats = {c: errsum(win_ss, c) for c in ("timeout", "reset", "refused", "http503", "other")}
    err_found = errsum(win_ss) >= 20 and win_rate >= 3 * max(base_rate, 0.5)
    if not any("err" in s for s in S):
        check("errors", "Download errors in the caching log", "nodata", "Log not available")
    else:
        detail = ", ".join(f"{v} {k}" for k, v in cats.items() if v) or "none"
        check("errors", "Download errors in the caching log", "found" if err_found else "clear",
              f"Around the drop: {detail}" + (f" (normally about {base_rate:.1f} per check)" if err_found else ""))
    for s in win_ss:
        for line in s.get("errSample") or []:
            report["logLines"].append({"t": s["t"], "line": line})
    report["logLines"] = report["logLines"][-25:]

    # 7. Did the devices finish? (download records + demand)
    dep = departed or []
    report["departed"] = dep[:200]
    known = [d for d in dep if d.get("completed") is not None]
    done = [d for d in known if d["completed"]]
    frac_done = len(done) / len(known) if len(known) >= 3 else None
    if dep:
        if known:
            check("finished", "Devices finished their downloads", "clear" if (frac_done or 0) >= 0.6 else "warn",
                  f"{len(done)} of {len(known)} devices that stopped had completed their download"
                  + (f" ({len(dep)-len(known)} unknown)" if len(dep) > len(known) else ""))
        else:
            check("finished", "Devices finished their downloads", "nodata",
                  f"{len(dep)} devices stopped, but the log didn't say whether their downloads completed")
    else:
        check("finished", "Devices finished their downloads", "nodata", "No per-device records for this time")
    req_b = [r.get("requestsFromClient") or 0 for r in metrics_rows if t0 - 600 <= r["t"] < t0]
    req_a = [r.get("requestsFromClient") or 0 for r in metrics_rows if t0 + 60 <= r["t"] < t0 + 300]
    demand_drop = None
    if req_b:
        rb, ra = sum(req_b) / max(1, len(req_b)), (sum(req_a) / len(req_a)) if req_a else 0
        demand_drop = 1 - ra / rb if rb else None
        check("demand", "Devices stopped asking for content", "clear" if (demand_drop or 0) >= 0.6 else "warn",
              f"Requests went from about {rb:,.0f}/min to {ra:,.0f}/min")
    new_b, new_a = avg(_vals(before, "newConns")), avg(_vals(after, "newConns"))
    if _vals(before, "newConns"):
        check("newconns", "New connections", "info",
              f"New connections went from about {new_b:,.0f} to {new_a:,.0f} per check")

    # 8. Was the cache still fetching the content from Apple? (supporting info)
    origin = sum(sum(r.get(k) or 0 for k in ("bytesFromOriginToClient", "bytesFromOriginToChild", "bytesFromOriginToPeer")) for r in rows)
    served = sum(sum(v for k, v in r.items() if k.startswith("bytesFrom") and isinstance(v, (int, float))) for r in rows)
    if served:
        share = origin / served
        check("cold", "Content still being fetched from Apple", "warn" if share >= 0.5 else "clear",
              f"{share*100:.0f}% of what was served had to come from Apple first"
              + (". Devices may have timed out waiting for the cache." if share >= 0.5 else ""))

    # ------------------------------------------------------------------ verdict
    f = lambda k: next((c for c in checks if c["key"] == k), {}).get("state")
    if f("cache_full") == "found":
        kind, head = "cache_full", f"The cache ran out of space and turned {rej:,} requests away with HTTP 503. Those devices switched to the internet."
    elif f("restart") == "found":
        kind, head = "service_restart", "The caching service stopped or restarted, which cut every device's connection."
    elif f("conn_limit") == "found":
        kind, head = "connection_limit", ("The cache hit a connection limit: new connections were being dropped "
                                          f"({ov_delta or 0:,} listen-queue overflows), so devices couldn't connect and fell back to the internet.")
    elif f("network") == "found":
        kind, head = "network", "The cache's network link had a problem (link down, errors or saturation) right before the drop."
    elif f("cpu") == "found":
        kind, head = "cpu", "The caching service was using all available CPU, so it couldn't keep up with requests."
    elif f("errors") == "found":
        kind, head = "abandoned", "Downloads were failing (timeouts/resets) right before the drop, so devices most likely gave up on the cache."
    elif frac_done is not None and frac_done >= 0.6:
        kind, head = "finished", f"Normal: {len(done)} of {len(known)} devices that stopped had finished their downloads, and no errors, limits or rejections were recorded."
    elif frac_done is not None and frac_done < 0.4:
        kind, head = "abandoned", f"Only {len(done)} of {len(known)} devices that stopped had finished their downloads. The rest stopped part-way, which usually means they switched to the internet."
    elif (demand_drop is None or demand_drop >= 0.6) and f("errors") != "found":
        kind, head = "finished_probably", ("Probably normal: devices stopped asking for content and no errors, connection limits, "
                                           "rejections or restarts were recorded. This is what a batch of downloads finishing looks like.")
    else:
        kind, head = "unclear", "No limit, error or restart was found, but devices kept requesting content while traffic fell. Check the devices' own update status."
    if f("cold") == "warn" and kind in ("abandoned", "unclear"):
        head += " Most content was still coming from Apple at the time, so the update probably wasn't fully cached yet."
    report["verdict"] = {"kind": kind, "headline": head, "severity": SEVERITY.get(kind, "critical"), "title": TITLES[kind]}

    # chart series around the drop
    report["series"] = [{"t": s["t"], "outMbps": s.get("outMbps"), "upMbps": s.get("upMbps"), "devices": s.get("devices"),
                         "conns": s.get("conns"), "newConns": s.get("newConns"),
                         "errors": sum((s.get("err") or {}).values()) if "err" in s else None,
                         "overflows": s.get("listenOverflows"), "cpu": s.get("cpu")}
                        for s in _between(S, t0 - 900, t0 + 600)]
    return report
