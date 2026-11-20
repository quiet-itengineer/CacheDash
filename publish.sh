#!/bin/zsh
# Put a new CacheDash release (the zip from Claude, or anyone's) into your GitHub repo.
#
#   cd ~/CacheDash-repo && zsh publish.sh ~/Downloads/CacheDash_1.17.zip
#
# Unzips it, runs the built-in tests, copies it over this clone (your own settings and
# data files are never touched or uploaded), shows what changed, commits and pushes.
# Then run  git up  on each caching Mac.
set -euo pipefail
# This script is inside the repo it updates, so run from a temporary copy (the file is replaced part-way through).
if [[ -z "${CACHEDASH_REPO:-}" ]]; then
  T=$(mktemp "${TMPDIR:-/tmp}/cachedash-publish.XXXXXX"); cp "$0" "$T"
  CACHEDASH_REPO="${0:A:h}" exec zsh "$T" "$@"
fi
REPO="$CACHEDASH_REPO"
ZIP="${1:-}"
[[ -n "$ZIP" && -f "$ZIP" ]] || { echo "Usage: zsh publish.sh path/to/CacheDash_x.y.zip"; exit 1; }
[[ -d "$REPO/.git" ]] || { echo "Run this from your git clone of CacheDash."; exit 1; }
[[ $EUID -ne 0 ]] || { echo "Run this without sudo, as yourself, so git uses your GitHub login."; exit 1; }

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
unzip -q "$ZIP" -d "$TMP"
NEW=$(dirname "$(find "$TMP" -maxdepth 3 -name cachedash.py | head -1)")
[[ -f "$NEW/cachedash.py" ]] || { echo "That zip doesn't contain CacheDash."; exit 1; }
VERSION=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$NEW/cachedash.py" | head -1)
echo "Release in the zip: CacheDash $VERSION"

git -C "$REPO" remote get-url origin >/dev/null 2>&1 || { echo "This clone has no GitHub address. Run:  git -C $REPO remote add origin https://github.com/OWNER/CacheDash.git"; exit 1; }
if [[ -n "$(git -C "$REPO" ls-remote --heads origin 2>/dev/null)" ]]; then git -C "$REPO" pull --ff-only -q origin "$(git -C "$REPO" symbolic-ref --short HEAD 2>/dev/null || echo main)"; fi
echo "Running the built-in tests…"
(cd "$NEW" && /usr/bin/python3 -m unittest discover -s tests -q 2>&1 | tail -3) || { echo "Tests failed. Nothing was published."; exit 1; }

PRIVATE=(--exclude .git --exclude '*.db' --exclude '*.db-*' --exclude mosyle.json --exclude alerts.json
         --exclude clients.csv --exclude servers.json --exclude source.json --exclude catalog-cache.json --exclude update-pin.json --exclude update-status.json --exclude update-request
         --exclude '*.log' --exclude '*.ndjson' --exclude __pycache__ --exclude .DS_Store)
rsync -rlp --delete "${PRIVATE[@]}" "$NEW/" "$REPO/"
git -C "$REPO" add -A
if git -C "$REPO" diff --cached --quiet; then echo "The repo already has this exact release. Nothing to publish."; exit 0; fi
STAGED=$(git -C "$REPO" diff --cached --name-only)
if echo "$STAGED" | grep -Eq '(^|/)(mosyle\.json|alerts\.json|clients\.csv|servers\.json|[^/]*\.db)$'; then
  git -C "$REPO" reset -q
  echo "Stopped: a settings or data file was about to be uploaded. Nothing was published."; exit 1
fi
echo "\nChanges:"; git -C "$REPO" status --short
git -C "$REPO" commit -qm "CacheDash $VERSION"
git -C "$REPO" push -q -u origin HEAD
echo "\n✅ Published CacheDash $VERSION. Now on each caching Mac run:  cd ~/CacheDash-repo && git up"
