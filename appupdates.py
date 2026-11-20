"""
CacheDash app updates: which App Store apps have new versions waiting, and how many
of your devices still need them. Works with any MDM.

The cache can't tell which app a download is (App Store addresses are random strings),
but your MDM knows every app on every device, with its version. This module:

  1. gets that inventory from your MDM, either
       * from a CSV report uploaded in the dashboard (any MDM: any CSV with app or
         bundle ID, and version columns, plus optionally device; names detected automatically), or
       * by optional automatic sync (currently Mosyle Manager's API, mosyle.json next to this file);
  2. looks up each app's current version, size and release date in Apple's public
     App Store lookup service (itunes.apple.com/lookup);
  3. reports, per app: devices on the current version, devices that still need the
     update, and roughly how much data that is.

Each new inventory is compared with the previous one, so version changes show up as
"updated in the last 24 hours / 7 days".

Stored in apps.db. Credentials in mosyle.json are never sent to the browser.
Python 3 standard library only.
"""

import csv
import io
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

UA = {"User-Agent": "CacheDash/1.18 (+content-cache dashboard)"}
LOOKUP = "https://itunes.apple.com/lookup"
SEARCH = "https://itunes.apple.com/search"
MOSYLE = "https://managerapi.mosyle.com/v2"
STORE_TTL = 12 * 3600
CHANGES_KEPT = 60 * 86400
MAX_CSV = 25 * 1024 * 1024
_pause = time.sleep           # between App Store requests (Apple allows about 20 a minute)

# Possible names for the installed-apps field in a Mosyle device record. Mosyle doesn't
# publish this, so CacheDash looks for any list of {name/identifier/version} records,
# and `--check-mosyle` shows what your account actually returns.
APPS_FIELDS = ["installed_apps", "applications", "apps", "installedapps", "app_list", "InstalledApplicationList"]
K_BUNDLE = ("identifier", "bundle_id", "bundleid", "bundleidentifier", "bundle", "app_identifier", "appidentifier", "app_id")
K_NAME = ("name", "app_name", "appname", "title", "display_name", "displayname", "application", "app")
K_VERSION = ("short_version", "shortversion", "version", "app_version", "appversion", "bundle_version", "bundleversion")
K_DEVICE = ("serial_number", "serialnumber", "serial", "device_name", "devicename", "device", "udid", "deviceudid",
            "asset_tag", "assettag", "device_id", "deviceid")
K_DEVNAME = ("device_name", "devicename", "name_of_device")
K_OS = ("os", "platform", "device_type", "devicetype", "os_type")
K_COUNT = ("count", "devices", "device_count", "installs", "total", "quantity")


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def vkey(v):
    """Version string -> comparable tuple ("17.2.1" -> (17, 2, 1)); None if there are no numbers."""
    nums = [int(x) for x in re.findall(r"\d+", str(v or ""))[:6]]
    return tuple(nums) if nums else None


def older(installed, latest):
    a, b = vkey(installed), vkey(latest)
    if a is None or b is None:
        return None
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) < b + (0,) * (n - len(b))


def platform_of(s):
    s = (s or "").lower()
    if "mac" in s:
        return "macos"
    if "tv" in s:
        return "tvos"
    if "vision" in s:
        return "visionos"
    return "ios"


def _pick(d, keys):
    """First value in dict d whose normalised key is in keys."""
    nd = {_norm(k): v for k, v in d.items()}
    for k in keys:
        v = nd.get(_norm(k))
        if v not in (None, ""):
            return v
    return None


def app_records(device):
    """Find the installed-apps list in one Mosyle device record, whatever it's called."""
    for k in APPS_FIELDS:
        v = device.get(k)
        if isinstance(v, list):
            return k, v
    for k, v in device.items():
        if isinstance(v, list) and v and isinstance(v[0], dict) and _pick(v[0], K_VERSION) is not None \
                and (_pick(v[0], K_BUNDLE) or _pick(v[0], K_NAME)):
            return k, v
    return None, None


# ------------------------------------------------------------------ CSV import --

def parse_csv(text):
    """Rows of (device, device_name, bundle, name, version, platform, count) from an MDM export."""
    if len(text) > MAX_CSV:
        raise ValueError("That file is too big (over 25 MB).")
    text = text.lstrip("﻿")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    if len(rows) < 2:
        raise ValueError("The file has no data rows.")
    # The header is the first row that names a version column (some exports put a title line first).
    hi = next((i for i, r in enumerate(rows[:10]) if any(_norm(c) in K_VERSION for c in r)), None)
    if hi is None:
        raise ValueError("Couldn't find a Version column. The file needs app name or bundle ID, and version columns.")
    head = [_norm(c) for c in rows[hi]]

    def col(keys, exclude=()):
        for k in keys:
            for i, h in enumerate(head):
                if h == _norm(k) and i not in exclude:
                    return i
        return None
    c_ver = col(K_VERSION)
    c_bundle = col(K_BUNDLE)
    c_name = col(("app_name", "appname", "application", "app", "title", "name", "display_name"), exclude=(c_ver,))
    c_dev = col(K_DEVICE)
    c_devname = col(K_DEVNAME)
    if c_devname is not None and c_devname == c_name:
        c_name = col(("app_name", "appname", "application", "app", "title"), exclude=(c_ver, c_devname))
    c_os = col(K_OS)
    c_count = col(K_COUNT, exclude=(c_dev,) if c_dev is not None else ())
    if c_bundle is None and c_name is None:
        raise ValueError("Couldn't find an app name or bundle ID column.")
    out = []
    for r in rows[hi + 1:]:
        if not any(x.strip() for x in r):
            continue
        g = lambda i: (r[i].strip() if i is not None and i < len(r) else "")
        cnt = 1
        if c_count is not None and c_dev is None:
            try:
                cnt = max(0, int(float(g(c_count).replace(",", "") or 1)))
            except ValueError:
                cnt = 1
        bundle, name = g(c_bundle), g(c_name)
        if not (bundle or name):
            continue
        out.append({"device": g(c_dev) or None, "deviceName": g(c_devname) or None, "bundle": bundle or None,
                    "name": name or bundle, "version": g(c_ver), "platform": platform_of(g(c_os)), "n": cnt})
    if not out:
        raise ValueError("No app rows were found in the file.")
    return out, {"columns": [rows[hi][i] for i in (c_dev, c_bundle, c_name, c_ver) if i is not None],
                 "perDevice": c_dev is not None}


# --------------------------------------------------------------- Mosyle API --

class MosyleError(Exception):
    pass


class Mosyle:
    def __init__(self, cfg):
        self.cfg = cfg
        self.base = (cfg.get("baseUrl") or MOSYLE).rstrip("/")
        self.token, self.token_at = None, 0
        self.uses_login = bool(cfg.get("email") and cfg.get("password"))

    def _post(self, path, body, auth=True):
        body = {"accessToken": self.cfg["accessToken"], **body}
        h = {"Content-Type": "application/json", "accessToken": self.cfg["accessToken"], **UA}
        if auth and self.uses_login:     # with no admin login set, the access token alone is sent
            if not self.token or time.time() - self.token_at > 20 * 3600:
                self.login()
            h["Authorization"] = self.token
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), headers=h)
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return r, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            hint = (" With only an access token, Mosyle may also need an admin email and password in mosyle.json."
                    if e.code in (401, 403) and not self.uses_login else "")
            raise MosyleError(f"Mosyle answered HTTP {e.code} for {path}.{hint}") from None

    def login(self):
        req = urllib.request.Request(self.base + "/login", headers={"Content-Type": "application/json", **UA},
                                     data=json.dumps({"accessToken": self.cfg["accessToken"], "email": self.cfg["email"],
                                                      "password": self.cfg["password"]}).encode())
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                tok = r.headers.get("Authorization")
        except urllib.error.HTTPError as e:
            raise MosyleError(f"Mosyle login failed (HTTP {e.code}). Check the access token, email and password.") from None
        if not tok:
            raise MosyleError("Mosyle login didn't return a token. Check the access token, email and password.")
        self.token = tok if tok.lower().startswith("bearer") else "Bearer " + tok
        self.token_at = time.time()

    @staticmethod
    def _devices(resp):
        r = resp.get("response", resp)
        if isinstance(r, list):
            r = next((x for x in r if isinstance(x, dict) and "devices" in x), r[0] if r else {})
        return (r.get("devices") or []) if isinstance(r, dict) else [], (r.get("rows") if isinstance(r, dict) else None)

    def list_devices(self, os_name, columns=None, max_pages=500):
        seen, out, page = set(), [], 1
        while page <= max_pages:
            opts = {"os": os_name, "page": page}
            if columns:
                opts["specific_columns"] = columns
            _, data = self._post("/listdevices", {"options": opts})
            if str(data.get("status", "OK")).upper() not in ("OK", "DEVICES_NOTFOUND"):
                raise MosyleError(f"Mosyle: {data.get('status')} {data.get('message') or ''}".strip())
            devs, rows = self._devices(data)
            fresh = [d for d in devs if (d.get("serial_number") or d.get("deviceudid") or json.dumps(d, sort_keys=True)) not in seen]
            if not fresh:
                break
            for d in fresh:
                seen.add(d.get("serial_number") or d.get("deviceudid") or json.dumps(d, sort_keys=True))
            out += fresh
            if rows is not None and len(out) >= int(rows):
                break
            page += 1
        return out

    def inventory(self):
        """Every app on every device, for each platform in the config."""
        rows, info = [], {"devices": 0, "appsField": None, "platforms": {}}
        field = self.cfg.get("appsColumn")
        for osn in self.cfg.get("os") or ["ios", "macos"]:
            cols = None
            if field:
                cols = ["serial_number", "device_name", "deviceudid", field]
            devs = self.list_devices(osn, cols)
            if devs and not field:
                key, _ = next(((k, v) for k, v in map(app_records, devs) if k), (None, None))
                if not key:   # not in the default columns: ask for the likely ones explicitly
                    for cand in APPS_FIELDS[:5]:
                        devs2 = self.list_devices(osn, ["serial_number", "device_name", "deviceudid", cand], max_pages=1)
                        if devs2 and any(app_records(d)[0] for d in devs2):
                            field = cand
                            devs = self.list_devices(osn, ["serial_number", "device_name", "deviceudid", cand])
                            break
            n_apps = 0
            for d in devs:
                key, apps = app_records(d)
                if key:
                    info["appsField"] = key
                serial = d.get("serial_number") or d.get("deviceudid") or d.get("device_name")
                for a in apps or []:
                    if not isinstance(a, dict):
                        continue
                    bundle, name, ver = _pick(a, K_BUNDLE), _pick(a, K_NAME), _pick(a, K_VERSION)
                    if not (bundle or name):
                        continue
                    rows.append({"device": serial, "deviceName": d.get("device_name"), "bundle": bundle,
                                 "name": name or bundle, "version": str(ver or ""), "platform": platform_of(osn), "n": 1})
                    n_apps += 1
            info["devices"] += len(devs)
            info["platforms"][osn] = {"devices": len(devs), "appRows": n_apps}
        if info["devices"] and not rows:
            raise MosyleError("Mosyle returned devices but no installed-apps list. Run `cachedash.py --check-mosyle` to see the "
                              "fields your account returns, and set \"appsColumn\" in mosyle.json, or upload a CSV export instead.")
        return rows, info


def check_mosyle(path):
    """CLI: log in, fetch one page of devices, and show which fields hold the app list."""
    cfg = load_mosyle(path)
    if not cfg:
        print(f"No usable Mosyle settings in {path}. Copy mosyle.example.json to mosyle.json and fill it in.")
        return
    m = Mosyle(cfg)
    if m.uses_login:
        try:
            m.login()
            print("Login with the admin account: OK")
        except Exception as e:
            print("Login FAILED:", e)
            return
    else:
        print("No admin email/password in the settings: using the access token alone.")
    for osn in cfg.get("os") or ["ios", "macos"]:
        try:
            devs = m.list_devices(osn, None, max_pages=1)
        except Exception as e:
            print(f"\n[{osn}] listdevices FAILED: {e}")
            continue
        print(f"\n[{osn}] first page: {len(devs)} devices")
        if not devs:
            continue
        print("  Fields on a device:", ", ".join(sorted(devs[0].keys())))
        key, apps = next(((k, v) for k, v in map(app_records, devs) if k), (None, None))
        if key:
            print(f"  App list found in \"{key}\" ({len(apps)} apps on the first device that has it)")
            if apps:
                print("  Fields on an app:", ", ".join(sorted(apps[0].keys())), "\n  Example:", json.dumps(apps[0])[:300])
            continue
        print("  No app list in the default fields. Trying likely column names…")
        for cand in APPS_FIELDS[:5]:
            try:
                d2 = m.list_devices(osn, ["serial_number", cand], max_pages=1)
            except Exception as e:
                print(f"    {cand}: error {e}")
                continue
            k2, a2 = next(((k, v) for k, v in map(app_records, d2) if k), (None, None))
            print(f"    {cand}: {'FOUND (' + str(len(a2)) + ' apps)' if k2 else 'not returned'}")
            if k2:
                print(f"  -> add  \"appsColumn\": \"{cand}\"  to mosyle.json")
                break
        else:
            print("  Mosyle didn't return installed apps through the API for this account. Use a CSV export instead.")


def load_mosyle(path):
    try:
        with open(path) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return None
    if not cfg.get("accessToken"):      # an admin email and password are optional
        return None
    return cfg


# ------------------------------------------------------------- App Store lookup --

def _get_json(url, timeout=30):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def _store_row(x):
    try:
        size = int(x.get("fileSizeBytes") or 0)
    except ValueError:
        size = 0
    notes = (x.get("releaseNotes") or "").strip()
    return {"name": x.get("trackName"), "version": x.get("version"), "size": size,
            "released": x.get("currentVersionReleaseDate"), "notes": notes[:600], "url": x.get("trackViewUrl"),
            "seller": x.get("sellerName"), "bundle": x.get("bundleId")}


# ---------------------------------------------------------------- the engine --

class AppUpdates:
    def __init__(self, db_path, mosyle_path=None, country="us", enabled=True):
        self.mosyle_path = mosyle_path
        self.country = country
        self.lock = threading.Lock()
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS inventory (source TEXT, device TEXT, device_name TEXT, bundle TEXT, name TEXT,
                version TEXT, platform TEXT, n INTEGER);
            CREATE INDEX IF NOT EXISTS inventory_bundle ON inventory(bundle);
            CREATE TABLE IF NOT EXISTS changes (t REAL, device TEXT, bundle TEXT, name TEXT, old TEXT, new TEXT);
            CREATE TABLE IF NOT EXISTS store (key TEXT PRIMARY KEY, j TEXT, found INTEGER, checked REAL);
            CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
        """)
        self.db.commit()
        self.state = {"syncing": False, "error": None, "storeError": None, "lookingUp": 0}
        self._wake = threading.Event()
        cfg = load_mosyle(mosyle_path) if mosyle_path else None
        self.mosyle_cfg = cfg
        if enabled:
            threading.Thread(target=self._loop, daemon=True).start()

    # -- meta -------------------------------------------------------------
    def _meta(self, k, v=None):
        with self.lock:
            if v is None:
                r = self.db.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
                return json.loads(r[0]) if r else None
            self.db.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (k, json.dumps(v)))
            self.db.commit()

    # -- saving an inventory ---------------------------------------------
    def save_inventory(self, source, rows, info=None):
        """Replace the inventory from one source, recording every version change since the last one."""
        now = time.time()
        with self.lock:
            old = {(d, b or n): v for d, b, n, v in self.db.execute(
                "SELECT device, bundle, name, version FROM inventory WHERE source = ? AND device IS NOT NULL", (source,))}
            changes = []
            for r in rows:
                if not r.get("device"):
                    continue
                k = (r["device"], r.get("bundle") or r["name"])
                prev = old.get(k)
                if prev is not None and prev != r["version"] and older(prev, r["version"]):
                    changes.append((now, r["device"], r.get("bundle"), r["name"], prev, r["version"]))
            self.db.execute("DELETE FROM inventory WHERE source = ?", (source,))
            self.db.executemany("INSERT INTO inventory (source, device, device_name, bundle, name, version, platform, n) VALUES (?,?,?,?,?,?,?,?)",
                                [(source, r.get("device"), r.get("deviceName"), r.get("bundle"), r["name"], r["version"],
                                  r.get("platform") or "ios", r.get("n") or 1) for r in rows])
            self.db.executemany("INSERT INTO changes (t, device, bundle, name, old, new) VALUES (?,?,?,?,?,?)", changes)
            self.db.execute("DELETE FROM changes WHERE t < ?", (now - CHANGES_KEPT,))
            self.db.commit()
        devices = len({r["device"] for r in rows if r.get("device")})
        self._meta("source", {"kind": source, "at": now, "rows": len(rows), "devices": devices or None,
                              "apps": len({(r.get("bundle") or r["name"]) for r in rows}), **(info or {})})
        self._wake.set()          # look up any new apps now
        return {"ok": True, "rows": len(rows), "devices": devices, "changes": len(changes)}

    def import_csv(self, text, filename=""):
        rows, info = parse_csv(text)
        return {**self.save_inventory("csv", rows, {"file": (filename or "")[:120], **info}), "columns": info["columns"]}

    def sync_mosyle(self):
        if not self.mosyle_cfg:
            return {"error": "Mosyle isn't set up (mosyle.json)"}
        self.state["syncing"] = True
        try:
            rows, info = Mosyle(self.mosyle_cfg).inventory()
            res = self.save_inventory("mosyle", rows, info)
            self.state["error"] = None
            return res
        except Exception as e:
            self.state["error"] = str(e)
            return {"error": str(e)}
        finally:
            self.state["syncing"] = False

    # -- App Store lookups ------------------------------------------------
    def _lookup_batch(self, items):
        """items: list of (key, bundle, name, platform). Fills the store table."""
        by_plat = {}
        for it in items:
            by_plat.setdefault(it[3], []).append(it)
        for plat, its in by_plat.items():
            entity = "macSoftware" if plat == "macos" else "software"
            withb = [it for it in its if it[1]]
            for i in range(0, len(withb), 25):
                chunk = withb[i:i + 25]
                found = self._query({"bundleId": ",".join(it[1] for it in chunk), "entity": entity})
                if found is None:
                    return False
                if not found and len(chunk) > 1:     # batch not accepted: ask one at a time
                    found = {}
                    for it in chunk:
                        f = self._query({"bundleId": it[1], "entity": entity})
                        if f is None:
                            return False
                        found.update(f)
                        _pause(3)
                self._store(chunk, found, by="bundle")
                _pause(3)
            for it in [it for it in its if not it[1]]:   # no bundle ID: exact name match only
                q = {"term": it[2], "entity": entity, "limit": "10", "country": self.country}
                try:
                    res = _get_json(SEARCH + "?" + urllib.parse.urlencode(q)).get("results") or []
                except Exception as e:
                    self.state["storeError"] = f"App Store search: {e}"
                    return False
                hit = next((x for x in res if (x.get("trackName") or "").strip().lower() == it[2].strip().lower()), None)
                self._store([it], {it[0]: _store_row(hit)} if hit else {}, by="name")
                _pause(3)
        return True

    def _query(self, params):
        params = {**params, "country": self.country}
        try:
            res = _get_json(LOOKUP + "?" + urllib.parse.urlencode(params)).get("results") or []
            self.state["storeError"] = None
        except Exception as e:
            self.state["storeError"] = f"App Store lookup failed: {e}. The caching Mac needs to reach itunes.apple.com."
            return None
        return {r.get("bundleId"): _store_row(r) for r in res if r.get("bundleId")}

    def _store(self, chunk, found, by):
        now = time.time()
        with self.lock:
            for key, bundle, name, plat in chunk:
                x = found.get(bundle if by == "bundle" else key)
                if x:
                    x["matchedBy"] = by
                self.db.execute("INSERT OR REPLACE INTO store (key, j, found, checked) VALUES (?,?,?,?)",
                                (key, json.dumps(x or {}), 1 if x else 0, now))
            self.db.commit()

    def refresh_store(self, force=False):
        with self.lock:
            apps = self.db.execute("""SELECT bundle, MAX(name), platform FROM inventory
                                      GROUP BY COALESCE(bundle, name), platform""").fetchall()
            have = {k: c for k, c in self.db.execute("SELECT key, checked FROM store")}
        now, todo = time.time(), []
        for bundle, name, plat in apps:
            key = f"{plat}:{bundle or 'name:' + (name or '').lower()}"
            if force or now - have.get(key, 0) > STORE_TTL:
                todo.append((key, bundle, name, plat))
        self.state["lookingUp"] = len(todo)
        for i in range(0, len(todo), 100):
            if not self._lookup_batch(todo[i:i + 100]):
                break
            self.state["lookingUp"] = max(0, len(todo) - i - 100)
        self.state["lookingUp"] = 0

    def _loop(self):
        time.sleep(15)
        hours = float((self.mosyle_cfg or {}).get("refreshHours") or 6)
        last_sync = (self._meta("lastMosyleSync") or 0)
        while True:
            try:
                if self.mosyle_cfg and time.time() - last_sync > hours * 3600:
                    self.sync_mosyle()
                    last_sync = time.time()
                    self._meta("lastMosyleSync", last_sync)
                self.refresh_store()
            except Exception as e:
                self.state["error"] = str(e)
            self._wake.wait(1800)
            self._wake.clear()

    def request_sync(self):
        if self.mosyle_cfg:
            threading.Thread(target=self.sync_mosyle, daemon=True).start()
        self._wake.set()
        return {"ok": True, "autoSync": bool(self.mosyle_cfg)}

    # -- report ------------------------------------------------------------
    def report(self):
        src = self._meta("source")
        now = time.time()
        with self.lock:
            # only the newest inventory (an uploaded report or the last sync), so the two never add up twice
            inv = self.db.execute("""SELECT bundle, name, version, platform, SUM(n) FROM inventory WHERE source = ?
                                     GROUP BY COALESCE(bundle, name), platform, version""", ((src or {}).get("kind", ""),)).fetchall()
            store = {k: (json.loads(j), f, c) for k, j, f, c in self.db.execute("SELECT key, j, found, checked FROM store")}
            ch = self.db.execute("""SELECT COALESCE(bundle, name), MAX(name), COUNT(DISTINCT device), MAX(t),
                                    SUM(CASE WHEN t >= ? THEN 1 ELSE 0 END), MAX(new)
                                    FROM changes WHERE t >= ? GROUP BY 1""", (now - 86400, now - 7 * 86400)).fetchall()
        recent = {k: {"name": n, "devices7d": d, "last": t, "devices24h": d24, "toVersion": v} for k, n, d, t, d24, v in ch}
        apps = {}
        for bundle, name, ver, plat, n in inv:
            key = f"{plat}:{bundle or 'name:' + (name or '').lower()}"
            a = apps.setdefault(key, {"key": key, "bundle": bundle, "name": name, "platform": plat, "devices": 0,
                                      "versions": {}, "current": 0, "pending": 0, "unknown": 0})
            a["devices"] += n or 0
            a["versions"][ver or "?"] = a["versions"].get(ver or "?", 0) + (n or 0)
        out = []
        for key, a in apps.items():
            st = store.get(key)
            s = st[0] if st else {}
            a["checked"] = st[2] if st else None
            a["inStore"] = bool(st and st[1])
            if s:
                a.update({"name": s.get("name") or a["name"], "storeVersion": s.get("version"), "size": s.get("size") or 0,
                          "released": s.get("released"), "notes": s.get("notes"), "url": s.get("url"),
                          "seller": s.get("seller"), "matchedBy": s.get("matchedBy")})
                for v, n in a["versions"].items():
                    o = older(v, s.get("version"))
                    a["pending" if o else "unknown" if o is None else "current"] += n
            a["pendingBytes"] = a["pending"] * (a.get("size") or 0)
            r = recent.get(a["bundle"] or a["name"])
            if r:
                a["updated24h"], a["updated7d"], a["lastUpdate"] = r["devices24h"], r["devices7d"], r["last"]
            a["versions"] = sorted(a["versions"].items(), key=lambda kv: (vkey(kv[0]) or (), kv[1]), reverse=True)
            out.append(a)
        out.sort(key=lambda a: (a["pendingBytes"], a["pending"], a["devices"]), reverse=True)
        unchecked = sum(1 for a in out if a["checked"] is None)
        return {
            "configured": {"autoSync": bool(self.mosyle_cfg), "country": self.country},
            "source": src, "state": dict(self.state), "unchecked": unchecked,
            "totals": {"apps": len(out), "withUpdates": sum(1 for a in out if a["pending"]),
                       "pendingInstalls": sum(a["pending"] for a in out),
                       "pendingBytes": sum(a["pendingBytes"] for a in out),
                       "updated24h": sum(a.get("updated24h") or 0 for a in out),
                       "notInStore": sum(1 for a in out if a["checked"] is not None and not a["inStore"])},
            "apps": out[:500],
        }

    def devices(self, key):
        """Devices that have one app, with their versions (those needing the update first)."""
        plat, _, ident = key.partition(":")
        kind = (self._meta("source") or {}).get("kind", "")
        with self.lock:
            if ident.startswith("name:"):
                rows = self.db.execute("""SELECT device, device_name, version, n FROM inventory
                                          WHERE source = ? AND platform = ? AND bundle IS NULL AND LOWER(name) = ?""", (kind, plat, ident[5:])).fetchall()
            else:
                rows = self.db.execute("SELECT device, device_name, version, n FROM inventory WHERE source = ? AND platform = ? AND bundle = ?",
                                       (kind, plat, ident)).fetchall()
            r = self.db.execute("SELECT j FROM store WHERE key = ?", (key,)).fetchone()
        latest = (json.loads(r[0]) if r else {}).get("version")
        devs = [{"device": d, "name": nm, "version": v, "count": n, "needsUpdate": older(v, latest)} for d, nm, v, n in rows]
        devs.sort(key=lambda x: (x["needsUpdate"] is not True, vkey(x["version"]) or (), x["name"] or x["device"] or ""))
        return {"key": key, "storeVersion": latest, "devices": devs[:3000], "total": len(devs)}
