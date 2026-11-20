#!/bin/zsh
# Started by launchd (local.cachedash.updater) when the dashboard's Update button drops
# /usr/local/cachedash/update-request. Runs `git up` on the clone CacheDash was installed
# from, as that clone's owner, and records progress in update-status.json.
# Output goes to /var/log/cachedash-update.log.
DEST="/usr/local/cachedash"
PY="/usr/bin/python3"
REQ="$DEST/update-request"
STATUS="$DEST/update-status.json"
[[ -f "$REQ" ]] || exit 0
rm -f "$REQ"

status() {   # status key=value ... (merged into update-status.json)
  "$PY" - "$STATUS" "$@" <<'EOPY'
import json, sys, time
p = sys.argv[1]
try:
    st = json.load(open(p))
except Exception:
    st = {}
for kv in sys.argv[2:]:
    k, _, v = kv.partition("=")
    st[k] = time.time() if v == "@now" else v
json.dump(st, open(p, "w"))
EOPY
}

REPO=$("$PY" -c 'import json; print(json.load(open("/usr/local/cachedash/source.json")).get("path",""))' 2>/dev/null)
if [[ -z "$REPO" || ! -d "$REPO/.git" ]]; then
  status state=failed finished=@now error="CacheDash wasn't installed from a git clone."
  exit 1
fi
OWNER=$("$PY" -c 'import os, pwd, sys; print(pwd.getpwuid(os.stat(sys.argv[1]).st_uid).pw_name)' "$REPO")
FROM=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$DEST/cachedash.py" | head -1)
status state=running started=@now from="$FROM"
echo "=== $(date) update requested from the dashboard ==="

SUDO_USER="$OWNER" CACHEDASH_FROM_UPDATER=1 zsh "$REPO/update.sh" < /dev/null 2>&1 | tee /tmp/.cachedash-update.out
RC=${pipestatus[1]}
TO=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$DEST/cachedash.py" | head -1)
TAIL=$(tail -12 /tmp/.cachedash-update.out | sed 's/\x1b\[[0-9;]*m//g'); rm -f /tmp/.cachedash-update.out
if (( RC == 0 )); then
  if grep -q "already installed" <<< "$TAIL"; then status state=uptodate finished=@now to="$TO" log="$TAIL"
  else status state=done finished=@now to="$TO" log="$TAIL"; fi
else
  status state=failed finished=@now to="$TO" log="$TAIL" error="The update didn't complete. CacheDash $TO is running."
fi
exit $RC
