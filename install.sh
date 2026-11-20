#!/bin/zsh
# Installs or updates CacheDash as a LaunchDaemon on a caching Mac (macOS 27+).
#
#   First install:  sudo zsh install.sh ["Display Name"] [port] ["Other cache=http://ip:8027" ...]
#   Update:         sudo zsh install.sh          (or `git up` in a git clone; see update.sh)
#
# With no arguments, an existing install keeps its name, port, allowed networks, other
# caches and any extra options, so updating never needs the original command again.
# Your own files in /usr/local/cachedash (servers.json, alerts.json, mosyle.json,
# clients.csv and the databases) are never overwritten unless you put new ones next to
# this script. The previous version is kept, and is put back automatically if the new
# one doesn't start.
set -euo pipefail

SRC="${0:A:h}"
DEST="/usr/local/cachedash"
PLIST="/Library/LaunchDaemons/local.cachedash.plist"
PY="/usr/bin/python3"
CODE=(cachedash.py clientmon.py catalog.py alerts.py incident.py logs.py history.py appupdates.py dashboard.html)

if [[ $EUID -ne 0 ]]; then echo "Run with sudo."; exit 1; fi
if ! "$PY" -c 'import sys; assert sys.version_info >= (3,9)' 2>/dev/null; then
  echo "Python 3.9+ not available. Install the Command Line Tools first:  xcode-select --install"
  exit 1
fi
for f in $CODE; do [[ -f "$SRC/$f" ]] || { echo "Missing $f next to install.sh. Is this the whole CacheDash folder?"; exit 1; }; done

# The service file from an earlier install (any label that runs our cachedash.py).
OLD_PLIST=""
[[ -f "$PLIST" ]] && OLD_PLIST="$PLIST"
if [[ -z "$OLD_PLIST" ]]; then
  for P in /Library/LaunchDaemons/*.plist(N); do
    grep -q "$DEST/cachedash.py" "$P" 2>/dev/null && { OLD_PLIST="$P"; break; }
  done
fi

OLD_VERSION=""
[[ -f "$DEST/cachedash.py" ]] && OLD_VERSION=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$DEST/cachedash.py" | head -1)
NEW_VERSION=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$SRC/cachedash.py" | head -1)

# --- keep a copy of what's installed now, for rollback -------------------------------
if [[ -n "$OLD_VERSION" ]]; then
  rm -rf "$DEST/.previous"; mkdir -p "$DEST/.previous"
  for f in $CODE; do [[ -f "$DEST/$f" ]] && cp -p "$DEST/$f" "$DEST/.previous/"; done
  [[ -n "$OLD_PLIST" ]] && cp -p "$OLD_PLIST" "$DEST/.previous/service.plist"
fi

# --- copy the program ------------------------------------------------------------------
mkdir -p "$DEST"
for f in $CODE; do cp "$SRC/$f" "$DEST/"; done
chmod 755 "$DEST" "$DEST/cachedash.py"
for f in $CODE; do [[ "$f" == cachedash.py ]] || chmod 644 "$DEST/$f"; done

# Your settings: only replaced when you give new ones.
if (( $# > 2 )); then
  "$PY" - "${@:3}" > "$DEST/servers.json" <<'EOPY'
import json, sys
out = []
for arg in sys.argv[1:]:
    name, _, url = arg.partition("=")
    if not url:
        name, url = "", name
    out.append({"name": name.strip(), "url": url.strip().rstrip("/")})
print(json.dumps(out, indent=2))
EOPY
  echo "Other caches on this dashboard:"; cat "$DEST/servers.json"
elif [[ -f "$SRC/servers.json" ]]; then
  cp "$SRC/servers.json" "$DEST/"
fi
[[ -f "$SRC/clients.csv" ]] && cp "$SRC/clients.csv" "$DEST/"
[[ -f "$SRC/mosyle.json" ]] && cp "$SRC/mosyle.json" "$DEST/"
[[ -f "$SRC/alerts.json" ]] && cp "$SRC/alerts.json" "$DEST/"
for f in mosyle.json alerts.json; do [[ -f "$DEST/$f" ]] && chmod 600 "$DEST/$f"; done

# --- where updates come from (used by `git up` and the dashboard's update notice) --------
if [[ -d "$SRC/.git" ]]; then
  G=(git -C "$SRC")
  [[ -n "${SUDO_USER:-}" ]] && G=(sudo -u "$SUDO_USER" git -C "$SRC")   # git refuses repos owned by someone else when run as root
  REPO=$("${G[@]}" remote get-url origin 2>/dev/null || true)
  BRANCH=$("${G[@]}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)
  COMMIT=$("${G[@]}" rev-parse --short HEAD 2>/dev/null || true)
  "$PY" -c 'import json,sys; print(json.dumps(dict(zip(["repo","branch","commit","path"], sys.argv[1:]))))' \
      "${REPO//*@/https://}" "$BRANCH" "$COMMIT" "$SRC" > "$DEST/source.json"
  # `git up` in this folder = pull the latest version and install it
  "${G[@]}" config alias.up '!sudo zsh ./update.sh' 2>/dev/null && echo "Set up \`git up\` in $SRC"
fi

# --- the service: reuse the earlier settings unless new ones were given ---------------------
"$PY" - "$OLD_PLIST" "$PLIST" "$PY" "$DEST/cachedash.py" "${1:-}" "${2:-}" "${CACHEDASH_ALLOW-__keep__}" "$(scutil --get ComputerName 2>/dev/null || hostname -s)" <<'EOPY'
import plistlib, re, sys
old, new, py, script, name, port, allow, computer = sys.argv[1:9]
args = []
if old:
    try:
        with open(old, "rb") as f:
            a = plistlib.load(f).get("ProgramArguments", [])
        args = a[a.index(next(x for x in a if x.endswith("cachedash.py"))) + 1:]
    except Exception as e:
        print(f"Couldn't read the old service settings ({e}); starting fresh.")
def take(flag):                      # remove every "flag value" pair, return the values
    vals, out, i = [], [], 0
    while i < len(args):
        if args[i] == flag and i + 1 < len(args):
            vals.append(args[i + 1]); i += 2
        else:
            out.append(args[i]); i += 1
    args[:] = out
    return vals
old_name, old_port, old_allow = take("--name"), take("--port"), take("--allow")
name = name or (old_name[-1] if old_name else computer)
port = port or (old_port[-1] if old_port else "8027")
if allow == "__keep__":
    nets = old_allow
else:
    nets = [n for n in allow.split() if re.fullmatch(r"[0-9A-Fa-f:.]+/\d+", n)]
    for n in allow.split():
        if n not in nets:
            print(f"Ignoring CACHEDASH_ALLOW entry: {n}")
prog = [py, script, "--port", port, "--name", name] + [x for n in nets for x in ("--allow", n)] + args
with open(new, "wb") as f:
    plistlib.dump({"Label": "local.cachedash", "ProgramArguments": prog, "RunAtLoad": True, "KeepAlive": True,
                   "StandardOutPath": "/var/log/cachedash.log", "StandardErrorPath": "/var/log/cachedash.log"}, f)
print(f"Name: {name} · port {port}" + (f" · allowed: {' '.join(nets)}" if nets else "") + (f" · options: {' '.join(args)}" if args else ""))
with open("/tmp/.cachedash-port", "w") as f:
    f.write(port)
EOPY
PORT=$(cat /tmp/.cachedash-port); rm -f /tmp/.cachedash-port
chmod 644 "$PLIST"

# Stop any CacheDash service installed earlier under a different label, so two copies don't fight over the port.
for OLD in /Library/LaunchDaemons/*.plist(N); do
  [[ "$OLD" == "$PLIST" ]] && continue
  if grep -q "$DEST/cachedash.py" "$OLD" 2>/dev/null; then
    echo "Replacing older CacheDash service: $OLD"
    launchctl bootout system "$OLD" 2>/dev/null || true
    rm -f "$OLD"
  fi
done

start() { launchctl bootout system "$PLIST" 2>/dev/null || true; sleep 1; launchctl bootstrap system "$PLIST"; }
healthy() {   # the new server answers with the version we just installed
  for i in {1..30}; do
    V=$(curl -s -m 3 "http://127.0.0.1:$PORT/api/info" 2>/dev/null | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("version",""))' 2>/dev/null || true)
    [[ "$V" == "$1" ]] && return 0
    sleep 1
  done
  return 1
}

start
if healthy "$NEW_VERSION"; then
  IP=$(ipconfig getifaddr en0 2>/dev/null || echo "<this-mac>")
  if [[ -n "$OLD_VERSION" && "$OLD_VERSION" != "$NEW_VERSION" ]]; then
    echo "\n✅ Updated CacheDash $OLD_VERSION → $NEW_VERSION.  http://$IP:$PORT/"
  else
    echo "\n✅ CacheDash $NEW_VERSION is running.  http://$IP:$PORT/"
  fi
  "$PY" "$DEST/cachedash.py" --check >/dev/null 2>&1 || echo "Note: Metrics.db can't be read yet. Run  sudo $PY $DEST/cachedash.py --check  to see why."
  exit 0
fi

echo "\n❌ CacheDash $NEW_VERSION didn't start. Last lines of /var/log/cachedash.log:"
tail -15 /var/log/cachedash.log 2>/dev/null || true
if [[ -d "$DEST/.previous" && -n "$OLD_VERSION" ]]; then
  echo "\nPutting back version $OLD_VERSION…"
  cp -p "$DEST/.previous/"*.py "$DEST/.previous/dashboard.html" "$DEST/" 2>/dev/null || true
  [[ -f "$DEST/.previous/service.plist" ]] && cp -p "$DEST/.previous/service.plist" "$PLIST"
  start
  if healthy "$OLD_VERSION"; then echo "Version $OLD_VERSION is running again. Nothing else changed."
  else echo "The previous version didn't start either. Check /var/log/cachedash.log."; fi
fi
exit 1
