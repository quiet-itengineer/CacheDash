"""
Saved dashboard views: named widget layouts kept on the caching Mac (views.json), so the
same screens are available from any computer's browser.

A view holds only layout: which widgets are on, their order and sizes, and a few display
options. It never holds server addresses or anything about devices. One view can be the
default, which a browser that has never opened the dashboard starts with.
"""

import json
import os
import re
import threading
import time

MAX_VIEWS = 50
MAX_NAME = 60
ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")      # widget ids, e.g. "cl_live"
LAYOUT_KEYS = {"order", "enabled", "sizes", "stack", "dense", "heatRange", "hours", "bucket"}


def clean_layout(lay):
    """Keep only known layout fields with sane values; raise ValueError on anything odd."""
    if not isinstance(lay, dict):
        raise ValueError("layout must be an object")
    out = {}
    for k in ("order", "enabled"):
        v = lay.get(k)
        if v is not None:
            if not isinstance(v, list) or len(v) > 200 or not all(isinstance(x, str) and ID_RE.match(x) for x in v):
                raise ValueError(f"bad {k}")
            out[k] = list(dict.fromkeys(v))
    if "sizes" in lay:
        v = lay["sizes"]
        if not isinstance(v, dict) or len(v) > 200 or not all(ID_RE.match(k) and s in ("s", "m", "l") for k, s in v.items()):
            raise ValueError("bad sizes")
        out["sizes"] = dict(v)
    for k in ("stack", "dense"):
        if k in lay:
            out[k] = bool(lay[k])
    if "heatRange" in lay:
        if lay["heatRange"] not in ("today", "7d", "30d", "range"):
            raise ValueError("bad heatRange")
        out["heatRange"] = lay["heatRange"]
    if "hours" in lay:
        h = float(lay["hours"])
        if not 0.25 <= h <= 744:
            raise ValueError("bad hours")
        out["hours"] = h
    if "bucket" in lay:
        b = lay["bucket"]
        if b != "auto" and not (isinstance(b, (int, float)) and 60 <= b <= 86400):
            raise ValueError("bad bucket")
        out["bucket"] = b
    if not out.get("enabled"):
        raise ValueError("a view needs at least one widget")
    return out


class Views:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    def _read(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("views"), list):
                return d
        except (OSError, ValueError):
            pass
        return {"views": [], "default": None}

    def _write(self, d):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=1)
        os.replace(tmp, self.path)

    def list(self):
        with self.lock:
            return self._read()

    def save(self, name, layout, make_default=None):
        name = re.sub(r"\s+", " ", str(name or "")).strip()[:MAX_NAME]
        if not name:
            raise ValueError("Give the view a name.")
        layout = clean_layout(layout)
        with self.lock:
            d = self._read()
            v = next((x for x in d["views"] if x["name"].lower() == name.lower()), None)
            if v:
                v.update({"name": name, "layout": layout, "updated": time.time()})
            else:
                if len(d["views"]) >= MAX_VIEWS:
                    raise ValueError(f"There are already {MAX_VIEWS} saved views. Delete one first.")
                d["views"].append({"name": name, "layout": layout, "created": time.time(), "updated": time.time()})
            if make_default is True:
                d["default"] = name
            elif make_default is False and (d.get("default") or "").lower() == name.lower():
                d["default"] = None
            self._write(d)
            return d

    def delete(self, name):
        with self.lock:
            d = self._read()
            d["views"] = [x for x in d["views"] if x["name"].lower() != str(name).lower()]
            if (d.get("default") or "").lower() == str(name).lower():
                d["default"] = None
            self._write(d)
            return d

    def rename(self, old, new):
        new = re.sub(r"\s+", " ", str(new or "")).strip()[:MAX_NAME]
        if not new:
            raise ValueError("Give the view a name.")
        with self.lock:
            d = self._read()
            if any(x["name"].lower() == new.lower() for x in d["views"]) and new.lower() != str(old).lower():
                raise ValueError("A view with that name already exists.")
            for x in d["views"]:
                if x["name"].lower() == str(old).lower():
                    x["name"], x["updated"] = new, time.time()
            if (d.get("default") or "").lower() == str(old).lower():
                d["default"] = new
            self._write(d)
            return d

    def set_default(self, name):
        with self.lock:
            d = self._read()
            if name and not any(x["name"].lower() == str(name).lower() for x in d["views"]):
                raise ValueError("No view with that name.")
            d["default"] = next((x["name"] for x in d["views"] if name and x["name"].lower() == str(name).lower()), None)
            self._write(d)
            return d
