"""
Where a download is stored in the content cache on this Mac (read-only).

Apple doesn't document the cache's storage, but it has been stable for years:

  * the data folder is DataPath in /Library/Preferences/com.apple.AssetCache.plist,
    or /Library/Application Support/Apple/AssetCache/Data
  * AssetInfo.db in that folder (Core Data SQLite) has one row per cached asset in
    ZASSET, with the original address (ZURI), a GUID (ZGUID) and the size (ZTOTALBYTES)
  * the content itself is in <data folder>/<GUID>/0 (more numbered parts for big assets)

CacheDash only ever reads a snapshot copy of AssetInfo.db and lists the asset's folder.
It never changes, moves or deletes cached content. Column names are found at run time,
so a renamed column shows up in `cachedash.py --find-cached` instead of breaking.
"""

import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.parse

DEFAULT_DATA = "/Library/Application Support/Apple/AssetCache/Data"
CORE_DATA_EPOCH = 978307200


def data_path():
    try:
        out = subprocess.run(["/usr/bin/defaults", "read", "/Library/Preferences/com.apple.AssetCache.plist", "DataPath"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        if out:
            return out
    except Exception:
        pass
    return DEFAULT_DATA


def url_path(url):
    """The part of a download address that identifies the file: its path, without the host,
    the cache's own address, or query parameters (?source=…)."""
    u = (url or "").strip()
    if not u:
        return ""
    p = urllib.parse.urlsplit(u if "://" in u else "http://x" + (u if u.startswith("/") else "/" + u)).path
    return urllib.parse.unquote(p).rstrip("/")


def _like(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class CacheStore:
    def __init__(self, data_dir=None, ttl=300):
        self.data_dir = data_dir or data_path()
        self.ttl = ttl
        self.lock = threading.Lock()
        self._snap = None          # (tmpdir, path, made_at)
        self.schema = None
        self.error = None

    # -- snapshot of AssetInfo.db ------------------------------------------------
    def _db(self):
        src = os.path.join(self.data_dir, "AssetInfo.db")
        with self.lock:
            if self._snap and time.time() - self._snap[2] < self.ttl and os.path.exists(self._snap[1]):
                return self._snap[1]
            if not os.path.exists(src):
                raise FileNotFoundError(f"No AssetInfo.db in {self.data_dir}. Is content caching on, and is this the cache's data folder?")
            tmp = tempfile.mkdtemp(prefix="cachedash-assets-")
            dst = os.path.join(tmp, "AssetInfo.db")
            shutil.copy2(src, dst)
            for sfx in ("-wal", "-shm"):
                if os.path.exists(src + sfx):
                    shutil.copy2(src + sfx, dst + sfx)
            if self._snap:
                shutil.rmtree(self._snap[0], ignore_errors=True)
            self._snap = (tmp, dst, time.time())
            return dst

    def _table(self, con):
        """The asset table and which columns hold the address, GUID and size."""
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        best = None
        for t in tables:
            cols = [r[1] for r in con.execute(f'PRAGMA table_info("{t}")')]
            up = {c.upper(): c for c in cols}
            uri = up.get("ZURI") or next((c for c in cols if re.search(r"URI|URL", c, re.I)), None)
            guid = up.get("ZGUID") or next((c for c in cols if re.search(r"GUID|UUID", c, re.I)), None)
            if uri and guid:
                best = {"table": t, "uri": uri, "guid": guid, "columns": cols,
                        "size": up.get("ZTOTALBYTES") or next((c for c in cols if re.search(r"BYTES|SIZE", c, re.I)), None),
                        "namespace": up.get("ZNAMESPACE"),
                        "dates": [c for c in cols if re.search(r"DATE|ACCESS|CREAT|MODIF", c, re.I)]}
                if t.upper() == "ZASSET":
                    break
        self.schema = best or {"tables": tables}
        return best

    # -- lookups ------------------------------------------------------------------
    def find(self, url, limit=20):
        """Cached copies of the file at this address (or, failing that, with this file name)."""
        path = url_path(url)
        if not path:
            return {"error": "no address given"}
        try:
            con = sqlite3.connect(f"file:{self._db()}?mode=ro", uri=True)
        except Exception as e:
            self.error = str(e)
            return {"error": str(e), "dataPath": self.data_dir}
        try:
            t = self._table(con)
            if not t:
                return {"error": "AssetInfo.db doesn't have the expected asset table. Run cachedash.py --find-cached to see what it contains.",
                        "dataPath": self.data_dir}
            cols = [t["uri"], t["guid"]] + ([t["size"]] if t["size"] else []) + t["dates"] + ([t["namespace"]] if t["namespace"] else [])
            sel = ", ".join(f'"{c}"' for c in cols)
            q = f'SELECT {sel} FROM "{t["table"]}" WHERE "{t["uri"]}" LIKE ? ESCAPE \'\\\' LIMIT ?'
            rows, how = [], "address"
            for cand in dict.fromkeys([path, urllib.parse.quote(path)]):   # stored plain or percent-encoded
                rows = con.execute(q, (f"%{_like(cand)}%", limit)).fetchall()
                if rows:
                    break
            if not rows:
                name = path.rsplit("/", 1)[-1]
                if len(name) >= 6:
                    rows = con.execute(q, (f"%/{_like(name)}%", limit)).fetchall()
                    how = "file name"
        finally:
            con.close()
        out = []
        for r in rows:
            d = dict(zip(cols, r))
            guid = str(d[t["guid"]] or "")
            folder = os.path.join(self.data_dir, guid) if re.fullmatch(r"[0-9A-Fa-f-]{8,64}", guid) else None
            parts = []
            if folder and os.path.isdir(folder):
                for f in sorted(os.listdir(folder))[:50]:
                    fp = os.path.join(folder, f)
                    try:
                        st = os.stat(fp)
                        parts.append({"name": f, "path": fp, "bytes": st.st_size, "modified": st.st_mtime})
                    except OSError:
                        pass
            dates = {}
            for c in t["dates"]:
                v = d.get(c)
                if isinstance(v, (int, float)) and v:
                    dates[c[1:] if c.startswith("Z") else c] = v + CORE_DATA_EPOCH if v < 1.2e9 else v
            out.append({"uri": d[t["uri"]], "guid": guid, "folder": folder, "onDisk": bool(parts),
                        "bytes": d.get(t["size"]) if t["size"] else None,
                        "diskBytes": sum(p["bytes"] for p in parts), "files": parts, "dates": dates,
                        "namespace": d.get(t["namespace"]) if t["namespace"] else None})
        return {"dataPath": self.data_dir, "matchedBy": how if out else None, "matches": out}


def cli(arg):
    """--find-cached: show the cache's storage layout and look up one address or file name."""
    cs = CacheStore()
    print("Cache data folder:", cs.data_dir)
    try:
        con = sqlite3.connect(f"file:{cs._db()}?mode=ro", uri=True)
    except Exception as e:
        print("Can't read AssetInfo.db:", e)
        return
    t = cs._table(con)
    if t:
        n = con.execute(f'SELECT COUNT(*) FROM "{t["table"]}"').fetchone()[0]
        print(f"Asset table: {t['table']} ({n:,} cached items)\n  address column: {t['uri']}, id column: {t['guid']}, size column: {t['size']}"
              f"\n  all columns: {', '.join(t['columns'])}")
        ex = con.execute(f'SELECT "{t["uri"]}", "{t["guid"]}" FROM "{t["table"]}" LIMIT 1').fetchone()
        if ex:
            print(f"  example: {str(ex[0])[:140]}\n           -> {os.path.join(cs.data_dir, str(ex[1]))}"
                  f" ({'exists' if os.path.isdir(os.path.join(cs.data_dir, str(ex[1]))) else 'folder not found'})")
    else:
        print("No table with an address and an id column. Tables:", ", ".join(cs.schema.get("tables", [])))
    con.close()
    if arg and arg != "-":
        res = cs.find(arg)
        if res.get("error"):
            print("\n", res["error"])
            return
        print(f"\n{len(res['matches'])} cached match(es) for {url_path(arg)}" + (f" (by {res['matchedBy']})" if res["matchedBy"] else ""))
        for m in res["matches"]:
            print(f"  {m['folder']}  {m['diskBytes']:,} bytes on disk  {'' if m['onDisk'] else '(folder missing)'}\n    {str(m['uri'])[:160]}")
