#!/usr/bin/env python3
"""
CacheDash - a customizable dashboard for Apple Content Caching (macOS 27).

Reads the per-cache usage database Apple documents at
  /Library/Application Support/Apple/AssetCache/Metrics/Metrics.db
(one row per active minute, 30-day retention) and serves:

  /               the dashboard (dashboard.html next to this file)
  /api/info       server name, database coverage, columns found
  /api/metrics    time-bucketed sums of every metric column
                  ?start=<unix>&end=<unix>&bucket=<seconds|auto>   (or ?hours=24)
  /api/heatmap    bytes served by weekday x hour, last 30 days
  /api/status     `AssetCacheManagerUtil -j status` passthrough (optional)
  /api/alerts     incidents (cache full, traffic collapse, service down, …)  ?start=&end=
                  /api/alerts/report?id=N explains one incident (why traffic dropped)
                  POST /api/alerts/test sends a test notification
                  POST /api/alerts/ack {id}, /mute {rule, days|null}, /unmute {rule}
  /api/logs       AssetCache log lines + grouped errors   ?start=&end=&errors=1&q=
  /api/logs/trace every line for one request ID       ?id=EPjRH9oiohMl
  /api/logs/download  the lines as a .log text file   ?start=&end=&errors=0
  /api/logs/probe who answers an Apple address from this Mac (Apple or a filter)  ?url=
  /api/system     CPU, memory, swap, processes, crashes and restarts of this Mac  ?start=&end=
  /api/catalogs   status of Apple's update catalogs used to name downloads
  /api/device     one device: downloads grouped by app/update + usage timeline  ?ip=&start=&end=
  /api/clients    live per-device bandwidth + what each device downloaded
                  ?start=<unix>&end=<unix>&ip=<device>      (see clientmon.py)
  /api/apps       App Store apps with updates waiting, from your MDM inventory (appupdates.py)
                  /api/apps/devices?key=  devices with one app and their versions
                  POST /api/apps/import {csv, filename}  upload an MDM export;  POST /api/apps/sync
  /api/rollouts   App Store downloads grouped by app version ("rolling out now")  ?start=&end=
  /api/report     totals for whole local days, for the Report view   ?from=YYYY-MM-DD&to=YYYY-MM-DD
                  (uses history.db, which keeps one summary per day beyond Metrics.db's 30 days)

Python 3 standard library only. Run with sudo (Metrics.db is root-owned):
  sudo /usr/bin/python3 cachedash.py --port 8027

Install the same script on a second caching Mac later and add it in the
dashboard's Servers panel; CORS is enabled so one dashboard can read both.
"""

import argparse
import ipaddress
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alerts  # noqa: E402
import logs  # noqa: E402
import catalog  # noqa: E402
import clientmon  # noqa: E402
import history  # noqa: E402
import appupdates  # noqa: E402
import cluster  # noqa: E402
import selfupdate  # noqa: E402
import cachestore  # noqa: E402

VERSION = "1.20.0"
DEFAULT_DB = "/Library/Application Support/Apple/AssetCache/Metrics/Metrics.db"
CORE_DATA_EPOCH = 978307200  # 2001-01-01T00:00:00Z in Unix seconds
HERE = os.path.dirname(os.path.abspath(__file__))

# Column names as documented by Apple ("Content caching metrics on Mac").
_EXPAND = {
    "bytesFromCacheTo": ["Child", "Client", "Peer"],
    "bytesFromOriginTo": ["Child", "Client", "Peer"],
    "bytesFromParentTo": ["Child", "Client", "Peer"],
    "bytesFromPeerTo": ["Child", "Client"],
    "repliesFromCacheTo": ["Child", "Client", "Peer"],
    "repliesFromOriginTo": ["Child", "Client", "Peer"],
    "repliesFromParentTo": ["Child", "Client", "Peer"],
    "repliesFromPeerTo": ["Child", "Client"],
}
KNOWN_METRICS = [p + s for p, ss in _EXPAND.items() for s in ss] + [
    "bytesDropped",
    "bytesImportedByHTTP", "bytesImportedByXPC",
    "bytesPurgedTotal", "bytesPurgedYoungerThan1Day",
    "bytesPurgedYoungerThan7Days", "bytesPurgedYoungerThan30Days",
    "requestsFromChild", "requestsFromClient", "requestsFromPeer",
    "requestsRejectedForNoSpace",
    "importsByHTTP", "importsByXPC",
]
CORE_DATA_INTERNAL = {"Z_PK", "Z_ENT", "Z_OPT"}


class MetricsStore:
    """Snapshot-reads Metrics.db without disturbing the AssetCache process."""

    def __init__(self, db_path, cache_seconds=30):
        self.db_path = db_path
        self.cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._loaded_at = 0
        self._rows = []        # list of dicts: {"t": unix, "period": s, metric: value}
        self._columns = []     # metric names found
        self._table = None
        self._error = None

    # -- loading ---------------------------------------------------------
    def _snapshot(self):
        """Copy db + WAL/SHM to a temp dir so we never lock the live file."""
        tmp = tempfile.mkdtemp(prefix="cachedash-")
        dst = os.path.join(tmp, "Metrics.db")
        shutil.copy2(self.db_path, dst)
        for suffix in ("-wal", "-shm"):
            src = self.db_path + suffix
            if os.path.exists(src):
                shutil.copy2(src, dst + suffix)
        return tmp, dst

    @staticmethod
    def _to_unix(v):
        if v is None:
            return None
        v = float(v)
        # Core Data stores seconds since 2001; tolerate Unix seconds too.
        return v + CORE_DATA_EPOCH if v < 1.2e9 else v

    def _load(self):
        if not os.path.exists(self.db_path):
            raise FileNotFoundError(
                f"Metrics database not found at {self.db_path}. Is content "
                "caching on, and is this macOS 27 or later?")
        tmp, path = self._snapshot()
        try:
            con = sqlite3.connect(path)
            con.row_factory = sqlite3.Row
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            table = next((t for t in tables if t.upper() == "ZMETRIC"), None) \
                or next((t for t in tables if "METRIC" in t.upper()), None)
            if not table:
                raise RuntimeError(f"No Metric table in database (tables: {tables})")
            cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]

            # Map SQLite column -> documented camelCase name.
            known = {"Z" + m.upper(): m for m in KNOWN_METRICS}
            known["ZCREATIONDATE"] = "creationDate"
            known["ZPERIOD"] = "period"
            colmap = {}
            for c in cols:
                if c in CORE_DATA_INTERNAL:
                    continue
                if c.upper() in known:
                    colmap[c] = known[c.upper()]
                elif c.upper() in {m.upper() for m in KNOWN_METRICS}:
                    colmap[c] = next(m for m in KNOWN_METRICS if m.upper() == c.upper())
                elif c.lower() in ("creationdate", "period"):
                    colmap[c] = "creationDate" if c.lower() == "creationdate" else "period"
                else:
                    # Future columns Apple may add: keep them, lower-camel-ish.
                    name = c[1:] if c.startswith("Z") else c
                    colmap[c] = name[:1].lower() + name[1:].lower()
            date_col = next((c for c, n in colmap.items() if n == "creationDate"), None)
            if not date_col:
                raise RuntimeError(f"No creationDate column in {table}: {cols}")

            rows = []
            for r in con.execute(f'SELECT * FROM "{table}" ORDER BY "{date_col}"'):
                d = {}
                for c, n in colmap.items():
                    v = r[c]
                    if n == "creationDate":
                        d["t"] = self._to_unix(v)
                    elif isinstance(v, (int, float)):
                        d[n] = v
                if d.get("t") is not None:
                    rows.append(d)
            con.close()
            metrics = sorted({n for n in colmap.values()
                              if n not in ("creationDate",)})
            return table, metrics, rows
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def refresh(self, force=False):
        with self._lock:
            if not force and time.time() - self._loaded_at < self.cache_seconds:
                return
            try:
                self._table, self._columns, self._rows = self._load()
                self._error = None
            except Exception as e:  # keep serving last good snapshot
                self._error = str(e)
            self._loaded_at = time.time()

    # -- queries ---------------------------------------------------------
    def info(self):
        self.refresh()
        rows = self._rows
        return {
            "rowCount": len(rows),
            "oldest": rows[0]["t"] if rows else None,
            "newest": rows[-1]["t"] if rows else None,
            "table": self._table,
            "columns": self._columns,
            "dbPath": self.db_path,
            "error": self._error,
        }

    ALLOWED_BUCKETS = (60, 300, 600, 900, 1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600, 86400)
    MAX_BUCKETS = 2000

    @classmethod
    def bucket_for(cls, span):
        """Pick a sensible interval for a time span (seconds)."""
        if span <= 3 * 3600:
            return 60
        if span <= 12 * 3600:
            return 300
        if span <= 2 * 86400:
            return 900
        if span <= 8 * 86400:
            return 3600
        return 6 * 3600

    @staticmethod
    def _align(t, bucket):
        """Floor t to a bucket boundary in the server's local time,
        so hourly/daily intervals start on local hours and midnights."""
        off = time.localtime(t).tm_gmtoff
        return int((t + off) // bucket * bucket - off)

    def metrics(self, start=None, end=None, bucket=None, hours=None):
        self.refresh()
        now = time.time()
        if end is None:
            end = now
        end = min(float(end), now)
        if start is None:
            start = end - (hours or 24) * 3600
        start = max(float(start), now - 31 * 86400)
        if start >= end:
            start = end - 60
        span = end - start

        if not bucket:
            bucket = self.bucket_for(span)
        bucket = min(self.ALLOWED_BUCKETS, key=lambda b: abs(b - int(bucket)))
        while span / bucket > self.MAX_BUCKETS:  # protect the browser
            bigger = [b for b in self.ALLOWED_BUCKETS if b > bucket]
            if not bigger:
                break
            bucket = bigger[0]

        first = self._align(start, bucket)
        last = self._align(end, bucket)
        # Whole intervals: a row belongs to the interval its timestamp falls in.
        rows = [r for r in self._rows if first <= r["t"] < last + bucket]
        names = [c for c in self._columns if c != "period"]
        zero = lambda: {**{n: 0 for n in names}, "period": 0, "rows": 0}

        buckets, totals = {}, zero()
        for r in rows:
            b = self._align(r["t"], bucket)
            agg = buckets.setdefault(b, zero())
            for n in names:
                v = r.get(n) or 0
                agg[n] += v
                totals[n] += v
            agg["period"] += r.get("period") or 0
            agg["rows"] += 1
            totals["period"] += r.get("period") or 0
            totals["rows"] += 1

        # Emit every bucket in range (zeros for idle time) so charts are honest.
        series, t = [], first
        while t <= last:
            series.append({"t": t, **buckets.get(t, zero())})
            t = self._align(t + bucket + 7200, bucket) if bucket >= 86400 else t + bucket  # DST-safe days

        return {
            "hours": span / 3600,
            "bucketSeconds": bucket,
            "start": start,
            "end": end,
            "live": now - end < 120,
            "metrics": names,
            "totals": totals,
            "buckets": series,
            "pressure": self.pressure(end),
            "error": self._error,
        }

    def pressure(self, at=None):
        """Estimate Apple's documented 0-100 cache-pressure step for the 24 h ending at `at`.

        Apple: first matching of rejected-for-no-space (100), bytes purged that
        were younger than 1 day (80), 7 days (60), 30 days (40), any age (20), else 0.
        """
        at = at or time.time()
        recent = [r for r in self._rows if at - 86400 <= r["t"] <= at + 60]
        def s(k):
            return sum(r.get(k) or 0 for r in recent)
        checks = [
            (100, "requestsRejectedForNoSpace", "Requests were rejected for lack of space"),
            (80, "bytesPurgedYoungerThan1Day", "Content less than 1 day old was purged"),
            (60, "bytesPurgedYoungerThan7Days", "Content less than 7 days old was purged"),
            (40, "bytesPurgedYoungerThan30Days", "Content less than 30 days old was purged"),
            (20, "bytesPurgedTotal", "Older content was purged"),
        ]
        for level, key, why in checks:
            if s(key) > 0:
                return {"level": level, "reason": why, "windowHours": 24, "at": at}
        return {"level": 0, "reason": "Nothing purged or rejected", "windowHours": 24, "at": at}

    def heatmap(self, start=None, end=None):
        """Bytes served by local weekday x hour, by hour of day, and by date, for a window
        (default: last 30 days). dayCounts says how many of each weekday the window covers,
        so the dashboard can show averages per day."""
        self.refresh()
        now = time.time()
        end = min(end or now, now)
        start = max(start or end - 30 * 86400, now - 31 * 86400)
        grid = [[0] * 24 for _ in range(7)]
        hourly = [0] * 24
        days = {}
        served_keys = [m for m in self._columns if m.startswith("bytesFrom")]
        for r in self._rows:
            if not (start <= r["t"] <= end):
                continue
            lt = datetime.fromtimestamp(r["t"])
            v = sum(r.get(k) or 0 for k in served_keys)
            grid[lt.weekday()][lt.hour] += v
            hourly[lt.hour] += v
            key = lt.strftime("%Y-%m-%d")
            days[key] = days.get(key, 0) + v
        day_counts = [0] * 7
        d = datetime.fromtimestamp(start).date()
        last = datetime.fromtimestamp(end).date()
        dates = []
        while d <= last:
            day_counts[d.weekday()] += 1
            dates.append(d)
            d = d.fromordinal(d.toordinal() + 1)
        return {"weekdayStartsMonday": True, "start": start, "end": end, "grid": grid, "hourly": hourly,
                "dayCounts": day_counts,
                "days": [{"date": x.isoformat(), "weekday": x.weekday(), "bytes": days.get(x.isoformat(), 0)} for x in dates]}


def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:4])


class UpdateCheck:
    """Is a newer CacheDash on GitHub? Reads source.json (written by install.sh from a git clone)
    and, every 2 hours (or when someone clicks Check now), the VERSION line of cachedash.py on that branch. Public repositories only;
    nothing is downloaded or installed, the dashboard just says an update is available."""

    def __init__(self, path, enabled=True):
        self.state = {"current": VERSION, "latest": None, "available": False, "checked": None, "error": None}
        try:
            with open(path) as f:
                src = json.load(f)
        except (OSError, ValueError):
            src = {}
        self.source = src
        m = re.search(r"github\.com[:/]+([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", src.get("repo") or "")
        self.raw = None
        if m:
            owner, repo, branch = m.group(1), m.group(2), src.get("branch") or "main"
            self.raw = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/cachedash.py"
            self.state.update({"repo": f"https://github.com/{owner}/{repo}", "branch": branch,
                               "commit": src.get("commit"), "path": src.get("path")})
        if enabled and self.raw:
            threading.Thread(target=self._loop, daemon=True).start()

    def check(self):
        try:
            req = urllib.request.Request(f"{self.raw}?t={int(time.time())}", headers={"User-Agent": f"CacheDash/{VERSION} update check"})
            with urllib.request.urlopen(req, timeout=20) as r:
                text = r.read(200_000).decode("utf-8", "replace")
            m = re.search(r'^VERSION = "([^"]+)"', text, re.M)
            if not m:
                raise ValueError("no version found")
            latest = m.group(1)
            self.state.update({"latest": latest, "available": vtuple(latest) > vtuple(VERSION), "error": None})
        except Exception as e:
            self.state["error"] = f"couldn't check for updates: {e}"
        self.state["checked"] = time.time()

    def _loop(self):
        time.sleep(60)
        while True:
            self.check()
            time.sleep(2 * 3600)


class StatusReader:
    """Optional: live cache status from the built-in AssetCacheManagerUtil."""

    def __init__(self, ttl=60):
        self.ttl = ttl
        self._at = 0
        self._val = None
        self._lock = threading.Lock()

    def get(self):
        with self._lock:
            if time.time() - self._at < self.ttl and self._val:
                return self._val
            try:
                out = subprocess.run(
                    ["/usr/bin/AssetCacheManagerUtil", "-j", "status"],
                    capture_output=True, text=True, timeout=20)
                data = json.loads(out.stdout or "{}")
                self._val = {"ok": True, "result": data.get("result", data)}
            except Exception as e:
                self._val = {"ok": False, "error": str(e)}
            self._at = time.time()
            return self._val


# --- Who may talk to this server -------------------------------------------------------
# By default only computers on private networks (10.x, 172.16-31.x, 192.168.x, loopback,
# IPv6 ULA/link-local) may connect, and browsers only let pages served from such
# addresses read the API. A random website a teacher has open can't read device data or
# mute alerts. Widen with --allow and --allow-origin.
PRIVATE_NETS = ["127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
                "100.64.0.0/10", "::1/128", "fc00::/7", "fe80::/10"]


def _ip(addr):
    try:
        a = ipaddress.ip_address(addr.split("%")[0])
        return a.ipv4_mapped or a if a.version == 6 else a
    except ValueError:
        return None


class Access:
    def __init__(self, allow=None, origins=()):
        self.nets = [ipaddress.ip_network(n, strict=False) for n in (allow or PRIVATE_NETS)]
        self.origins = {o.rstrip("/").lower() for o in origins}
        self._resolved = {}

    def client_ok(self, addr):
        a = _ip(addr)
        # The caching Mac itself is always allowed (the update check after installing, the desktop widget).
        return a is not None and (a.is_loopback or any(a in n for n in self.nets))

    def _host_private(self, host):
        a = _ip(host.strip("[]"))
        if a is not None:
            return any(a in n for n in self.nets)
        if host == "localhost" or host.endswith(".local"):
            return True
        hit = self._resolved.get(host)
        if hit and time.time() - hit[0] < 300:
            return hit[1]
        try:
            ok = all(any(_ip(i[4][0]) in n for n in self.nets) for i in socket.getaddrinfo(host, None))
        except OSError:
            ok = False
        self._resolved[host] = (time.time(), ok)
        return ok

    def origin_ok(self, origin):
        """May a page served from `origin` (another caching Mac's dashboard) use this API?"""
        if not origin:
            return True                       # not a cross-site browser request
        o = origin.rstrip("/").lower()
        if o in self.origins:
            return True
        p = urlparse(o)
        return p.scheme in ("http", "https") and bool(p.hostname) and self._host_private(p.hostname)


class Handler(BaseHTTPRequestHandler):
    store: MetricsStore = None
    status: StatusReader = None
    clients = None
    cluster = None    # cluster.Cluster: other caching Macs to show on this dashboard
    logs = None
    resolver = None
    alerts = None
    history = None
    apps = None
    updates = None
    updater = None
    store_files = None    # cachestore.CacheStore: where downloads are kept on disk
    access = Access()
    name = socket.gethostname()

    def log_message(self, fmt, *args):  # quieter logs
        if os.environ.get("CACHEDASH_VERBOSE"):
            super().log_message(fmt, *args)

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        origin = self.headers.get("Origin")
        if origin and self.access.origin_ok(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _gate(self, write=False):
        """Refuse callers outside the allowed networks, and cross-site writes from unknown pages."""
        if not self.access.client_ok(self.client_address[0]):
            self._send(403, {"error": "This computer's address isn't allowed (see --allow)."})
            return False
        if not self.access.origin_ok(self.headers.get("Origin")):
            self._send(403, {"error": "Requests from that web page aren't allowed (see --allow-origin)."})
            return False
        if write and (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            self._send(415, {"error": "Send JSON (Content-Type: application/json)."})
            return False
        return True

    def do_OPTIONS(self):
        if not self.access.client_ok(self.client_address[0]):
            return self._send(403, {"error": "not allowed"})
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_POST(self):
        if not self._gate(write=True):
            return
        u = urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 30 * 1024 * 1024:
                return self._send(413, {"error": "That upload is too big (over 30 MB)."})
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            if u.path == "/api/alerts/test" and self.alerts:
                return self._send(200, self.alerts.test())
            if u.path == "/api/alerts/ack" and self.alerts:
                return self._send(200, self.alerts.ack(body["id"], body.get("acked", True)))
            if u.path == "/api/alerts/mute" and self.alerts:
                return self._send(200, self.alerts.mute(body["rule"], body.get("days")))
            if u.path == "/api/alerts/unmute" and self.alerts:
                return self._send(200, self.alerts.unmute(body["rule"]))
            if u.path == "/api/update/check" and self.updates:
                if self.updates.raw:
                    self.updates.check()
                return self._send(200, {**self.updates.state, "status": self.updater.status() if self.updater else None})
            if u.path == "/api/update/run" and self.updater:
                code, res = self.updater.request(str(body.get("pin") or ""), self.client_address[0])
                return self._send(code, res)
            if u.path == "/api/cluster/join" and self.cluster:
                if self.headers.get("Origin"):        # Mac-to-Mac only, never from a web page
                    return self._send(403, {"error": "not from a browser"})
                return self._send(200, self.cluster.join(self.client_address[0], body.get("port", 8027)))
            if u.path == "/api/apps/import" and self.apps:
                try:
                    return self._send(200, self.apps.import_csv(str(body.get("csv") or ""), str(body.get("filename") or "")))
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
            if u.path == "/api/apps/sync" and self.apps:
                return self._send(200, self.apps.request_sync())
            self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def do_GET(self):
        if not self._gate():
            return
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html", "/dashboard.html"):
                with open(os.path.join(HERE, "dashboard.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if u.path == "/api/info":
                return self._send(200, {"name": self.name, "version": VERSION,
                                        "hostname": socket.gethostname(), "cluster": self.cluster.list() if self.cluster else [],
                                        "update": ({**self.updates.state, "canUpdate": self.updater.status()["ready"] if self.updater else False}
                                                   if self.updates else None),
                                        **self.store.info()})
            if u.path == "/api/metrics":
                num = lambda k: float(q[k][0]) if q.get(k) and q[k][0] not in ("", "auto") else None
                hours = num("hours")
                return self._send(200, self.store.metrics(
                    start=num("start"), end=num("end"), bucket=num("bucket"),
                    hours=max(0.25, min(hours, 24 * 31)) if hours else None))
            if u.path == "/api/heatmap":
                num = lambda k: float(q[k][0]) if q.get(k) and q[k][0] else None
                return self._send(200, self.store.heatmap(num("start"), num("end")))
            if u.path == "/api/status":
                return self._send(200, self.status.get())
            if u.path.startswith("/api/logs"):
                num = lambda k: float(q[k][0]) if q.get(k) and q[k][0] else None
                arg = lambda k, d=None: (q.get(k) or [d])[0]
                if u.path == "/api/logs/probe":
                    return self._send(200, logs.probe(arg("url", "")))
                if u.path == "/api/logs/trace":
                    return self._send(200, self.logs.trace(arg("id", ""), float(arg("hours", "6"))))
                if u.path == "/api/logs/download":
                    errors_only = arg("errors", "0") == "1"
                    text = self.logs.download(num("start"), num("end"), errors_only).encode()
                    fname = f"assetcache-{self.name}-{time.strftime('%Y%m%d-%H%M')}{'-errors' if errors_only else ''}.log".replace(" ", "_")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
                    self.send_header("Content-Length", str(len(text)))
                    self._cors()
                    self.end_headers()
                    return self.wfile.write(text)
                return self._send(200, self.logs.query(num("start"), num("end"), arg("errors", "1") == "1",
                                                       arg("q"), int(arg("limit", "2000"))))
            if u.path == "/api/system":
                if not self.alerts:
                    return self._send(200, {"disabled": True})
                num = lambda k: float(q[k][0]) if q.get(k) and q[k][0] else None
                return self._send(200, self.alerts.system(num("start"), num("end")))
            if u.path == "/api/alerts/report":
                if not self.alerts:
                    return self._send(200, {"disabled": True})
                return self._send(200, self.alerts.report(int((q.get("id") or ["0"])[0])))
            if u.path == "/api/alerts":
                if not self.alerts:
                    return self._send(200, {"disabled": True, "events": [], "active": []})
                num = lambda k: float(q[k][0]) if q.get(k) and q[k][0] else None
                return self._send(200, self.alerts.snapshot(num("start"), num("end")))
            if u.path == "/api/catalogs":
                return self._send(200, self.resolver.summary() if self.resolver else {"enabled": False})
            if u.path == "/api/device":
                if not self.clients:
                    return self._send(200, {"disabled": True})
                num = lambda k: float(q[k][0]) if q.get(k) and q[k][0] else None
                ip = (q.get("ip") or [""])[0]
                if not ip:
                    return self._send(400, {"error": "ip is required"})
                return self._send(200, self.clients.device(ip, num("start"), num("end")))
            if u.path == "/api/update":
                return self._send(200, {**(self.updates.state if self.updates else {}),
                                        "status": self.updater.status() if self.updater else None})
            if u.path == "/api/download":
                url = (q.get("url") or [""])[0][:2000]
                if not url:
                    return self._send(400, {"error": "url is required"})
                num = lambda k: float(q[k][0]) if q.get(k) and q[k][0] else None
                who = self.clients.downloads_of(url, num("start"), num("end")) if self.clients else {"disabled": True}
                what = dict((who or {}).get("sample") or {"url": url})
                if self.clients and not what.get("kind"):
                    what["kind"], what["item"] = clientmon.classify(url)
                    what = self.clients.label(what)
                parts = urlparse(apple_url := logs.full_url(url))
                res = self.resolver.lookup(url, fetch=False) if self.resolver else None
                return self._send(200, {"server": self.name, "url": url, "appleUrl": apple_url, "host": parts.hostname,
                                        "path": cachestore.url_path(url), "what": what, "catalog": res, "who": who,
                                        "where": self.store_files.find(url) if self.store_files else {"error": "turned off"}})
            if u.path == "/api/apps":
                return self._send(200, self.apps.report() if self.apps else {"disabled": True})
            if u.path == "/api/apps/devices":
                if not self.apps:
                    return self._send(200, {"disabled": True})
                return self._send(200, self.apps.devices((q.get("key") or [""])[0][:300]))
            if u.path == "/api/rollouts":
                if not self.clients:
                    return self._send(200, {"disabled": True})
                num = lambda k: float(q[k][0]) if q.get(k) and q[k][0] else None
                return self._send(200, self.clients.rollouts(num("start"), num("end")))
            if u.path == "/api/report":
                if not self.history:
                    return self._send(200, {"disabled": True})
                arg = lambda k: (q.get(k) or [""])[0]
                if not arg("from") or not arg("to"):
                    return self._send(400, {"error": "from and to (YYYY-MM-DD) are required"})
                try:
                    rep = self.history.report(arg("from"), arg("to"))
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                rep["server"] = self.name
                rep["status"] = self.status.get()
                return self._send(200, rep)
            if u.path == "/api/clients":
                if not self.clients:
                    return self._send(200, {"disabled": True})
                num = lambda k: float(q[k][0]) if q.get(k) and q[k][0] else None
                ip = (q.get("ip") or [None])[0]
                light = (q.get("light") or ["0"])[0] == "1"
                find = (q.get("find") or [""])[0][:100] or None
                return self._send(200, self.clients.snapshot(num("start"), num("end"), ip, light=light, find=find))
            self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e)})


def main():
    ap = argparse.ArgumentParser(description="Apple Content Cache dashboard server")
    ap.add_argument("--db", default=DEFAULT_DB, help="path to Metrics.db")
    ap.add_argument("--port", type=int, default=8027)
    ap.add_argument("--bind", default="0.0.0.0",
                    help="address to listen on (127.0.0.1 = this Mac only)")
    ap.add_argument("--name", default=socket.gethostname().split(".")[0],
                    help="display name for this cache in the dashboard")
    ap.add_argument("--check", action="store_true",
                    help="print a database summary and exit")
    ap.add_argument("--allow", action="append", metavar="CIDR",
                    help="networks allowed to connect (repeatable). Default: private and loopback addresses only. "
                         "Use 0.0.0.0/0 and ::/0 to allow everyone")
    ap.add_argument("--allow-origin", action="append", default=[], metavar="URL",
                    help="extra web page origin allowed to use the API from a browser, e.g. https://dash.example.org")
    ap.add_argument("--no-clients", action="store_true",
                    help="turn off per-device monitoring (nettop + AssetCache log)")
    ap.add_argument("--names", default=os.path.join(HERE, "clients.csv"),
                    help="optional CSV of ip,name for friendly device names")
    ap.add_argument("--clients-db", default=os.path.join(HERE, "clients.db"),
                    help="where to keep 30 days of per-device download records")
    ap.add_argument("--servers", default=os.path.join(HERE, "servers.json"),
                    help='JSON list of the other caching Macs: [{"name": "Cache 2", "url": "http://10.0.0.2:8027"}]')
    ap.add_argument("--no-alerts", action="store_true", help="turn off incident detection and notifications")
    ap.add_argument("--alerts-config", default=os.path.join(HERE, "alerts.json"),
                    help="thresholds, webhook and email settings (see alerts.example.json)")
    ap.add_argument("--alerts-db", default=os.path.join(HERE, "alerts.db"))
    ap.add_argument("--history-db", default=os.path.join(HERE, "history.db"),
                    help="daily summaries kept for reports beyond Metrics.db's 30 days")
    ap.add_argument("--no-history", action="store_true", help="don't keep daily history (reports cover ~30 days)")
    ap.add_argument("--no-catalogs", action="store_true",
                    help="don't download Apple's update catalogs to name downloads")
    ap.add_argument("--sucatalog", help="use this Software Update catalog URL instead of Apple's default")
    ap.add_argument("--catalog-cache", default=os.path.join(HERE, "catalog-cache.json"))
    ap.add_argument("--check-catalogs", nargs="*", metavar="URL",
                    help="download Apple's catalogs now, report, optionally resolve download URLs, and exit")
    ap.add_argument("--apps-db", default=os.path.join(HERE, "apps.db"), help="where to keep the MDM app inventory")
    ap.add_argument("--mosyle", default=os.path.join(HERE, "mosyle.json"),
                    help="Mosyle Manager API settings (see mosyle.example.json); leave the file out to use CSV uploads only")
    ap.add_argument("--app-store-country", default="us", help="App Store country for version lookups (two letters)")
    ap.add_argument("--no-cluster", action="store_true",
                    help="don't look for or announce this Mac to other caching Macs (servers.json still works)")
    ap.add_argument("--find-cached", nargs="?", const="-", metavar="URL_OR_FILE",
                    help="show where the cache stores files, and look up one download address or file name, then exit")
    ap.add_argument("--find-caches", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--probe", metavar="ADDRESS", help=argparse.SUPPRESS)
    ap.add_argument("--join", metavar="URL", help=argparse.SUPPRESS)
    ap.add_argument("--no-update-check", action="store_true",
                    help="don't check GitHub twice a day for a newer CacheDash")
    ap.add_argument("--no-apps", action="store_true", help="turn off the App updates feature")
    ap.add_argument("--check-mosyle", action="store_true", help="test the Mosyle API settings, show what it returns, and exit")
    ap.add_argument("--diagnose-clients", type=int, metavar="SECONDS",
                    help="show what per-device monitoring can see, save a log sample, and exit")
    a = ap.parse_args()

    if a.check_catalogs is not None:
        catalog.check(a.catalog_cache, a.check_catalogs, su_catalog=a.sucatalog)
        sys.exit(0)

    if a.find_cached:
        cachestore.cli(a.find_cached)
        sys.exit(0)
    if a.find_caches:            # installer helpers
        cluster.cli_find(a.port)
        sys.exit(0)
    if a.probe:
        cluster.cli_probe(a.probe, a.port)
        sys.exit(0)
    if a.join:
        cluster.cli_join(a.join, a.port)
        sys.exit(0)

    if a.check_mosyle:
        appupdates.check_mosyle(a.mosyle)
        sys.exit(0)

    if a.diagnose_clients:
        clientmon.diagnose(a.diagnose_clients)
        sys.exit(0)

    store = MetricsStore(a.db)
    if a.check:
        info = store.info()
        print(json.dumps(info, indent=2, default=str))
        sys.exit(1 if info["error"] else 0)

    Handler.store = store
    Handler.access = Access(a.allow, a.allow_origin)
    Handler.status = StatusReader()
    Handler.name = a.name
    Handler.cluster = cluster.Cluster(a.servers, a.port, a.name, status=Handler.status, enabled=not a.no_cluster)
    if not a.no_catalogs:
        Handler.resolver = catalog.CatalogResolver(a.catalog_cache, su_catalog=a.sucatalog)
    if not a.no_clients:
        Handler.clients = clientmon.ClientMonitor(a.clients_db, names_file=a.names, resolver=Handler.resolver)
    Handler.logs = logs.LogReader(lambda: list(Handler.clients.logbuf) if Handler.clients else [])
    if not a.no_alerts:
        Handler.alerts = alerts.AlertEngine(a.alerts_db, a.alerts_config, store=store, status=Handler.status,
                                            clients=Handler.clients, server_name=a.name)
        if Handler.clients:
            Handler.clients.on_log_line = Handler.alerts.note_log_line
    Handler.updates = UpdateCheck(os.path.join(HERE, "source.json"), enabled=not a.no_update_check)
    Handler.store_files = cachestore.CacheStore()
    Handler.updater = selfupdate.Updater(HERE, VERSION, Handler.updates.source)
    if not a.no_apps:
        Handler.apps = appupdates.AppUpdates(a.apps_db, a.mosyle, country=a.app_store_country.lower()[:2])
    if not a.no_history:
        Handler.history = history.History(a.history_db, store, clients=Handler.clients, alerts=Handler.alerts)
    srv = ThreadingHTTPServer((a.bind, a.port), Handler)
    print(f"CacheDash {VERSION}: http://{a.bind}:{a.port}/  (db: {a.db})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
