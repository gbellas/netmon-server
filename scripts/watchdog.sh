#!/bin/bash
# NetMon watchdog. Runs every 2 minutes via launchd.
#
# Two triggers for a service restart:
#   1. HTTP liveness: /api/health returns non-200 three times in a row.
#   2. Poller staleness: /api/health reports max_stale_poller_seconds >600
#      (10 min) — a poller is wedged even though the HTTP server is up.
#
# Restart = `launchctl kickstart -k`, which gracefully stops then relaunches
# the service. Same behavior as a user-triggered restart, no data loss beyond
# the in-memory history buffer.

set -euo pipefail

LOG="$HOME/NetworkMonitor/logs/watchdog.log"
STATE_FILE="$HOME/NetworkMonitor/.watchdog_fails"
URL="http://localhost:8077/api/health"
STALE_THRESHOLD=600    # seconds
mkdir -p "$(dirname "$LOG")"

ts() { date "+%Y-%m-%d %H:%M:%S"; }

fails=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
fails=${fails:-0}

body=$(curl -sf --max-time 4 "$URL" 2>/dev/null || true)
if [ -z "$body" ]; then
    # HTTP is dead.
    fails=$((fails + 1))
    echo "$fails" > "$STATE_FILE"
    echo "$(ts) /api/health unreachable (fail #$fails)" >> "$LOG"
    if [ "$fails" -ge 3 ]; then
        echo "$(ts) KICKING NetMon (HTTP dead)" >> "$LOG"
        launchctl kickstart -k "gui/$UID/com.gbellas.netmon" >> "$LOG" 2>&1 || true
        echo 0 > "$STATE_FILE"
    fi
    exit 0
fi

# HTTP is up. Reset HTTP-fail counter.
echo 0 > "$STATE_FILE"

# Escalation: any poller stuck longer than STALE_THRESHOLD seconds?
max_stale=$(printf '%s' "$body" | python3 -c '
import sys, json
try:
    j = json.loads(sys.stdin.read())
    print(int(j.get("max_stale_poller_seconds", 0) or 0))
except Exception:
    print(0)
' 2>/dev/null || echo 0)

if [ "$max_stale" -gt "$STALE_THRESHOLD" ]; then
    echo "$(ts) stale poller detected (${max_stale}s > ${STALE_THRESHOLD}s); kicking" >> "$LOG"
    # Log which pollers are stale before restarting, useful for root-cause.
    printf '%s' "$body" | python3 -c '
import sys, json
try:
    j = json.loads(sys.stdin.read())
    for p in j.get("pollers", []):
        s = p.get("seconds_since_success")
        if s is None or s > 600:
            print(f"  stale poller: {p.get(\"name\")} - {s}s, last_error={p.get(\"last_error\")}")
except Exception as e:
    print(f"  parse error: {e}")
' >> "$LOG" 2>&1 || true
    launchctl kickstart -k "gui/$UID/com.gbellas.netmon" >> "$LOG" 2>&1 || true
fi
