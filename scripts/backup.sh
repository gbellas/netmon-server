#!/bin/bash
# Nightly backup of NetMon configuration.
#
# Saves a timestamped git commit into ~/NetworkMonitor/.backup-repo so you
# have rollback history for everything the server generated / you edited:
#   - config.yaml
#   - alerts_config.json
#   - scheduled_config.json
#   - launchd plists in ~/Library/LaunchAgents/com.gbellas.netmon*
#
# DOES NOT back up .env — that file contains secrets. Back it up separately
# via a password manager or your own encrypted store.
#
# Run via cron / launchd (install.sh adds this).
set -euo pipefail

NM="$HOME/NetworkMonitor"
REPO="$NM/.backup-repo"
LOG="$NM/logs/backup.log"
mkdir -p "$REPO" "$NM/logs"
cd "$REPO"

if [ ! -d .git ]; then
    git init --quiet
    git config user.email "netmon@localhost"
    git config user.name "NetMon Backup"
fi

STAGE="$REPO/snapshot"
rm -rf "$STAGE"
mkdir -p "$STAGE/launchd"

# Copy non-secret config files.
for f in config.yaml alerts_config.json scheduled_config.json; do
    [ -f "$NM/$f" ] && cp "$NM/$f" "$STAGE/$f"
done
# Copy launchd plists.
for p in "$HOME/Library/LaunchAgents/com.gbellas.netmon"*.plist; do
    [ -f "$p" ] && cp "$p" "$STAGE/launchd/$(basename "$p")"
done

# git commit only if something actually changed.
cd "$REPO"
rm -rf "$REPO/current" 2>/dev/null || true
mv "$STAGE" "$REPO/current"
git add current
if git diff --cached --quiet; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') no changes" >> "$LOG"
else
    MSG="backup $(date '+%Y-%m-%d %H:%M:%S')"
    git commit --quiet -m "$MSG"
    echo "$(date '+%Y-%m-%d %H:%M:%S') committed: $MSG" >> "$LOG"
fi

# Prune the repo occasionally so it doesn't balloon. Keep ~90 days.
# git log --before="90 days ago" --format=%H | xargs -r git tag --delete
# (Skipped for simplicity; git's own GC handles packs.)
