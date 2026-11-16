"""
CacheDash catalog resolver: turns Apple download URLs into names people recognise.

Apple publishes two public catalogs that the caching Mac can already reach:

  * Software Update catalog (swscan.apple.com, ".sucatalog")
      Every .pkg served from swcdn.apple.com lives under a product ID, e.g.
      /content/downloads/12/34/093-55555/<hash>/Safari26.1Auto.pkg
      The catalog lists that product's metadata (.smd) and distribution (.dist)
      files, which contain the real title and version ("Safari 26.1",
      "XProtectPlistConfigData 5321", "macOS Tahoe 26.4", "Command Line Tools…").

  * MobileAsset update catalogs (mesu.apple.com)
      iOS/iPadOS and macOS updates download as <hash>.zip from updates.cdn-apple.com.
      These XML catalogs map each file to its OS version, build and devices.

App Store apps (.ipa, Mac App Store .pkg) have no public catalog, so they
cannot be named from the cache side.

Everything is cached on disk (catalog-cache.json) and refreshed every 12 hours.
"""

import gzip
import json
import os
import plistlib
import re
import subprocess
import threading
import time
import urllib.request

UA = {"User-Agent": "CacheDash/1.3 (+content-cache dashboard)"}
_TAIL = "15-14-13-12-10.16-10.15-10.14-10.13-10.12-10.11-10.10-10.9-mountainlion-lion-snowleopard-leopard.merged-1.sucatalog.gz"
SU_CATALOG_CANDIDATES = [
    "https://swscan.apple.com/content/catalogs/others/index-27-26-" + _TAIL,
    "https://swscan.apple.com/content/catalogs/others/index-26-" + _TAIL,
    "https://swscan.apple.com/content/catalogs/others/index-" + _TAIL,
]
# (feed, candidate URLs tried in order). Apple doesn't always publish the macOS
# feed publicly; when every candidate is refused, macOS OS updates are labelled
# from the device's own OS version instead (see clientmon.ClientMonitor.label).
MESU_CATALOGS = [
    ("ios", ["https://mesu.apple.com/assets/com_apple_MobileAsset_SoftwareUpdate/com_apple_MobileAsset_SoftwareUpdate.xml"]),
    ("macos", ["https://mesu.apple.com/assets/macos/com_apple_MobileAsset_MacSoftwareUpdate/com_apple_MobileAsset_MacSoftwareUpdate.xml",
               "https://mesu.apple.com/assets/com_apple_MobileAsset_MacSoftwareUpdate/com_apple_MobileAsset_MacSoftwareUpdate.xml"]),
]
RE_PID = re.compile(r"/(\d{3}-\d{4,6})/")
REFRESH = 12 * 3600


def _get(url, timeout=60):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        data = r.read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


def _filename(url):
    return (url or "").split("?")[0].rstrip("/").split("/")[-1]


def _osver(v):
    """mesu prefixes iOS versions with '9.9.' - strip it."""
    v = str(v or "")
    return v[4:] if v.startswith("9.9.") else v


def _platform(devices, feed):
    if feed == "macos":
        return "macOS"
    d = " ".join(devices or [])
    if "iPad" in d and "iPhone" not in d:
        return "iPadOS"
    if "iPhone" in d or "iPod" in d:
        return "iOS" if "iPad" not in d else "iOS/iPadOS"
    if "AppleTV" in d:
        return "tvOS"
    if "Watch" in d:
        return "watchOS"
    if "RealityDevice" in d:
        return "visionOS"
    return "iOS/iPadOS"


def parse_dist(text):
    """Pull title/version out of a Software Update .dist file."""
    out = {}
    m = re.search(r'"SU_TITLE"\s*=\s*"([^"]+)"', text)
    if m:
        out["title"] = m.group(1)
    else:
        m = re.search(r"<title>\s*([^<]+?)\s*</title>", text)
        if m and not m.group(1).startswith("SU_"):
            out["title"] = m.group(1)
    for key, pat in (("version", r'"SU_VERS"\s*=\s*"([^"]+)"'),
                     ("version", r"<key>VERSION</key>\s*<string>([^<]+)</string>"),
                     ("build", r"<key>BUILD</key>\s*<string>([^<]+)</string>")):
        if key not in out and (m := re.search(pat, text)):
            out[key] = m.group(1)
    return out


def parse_smd(data):
    """Pull title/version out of a Software Update .smd (ServerMetadata) plist."""
    try:
        p = plistlib.loads(data)
    except Exception:
        return {}
    out = {}
    loc = p.get("localization") or {}
    for lang in ("English", "en", "en_US", "en_GB"):
        if isinstance(loc.get(lang), dict) and loc[lang].get("title"):
            out["title"] = loc[lang]["title"]
            break
    if "title" not in out and p.get("Title"):
        out["title"] = p["Title"]
    if p.get("CFBundleShortVersionString"):
        out["version"] = p["CFBundleShortVersionString"]
    return out


class CatalogResolver:
    def __init__(self, cache_path, enabled=True, su_catalog=None):
        self.cache_path = cache_path
        self.enabled = enabled
        self.su_catalog = su_catalog
        self.lock = threading.Lock()
        self.products = {}   # pid -> {"smd":url, "dist":url, "post":ts, "title":..., "version":..., "build":..., "tried":ts}
        self.assets = {}     # filename -> {"title","kind","version","build"}
        self.status = {"enabled": enabled, "su": None, "mesu": {}, "updated": None, "error": None}
        self._pending = set()
        self._dirty = False
        self._load_cache()
        if enabled:
            threading.Thread(target=self._loop, daemon=True).start()
            threading.Thread(target=self._saver, daemon=True).start()

    # -- persistence ------------------------------------------------------
    def _load_cache(self):
        try:
            with open(self.cache_path) as f:
                c = json.load(f)
            self.products = c.get("products", {})
            self.assets = c.get("assets", {})
            self.status.update({k: c.get("status", {}).get(k) for k in ("su", "mesu", "updated")})
        except Exception:
            pass

    def _save(self):
        with self.lock:
            blob = {"products": self.products, "assets": self.assets, "status": self.status}
            self._dirty = False
        tmp = self.cache_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(blob, f)
        os.replace(tmp, self.cache_path)

    def _saver(self):
        while True:
            time.sleep(30)
            if self._dirty:
                try:
                    self._save()
                except Exception:
                    pass

    # -- catalogs -----------------------------------------------------------
    def _su_urls(self):
        urls = []
        if self.su_catalog:
            urls.append(self.su_catalog)
        try:  # a custom catalog the Mac itself is pointed at (e.g. beta seed)
            out = subprocess.run(["/usr/bin/defaults", "read", "/Library/Preferences/com.apple.SoftwareUpdate", "CatalogURL"],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
            if out.startswith("http"):
                urls.append(out)
        except Exception:
            pass
        return urls + SU_CATALOG_CANDIDATES

    def refresh_su(self):
        last_err = None
        for url in self._su_urls():
            try:
                cat = plistlib.loads(_get(url, timeout=120))
            except Exception as e:
                last_err = f"{url.split('/')[-1][:40]}…: {e}"
                continue
            prods = cat.get("Products") or {}
            with self.lock:
                for pid, p in prods.items():
                    cur = self.products.get(pid, {})
                    dists = p.get("Distributions") or {}
                    cur["smd"] = p.get("ServerMetadataURL")
                    cur["dist"] = dists.get("English") or dists.get("en") or next(iter(dists.values()), None)
                    post = p.get("PostDate")
                    cur["post"] = post.timestamp() if hasattr(post, "timestamp") else None
                    ext = p.get("ExtendedMetaInfo") or {}
                    if ext.get("InstallAssistantPackageIdentifiers"):
                        cur["installer"] = True
                    self.products[pid] = cur
                self.status["su"] = {"url": url, "products": len(prods), "at": time.time()}
                self._dirty = True
            return True
        self.status["su"] = {"error": last_err or "no catalog reachable", "at": time.time()}
        return False

    def refresh_mesu(self):
        for entry in MESU_CATALOGS:
            feed, urls = (entry[1], [entry[0]]) if isinstance(entry[1], str) else (entry[0], entry[1])
            cat, errs = None, []
            for url in urls:
                try:
                    cat = plistlib.loads(_get(url, timeout=120))
                    break
                except Exception as e:
                    errs.append(str(e))
            if cat is None:
                refused = errs and all(("403" in e or "404" in e) for e in errs)
                self.status["mesu"][feed] = (
                    {"unavailable": "Apple doesn't publish this catalog publicly", "detail": errs[-1], "at": time.time()}
                    if refused else {"error": errs[-1] if errs else "no URL", "at": time.time()})
                continue
            n = 0
            with self.lock:
                for a in cat.get("Assets") or []:
                    rel = a.get("__RelativePath")
                    if not rel:
                        continue
                    plat = _platform(a.get("SupportedDevices"), feed)
                    ver = _osver(a.get("OSVersion"))
                    build = a.get("Build") or ""
                    pre = _osver(a.get("PrerequisiteOSVersion"))
                    title = f"{plat} {ver}".strip() + (f" ({build})" if build else "")
                    if a.get("SplatOnly") or a.get("RSR") or "RSR" in str(a.get("ReleaseType", "")):
                        title += " Rapid Security Response"
                    detail = f"update from {pre}" if pre else "full update"
                    self.assets[_filename(rel)] = {
                        "title": title, "detail": detail, "version": ver, "build": build,
                        "kind": "macOS update" if feed == "macos" else "iOS/iPadOS update",
                    }
                    n += 1
                self.status["mesu"][feed] = {"assets": n, "at": time.time()}
                self._dirty = True

    def _loop(self):
        while True:
            try:
                self.refresh_su()
                self.refresh_mesu()
                self.status["updated"] = time.time()
                self.status["error"] = None
            except Exception as e:
                self.status["error"] = str(e)
            self._dirty = True
            time.sleep(REFRESH)

    # -- per-product metadata (fetched lazily, only for products actually seen) --
    def _fetch_meta(self, pid):
        p = self.products.get(pid) or {}
        meta = {}
        try:
            if p.get("smd"):
                meta = parse_smd(_get(p["smd"], timeout=30))
        except Exception:
            pass
        try:
            if p.get("dist") and (not meta.get("title") or not meta.get("version")):
                d = parse_dist(_get(p["dist"], timeout=30).decode("utf-8", "replace"))
                meta = {**d, **{k: v for k, v in meta.items() if v}}
        except Exception:
            pass
        with self.lock:
            p.update(meta)
            p["tried"] = time.time()
            self.products[pid] = p
            self._pending.discard(pid)
            self._dirty = True

    # -- public -------------------------------------------------------------
    def lookup(self, url, fetch=True):
        """Return {"title","kind","version","detail"} for a download URL, or None."""
        if not url:
            return None
        fn = _filename(url)
        a = self.assets.get(fn)
        if a:
            return a
        m = RE_PID.search(url)
        if not m:
            return None
        pid = m.group(1)
        p = self.products.get(pid)
        if p is None:
            return None
        if p.get("title"):
            title = p["title"]
            ver = p.get("version")
            if ver and ver not in title:
                title = f"{title} {ver}"
            if p.get("installer") or "installassistant" in fn.lower():
                kind = "macOS installer"
            elif re.search(r"xprotect|\bmrt\b|gatekeeper|configdata|compatibilitydata|background ?(task|security)|bridgeos", title, re.I):
                kind = "macOS security data"
            else:
                kind = "macOS software update"
            return {"title": title, "version": ver, "build": p.get("build"), "kind": kind, "product": pid}
        if fetch and self.enabled and pid not in self._pending and time.time() - p.get("tried", 0) > 6 * 3600:
            self._pending.add(pid)
            threading.Thread(target=self._fetch_meta, args=(pid,), daemon=True).start()
        return None

    def resolve_now(self, url):
        """Blocking lookup used by --resolve: fetches metadata if needed."""
        r = self.lookup(url, fetch=False)
        if r:
            return r
        m = RE_PID.search(url or "")
        if m and m.group(1) in self.products:
            self._fetch_meta(m.group(1))
            return self.lookup(url, fetch=False)
        return None

    def summary(self):
        return {**self.status,
                "productsKnown": len(self.products),
                "productsNamed": sum(1 for p in self.products.values() if p.get("title")),
                "updateFiles": len(self.assets)}


def check(cache_path, urls=(), su_catalog=None):
    """CLI: download the catalogs now, print what was found, and resolve sample URLs."""
    r = CatalogResolver(cache_path, enabled=False, su_catalog=su_catalog)
    r.enabled = True
    print("Software Update catalog …", "ok" if r.refresh_su() else "FAILED", r.status["su"])
    r.refresh_mesu()
    for feed, s in r.status["mesu"].items():
        print(f"Update catalog ({feed}) …", s)
    r.status["updated"] = time.time()
    r._save()
    for u in urls:
        print(f"\n{u}\n  -> {r.resolve_now(u) or 'not found in Apple catalogs'}")
    r._save()
    print(f"\nSaved to {cache_path}")
