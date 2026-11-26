#!/bin/bash
# box_heartbeat.sh STATUS [DETAIL] -- write s3://<bucket>/health/heartbeat.json (time, status, detail, last_ok). Keyless (instance role).
# STATUS: ok | running | alive | alert. last_ok is advanced only by `ok`. `tracedb health` and the CloudWatch alarm read this.
W=${TRACEDB_WORK:-$HOME/tracedb-work}; mkdir -p "$W/state"
[ -f ~/.config/tracedb/env ] && { set -a; . ~/.config/tracedb/env; set +a; }
B=${TRACEDB_STORE%/*}
[ "$1" = ok ] && date -u +%FT%TZ > "$W/state/last_ok"
LAST=$(cat "$W/state/last_ok" 2>/dev/null || echo never)
[ "$1" = alive ] && S=$(cat "$W/state/status" 2>/dev/null || echo ok) || { S=$1; echo "$1" > "$W/state/status"; }
printf '{"time":"%s","status":"%s","detail":"%s","last_ok":"%s","host":"tracedb-sync"}\n' "$(date -u +%FT%TZ)" "$S" "${2:-}" "$LAST" > "$W/state/hb.json"
aws s3 cp "$W/state/hb.json" "$B/health/heartbeat.json" --only-show-errors --content-type application/json
