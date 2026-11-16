"""
CacheDash log access: lets the dashboard read the AssetCache system log on each
caching Mac, group repeated errors, follow one request from start to finish, and
download the raw lines.

Sources, newest first:
  * the live buffer kept by the client monitor (last ~20,000 lines, instant)
  * `log show` on the Mac's unified log for older times (slower, capped)
"""

import json
import re
import subprocess
import time
from datetime import datetime

LOG = "/usr/bin/log"
PREDICATE = 'subsystem == "com.apple.AssetCache"'
MAX_SPAN = 4 * 3600          # longest window one request may read from the system log
MAX_LINES = 20000

RE_ERR = re.compile(r"\b(error|failed|failure|timed out|timeout|refused|reset by peer|unavailable|503|too many|denied|forbidden)\b"
                    r"|status code [45]\d\d", re.I)
RE_REQID = re.compile(r"#([A-Za-z0-9]{6,20})\b")

_NORM = [
    (re.compile(r"#[A-Za-z0-9]{6,20}\b"), "#…"),                                     # request IDs
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<uuid>"),
    (re.compile(r"(https?://[^/\s\"']+)[^\s\"']*"), r"\1/…"),                        # keep host, drop path
    (re.compile(r"(?<![\w.])/[^\s\"']{12,}"), "/…"),                                  # long paths
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "0x…"),
    (re.compile(r"\b[0-9a-fA-F]{12,}\b"), "<hex>"),
    (re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:bytes?|[KMGT]i?B)\b", re.I), "<size>"),
    (re.compile(r"\b\d{4,}(?:\.\d+)?\b"), "<n>"),                                       # long numbers (keep 403, 26 …)
    (re.compile(r"\b\d+\.\d+\b"), "<n>"),
]



def ip_pattern(q):
    """A regex that finds this exact IPv4 address in a log line (not 10.0.0.50 for 10.0.0.5), or None."""
    q = (q or "").strip()
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", q):
        return re.compile(r"(?<![\d.])" + re.escape(q) + r"(?![\d])")
    return None

def normalize(msg):
    """Collapse the variable parts of a log message so repeats group together."""
    m = msg or ""
    for rx, sub in _NORM:
        m = rx.sub(sub, m)
    return re.sub(r"\s+", " ", m).strip()[:240]


def is_error(line):
    return line.get("type") in ("Error", "Fault") or bool(RE_ERR.search(line.get("msg") or ""))


def _fmt(t):
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")


def _parse_ts(s):
    try:
        return datetime.strptime(s[:26], "%Y-%m-%d %H:%M:%S.%f").timestamp()
    except Exception:
        return time.time()


def system_log(start, end, contains=None, limit=MAX_LINES, timeout=45):
    """Read AssetCache lines from the unified log with `log show`."""
    pred = PREDICATE
    if contains and re.fullmatch(r"[A-Za-z0-9#._:/ -]{3,80}", contains):
        pred += f' AND eventMessage CONTAINS[c] "{contains}"'
    args = [LOG, "show", "--style", "ndjson", "--info", "--predicate", pred, "--start", _fmt(start), "--end", _fmt(end)]
    out, t0 = [], time.time()
    try:
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except FileNotFoundError:
        return None, "the `log` command isn't available (this only works on macOS)"
    try:
        for raw in p.stdout:
            if not raw.startswith("{"):
                continue
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            out.append({"t": _parse_ts(ev.get("timestamp", "")), "type": ev.get("messageType", "Default"),
                        "msg": ev.get("eventMessage", "")})
            if len(out) >= limit or time.time() - t0 > timeout:
                break
    finally:
        p.kill()
    return out, None


def group(lines, top=40):
    g = {}
    for ln in lines:
        k = normalize(ln["msg"])
        e = g.setdefault(k, {"pattern": k, "count": 0, "first": ln["t"], "last": ln["t"], "example": ln["msg"][:400],
                             "type": ln.get("type")})
        e["count"] += 1
        e["first"], e["last"] = min(e["first"], ln["t"]), max(e["last"], ln["t"])
    return sorted(g.values(), key=lambda x: -x["count"])[:top]


class LogReader:
    def __init__(self, buffer_getter=None):
        self.buffer_getter = buffer_getter or (lambda: [])

    def _lines(self, start, end, contains=None):
        buf = list(self.buffer_getter())
        oldest = buf[0]["t"] if buf else None
        lines, source, note = [], [], None
        if oldest is not None and end >= oldest:
            lines += [b for b in buf if start <= b["t"] <= end]
            source.append("live buffer")
        if oldest is None or start < oldest:
            sys_end = min(end, oldest) if oldest else end
            sys_start = max(start, sys_end - MAX_SPAN)
            got, err = system_log(sys_start, sys_end, contains)
            if got is not None:
                lines = got + lines
                source.append("system log")
                if sys_start > start:
                    note = f"Only the last {MAX_SPAN//3600} hours before the live buffer were read from the system log. Pick a shorter window to see earlier lines."
            elif not lines:
                note = err
        return lines, ", ".join(source) or "none", note

    def query(self, start=None, end=None, errors_only=True, q=None, limit=2000):
        now = time.time()
        end = min(end or now, now)
        start = start or end - 900
        lines, source, note = self._lines(start, end)
        total = len(lines)
        errs = [ln for ln in lines if is_error(ln)]
        sel = errs if errors_only else lines
        if q:
            ip = ip_pattern(q)
            if ip:   # an address: match it exactly, so 10.0.0.5 doesn't also find 10.0.0.50
                sel = [ln for ln in sel if ip.search(ln["msg"] or "")]
            else:
                ql = q.lower()
                sel = [ln for ln in sel if ql in (ln["msg"] or "").lower() or ql == normalize(ln["msg"]).lower()]
        sel.sort(key=lambda x: x["t"])
        return {
            "start": start, "end": end, "source": source, "note": note,
            "totalLines": total, "errorLines": len(errs),
            "groups": group(errs),
            "lines": sel[-limit:], "matched": len(sel), "truncated": len(sel) > limit,
        }

    def trace(self, req_id, hours=6):
        req_id = req_id.lstrip("#")
        if not re.fullmatch(r"[A-Za-z0-9]{6,20}", req_id):
            return {"error": "That doesn't look like a request ID"}
        now = time.time()
        buf = [b for b in self.buffer_getter() if req_id in (b["msg"] or "")]
        lines = buf
        if not buf or buf[0]["t"] > now - 60:
            got, _ = system_log(now - hours * 3600, now, contains=req_id, limit=500)
            if got:
                seen = {(x["t"], x["msg"]) for x in buf}
                lines = [g for g in got if req_id in (g["msg"] or "") and (g["t"], g["msg"]) not in seen] + buf
        lines.sort(key=lambda x: x["t"])
        urls = sorted({u for ln in lines for u in re.findall(r"https?://[^\s\"']+", ln["msg"] or "")})
        ips = sorted({ip for ln in lines for ip in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", ln["msg"] or "")})
        return {"id": req_id, "lines": lines[:500], "urls": urls[:20], "ips": ips[:20]}

    def download(self, start, end, errors_only=False):
        res = self.query(start, end, errors_only=errors_only, limit=MAX_LINES)
        out = [f"# AssetCache log {_fmt(res['start'])} to {_fmt(res['end'])} ({res['source']}); "
               f"{res['matched']} lines{' (errors only)' if errors_only else ''}"]
        for ln in res["lines"]:
            out.append(f"{datetime.fromtimestamp(ln['t']).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}  {ln.get('type','')[:7]:7}  {ln['msg']}")
        return "\n".join(out) + "\n"


# ------------------------------------------------------------------ probe ----
# "Who sent this error?" The cache Mac re-requests one byte of an Apple address
# and looks at who answers: Apple's CDN, or a filter/proxy/firewall in between.
# Only Apple's own domains can be probed.

import urllib.error
import urllib.parse
import urllib.request

APPLE_DOMAINS = re.compile(r"(^|\.)(apple\.com|cdn-apple\.com|mzstatic\.com|icloud\.com|icloud-content\.com|"
                           r"apple-cloudkit\.com|aaplimg\.com|apple-dns\.net|apple\.news)$", re.I)
FILTER_NAMES = ["fortigate", "fortinet", "fortiguard", "sophos", "palo alto", "pan-os", "paloalto", "cisco", "umbrella", "opendns",
                "zscaler", "lightspeed", "securly", "goguardian", "iboss", "contentkeeper", "bluecoat", "blue coat", "symantec",
                "squid", "smoothwall", "barracuda", "sonicwall", "watchguard", "untangle", "meraki", "netsweeper", "linewize",
                "cloudflare gateway", "websense", "forcepoint", "mcafee web gateway", "trend micro", "checkpoint", "check point",
                "deledao", "gaggle", "content filter", "web filter", "proxy"]
_probe_cache = {}


def full_url(u):
    """Turn a cache-relative path (…?source=host) back into the Apple address."""
    u = (u or "").strip()
    p = urllib.parse.urlsplit(u)
    q = urllib.parse.parse_qs(p.query)
    host = (q.get("source") or [None])[0]
    if host and (not p.netloc or re.match(r"^\d+\.\d+\.\d+\.\d+(:\d+)?$", p.netloc)):
        rest = [(k, v) for k, vs in q.items() if k != "source" for v in vs]
        return urllib.parse.urlunsplit(("http", host, p.path, urllib.parse.urlencode(rest), ""))
    return u


def _who(status, headers, body):
    h = {k.lower(): v for k, v in (headers or {}).items()}
    text = " ".join([h.get("server", ""), h.get("via", ""), h.get("x-cache", ""), " ".join(k for k in h if k.startswith("x-")),
                     body or ""]).lower()
    for name in FILTER_NAMES:
        if name in text and name not in ("proxy",) or (name == "proxy" and ("proxy-agent" in h or "x-squid-error" in h)):
            return "filter", name.title()
    if any(k.startswith("x-apple") or k in ("cdnuuid", "x-responding-instance", "x-cache-hits") for k in h) or \
            re.search(r"\b(ats|applehttpserver|apple)\b", h.get("server", "").lower()) or "apple" in h.get("via", "").lower():
        return "apple", "Apple's servers"
    if "akamai" in text or "edgecast" in text or "fastly" in text:
        return "apple", "Apple's CDN (" + ("Akamai" if "akamai" in text else "Edgecast" if "edgecast" in text else "Fastly") + ")"
    if body and re.search(r"blocked|access denied|not allowed|policy|category|filtered", body, re.I):
        return "filter", "a block page"
    return "unknown", h.get("server") or "unknown server"


def probe(url):
    url = full_url(url)
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname or not APPLE_DOMAINS.search(p.hostname):
        return {"error": "Only Apple download addresses can be checked", "url": url}
    hit = _probe_cache.get(url)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    req = urllib.request.Request(url, headers={"Range": "bytes=0-0", "User-Agent": "CacheDash/1.11 probe"})
    out = {"url": url, "host": p.hostname, "checkedAt": time.time()}
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            status, headers, body = r.status, dict(r.headers), ""
    except urllib.error.HTTPError as e:
        status, headers = e.code, dict(e.headers or {})
        try:
            body = e.read(4096).decode("utf-8", "replace")
        except Exception:
            body = ""
    except Exception as e:
        out.update({"status": None, "error": f"{type(e).__name__}: {e}", "who": "network",
                    "whoName": "the network (no HTTP answer)"})
        _probe_cache[url] = (time.time(), out)
        return out
    who, name = _who(status, headers, body)
    keep = {k: v for k, v in headers.items() if k.lower() in ("server", "via", "x-cache", "content-type", "location", "cdnuuid",
                                                             "x-apple-request-uuid", "x-squid-error", "proxy-agent", "age")}
    title = re.search(r"<title>([^<]{1,120})</title>", body or "", re.I)
    out.update({"status": status, "ms": int((time.time() - t0) * 1000), "headers": keep, "who": who, "whoName": name,
                "pageTitle": title.group(1).strip() if title else None})
    _probe_cache[url] = (time.time(), out)
    return out
