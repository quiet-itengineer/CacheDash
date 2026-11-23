"""
CacheDash client monitor: which devices are pulling from the cache, what they
are downloading, and how fast, right now.

Two independent sources, both built into macOS and both readable only as root:

  1. LIVE BANDWIDTH  - `nettop` sampled every few seconds for the AssetCache
     process. Each TCP connection shows cumulative bytes; the difference between
     samples is that device's current rate. Connections to private (LAN)
     addresses are clients; connections to public addresses are the cache
     fetching from Apple.

  2. WHAT IS DOWNLOADED - `log stream` for the com.apple.AssetCache subsystem.
     Apple doesn't document this log format, so parsing is best-effort and
     pattern-based. Records are stored in clients.db (SQLite, 30 days).

Neither source sees traffic that does NOT go through the cache. Devices that
download straight from Apple are invisible here - use your firewall for those.
"""

import concurrent.futures
import csv
import ipaddress
import json
import os
import re
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.parse
from collections import deque

NETTOP = "/usr/bin/nettop"
LOG = "/usr/bin/log"
PGREP = "/usr/bin/pgrep"
PREDICATE = 'subsystem == "com.apple.AssetCache"'
RETENTION = 30 * 86400

# ---------------------------------------------------------------- parsing ---

RE_IPV4 = re.compile(r"(?<![\d.])((?:\d{1,3}\.){3}\d{1,3})(?::(\d{1,5}))?(?![\d.])")
RE_RID = re.compile(r"#([0-9A-Fa-f]{6,16})\b")
RE_URL = re.compile(r"(https?://[^\s\"'<>]+|/[^\s\"'<>]+\.(?:ipa|pkg|ipsw|zip|dmg|xip|aar|tar|bom|plist|asset|signed)[^\s\"'<>]*)", re.I)
RE_QUOTED = re.compile(r"\"([^\"]{3,300})\"")
RE_MODEL = re.compile(r"(?:model|hwmodel)/([A-Za-z]+\d+,\d+)")
RE_OS = re.compile(r"\b(iOS|iPadOS|macOS|Mac OS X|tvOS|watchOS|visionOS|xrOS)[/ ]([\d._]+)")
RE_SIZE = re.compile(r"(Zero|[\d][\d,.]*)\s*(bytes?|[KMGTP]i?B)\b", re.I)
RE_SERVED = re.compile(r"\bserved\b", re.I)
UNITS = {"B": 1, "BYTE": 1, "BYTES": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12, "PB": 1e15,
         "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3, "TIB": 1024**4}


def parse_size(num, unit):
    if num.lower() == "zero":
        return 0
    try:
        return int(float(num.replace(",", "")) * UNITS.get(unit.upper(), 1))
    except ValueError:
        return None


def is_lan(ip):
    try:
        a = ipaddress.ip_address(ip)
        return a.is_private or a.is_link_local
    except ValueError:
        return False


def classify(url, agent=""):
    """Turn an Apple CDN URL into (kind, item) a person can read."""
    u = (url or "").lower()
    a = (agent or "").lower()
    name = (url or "").split("?")[0].rstrip("/").split("/")[-1] if url else ""
    ver = re.search(r"(?<![\d.])(\d{1,2}\.\d{1,2}(?:\.\d{1,2})?)(?![\d.])", url or "")
    dev = re.search(r"(iPad\d+,\d+|iPhone\d+,\d+|Mac\d+,\d+|AppleTV\d+,\d+)", url or "")
    if "installassistant" in u or "installmacos" in u:
        return "macOS installer", name
    if u.endswith(".ipsw") or "ipsw" in u or "updatebrain" in u or "mesu." in u or "appldnld" in u \
            or ("updates.cdn-apple.com" in u and "macos" not in u) or "softwareupdateservices" in a:
        item = "Update" + (f" {ver.group(1)}" if ver else "") + (f" for {dev.group(1)}" if dev else "")
        return "iOS/iPadOS update", item
    if "swcdn.apple.com" in u or "/content/downloads/" in u or "swdist" in u or "softwareupdated" in a \
            or (u.endswith(".pkg") and "osxapps" not in u):
        return "macOS update", name
    if "osxapps" in u or ("appstore" in a and "macos" in a):
        return "Mac App Store app", name
    if ".ipa" in u or "itunes-assets" in u or "purple" in u or "appstored" in a or "itunesstored" in a:
        return "App Store app", name
    if "audiocontentdownload" in u or "garageband" in u or "logic" in u:
        return "Music & sound content", name
    if "icloud" in u or "cloudkit" in u or "icloud" in a:
        return "iCloud", name
    if "books" in u or "ibooks" in a:
        return "Books", name
    if "xcode" in u or "devimage" in u or "simulator" in u:
        return "Developer content", name
    if "asset" in u or "mobileasset" in a:
        return "System assets", name
    return ("Other Apple content", name) if url else ("Unknown", "")


RE_HASHY = re.compile(r"^(?:[0-9a-f]{8,}|[0-9a-f-]{20,}|pre-?thinned\d*|thinned\d*|\d+)$", re.I)


def rollout_key(url):
    """(group key, file name) for an App Store download address. All devices downloading the same
    app version fetch from the same folder; the file name differs by device type (app thinning)."""
    if not url:
        return None, None
    path = urllib.parse.urlsplit(url if "://" in url else "http://x" + url).path.rstrip("/")
    folder, _, fname = path.rpartition("/")
    if not folder or not fname:
        return None, None
    return folder.lower(), fname


def rollout_path(url):
    """Path of a download address without host or query (?source=…), lower-cased for matching."""
    if not url:
        return ""
    return urllib.parse.unquote(urllib.parse.urlsplit(url if "://" in url else "http://x" + url).path).rstrip("/").lower()


def readable_app_name(fname):
    """'Keynote-14.4.signed.dpkg.ipa' -> 'Keynote 14.4'; random or thinned file names -> None."""
    base = re.sub(r"(\.signed)?(\.dpkg)?\.(ipa|pkg)$|\.signed\.dpkg$|\.thinned.*$", "", fname or "", flags=re.I)
    base = re.sub(r"[._]?(signed|dpkg|thinned|lc|npk)$", "", base, flags=re.I)
    if not base or RE_HASHY.match(base) or not re.search(r"[A-Za-z]{3,}", base):
        return None
    if re.fullmatch(r"[A-Za-z0-9]{16,}", base) and re.search(r"\d", base):
        return None   # long random token
    return re.sub(r"[-_]+", " ", base).strip()


class LogParser:
    """Merges AssetCache log lines into download records.

    Apple's messages commonly carry a request tag like "#a1b2c3d4" that ties
    the request line (client address, user agent, URL) to the completion line
    ("Served … bytes … from cache …"). We collect fields per tag and emit a
    record when we see a byte count or after a timeout.
    """

    def __init__(self, own_ips=()):
        self.pending = {}
        self.own_ips = set(own_ips)
        self.stats = {"lines": 0, "records": 0, "redacted": 0}

    def _fields(self, msg):
        f = {}
        for m in RE_IPV4.finditer(msg):
            ip = m.group(1)
            if is_lan(ip) and ip not in self.own_ips:
                f["ip"] = ip
                break
        if (u := RE_URL.search(msg)):
            f["url"] = u.group(1)
        for q in RE_QUOTED.findall(msg):
            if "/" in q and any(k in q.lower() for k in ("os/", "model/", "cfnetwork", "store", "update", "com.apple", "darwin", "mobileasset", "nsurlsession")):
                f["agent"] = q
                break
        if (mm := RE_MODEL.search(msg)):
            f["model"] = mm.group(1)
        if (mo := RE_OS.search(msg)):
            f["os"] = f"{mo.group(1)} {mo.group(2).replace('_', '.')}"
        if RE_SERVED.search(msg):
            sizes = [(m.start(), parse_size(m.group(1), m.group(2))) for m in RE_SIZE.finditer(msg)]
            sizes = [(p, s) for p, s in sizes if s is not None]
            if sizes:
                f["bytes"] = max(s for _, s in sizes)
                low = msg.lower()
                def near(word):
                    i = low.find(word)
                    if i < 0:
                        return 0
                    before = [s for p, s in sizes if p < i]
                    return before[-1] if before else 0
                f["from_cache"] = near("from cache")
                f["from_origin"] = near("from internet") or near("from origin") or near("from apple")
                f["from_peer"] = near("from peer")
        return f

    def feed(self, msg, t=None):
        t = t or time.time()
        self.stats["lines"] += 1
        if "<private>" in msg:
            self.stats["redacted"] += 1
        f = self._fields(msg)
        m_rid = RE_RID.search(msg)
        rid = m_rid.group(1) if m_rid else None
        out = []   # list of records; a record with "update": True completes one already returned
        if rid:
            rec = self.pending.setdefault(rid, {"rid": rid, "t": t})
            for k, v in f.items():
                if k not in rec or k in ("bytes", "from_cache", "from_origin", "from_peer"):
                    rec[k] = v
            rec["last"] = t
            # Report the download as soon as we know who and what, so it shows up
            # (and gets measured bytes) while it is still in progress.
            if not rec.get("emitted") and rec.get("ip") and (rec.get("url") or "bytes" in rec):
                rec["emitted"] = True
                out.append(dict(rec))
            if "bytes" in f and rec.get("emitted"):
                done = dict(self.pending.pop(rid))
                done["update"] = True
                out.append(done)
        elif f.get("ip") and (f.get("url") or "bytes" in f):
            out.append({"t": t, **f})
        # forget partial records that went quiet
        for k in [k for k, r in self.pending.items() if t - r.get("last", r["t"]) > 900]:
            r = self.pending.pop(k)
            if r.get("ip") and not r.get("emitted"):
                out.append(r)
        for r in out:
            r["kind"], r["item"] = classify(r.get("url"), r.get("agent", ""))
            if not r.get("update"):
                self.stats["records"] += 1
        return out


def parse_nettop(text):
    """Parse `nettop -L 1 -x` CSV into {flow: (remote_ip, bytes_in, bytes_out)}."""
    flows = {}
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return flows
    head = [h.strip() for h in lines[0].split(",")]
    try:
        i_in, i_out = head.index("bytes_in"), head.index("bytes_out")
    except ValueError:
        i_in, i_out = 2, 3
    for l in lines[1:]:
        cols = [c.strip() for c in l.split(",")]
        if len(cols) <= max(i_in, i_out):
            continue
        desc = next((c for c in cols if "<->" in c), None)
        if not desc:
            continue
        try:
            bi, bo = int(float(cols[i_in] or 0)), int(float(cols[i_out] or 0))
        except ValueError:
            # Column layout differs from the header: use the last two numbers on the row.
            nums = [c for c in cols if re.fullmatch(r"\d+(?:\.\d+)?", c)]
            if len(nums) < 2:
                continue
            bi, bo = int(float(nums[-2])), int(float(nums[-1]))
        pair = desc.split()[-1]
        local, remote = pair.split("<->", 1)
        rip = re.sub(r"[:.](\d+)$", "", remote) if remote.count(":") <= 1 else remote.rsplit(".", 1)[0]
        rip = rip.split("%")[0]
        flows[desc] = (rip, bi, bo)
    return flows


# ------------------------------------------------------------- the monitor ---

class ClientMonitor:
    def __init__(self, db_path, names_file=None, interval=3.0, enabled=True, resolver=None):
        self.db_path = db_path
        self.resolver = resolver   # catalog.CatalogResolver: real names for update/.pkg downloads
        self.on_log_line = None    # optional hook (alerts engine counts error lines)
        self.logbuf = deque(maxlen=20000)   # recent AssetCache log lines for the dashboard's log viewer
        self.names_file = names_file
        self.interval = interval
        self.enabled = enabled
        self.lock = threading.Lock()
        self.live = {"available": False, "error": "starting", "at": 0, "clients": [], "upstream": {}}
        self.log_state = {"available": False, "error": "starting", "startedAt": time.time()}
        self.meta = {}           # ip -> latest {model, os, agent, kind, item, t}
        self.names = {}
        self.dns = {}            # ip -> (name|None, fetched_at)
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        self.parser = LogParser(own_ips=self._own_ips())
        self.recent = deque(maxlen=500)
        self._load_names()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute("""CREATE TABLE IF NOT EXISTS downloads (
            t REAL, ip TEXT, model TEXT, os TEXT, agent TEXT, kind TEXT, item TEXT,
            url TEXT, bytes INTEGER, from_cache INTEGER, from_origin INTEGER, from_peer INTEGER, rid TEXT)""")
        self._db.execute("CREATE INDEX IF NOT EXISTS downloads_t ON downloads(t)")
        if "measured" not in [c[1] for c in self._db.execute("PRAGMA table_info(downloads)")]:
            self._db.execute("ALTER TABLE downloads ADD COLUMN measured INTEGER")
        # Bytes actually sent to each device, measured by nettop, one row per device per minute.
        self._db.execute("""CREATE TABLE IF NOT EXISTS usage (
            t INTEGER, ip TEXT, bytes_out INTEGER, bytes_in INTEGER, PRIMARY KEY (t, ip))""")
        self._db.commit()
        self.active = {}   # ip -> (rowid of its latest download record, started at)
        if enabled:
            threading.Thread(target=self._live_loop, daemon=True).start()
            threading.Thread(target=self._log_loop, daemon=True).start()

    # -- what is it? ----------------------------------------------------------
    def label(self, r):
        """Replace a raw file name with the real product name when Apple's catalogs know it."""
        url = r.get("url")
        fn = (url or "").split("?")[0].rstrip("/").split("/")[-1]
        r["file"] = fn or None
        res = self.resolver.lookup(url) if (self.resolver and url) else None
        if res:
            r["item"] = res["title"]
            r["detail"] = res.get("detail")
            if res.get("kind"):
                r["kind"] = res["kind"]
            r["named"] = True
        elif "MobileAsset_MacSoftwareUpdate" in (url or "") or "MobileAsset_SoftwareUpdate" in (url or ""):
            # OS update that isn't in Apple's public catalog: say what it is, not a hash.
            mac = "MacSoftwareUpdate" in url
            r["kind"] = "macOS update" if mac else "iOS/iPadOS update"
            r["item"] = ("macOS" if mac else "iOS/iPadOS") + " system update"
            r["detail"] = f"device is on {r['os']}" if r.get("os") else "version not in Apple's public catalog"
            r["named"] = True
        return r

    # -- naming -----------------------------------------------------------
    @staticmethod
    def _own_ips():
        ips = set()
        try:
            out = subprocess.run(["/sbin/ifconfig"], capture_output=True, text=True, timeout=5).stdout
            ips.update(re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", out))
        except Exception:
            pass
        return ips

    def _load_names(self):
        """Optional CSV of `ip,name` (header optional) for friendly device names."""
        if not self.names_file or not os.path.exists(self.names_file):
            return
        with open(self.names_file, newline="") as f:
            for row in csv.reader(f):
                if len(row) >= 2 and RE_IPV4.fullmatch(row[0].strip()):
                    self.names[row[0].strip()] = row[1].strip()

    def name_for(self, ip):
        if ip in self.names:
            return self.names[ip]
        hit = self.dns.get(ip)
        if hit and time.time() - hit[1] < 3600:
            return hit[0]
        if not hit or time.time() - hit[1] >= 3600:
            self.dns[ip] = (hit[0] if hit else None, time.time())
            self.pool.submit(self._resolve, ip)
        return hit[0] if hit else None

    def _resolve(self, ip):
        try:
            name = socket.gethostbyaddr(ip)[0].split(".")[0]
        except Exception:
            name = None
        self.dns[ip] = (name, time.time())

    # -- live bandwidth via nettop ---------------------------------------------
    def _sample(self):
        pids = subprocess.run([PGREP, "-x", "AssetCache"], capture_output=True, text=True, timeout=5).stdout.split()
        if not pids:
            raise RuntimeError("The AssetCache process isn't running (is content caching on?)")
        args = [NETTOP, "-L", "1", "-x", "-n", "-m", "tcp", "-J", "bytes_in,bytes_out"]
        for p in pids:
            args += ["-p", p]
        r = subprocess.run(args, capture_output=True, text=True, timeout=20)
        if r.returncode != 0 and not r.stdout:
            raise RuntimeError((r.stderr or "nettop failed").strip()[:200])
        return parse_nettop(r.stdout)

    def _live_loop(self):
        prev, prev_t = {}, None
        while True:
            try:
                flows = self._sample()
                now = time.time()
                per = {}
                up = {"rateIn": 0.0, "rateOut": 0.0, "conns": 0}
                for key, (ip, bi, bo) in flows.items():
                    dt = (now - prev_t) if prev_t else None
                    if key in prev:
                        pbi, pbo = prev[key][1], prev[key][2]
                    elif prev_t:
                        pbi, pbo = 0, 0      # new connection since last sample: all of it is new
                    else:
                        pbi, pbo = bi, bo    # very first sample: no baseline yet
                    din, dout = max(0, bi - pbi), max(0, bo - pbo)
                    rin = din / dt if dt else 0.0
                    rout = dout / dt if dt else 0.0
                    if is_lan(ip):
                        c = per.setdefault(ip, {"ip": ip, "rateOut": 0.0, "rateIn": 0.0, "conns": 0, "bytesOut": 0})
                        c["rateOut"] += rout
                        c["rateIn"] += rin
                        c["conns"] += 1
                        c["bytesOut"] += bo
                    else:
                        up["rateIn"] += rin
                        up["rateOut"] += rout
                        up["conns"] += 1
                clients = []
                for ip, c in per.items():
                    m = self.label(dict(self.meta.get(ip, {})))
                    clients.append({**c, "name": self.name_for(ip), "model": m.get("model"), "os": m.get("os"), "url": m.get("url"),
                                    "kind": m.get("kind"), "item": m.get("item"), "file": m.get("file"),
                                    "detail": m.get("detail"), "named": m.get("named"), "lastSeen": m.get("t")})
                clients.sort(key=lambda c: c["rateOut"], reverse=True)
                if prev_t:
                    self._record_usage(now, per, now - prev_t)
                with self.lock:
                    self.live = {"available": True, "error": None, "at": now, "interval": self.interval,
                                 "clients": clients, "upstream": up,
                                 "totalOut": sum(c["rateOut"] for c in clients),
                                 "conns": sum(c["conns"] for c in clients),
                                 "newConns": sum(1 for k, v in flows.items() if k not in prev and is_lan(v[0])) if prev_t else 0}
                prev, prev_t = flows, now
            except FileNotFoundError:
                with self.lock:
                    self.live = {"available": False, "error": "nettop not found (this only works on macOS)", "clients": [], "upstream": {}}
                return
            except Exception as e:
                with self.lock:
                    self.live = {**self.live, "available": False, "error": str(e)}
            time.sleep(self.interval)

    def _record_usage(self, now, per, dt):
        """Save bytes sent per device this sample, and credit them to the device's current download."""
        minute = int(now // 60 * 60)
        rows = [(minute, ip, int(c["rateOut"] * dt), int(c["rateIn"] * dt)) for ip, c in per.items() if c["rateOut"] > 0]
        if not rows:
            return
        with self.lock:
            self._db.executemany("""INSERT INTO usage (t, ip, bytes_out, bytes_in) VALUES (?,?,?,?)
                ON CONFLICT(t, ip) DO UPDATE SET bytes_out = bytes_out + excluded.bytes_out,
                                                 bytes_in = bytes_in + excluded.bytes_in""", rows)
            for _, ip, b, _ in rows:
                act = self.active.get(ip)
                if act and now - act[1] < 3 * 3600:
                    self._db.execute("UPDATE downloads SET measured = COALESCE(measured, 0) + ? WHERE rowid = ?", (b, act[0]))
            self._db.commit()

    # -- downloads via unified log -------------------------------------------------
    def _store(self, recs):
        if not recs:
            return
        updates = [r for r in recs if r.get("update")]
        recs = [r for r in recs if not r.get("update")]
        with self.lock:
            for u in updates:  # completion line for a download we already recorded
                self._db.execute("""UPDATE downloads SET bytes = ?, from_cache = ?, from_origin = ?, from_peer = ?
                                    WHERE rid = ? AND ip = ?""",
                                 (u.get("bytes"), u.get("from_cache"), u.get("from_origin"), u.get("from_peer"), u["rid"], u["ip"]))
            self.parser.stats["completed"] = self.parser.stats.get("completed", 0) + len(updates)
            if updates and not recs:
                self._db.commit()
            for r in recs:
                self.recent.appendleft(r)
                self.meta[r["ip"]] = {k: r.get(k) for k in ("model", "os", "agent", "kind", "item", "url", "t")}
                if self.resolver:
                    self.resolver.lookup(r.get("url"))  # start fetching its name in the background
            for r in recs:
                cur = self._db.execute(
                    """INSERT INTO downloads (t, ip, model, os, agent, kind, item, url, bytes, from_cache,
                       from_origin, from_peer, rid) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (r["t"], r["ip"], r.get("model"), r.get("os"), r.get("agent"), r["kind"], r["item"],
                     r.get("url"), r.get("bytes"), r.get("from_cache"), r.get("from_origin"),
                     r.get("from_peer"), r.get("rid")))
                # Later bytes measured for this device are credited to this download.
                self.active[r["ip"]] = (cur.lastrowid, r["t"])
            self._db.commit()

    def _log_loop(self):
        last_purge = 0
        while True:
            try:
                p = subprocess.Popen([LOG, "stream", "--style", "ndjson", "--level", "info",
                                      "--predicate", PREDICATE],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
                with self.lock:
                    self.log_state = {"available": True, "error": None, "startedAt": time.time()}
                for line in p.stdout:
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    msg = ev.get("eventMessage", "")
                    self.logbuf.append({"t": time.time(), "type": ev.get("messageType", "Default"), "msg": msg})
                    if self.on_log_line:
                        try:
                            self.on_log_line(msg)
                        except Exception:
                            pass
                    self._store(self.parser.feed(msg))
                    if time.time() - last_purge > 3600:
                        with self.lock:
                            self._db.execute("DELETE FROM downloads WHERE t < ?", (time.time() - RETENTION,))
                            self._db.execute("DELETE FROM usage WHERE t < ?", (time.time() - RETENTION,))
                            self._db.commit()
                        last_purge = time.time()
                err = (p.stderr.read() or "").strip()
                raise RuntimeError(err[:200] or "log stream ended")
            except FileNotFoundError:
                with self.lock:
                    self.log_state = {"available": False, "error": "`log` not found (this only works on macOS)"}
                return
            except Exception as e:
                with self.lock:
                    self.log_state = {"available": False, "error": str(e)}
                time.sleep(10)

    def departed(self, t0):
        """Devices that were downloading just before t0 and stopped right after it, with whether
        their last download had completed (per the log) and how much was measured going to them."""
        with self.lock:
            before = {r[0]: r[1] for r in self._db.execute(
                "SELECT ip, SUM(bytes_out) FROM usage WHERE t BETWEEN ? AND ? GROUP BY ip", (t0 - 300, t0))}
            after = {r[0] for r in self._db.execute(
                "SELECT ip FROM usage WHERE t BETWEEN ? AND ? AND bytes_out > 1000000 GROUP BY ip", (t0 + 60, t0 + 300))}
            out = []
            for ip, b in sorted(before.items(), key=lambda x: -(x[1] or 0)):
                if ip in after or (b or 0) < 1_000_000:
                    continue
                rec = self._db.execute(
                    """SELECT t, kind, item, url, bytes, measured, rid FROM downloads WHERE ip = ? AND t <= ?
                       ORDER BY t DESC LIMIT 1""", (ip, t0 + 300)).fetchone()
                d = {"ip": ip, "name": self.name_for(ip), "bytesBefore": b, "model": self.meta.get(ip, {}).get("model")}
                if rec:
                    r = self.label({"t": rec[0], "kind": rec[1], "item": rec[2], "url": rec[3]})
                    d.update({"started": rec[0], "kind": r["kind"], "item": r["item"], "named": r.get("named"),
                              "size": rec[4], "measured": rec[5],
                              # completed is only known when the log reported the download's end (with rid)
                              # (and only if this Mac's log reports download ends at all)
                              "completed": (rec[4] is not None) if (rec[6] and self.parser.stats.get("completed", 0) > 0) else None})
                out.append(d)
            return out

    # -- API -------------------------------------------------------------------
    def device(self, ip, start=None, end=None, limit=1000):
        """Everything one device downloaded in a window, grouped by app/update, plus a usage timeline."""
        now = time.time()
        end = end or now
        start = start or end - 86400
        span = max(60, end - start)
        bucket = next((b for b in (60, 300, 900, 3600, 3 * 3600, 6 * 3600, 86400) if span / b <= 240), 86400)
        with self.lock:
            rows = self._db.execute(
                """SELECT t, ip, model, os, agent, kind, item, url, bytes, from_cache, from_origin, from_peer, measured
                   FROM downloads WHERE ip = ? AND t BETWEEN ? AND ? ORDER BY t DESC""", (ip, start, end)).fetchall()
            use = self._db.execute(
                "SELECT (t / ?) * ?, SUM(bytes_out) FROM usage WHERE ip = ? AND t BETWEEN ? AND ? GROUP BY 1 ORDER BY 1",
                (bucket, bucket, ip, start - 60, end)).fetchall()
            live = next((c for c in self.live.get("clients", []) if c["ip"] == ip), None)
            meta = dict(self.meta.get(ip, {}))
        cols = ["t", "ip", "model", "os", "agent", "kind", "item", "url", "bytes", "fromCache", "fromOrigin", "fromPeer", "measured"]
        recs = [self.label(dict(zip(cols, r))) for r in rows]
        groups = {}
        for r in recs:
            if not r["bytes"] and r.get("measured"):
                r["bytes"], r["sizeMeasured"] = r["measured"], True
            key = (r["kind"], r["item"])
            g = groups.setdefault(key, {"kind": r["kind"], "item": r["item"], "detail": r.get("detail"), "file": r.get("file"), "url": r.get("url"),
                                        "named": r.get("named"), "count": 0, "bytes": 0, "fromCache": 0, "fromOrigin": 0,
                                        "measured": False, "first": r["t"], "last": r["t"]})
            g["count"] += 1
            g["bytes"] += r["bytes"] or 0
            g["fromCache"] += r["fromCache"] or 0
            g["fromOrigin"] += r["fromOrigin"] or 0
            g["measured"] = g["measured"] or bool(r.get("sizeMeasured"))
            g["first"], g["last"] = min(g["first"], r["t"]), max(g["last"], r["t"])
        measured_total = sum(b or 0 for _, b in use)
        log_total = sum(g["bytes"] for g in groups.values())
        model = meta.get("model") or next((r["model"] for r in recs if r.get("model")), None)
        osv = meta.get("os") or next((r["os"] for r in recs if r.get("os")), None)
        return {
            "ip": ip, "name": self.name_for(ip), "model": model, "os": osv,
            "start": start, "end": end,
            "downloads": len(recs), "bytes": max(measured_total, log_total),
            "firstSeen": min((r["t"] for r in recs), default=None), "lastSeen": max([r["t"] for r in recs] + [t for t, _ in use], default=None),
            "live": live,
            "groups": sorted(groups.values(), key=lambda g: g["bytes"], reverse=True),
            "records": recs[:limit],
            "timeline": {"bucket": bucket, "points": [[t, b or 0] for t, b in use]},
        }

    def summary(self, start, end):
        """Per-device and per-item totals for [start, end), used for daily history and reports."""
        with self.lock:
            rows = self._db.execute("""SELECT t, ip, model, os, agent, kind, item, url, bytes, measured
                                       FROM downloads WHERE t >= ? AND t < ?""", (start, end)).fetchall()
            usage = dict(self._db.execute("SELECT ip, SUM(bytes_out) FROM usage WHERE t >= ? AND t < ? GROUP BY ip",
                                          (start, end)).fetchall())
        cols = ["t", "ip", "model", "os", "agent", "kind", "item", "url", "bytes", "measured"]
        devices, items = {}, {}
        for row in rows:
            r = self.label(dict(zip(cols, row)))
            b = r.get("bytes") or r.get("measured") or 0
            d = devices.setdefault(r["ip"], {"logBytes": 0, "downloads": 0, "model": None, "os": None})
            d["logBytes"] += b
            d["downloads"] += 1
            d["model"] = d["model"] or r.get("model")
            d["os"] = d["os"] or r.get("os")
            key = (r.get("kind") or "Other", r.get("item") or r.get("file") or "")
            it = items.setdefault(key, {"downloads": 0, "bytes": 0, "ips": set()})
            it["downloads"] += 1
            it["bytes"] += b
            it["ips"].add(r["ip"])
        for ip, ub in usage.items():
            d = devices.setdefault(ip, {"logBytes": 0, "downloads": 0, "model": self.meta.get(ip, {}).get("model"),
                                        "os": self.meta.get(ip, {}).get("os")})
            d["measured"] = ub or 0
        for d in devices.values():
            d["bytes"] = max(d.get("measured") or 0, d["logBytes"])
        return devices, items

    def snapshot(self, start=None, end=None, ip=None, limit=200, light=False, find=None):
        now = time.time()
        if light:  # live bandwidth only (polled every few seconds)
            with self.lock:
                return {"live": dict(self.live), "log": {**self.log_state, **self.parser.stats}}
        catalog = self.resolver.summary() if self.resolver else {"enabled": False}
        end = end or now
        start = start or end - 3600
        with self.lock:
            live = dict(self.live)
            logs = {**self.log_state, **self.parser.stats}
            q = "SELECT t, ip, model, os, agent, kind, item, url, bytes, from_cache, from_origin, from_peer, measured FROM downloads WHERE t BETWEEN ? AND ?"
            args = [start, end]
            if ip:
                q += " AND ip = ?"
                args.append(ip)
            rows = self._db.execute(q + " ORDER BY t DESC", args).fetchall()
            uq = "SELECT ip, SUM(bytes_out), MAX(t) FROM usage WHERE t BETWEEN ? AND ?" + (" AND ip = ?" if ip else "") + " GROUP BY ip"
            usage = {u[0]: (u[1] or 0, u[2]) for u in self._db.execute(uq, [start - 60, end] + ([ip] if ip else []))}
        cols = ["t", "ip", "model", "os", "agent", "kind", "item", "url", "bytes", "fromCache", "fromOrigin", "fromPeer", "measured"]
        recs = [self.label(dict(zip(cols, r))) for r in rows]
        for r in recs:
            if not r["bytes"] and r.get("measured"):
                r["bytes"] = r["measured"]          # size from nettop when the log didn't say
                r["sizeMeasured"] = True
        top, kinds = {}, {}
        for r in recs:
            d = top.setdefault(r["ip"], {"ip": r["ip"], "bytes": 0, "logBytes": 0, "downloads": 0, "lastSeen": 0, "model": r["model"], "os": r["os"], "kinds": {}})
            b = r["bytes"] or 0
            d["logBytes"] += b
            d["downloads"] += 1
            d["lastSeen"] = max(d["lastSeen"], r["t"])
            d["model"] = d["model"] or r["model"]
            d["kinds"][r["kind"]] = d["kinds"].get(r["kind"], 0) + b
            k = kinds.setdefault(r["kind"], {"bytes": 0, "downloads": 0})
            k["bytes"] += b
            k["downloads"] += 1
        # Data per device = bytes nettop measured going to it (reliable), falling back to log totals.
        for uip, (ub, ut) in usage.items():
            d = top.setdefault(uip, {"ip": uip, "bytes": 0, "logBytes": 0, "downloads": 0, "lastSeen": 0,
                                     "model": self.meta.get(uip, {}).get("model"), "os": self.meta.get(uip, {}).get("os"), "kinds": {}})
            d["measuredBytes"] = ub
            d["lastSeen"] = max(d["lastSeen"], ut or 0)
        for d in top.values():
            d["bytes"] = max(d.get("measuredBytes") or 0, d["logBytes"])
            d["name"] = self.name_for(d["ip"])
        for r in recs[:limit]:
            r["name"] = self.name_for(r["ip"])
        return {
            "start": start, "end": end,
            "live": live, "log": logs, "catalog": catalog,
            "top": sorted(top.values(), key=lambda d: d["bytes"], reverse=True)[:50],
            "kinds": kinds,
            "recent": recs[:limit],
            "deviceCount": len(top),
            "found": self._find(top.values(), find) if find else None,
        }

    def downloads_of(self, url, start=None, end=None, limit=2000):
        """Every device that downloaded this file (same address path) in a window, newest first.
        For App Store apps, other device-type versions of the same app update (same folder) are
        listed too, marked as variants."""
        path = rollout_path(url)
        if not path:
            return {"error": "no address given"}
        folder = path.rpartition("/")[0]
        now = time.time()
        end = end or now
        start = start or end - RETENTION
        like = folder.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self.lock:
            rows = self._db.execute("""SELECT t, ip, model, os, agent, kind, item, url, bytes, from_cache, from_origin, from_peer, measured
                                       FROM downloads WHERE t BETWEEN ? AND ? AND url LIKE ? ESCAPE '\\' ORDER BY t DESC LIMIT ?""",
                                    (start, end, f"%{like}/%", limit * 3)).fetchall()
        cols = ["t", "ip", "model", "os", "agent", "kind", "item", "url", "bytes", "fromCache", "fromOrigin", "fromPeer", "measured"]
        recs, sample = [], None
        for r in rows:
            d = dict(zip(cols, r))
            p = rollout_path(d["url"])
            exact = p == path
            if not exact and not (p.rpartition("/")[0] == folder and d["kind"] in ("App Store app", "Mac App Store app")):
                continue
            d = self.label(d)
            if not d["bytes"] and d.get("measured"):
                d["bytes"], d["sizeMeasured"] = d["measured"], True
            d["variant"] = not exact
            d["name"] = self.name_for(d["ip"])
            if exact and sample is None:
                sample = d
            recs.append(d)
            if len(recs) >= limit:
                break
        devices = {}
        for d in recs:
            x = devices.setdefault(d["ip"], {"ip": d["ip"], "name": d["name"], "model": d["model"], "os": d["os"],
                                             "downloads": 0, "bytes": 0, "fromCache": 0, "fromOrigin": 0,
                                             "first": d["t"], "last": d["t"], "variant": True})
            x["downloads"] += 1
            x["bytes"] += d["bytes"] or 0
            x["fromCache"] += d["fromCache"] or 0
            x["fromOrigin"] += d["fromOrigin"] or 0
            x["first"], x["last"] = min(x["first"], d["t"]), max(x["last"], d["t"])
            x["variant"] = x["variant"] and d["variant"]
            x["model"] = x["model"] or d["model"]
        return {"path": path, "start": start, "end": end, "sample": sample or (recs[0] if recs else None),
                "devices": sorted(devices.values(), key=lambda x: -x["last"]),
                "downloads": len(recs), "exact": sum(1 for d in recs if not d["variant"]),
                "bytes": sum(d["bytes"] or 0 for d in recs), "records": recs[:200]}

    def rollouts(self, start=None, end=None, active_window=600):
        """App Store downloads grouped by app version: every device downloading the same app update
        fetches files from the same folder, so a big app update shows up as one group with many devices.
        Apple doesn't publish app names for these addresses, so most groups are unnamed."""
        now = time.time()
        end = end or now
        start = start or end - 6 * 3600
        with self.lock:
            rows = self._db.execute("""SELECT t, ip, model, kind, url, bytes, measured FROM downloads
                                       WHERE t BETWEEN ? AND ? AND kind IN ('App Store app', 'Mac App Store app')
                                       AND url IS NOT NULL""", (start, end)).fetchall()
        groups = {}
        for t, ip, model, kind, url, b, measured in rows:
            key, fname = rollout_key(url)
            if not key:
                continue
            g = groups.setdefault(key, {"key": key, "kind": kind, "url": url, "files": set(), "ips": set(), "active": set(),
                                        "downloads": 0, "bytes": 0, "sizes": [], "first": t, "last": t, "models": {}})
            size = b or measured or 0
            g["downloads"] += 1
            g["bytes"] += size
            if size:
                g["sizes"].append(size)
            g["ips"].add(ip)
            if t >= now - active_window:
                g["active"].add(ip)
            g["files"].add(fname)
            if model:
                g["models"][model] = g["models"].get(model, 0) + 1
            g["first"], g["last"] = min(g["first"], t), max(g["last"], t)
        out = []
        for g in groups.values():
            sizes = sorted(g["sizes"])
            name = next((n for n in (readable_app_name(f) for f in g["files"]) if n), None)
            out.append({"key": g["key"], "kind": g["kind"], "name": name, "file": sorted(g["files"])[0], "url": g["url"],
                        "devices": len(g["ips"]), "activeDevices": len(g["active"]), "downloads": g["downloads"],
                        "bytes": g["bytes"], "typicalSize": sizes[len(sizes) // 2] if sizes else None,
                        "first": g["first"], "last": g["last"], "variants": len(g["files"]),
                        "models": sorted(g["models"].items(), key=lambda kv: -kv[1])[:5],
                        "ips": sorted(g["ips"])[:5000]})
        out.sort(key=lambda g: (g["activeDevices"], g["devices"], g["bytes"]), reverse=True)
        return {"start": start, "end": end, "groups": out[:100], "total": len(out),
                "downloads": len(rows), "log": {**self.log_state, **self.parser.stats}}

    @staticmethod
    def _find(devices, q):
        """Devices whose address, name, model or OS matches q (an exact address if q is one), biggest first."""
        q = q.strip().lower()
        exact = bool(re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", q))
        hit = [d for d in devices if (d["ip"] == q if exact else
               any(q in str(d.get(k) or "").lower() for k in ("ip", "name", "model", "os")))]
        return sorted(hit, key=lambda d: d["bytes"], reverse=True)[:50]


def diagnose(seconds=30, out_path="assetcache-log-sample.ndjson"):
    """Print what the client monitor can see on this Mac, and save a raw log sample."""
    print("== Live bandwidth (nettop) ==")
    try:
        pids = subprocess.run([PGREP, "-x", "AssetCache"], capture_output=True, text=True).stdout.split()
        print("AssetCache PIDs:", pids or "none - is content caching on?")
        if pids:
            r = subprocess.run([NETTOP, "-L", "1", "-x", "-n", "-m", "tcp", "-J", "bytes_in,bytes_out", "-p", pids[0]],
                               capture_output=True, text=True, timeout=20)
            print("\n".join(r.stdout.splitlines()[:12]) or r.stderr)
            print(f"Parsed {len(parse_nettop(r.stdout))} connections")
    except Exception as e:
        print("nettop error:", e)
    print(f"\n== AssetCache log for {seconds}s (download something on a client now) ==")
    parser = LogParser(own_ips=ClientMonitor._own_ips())
    n = 0
    try:
        p = subprocess.Popen([LOG, "stream", "--style", "ndjson", "--level", "info", "--predicate", PREDICATE],
                             stdout=subprocess.PIPE, text=True)
        t0 = time.time()
        with open(out_path, "w") as f:
            while time.time() - t0 < seconds:
                line = p.stdout.readline()
                if not line:
                    break
                f.write(line)
                if line.startswith("{"):
                    try:
                        msg = json.loads(line).get("eventMessage", "")
                    except ValueError:
                        continue
                    n += 1
                    if n <= 15:
                        print("  ", msg[:220])
                    for r in parser.feed(msg):
                        if r.get("update"):
                            print("  -> COMPLETED", {k: r.get(k) for k in ("ip", "bytes", "from_cache", "from_origin")})
                            continue
                        print("  -> RECORD", {k: r.get(k) for k in ("ip", "model", "os", "kind", "item", "bytes", "from_cache")})
        p.terminate()
    except Exception as e:
        print("log error:", e)
    print(f"\n{parser.stats}  - raw sample saved to {os.path.abspath(out_path)}")
    if parser.stats["redacted"]:
        print("Some values were <private>. Install a logging profile that enables private data for com.apple.AssetCache (see README).")
