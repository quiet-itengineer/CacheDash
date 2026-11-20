#!/bin/zsh
# Update CacheDash on this caching Mac from its git clone.
#
#   cd ~/CacheDash-repo && git up          (install.sh sets up the `git up` shortcut)
#   sudo zsh update.sh                     (the same thing, spelled out)
#   sudo zsh update.sh --no-pull           (install what's in the folder now, e.g. a branch you checked out)
#
# Steps: download the latest version (as you, with your GitHub login), run the built-in
# tests, then install it keeping every setting, and check it starts. If the new version
# doesn't start, the previous one is put back automatically.
set -euo pipefail
if [[ $EUID -ne 0 ]]; then exec sudo zsh "$0" "$@"; fi
# `git pull` replaces this file part-way through, so run from a temporary copy.
if [[ -z "${CACHEDASH_SRC:-}" ]]; then
  T=$(mktemp "${TMPDIR:-/tmp}/cachedash-update.XXXXXX"); cp "$0" "$T"
  CACHEDASH_SRC="${0:A:h}" exec zsh "$T" "$@"
fi
SRC="$CACHEDASH_SRC"
PY="/usr/bin/python3"
ME="${SUDO_USER:-root}"
as_me() { if [[ "$ME" != root ]]; then sudo -u "$ME" "$@"; else "$@"; fi; }

if [[ "${1:-}" != "--no-pull" ]]; then
  [[ -d "$SRC/.git" ]] || { echo "This folder isn't a git clone, so there's nothing to pull. Use install.sh instead."; exit 1; }
  BEFORE=$(as_me git -C "$SRC" rev-parse --short HEAD)
  echo "Getting the latest CacheDash…"
  if ! as_me git -C "$SRC" pull --ff-only; then
    echo "\nCouldn't update the folder. If you changed files here, run  git -C $SRC status  to see them,"
    echo "then  git -C $SRC stash  (keeps your changes aside) and run  git up  again."
    exit 1
  fi
  AFTER=$(as_me git -C "$SRC" rev-parse --short HEAD)
  if [[ "$BEFORE" == "$AFTER" ]]; then
    echo "Already up to date ($AFTER)."
  else
    echo "\nWhat's new:"; as_me git -C "$SRC" --no-pager log --oneline "$BEFORE..$AFTER" | head -20
  fi
fi

INSTALLED=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' /usr/local/cachedash/cachedash.py 2>/dev/null | head -1 || true)
NEW=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$SRC/cachedash.py" | head -1)
SAME=1
for f in cachedash.py clientmon.py catalog.py alerts.py incident.py logs.py history.py appupdates.py cluster.py selfupdate.py updater.sh dashboard.html; do
  cmp -s "$SRC/$f" "/usr/local/cachedash/$f" || SAME=0
done
if (( SAME )) && [[ "${FORCE:-}" != 1 ]]; then
  echo "CacheDash $NEW is already installed and running this exact code. (Run  sudo FORCE=1 zsh update.sh  to reinstall anyway.)"
  exit 0
fi

echo "\nChecking the new version (built-in tests)…"
if [[ -d "$SRC/tests" ]]; then
  if ! (cd "$SRC" && as_me "$PY" -m unittest discover -s tests -q 2>&1 | tail -4); then
    echo "Tests failed, so nothing was installed. Your current version ($INSTALLED) is still running."
    exit 1
  fi
fi

echo "\nInstalling…"
CACHEDASH_UPDATE=1 exec zsh "$SRC/install.sh"
