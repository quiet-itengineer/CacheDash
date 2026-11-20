"""
CacheDash cluster: which other caching Macs this dashboard shows.

Three ways a caching Mac learns about the others, so you only have to enter each one once
(or not at all):

  1. servers.json, written by the installer when you answer its questions (or by
     "Name=URL" arguments);
  2. discovery: twice an hour the cache's own peers and parents (from
     AssetCacheManagerUtil) are checked for CacheDash on the same port;
  3. joining: every caching Mac in servers.json is told about this one
     (POST /api/cluster/join), and adds it to its own servers.json. A Mac can only add
     itself (the address the request came from), and only after the receiving Mac has
     checked that CacheDash really answers there.

The dashboard reads the combined list from /api/info and adds every Mac automatically.
"""

import ipaddress
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request

DEFAULT_PORT = 8027
# Mac-to-Mac requests stay on your network: never send them through a web proxy configured on the Mac.
_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
MAX_SERVERS = 12


def own_ips():
    ips = {"127.0.0.1", "::1"}
    try:
        out = subprocess.run(["/sbin/ifconfig"], capture_output=True, text=True, timeout=5).stdout
        ips.update(re.findall(r"inet6? ([0-9a-fA-F:.]+)", out))
    except Exception:
        pass
    try:
        ips.update(i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None))
    except Exception:
        pass
    return ips


def normalize(addr, port=DEFAULT_PORT):
    """'10.0.1.16', '10.0.1.16:8027', 'cache2.local' or 'http://10.0.1.16:8027/' -> 'http://10.0.1.16:8027'."""
    a = (addr or "").strip().rstrip("/")
    if not a:
        return None
    if not re.match(r"^https?://", a, re.I):
        a = "http://" + a
    m = re.match(r"^(https?)://(\[[0-9a-fA-F:]+\]|[A-Za-z0-9.-]+)(?::(\d{1,5}))?$", a)
    if not m:
        return None
    p = int(m.group(3) or port)
    if not 0 < p < 65536:
        return None
    return f"{m.group(1).lower()}://{m.group(2).lower()}:{p}"


def host_of(url):
    m = re.match(r"^https?://(\[[^\]]+\]|[^:/]+)", url or "")
    return m.group(1).strip("[]") if m else ""


def probe(url, timeout=4):
    """Is CacheDash answering at url? -> {"ok", "url", "name", "version"} or {"ok": False, "error"}."""
    try:
        req = urllib.request.Request(url.rstrip("/") + "/api/info", headers={"User-Agent": "CacheDash cluster"})
        with _direct.open(req, timeout=timeout) as r:
            info = json.loads(r.read(200_000) or b"{}")
        if not info.get("version"):
            return {"ok": False, "url": url, "error": "something answered, but it isn't CacheDash"}
        return {"ok": True, "url": url, "name": str(info.get("name") or "")[:80], "version": str(info["version"])[:20]}
    except urllib.error.HTTPError as e:
        return {"ok": False, "url": url, "error": f"HTTP {e.code}" + (" (this Mac's address isn't allowed there; see --allow)" if e.code == 403 else "")}
    except Exception as e:
        reason = getattr(e, "reason", e)
        return {"ok": False, "url": url, "error": str(reason)[:160]}


def peer_addresses(status_result):
    """Addresses of this cache's peers and parents, from AssetCacheManagerUtil -j status."""
    out = []
    for key in ("Peers", "Parents"):
        for p in (status_result or {}).get(key) or []:
            a = p.get("address") or p.get("Address") if isinstance(p, dict) else p
            if not isinstance(a, str):
                continue
            a = a.strip()
            if a.count(":") == 1:          # ip:port -> ip (the cache port, not CacheDash's)
                a = a.split(":")[0]
            a = a.strip("[]")
            if a and a not in out:
                out.append(a)
    return out


def read_status():
    try:
        r = subprocess.run(["/usr/bin/AssetCacheManagerUtil", "-j", "status"], capture_output=True, text=True, timeout=20)
        d = json.loads(r.stdout or "{}")
        return d.get("result", d)
    except Exception:
        return {}


def find(port=DEFAULT_PORT, status_result=None, exclude=()):
    """CacheDash on this cache's peers and parents."""
    mine = own_ips()
    found = []
    for a in peer_addresses(status_result if status_result is not None else read_status()):
        if a in mine:
            continue
        url = normalize(a, port)
        if not url or url in exclude:
            continue
        r = probe(url)
        if r["ok"]:
            found.append(r)
    return found


class Cluster:
    def __init__(self, path, port, name, status=None, enabled=True):
        self.path, self.port, self.name, self.status = path, int(port), name, status
        self.lock = threading.Lock()
        self.static = self._load()
        self.found = {}                 # url -> {"name","url"} discovered from the cache's peers
        self.joins = {}                 # url -> result of telling that Mac about this one
        self.mine = own_ips()
        if enabled:
            threading.Thread(target=self._loop, daemon=True).start()

    # -- servers.json ---------------------------------------------------------
    def _load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
        except FileNotFoundError:
            return []
        except Exception as e:
            print(f"Ignoring {self.path}: {e}")
            return []
        out = []
        for x in data if isinstance(data, list) else []:
            url = normalize(str(x.get("url", ""))) if isinstance(x, dict) else None
            if url:
                out.append({"name": str(x.get("name") or "")[:80], "url": url})
        return out

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.static, f, indent=2)
        os.replace(tmp, self.path)

    def list(self):
        with self.lock:
            out = [{**x, "source": "configured"} for x in self.static]
            hosts = {host_of(x["url"]) for x in out}
            out += [{**x, "source": "discovered"} for u, x in self.found.items() if host_of(u) not in hosts]
        return out

    # -- another Mac asks to be added ---------------------------------------------
    def join(self, ip, port):
        try:
            port = int(port)
            a = ipaddress.ip_address(ip.split("%")[0])
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad address or port"}
        if a.version == 6 and a.ipv4_mapped:
            a = a.ipv4_mapped
        if a.is_loopback or str(a) in self.mine:
            return {"ok": False, "error": "that's this Mac"}
        if not 0 < port < 65536:
            return {"ok": False, "error": "bad port"}
        url = f"http://{'[' + str(a) + ']' if a.version == 6 else a}:{port}"
        r = probe(url)                  # only add it if CacheDash really answers there
        if not r["ok"]:
            return {"ok": False, "error": f"couldn't reach CacheDash at {url}: {r['error']}"}
        with self.lock:
            cur = next((x for x in self.static if host_of(x["url"]) == str(a)), None)
            added = cur is None
            if cur:
                cur.update({"url": url, "name": cur["name"] or r["name"]})
            elif len(self.static) >= MAX_SERVERS:
                return {"ok": False, "error": "this dashboard already lists the maximum number of caching Macs"}
            else:
                self.static.append({"name": r["name"], "url": url})
            self._save()
        return {"ok": True, "added": added, "name": self.name}

    # -- tell the others about this Mac, and look for new ones -----------------------
    def announce(self, url):
        try:
            req = urllib.request.Request(url.rstrip("/") + "/api/cluster/join", method="POST",
                                         data=json.dumps({"port": self.port}).encode(),
                                         headers={"Content-Type": "application/json", "User-Agent": "CacheDash cluster"})
            with _direct.open(req, timeout=12) as r:
                res = json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            res = {"ok": False, "error": "it runs an older CacheDash (update it with git up)" if e.code == 404 else f"HTTP {e.code}"}
        except Exception as e:
            res = {"ok": False, "error": str(getattr(e, "reason", e))[:160]}
        self.joins[url] = {**res, "at": time.time()}
        return res

    def _loop(self):
        time.sleep(30)
        last_announce = 0
        while True:
            try:
                st = (self.status.get() or {}).get("result") if self.status else read_status()
                have = {x["url"] for x in self.static}
                found = {r["url"]: {"name": r["name"], "url": r["url"]} for r in find(self.port, st or {}, exclude=have)}
                with self.lock:
                    self.found = found
                if time.time() - last_announce > 6 * 3600:
                    for x in list(self.static):
                        self.announce(x["url"])
                    last_announce = time.time()
            except Exception:
                pass
            time.sleep(1800)


# ------------------------------------------------------------- installer helpers --
# Fields separated by \x1f (not tabs, which `read` merges when a field is empty), for install.sh.
SEP = "\x1f"

def cli_find(port):
    for r in find(port):
        print(SEP.join([r["name"], r["url"], r["version"]]))


def cli_probe(addr, port):
    url = normalize(addr, port)
    if not url:
        print(SEP.join(["bad", addr, "that doesn't look like an address"]))
        return
    r = probe(url)
    print(SEP.join(["ok", url, r["name"], r["version"]] if r["ok"] else ["fail", url, r["error"]]))


def cli_join(url, port, name=""):
    r = Cluster.__new__(Cluster)
    r.port, r.joins = int(port), {}
    res = r.announce(url)
    if res.get("ok"):
        print(SEP.join(["ok", res.get("name") or url, "added" if res.get("added") else "already listed"]))
    else:
        print(SEP.join(["fail", url, str(res.get("error"))]))
