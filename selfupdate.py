"""
CacheDash's Update button: install the newest version from your GitHub repo, from the dashboard.

How it stays safe:
  * It only ever runs `git up` on the git clone CacheDash was installed from (source.json):
    the code comes from your repository, never from the person clicking.
  * It needs a PIN, set on the caching Mac with  sudo zsh install.sh --pin. Only a salted
    PBKDF2 hash is stored (update-pin.json, readable by root only). Five wrong PINs lock the
    button for 10 minutes.
  * The work is done by a separate LaunchDaemon (local.cachedash.updater) that launchd
    starts when the dashboard drops a request file, so the update survives CacheDash
    restarting. It runs update.sh: pull, built-in tests, install, start-up check, and
    automatic rollback if the new version doesn't start.
"""

import hashlib
import hmac
import json
import os
import secrets
import time

ITERATIONS = 200_000
UPDATER_PLIST = "/Library/LaunchDaemons/local.cachedash.updater.plist"


def hash_pin(pin, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), ITERATIONS).hex()
    return {"salt": salt, "hash": h, "iterations": ITERATIONS, "set": time.time()}


def check_pin(pin, stored):
    if not stored or not isinstance(pin, str) or not pin:
        return False
    try:
        h = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(stored["salt"]),
                                int(stored.get("iterations", ITERATIONS))).hex()
    except (KeyError, ValueError):
        return False
    return hmac.compare_digest(h, stored.get("hash", ""))


def set_pin_file(path, pin):
    """Write the PIN hash (used by install.sh). An empty PIN removes it, which turns the button off."""
    if not pin:
        if os.path.exists(path):
            os.remove(path)
        return
    if len(pin) < 4:
        raise ValueError("Use at least 4 characters.")
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(hash_pin(pin), f)
    os.replace(tmp, path)


class Updater:
    def __init__(self, here, version, source):
        self.here, self.version, self.source = here, version, source or {}
        self.pin_path = os.path.join(here, "update-pin.json")
        self.request_path = os.path.join(here, "update-request")
        self.status_path = os.path.join(here, "update-status.json")
        self.fails = []          # times of wrong PINs

    def _pin(self):
        try:
            with open(self.pin_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def status(self):
        try:
            with open(self.status_path) as f:
                st = json.load(f)
        except (OSError, ValueError):
            st = {}
        repo = self.source.get("path")
        ready, why = True, None
        if not repo or not os.path.isdir(os.path.join(repo, ".git")):
            ready, why = False, "CacheDash wasn't installed from a git clone, so there's nothing to update from. Install it from a clone (see the README)."
        elif not os.path.exists(UPDATER_PLIST):
            ready, why = False, "The updater isn't installed yet. Run  sudo zsh install.sh  once from the clone."
        elif not self._pin():
            ready, why = False, "No update PIN is set on this Mac. Set one with  sudo zsh install.sh --pin"
        return {**st, "version": self.version, "ready": ready, "why": why, "pinSet": bool(self._pin())}

    def request(self, pin, who=""):
        now = time.time()
        self.fails = [t for t in self.fails if now - t < 600]
        if len(self.fails) >= 5:
            return 429, {"error": "Too many wrong PINs. Try again in 10 minutes."}
        st = self.status()
        if not st["ready"]:
            return 409, {"error": st["why"]}
        if not check_pin(pin, self._pin()):
            self.fails.append(now)
            return 403, {"error": "Wrong PIN."}
        if st.get("state") in ("queued", "running") and now - (st.get("started") or st.get("requested") or 0) < 900:
            return 200, {"ok": True, "already": True, **st}
        status = {"state": "queued", "requested": now, "from": self.version, "by": who}
        with open(self.status_path, "w") as f:
            json.dump(status, f)
        with open(self.request_path, "w") as f:      # launchd watches this file and starts the updater
            f.write(str(now))
        return 200, {"ok": True, **status}
