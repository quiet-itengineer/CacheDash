"""
Daily history for CacheDash reports.

Apple's Metrics.db and CacheDash's clients.db only keep about 30 days. To report on a
month, a semester or a school year, this module saves one summary per finished day in
history.db (kept indefinitely; a few KB to a few hundred KB per day depending on how
many devices you have):

  days        totals of every Metrics.db column, bytes served per hour, peak 15-minute rate
  day_devices bytes and downloads per device
  day_items   downloads, devices and bytes per app / OS update / content item

Days are local calendar days on the caching Mac. A day is saved about an hour after it
ends; on first start the last ~29 days are back-filled from the live databases. Reports
use saved days where they exist and compute the rest live (today, and anything not yet
saved), so a report covers whatever either source still has.

Python 3 standard library only.
"""

import json
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta

PEAK_BUCKET = 900          # peak throughput = busiest 15-minute average
LIVE_DAYS = 29             # whole days the live databases still cover
TOP_DEVICES = 25
TOP_ITEMS = 30


def day_bounds(d):
    """Local midnight to local midnight (DST-safe), as unix seconds."""
    nd = d + timedelta(days=1)
    return (time.mktime(datetime(d.year, d.month, d.day).timetuple()),
            time.mktime(datetime(nd.year, nd.month, nd.day).timetuple()))


def parse_day(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


class History:
    def __init__(self, path, store, clients=None, alerts=None, interval=3600):
        self.store, self.clients, self.alerts = store, clients, alerts
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS days (
                day TEXT PRIMARY KEY, t REAL, totals TEXT, hourly TEXT,
                peak_bps REAL, peak_t REAL, rows INTEGER, has_clients INTEGER, saved REAL);
            CREATE TABLE IF NOT EXISTS day_devices (
                day TEXT, ip TEXT, bytes INTEGER, downloads INTEGER, model TEXT, os TEXT,
                PRIMARY KEY (day, ip));
            CREATE TABLE IF NOT EXISTS day_items (
                day TEXT, kind TEXT, item TEXT, downloads INTEGER, bytes INTEGER, ips TEXT,
                PRIMARY KEY (day, kind, item));
        """)
        self.db.commit()
        self.error = None
        self.interval = interval
        threading.Thread(target=self._loop, daemon=True).start()

    # -- building one day -------------------------------------------------------
    def compute_day(self, d):
        """Summarise one local day from the live databases."""
        s, e = day_bounds(d)
        st = self.store
        st.refresh()
        cols = [c for c in st._columns]
        served_keys = [c for c in cols if c.startswith("bytesFrom")]
        totals = {c: 0 for c in cols}
        hourly = [0] * 24
        peak = {}
        n = 0
        for r in st._rows:
            t = r["t"]
            if not (s <= t < e):
                continue
            n += 1
            for c in cols:
                totals[c] += r.get(c) or 0
            v = sum(r.get(k) or 0 for k in served_keys)
            hourly[datetime.fromtimestamp(t).hour] += v
            b = int((t - s) // PEAK_BUCKET)
            peak[b] = peak.get(b, 0) + v
        if peak:
            pb, pv = max(peak.items(), key=lambda kv: kv[1])
            peak_bps, peak_t = pv * 8 / PEAK_BUCKET, s + pb * PEAK_BUCKET
        else:
            peak_bps, peak_t = 0, None

        devices, items, has_clients = {}, {}, False
        if self.clients is not None and getattr(self.clients, "enabled", False):
            retention = getattr(__import__("clientmon"), "RETENTION", 30 * 86400)
            has_clients = s >= time.time() - retention + 3600
            if has_clients:
                devices, items = self.clients.summary(s, e)
        return {"day": d.isoformat(), "t": s, "totals": totals, "hourly": hourly,
                "peak_bps": peak_bps, "peak_t": peak_t, "rows": n, "has_clients": has_clients,
                "devices": devices, "items": items}

    def save_day(self, x):
        with self.lock:
            db = self.db
            db.execute("DELETE FROM day_devices WHERE day = ?", (x["day"],))
            db.execute("DELETE FROM day_items WHERE day = ?", (x["day"],))
            db.execute("""INSERT OR REPLACE INTO days (day, t, totals, hourly, peak_bps, peak_t, rows, has_clients, saved)
                          VALUES (?,?,?,?,?,?,?,?,?)""",
                       (x["day"], x["t"], json.dumps(x["totals"]), json.dumps(x["hourly"]), x["peak_bps"],
                        x["peak_t"], x["rows"], 1 if x["has_clients"] else 0, time.time()))
            db.executemany("INSERT INTO day_devices (day, ip, bytes, downloads, model, os) VALUES (?,?,?,?,?,?)",
                           [(x["day"], ip, int(v["bytes"]), v["downloads"], v.get("model"), v.get("os"))
                            for ip, v in x["devices"].items()])
            db.executemany("INSERT INTO day_items (day, kind, item, downloads, bytes, ips) VALUES (?,?,?,?,?,?)",
                           [(x["day"], k, i, v["downloads"], int(v["bytes"]), json.dumps(sorted(v["ips"])))
                            for (k, i), v in x["items"].items()])
            db.commit()

    def load_day(self, day):
        with self.lock:
            r = self.db.execute("SELECT day, t, totals, hourly, peak_bps, peak_t, rows, has_clients FROM days WHERE day = ?",
                                (day,)).fetchone()
            if not r:
                return None
            devs = self.db.execute("SELECT ip, bytes, downloads, model, os FROM day_devices WHERE day = ?", (day,)).fetchall()
            its = self.db.execute("SELECT kind, item, downloads, bytes, ips FROM day_items WHERE day = ?", (day,)).fetchall()
        return {"day": r[0], "t": r[1], "totals": json.loads(r[2]), "hourly": json.loads(r[3]),
                "peak_bps": r[4], "peak_t": r[5], "rows": r[6], "has_clients": bool(r[7]),
                "devices": {d[0]: {"bytes": d[1], "downloads": d[2], "model": d[3], "os": d[4]} for d in devs},
                "items": {(i[0], i[1]): {"downloads": i[2], "bytes": i[3], "ips": set(json.loads(i[4] or "[]"))} for i in its}}

    def saved_days(self):
        with self.lock:
            return {r[0] for r in self.db.execute("SELECT day FROM days")}

    def _loop(self):
        time.sleep(20)   # let the live databases load first
        while True:
            try:
                self.backfill()
                self.error = None
            except Exception as e:  # never take the server down over history
                self.error = str(e)
            time.sleep(self.interval)

    def backfill(self):
        """Save every finished day the live databases still cover that isn't saved yet."""
        now = time.time()
        have = self.saved_days()
        oldest = self.store.info().get("oldest") or now
        today = date.today()
        for i in range(LIVE_DAYS, 0, -1):
            d = today - timedelta(days=i)
            s, e = day_bounds(d)
            if d.isoformat() in have or e > now - 3600 or e <= oldest:
                continue
            self.save_day(self.compute_day(d))

    # -- reports --------------------------------------------------------------------
    def report(self, first, last):
        """Everything the dashboard's Report view needs for local days first..last (inclusive)."""
        first, last = parse_day(first), parse_day(last)
        if last < first:
            first, last = last, first
        if (last - first).days > 800:
            raise ValueError("Reports are limited to about two years")
        today = date.today()
        last = min(last, today)
        now = time.time()
        oldest_live = self.store.info().get("oldest") or now
        have = self.saved_days()

        series, totals, hourly = [], {}, [0] * 24
        devices, items = {}, {}
        missing, devices_from, peak = [], None, (0, None)
        d = first
        while d <= last:
            key = d.isoformat()
            s, e = day_bounds(d)
            x = self.load_day(key) if key in have else None
            src = "saved"
            if x is None:
                if e > oldest_live and s >= now - (LIVE_DAYS + 1) * 86400:
                    x, src = self.compute_day(d), ("today" if d == today else "live")
                else:
                    missing.append(key)
                    series.append({"day": key, "t": s, "source": "missing"})
                    d += timedelta(days=1)
                    continue
            for k, v in x["totals"].items():
                totals[k] = totals.get(k, 0) + (v or 0)
            for h in range(24):
                hourly[h] += x["hourly"][h] or 0
            if (x["peak_bps"] or 0) > peak[0]:
                peak = (x["peak_bps"], x["peak_t"])
            if x["has_clients"] and devices_from is None:
                devices_from = key
            for ip, v in x["devices"].items():
                a = devices.setdefault(ip, {"ip": ip, "bytes": 0, "downloads": 0, "days": 0})
                a["bytes"] += v["bytes"] or 0
                a["downloads"] += v["downloads"] or 0
                a["days"] += 1
                a["model"] = v.get("model") or a.get("model")
                a["os"] = v.get("os") or a.get("os")
            for (kind, item), v in x["items"].items():
                a = items.setdefault((kind, item), {"kind": kind, "item": item, "downloads": 0, "bytes": 0, "ips": set()})
                a["downloads"] += v["downloads"]
                a["bytes"] += v["bytes"]
                a["ips"] |= set(v["ips"])
            t = x["totals"]
            served = sum(v or 0 for k, v in t.items() if k.startswith("bytesFrom"))
            from_cache = sum(v or 0 for k, v in t.items() if k.startswith("bytesFromCache"))
            series.append({"day": key, "t": s, "source": src, "served": served, "fromCache": from_cache,
                           "upstream": served - from_cache,
                           "requests": sum(t.get(k) or 0 for k in ("requestsFromClient", "requestsFromPeer", "requestsFromChild")),
                           "devices": len(x["devices"]) if x["has_clients"] else None,
                           "downloads": sum(v["downloads"] for v in x["devices"].values()) if x["has_clients"] else None,
                           "peakBps": x["peak_bps"], "peakT": x["peak_t"]})
            d += timedelta(days=1)

        name_for = self.clients.name_for if self.clients is not None else (lambda ip: None)
        top = sorted(devices.values(), key=lambda v: v["bytes"], reverse=True)[:TOP_DEVICES]
        for v in top:
            v["name"] = name_for(v["ip"])
        kinds = {}
        for v in items.values():
            k = kinds.setdefault(v["kind"], {"bytes": 0, "downloads": 0})
            k["bytes"] += v["bytes"]
            k["downloads"] += v["downloads"]
        top_items = sorted(items.values(), key=lambda v: (v["downloads"], v["bytes"]), reverse=True)[:TOP_ITEMS]
        top_items = [{**{k: v[k] for k in ("kind", "item", "downloads", "bytes")}, "devices": len(v["ips"])} for v in top_items]

        s0, e1 = day_bounds(first)[0], day_bounds(last)[1]
        events = []
        if self.alerts is not None:
            try:
                events = self.alerts.snapshot(s0, min(e1, now)).get("events", [])
                for ev in events:
                    ev.pop("details", None)
            except Exception:
                events = []
        with self.lock:
            r = self.db.execute("SELECT MIN(day), COUNT(*) FROM days").fetchone()
        return {
            "from": first.isoformat(), "to": last.isoformat(), "start": s0, "end": min(e1, now),
            "generated": now, "days": series, "totals": totals, "hourly": hourly,
            "peakBps": peak[0], "peakT": peak[1],
            "devices": {"count": len(devices), "downloads": sum(v["downloads"] for v in devices.values()),
                        "top": top, "ips": sorted(devices), "from": devices_from},
            "kinds": kinds, "items": top_items, "alerts": events,
            "coverage": {"missing": missing, "historyFrom": r[0], "historyDays": r[1],
                         "liveFrom": oldest_live if oldest_live < now else None,
                         "devicesTracked": self.clients is not None and getattr(self.clients, "enabled", False),
                         "error": self.error},
        }
