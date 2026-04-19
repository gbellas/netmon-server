#!/bin/bash
# Renders launchd/com.gbellas.netmon.plist by replacing __HOME__ with
# the current user's home, copies the result to ~/Library/LaunchAgents,
# then (re)loads it. Safe to re-run — idempotent.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$SRC_DIR/launchd/com.gbellas.netmon.plist"
TARGET="$HOME/Library/LaunchAgents/com.gbellas.netmon.plist"

if [ ! -f "$TEMPLATE" ]; then
  echo "ERROR: template not found at $TEMPLATE" >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$SRC_DIR/logs"

# sed delimiter # picked because $HOME always has slashes. If your
# HOME contains a literal '#' (extremely unlikely on macOS) switch to |.
sed "s#__HOME__#$HOME#g" "$TEMPLATE" > "$TARGET"

# Unload if already loaded (ignore failure: might not be loaded yet).
launchctl unload "$TARGET" 2>/dev/null || true
launchctl load   "$TARGET"

# Wait briefly, then report status.
sleep 2
if launchctl list com.gbellas.netmon >/dev/null 2>&1; then
  echo "Loaded. Check logs at $SRC_DIR/logs/netmon.err if something looks off."
  launchctl list | grep netmon
else
  echo "Agent didn't register — check $SRC_DIR/logs/netmon.err"
  exit 1
fi
