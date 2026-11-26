#!/bin/bash
# box_sync.sh -- the tracedb autosync cycle for the always-on sync box. Keyless (EC2 instance role via IMDSv2), no Mac dependency.
# Run by systemd tracedb-sync.timer (every ~30 min). One cycle:
#   0 self-update (git pull traceDB/ontolagni)      1 Mac inbox -> redacted store + vault + blobs, then ack (sha256) for the Mac
#   2 pull extra boxes listed in box-pull.txt        3 all GitHub repos (gh_capture, token from the vault)
#   4 index update + verify                          5 ontology/wiki rebuild (at most every 6 h, or FORCE_ONTOLOGY=1)
#   6 heartbeat (S3 health/heartbeat.json) + CloudWatch metric Tracedb/Sync SyncOK (alarm = no datapoint for 3 h)
# Log: ~/tracedb-work/logs/box-<UTC>.log (last 30 kept). Crash-safe: flock single instance, every step idempotent.
export PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH
W=${TRACEDB_WORK:-$HOME/tracedb-work}; export TRACEDB_WORK=$W; cd "$W" || exit 1
mkdir -p logs state
[ -f ~/.config/tracedb/env ] && { set -a; . ~/.config/tracedb/env; set +a; }
unset AWS_PROFILE; export TRACEDB_KEYLESS=1 AWS_DEFAULT_REGION=us-east-1 TRACEDB_SYNC_TTL=0 INBOX=${INBOX:-/srv/tracedb-inbox/mac}
exec 9>"$W/state/box-sync.lock"; flock -n 9 || { echo "another box_sync is running"; exit 0; }
LOG=logs/box-$(date -u +%Y%m%dT%H%M%SZ).log; exec >>"$LOG" 2>&1; ls -1t logs/box-*.log | tail -n +31 | xargs -r rm -f
log() { echo "[$(date -u +%H:%M:%S)] $*"; }
HB=traceDB/tools/box_heartbeat.sh
alert() { log "ALERT: $*"; echo "$(date -u +%FT%TZ) $*" >> logs/ALERT.txt; bash $HB alert "$*"; }
PY=venv/bin/python; T=venv/bin/tracedb; TL=traceDB/tools
bash $HB running "cycle start"
fail=0

log "== 0 self-update"
for r in traceDB ontolagni; do git -C $r pull -q --ff-only 2>&1 | tail -2 || true; done

log "== 1 inbox"
TOK=$($PY $TL/box_inbox.py stage) || { alert "inbox stage failed"; fail=1; TOK=""; }
if [ -n "$TOK" ]; then
  ok=1
  $PY ingest.py $TOK          || { log "ingest rc=$?"; ok=0; }
  $PY vault_ingest.py $TOK    || { log "vault rc=$?"; ok=0; }
  $PY ingest_blobs.py $TOK    || { log "blobs rc=$?"; ok=0; }
  if [ $ok = 1 ]; then $PY $TL/box_inbox.py commit; else $PY $TL/box_inbox.py abort; fail=1; alert "inbox ingest failed (will retry)"; fi
else $PY $TL/box_inbox.py abort; fi

log "== 2 extra boxes (box-pull.txt: <ssh-alias>:<remote-dir>=<store-prefix>)"
if [ -f box-pull.txt ]; then
  grep -v '^#' box-pull.txt | grep = | while IFS= read -r line; do
    spec=${line%%=*}; pre=${line#*=}; al=${spec%%:*}; rd=${spec#*:}; d=$W/pull/$al/$(echo "$rd" | tr / _)
    mkdir -p "$d"
    if rsync -rlt --timeout=300 -e "ssh -o BatchMode=yes -o ConnectTimeout=20" "$al:$rd/" "$d/"; then
      $PY ingest.py "$d=$pre" && $PY vault_ingest.py "$d=$pre" && $PY ingest_blobs.py "$d=$pre" && rm -rf "$d" || log "pull $al failed ingest"
    else log "pull $al unreachable (skipped)"; fi
  done
fi

log "== 3 github"
if [ -f state/gh.hold ]; then log "held (state/gh.hold: another machine is capturing the same repos)"; else
GHT=$($T --store "$VAULT_STORE" get --max-bytes -1 home/gh-current/hosts.yml 2>/dev/null | python3 -c 'import sys,re;m=re.search(r"oauth_token:\s*(\S+)",sys.stdin.read());print(m.group(1) if m else "")')
if [ -n "$GHT" ]; then
  export GH_TOKEN=$GHT; unset GHT
  withgit() { $PY $TL/gh_capture.py; }
  withgit || { log "gh_capture rc=$?"; fail=1; }
  unset GH_TOKEN
else alert "no GitHub token in vault home/gh-current/hosts.yml: one-time step, see docs/SYNC_BOX.md"; fail=1; fi
fi

log "== 4 index"
$T sync && $T index update --jobs 4 || { log "index rc=$?"; fail=1; }
$T verify | head -c 300 || true

log "== 5 ontology"
LASTO=state/last-ontology; NOW=$(date +%s)
if [ "${FORCE_ONTOLOGY:-0}" = 1 ] || [ ! -f $LASTO ] || [ $((NOW - $(cat $LASTO))) -gt 21600 ]; then
  aws s3 cp "${TRACEDB_STORE%/*}/setup/describe.json" describe.json --only-show-errors 2>/dev/null || true
  if DESCRIBE=$PWD/describe.json bash $TL/rebuild_ontology.sh; then echo $NOW > $LASTO; else log "ontology rc=$?"; fail=1; fi
else log "skip (built $(( (NOW - $(cat $LASTO))/60 )) min ago)"; fi

if [ $fail = 0 ]; then
  bash $HB ok "full cycle"
  aws cloudwatch put-metric-data --namespace Tracedb/Sync --metric-name SyncOK --value 1 --region us-east-1 || log "metric failed"
  log "OK"
else bash $HB alert "cycle finished with errors (see log)"; log "finished WITH ERRORS"; exit 1; fi
