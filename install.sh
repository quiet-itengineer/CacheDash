#!/bin/zsh
# Installs or updates CacheDash as a LaunchDaemon on a caching Mac (macOS 27+).
#
#   First install:  sudo zsh install.sh          (asks for a name, the port and the other caching Macs)
#   Change setup:   sudo zsh install.sh --setup  (asks again)
#   Update PIN:     sudo zsh install.sh --pin    (PIN for the dashboard's Update button)
#   Update:         git up                       (in a git clone; see update.sh; never asks anything)
#   Scripted:       sudo zsh install.sh "Display Name" [port] ["Other cache=http://ip:8027" ...]
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
CODE=(cachedash.py clientmon.py catalog.py alerts.py incident.py logs.py history.py appupdates.py cluster.py selfupdate.py cachestore.py dashboard.html)

SETUP=0; PIN_ONLY=0; ARGS=()
for x in "$@"; do
  case "$x" in --setup) SETUP=1 ;; --pin) PIN_ONLY=1 ;; *) ARGS+=("$x") ;; esac
done
set -- "${ARGS[@]}"

if [[ $EUID -ne 0 ]]; then echo "Run with sudo."; exit 1; fi
if ! "$PY" -c 'import sys; assert sys.version_info >= (3,9)' 2>/dev/null; then
  echo "Python 3.9+ not available. Install the Command Line Tools first:  xcode-select --install"
  exit 1
fi
for f in $CODE; do [[ -f "$SRC/$f" ]] || { echo "Missing $f next to install.sh. Is this the whole CacheDash folder?"; exit 1; }; done

ask_pin() {   # sets the PIN for the dashboard's Update button; Return = no PIN (button off)
  local p1 p2
  while true; do
    print -n "PIN for the dashboard's Update button (4 or more characters; Return to leave the button off): "; read -rs p1 || p1=""; echo
    [[ -z "$p1" ]] && { "$PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); import selfupdate; selfupdate.set_pin_file(sys.argv[2], "")' "$SRC" "$DEST/update-pin.json"; echo "  The Update button is off on this Mac."; return 0; }
    (( ${#p1} >= 4 )) || { echo "  Use at least 4 characters."; continue; }
    print -n "Type it again: "; read -rs p2 || p2=""; echo
    [[ "$p1" == "$p2" ]] || { echo "  They didn't match. Try again."; continue; }
    print -rn -- "$p1" | "$PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); import selfupdate; selfupdate.set_pin_file(sys.argv[2], sys.stdin.read())' "$SRC" "$DEST/update-pin.json"
    echo "  ✓ PIN saved. Use the same PIN on every caching Mac, so one click updates them all."; return 0
  done
}
if (( PIN_ONLY )); then
  [[ -t 0 ]] || { echo "Run this in Terminal: it asks you to type the PIN."; exit 1; }
  mkdir -p "$DEST"; ask_pin; exit 0
fi

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

# --- questions (only when someone is at the keyboard, and never during `git up`) -----------
INTERACTIVE=0
[[ -t 0 && -t 1 && -z "${CACHEDASH_UPDATE:-}" ]] && INTERACTIVE=1
yesno() {   # yesno "question" default(y|n)
  local ans; print -n -- "$1 "; read -r ans || ans=""; ans="${ans:-$2}"; [[ "$ans" == [Yy]* ]]
}
CUR=$("$PY" - "$OLD_PLIST" <<'EOPY'
import plistlib, sys
a = []
try:
    a = plistlib.load(open(sys.argv[1], "rb")).get("ProgramArguments", []) if sys.argv[1] else []
except Exception:
    pass
val = lambda f: a[a.index(f) + 1] if f in a and a.index(f) + 1 < len(a) else ""
print(f"{val('--name')}\t{val('--port')}")
EOPY
)
OLD_NAME="${CUR%%$'\t'*}"; OLD_PORT="${CUR#*$'\t'}"
NAME_IN="${1:-}"; PORT_IN="${2:-}"
if (( INTERACTIVE && $# == 0 )) && [[ -z "$OLD_PLIST" || $SETUP == 1 ]]; then
  echo "\nSetting up CacheDash $NEW_VERSION on this Mac. Press Return to accept the [suggestion].\n"
  DEF_NAME="${OLD_NAME:-$(scutil --get ComputerName 2>/dev/null || hostname -s)}"
  print -n "Name for this cache on the dashboard [$DEF_NAME]: "; read -r NAME_IN || true; NAME_IN="${NAME_IN:-$DEF_NAME}"
  while true; do
    print -n "Port for the dashboard [${OLD_PORT:-8027}]: "; read -r PORT_IN || true; PORT_IN="${PORT_IN:-${OLD_PORT:-8027}}"
    [[ "$PORT_IN" == <1-65535> ]] && break; echo "  Enter a number from 1 to 65535."
  done
fi
PORT_EFF="${PORT_IN:-${OLD_PORT:-8027}}"

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

# --- the other caching Macs on this dashboard ------------------------------------------------
C_NAMES=(); C_URLS=(); CHANGED=0
add_cache() {   # add_cache name url
  (( ${C_URLS[(Ie)$2]} )) && return 0
  C_NAMES+=("$1"); C_URLS+=("$2"); CHANGED=1
}
if [[ -f "$DEST/servers.json" ]]; then
  while IFS=$'\x1f' read -r -u 3 n u; do [[ -n "$u" ]] && { C_NAMES+=("$n"); C_URLS+=("$u"); }; done 3< <(
    "$PY" -c 'import json, sys
for x in json.load(open(sys.argv[1])):
    if isinstance(x, dict) and x.get("url"): print(x.get("name") or "", x["url"], sep="\x1f")' "$DEST/servers.json" 2>/dev/null)
fi
if (( $# > 2 )); then                                   # given on the command line: "Name=URL" ...
  C_NAMES=(); C_URLS=(); CHANGED=1
  for arg in "${@:3}"; do
    n="${arg%%=*}"; u="${arg#*=}"; [[ "$arg" == *=* ]] || { n=""; u="$arg"; }
    u=$("$PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); import cluster; print(cluster.normalize(sys.argv[2], int(sys.argv[3])) or "")' "$SRC" "$u" "$PORT_EFF")
    [[ -n "$u" ]] && add_cache "$n" "$u"
  done
elif [[ -f "$SRC/servers.json" ]]; then
  cp "$SRC/servers.json" "$DEST/"
elif (( INTERACTIVE )) && [[ -z "$OLD_PLIST" || $SETUP == 1 || ! -f "$DEST/servers.json" ]]; then
  echo ""
  if (( ${#C_URLS} )); then
    echo "This dashboard already shows:"; for i in {1..${#C_URLS}}; do echo "  • ${C_NAMES[$i]:-(no name)}  ${C_URLS[$i]}"; done
    yesno "Keep these? [Y/n]" y || { C_NAMES=(); C_URLS=(); CHANGED=1; }
  fi
  echo "Looking for other caching Macs running CacheDash…"
  NFOUND=0
  while IFS=$'\x1f' read -r -u 3 n u v; do
    [[ -z "$u" ]] && continue; (( ${C_URLS[(Ie)$u]} )) && continue; NFOUND=1
    yesno "  Found ${n:-a caching Mac} at $u (CacheDash $v). Show it on this dashboard? [Y/n]" y && add_cache "$n" "$u"
  done 3< <("$PY" "$SRC/cachedash.py" --find-caches --port "$PORT_EFF" 2>/dev/null)
  (( NFOUND )) || echo "  None found automatically. (This finds Macs that are this cache's peers or parents.)"
  while true; do
    print -n "Address of another caching Mac to add (for example 10.0.1.16), or Return to finish: "
    read -r ADDR || ADDR=""
    [[ -z "$ADDR" ]] && break
    IFS=$'\x1f' read -r st u n v <<< "$("$PY" "$SRC/cachedash.py" --probe "$ADDR" --port "$PORT_EFF" 2>/dev/null)"
    case "$st" in
      ok)   echo "  ✓ ${n:-CacheDash} at $u (CacheDash $v)"; add_cache "$n" "$u" ;;
      fail) echo "  ✗ Couldn't reach CacheDash at $u: $n"
            if yesno "  Add it anyway? Choose this if CacheDash isn't installed there yet. [y/N]" n; then
              print -n "  Name for it: "; read -r NM || NM=""; add_cache "$NM" "$u"
            fi ;;
      *)    echo "  That doesn't look like an address. Type an IP address or host name." ;;
    esac
  done
fi
if (( CHANGED )); then
  "$PY" - "${C_NAMES[@]}" -- "${C_URLS[@]}" > "$DEST/servers.json" <<'EOPY'
import json, sys
a = sys.argv[1:]; i = a.index("--"); names, urls = a[:i], a[i + 1:]
print(json.dumps([{"name": n, "url": u} for n, u in zip(names, urls)], indent=2))
EOPY
fi
if (( ${#C_URLS} )); then
  SHOWN=(); for i in {1..${#C_URLS}}; do SHOWN+=("${C_NAMES[$i]:-${C_URLS[$i]}}"); done
  echo "Other caching Macs on this dashboard: ${(j:, :)SHOWN}"
  (( INTERACTIVE && ! CHANGED )) && [[ -n "$OLD_PLIST" ]] && echo "  (To change them: sudo zsh install.sh --setup)"
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

  # The dashboard's Update button: a separate service that launchd starts when the button drops a request file.
  cp "$SRC/updater.sh" "$DEST/"; chmod 755 "$DEST/updater.sh"
  UPLIST="/Library/LaunchDaemons/local.cachedash.updater.plist"
  if [[ -z "${CACHEDASH_FROM_UPDATER:-}" || ! -f "$UPLIST" ]]; then
    "$PY" - "$UPLIST" "$DEST" <<'EOPY'
import plistlib, sys
plist, dest = sys.argv[1:3]
with open(plist, "wb") as f:
    plistlib.dump({"Label": "local.cachedash.updater", "ProgramArguments": ["/bin/zsh", f"{dest}/updater.sh"],
                   "WatchPaths": [f"{dest}/update-request"], "RunAtLoad": False,
                   "StandardOutPath": "/var/log/cachedash-update.log", "StandardErrorPath": "/var/log/cachedash-update.log"}, f)
EOPY
    chmod 644 "$UPLIST"
    [[ -z "${CACHEDASH_FROM_UPDATER:-}" ]] && { launchctl bootout system "$UPLIST" 2>/dev/null || true; launchctl bootstrap system "$UPLIST" 2>/dev/null || true; }
  fi
  if (( INTERACTIVE )); then
    if [[ ! -f "$DEST/update-pin.json" ]]; then
      echo "\nThe dashboard can update CacheDash with one click when you publish a new version."; ask_pin
    elif (( SETUP )); then
      yesno "Change the PIN for the dashboard's Update button? [y/N]" n && ask_pin
    fi
  fi
fi

# --- the service: reuse the earlier settings unless new ones were given ---------------------
"$PY" - "$OLD_PLIST" "$PLIST" "$PY" "$DEST/cachedash.py" "$NAME_IN" "$PORT_IN" "${CACHEDASH_ALLOW-__keep__}" "$(scutil --get ComputerName 2>/dev/null || hostname -s)" <<'EOPY'
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
  # Tell the other caching Macs about this one, so you don't have to add it there too.
  if (( CHANGED || ! ${#OLD_VERSION} )) && (( ${#C_URLS} )); then
    for i in {1..${#C_URLS}}; do
      IFS=$'\x1f' read -r st a b <<< "$("$PY" "$DEST/cachedash.py" --join "${C_URLS[$i]}" --port "$PORT" 2>/dev/null)"
      if [[ "$st" == ok ]]; then echo "  ✓ ${a} now shows this Mac too."
      else echo "  – ${C_NAMES[$i]:-${C_URLS[$i]}} wasn't told about this Mac: $b"; fi
    done
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
