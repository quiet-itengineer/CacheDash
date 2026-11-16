#!/bin/zsh
# Installs CacheDash as a LaunchDaemon on a caching Mac (macOS 27+).
# Usage:  sudo zsh install.sh ["Display Name"] [port] ["Other cache=http://ip:8027" ...]
# Any "Name=URL" arguments after the port list the other caching Macs, so every
# browser that opens this dashboard shows all of them automatically.
set -euo pipefail

NAME="${1:-$(scutil --get ComputerName)}"
PORT="${2:-8027}"
DEST="/usr/local/cachedash"
PLIST="/Library/LaunchDaemons/local.cachedash.plist"
PY="/usr/bin/python3"

if [[ $EUID -ne 0 ]]; then echo "Run with sudo."; exit 1; fi
if ! "$PY" -c 'import sys; assert sys.version_info >= (3,9)' 2>/dev/null; then
  echo "Python 3.9+ not available. Install the Command Line Tools first:  xcode-select --install"
  exit 1
fi

mkdir -p "$DEST"
cp "$(dirname "$0")/cachedash.py" "$(dirname "$0")/clientmon.py" "$(dirname "$0")/catalog.py" "$(dirname "$0")/alerts.py" "$(dirname "$0")/incident.py" "$(dirname "$0")/logs.py" "$(dirname "$0")/history.py" "$(dirname "$0")/appupdates.py" "$(dirname "$0")/dashboard.html" "$DEST/"
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
elif [[ -f "$(dirname "$0")/servers.json" ]]; then
  cp "$(dirname "$0")/servers.json" "$DEST/"
fi
[[ -f "$(dirname "$0")/clients.csv" ]] && cp "$(dirname "$0")/clients.csv" "$DEST/"
[[ -f "$(dirname "$0")/mosyle.json" ]] && cp "$(dirname "$0")/mosyle.json" "$DEST/" && chmod 600 "$DEST/mosyle.json"
[[ -f "$(dirname "$0")/alerts.json" ]] && cp "$(dirname "$0")/alerts.json" "$DEST/" && chmod 600 "$DEST/alerts.json"
chmod 755 "$DEST"; chmod 644 "$DEST/dashboard.html"; chmod 755 "$DEST/cachedash.py"; chmod 644 "$DEST/clientmon.py" "$DEST/catalog.py" "$DEST/alerts.py" "$DEST/incident.py" "$DEST/logs.py" "$DEST/history.py" "$DEST/appupdates.py"

# Optional: limit who can open the dashboard, e.g.  sudo CACHEDASH_ALLOW="10.0.5.0/24 10.0.9.0/24" zsh install.sh
ALLOW_ARGS=""
for NET in ${=CACHEDASH_ALLOW:-}; do
  [[ "$NET" =~ '^[0-9A-Fa-f:.]+/[0-9]+$' ]] || { echo "Ignoring CACHEDASH_ALLOW entry: $NET"; continue; }
  ALLOW_ARGS+="    <string>--allow</string><string>$NET</string>
"
done

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>local.cachedash</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$DEST/cachedash.py</string>
    <string>--port</string><string>$PORT</string>
    <string>--name</string><string>$NAME</string>
$ALLOW_ARGS  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/var/log/cachedash.log</string>
  <key>StandardErrorPath</key><string>/var/log/cachedash.log</string>
</dict>
</plist>
EOF
chmod 644 "$PLIST"

# Stop any CacheDash service installed earlier under a different label, so two copies don't fight over the port.
for OLD in /Library/LaunchDaemons/*.plist; do
  [[ "$OLD" == "$PLIST" ]] && continue
  if grep -q "$DEST/cachedash.py" "$OLD" 2>/dev/null; then
    echo "Replacing older CacheDash service: $OLD"
    launchctl bootout system "$OLD" 2>/dev/null || true
    rm -f "$OLD"
  fi
done

launchctl bootout system "$PLIST" 2>/dev/null || true
launchctl bootstrap system "$PLIST"

echo "Checking the metrics database…"
"$PY" "$DEST/cachedash.py" --check | head -6 || true
IP=$(ipconfig getifaddr en0 2>/dev/null || echo "<this-mac>")
echo "\nCacheDash is running:  http://$IP:$PORT/"
