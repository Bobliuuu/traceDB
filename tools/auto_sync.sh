#!/bin/bash
# auto_sync.sh -- incremental refresh of the tracedb store from this Mac. Run by launchd every 6 h (com.tracedb.autosync).
# Crash-safe: single-instance lock (stale-pid aware), every step idempotent, stamp only advances after a full run,
# store writes wrapped in the shared mkdir lock logs/store-write.lock. Runs at nice 19 (also set in the plist).
# Steps: snapshot -> changed-source ingest (redacted) -> vault originals -> blobs -> git bundles -> index update -> ontology/wiki.
# Log: ~/tracedb-work/logs/auto-<UTC stamp>.log     Manual run: bash ~/tracedb-work/auto-sync.sh
export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH
cd ~/tracedb-work || exit 1
mkdir -p logs state snap
STAMPNAME=$(date -u +%Y%m%dT%H%M%SZ); LOG=logs/auto-$STAMPNAME.log
exec >>"$LOG" 2>&1
log() { echo "[$(date -u +%H:%M:%S)] $*"; }
LK=state/auto-sync.lock
if ! mkdir "$LK" 2>/dev/null; then
  if kill -0 "$(cat $LK/pid 2>/dev/null)" 2>/dev/null; then log "another auto-sync is running; exit"; exit 0; fi
  rm -rf "$LK"; mkdir "$LK" || exit 0
fi
echo $$ > "$LK/pid"; trap 'rm -rf "$LK"; log exit' EXIT
renice -n 19 $$ >/dev/null 2>&1; taskpolicy -b -p $$ >/dev/null 2>&1 || true

# do not collide with the one-off refresh or any other ingest
if [ ! -f logs/refresh-done.flag ] && pgrep -f "refresh-run.sh" >/dev/null; then log "refresh-run.sh still running; skip this cycle"; exit 0; fi
if pgrep -f "(^|/)(ingest|ingest_blobs|vault_ingest|git_capture)\.py" >/dev/null; then log "another ingest running; skip this cycle"; exit 0; fi
FREE_GB=$(df -g ~ | awk 'NR==2{print $4}'); if [ "${FREE_GB:-0}" -lt 25 ]; then log "only ${FREE_GB}G free disk; skip"; exit 0; fi

# credentials: long-lived `tracedb` profile if it works, else lab; no prompts when expired
if aws configure list-profiles 2>/dev/null | grep -qx tracedb && aws sts get-caller-identity --profile tracedb >/dev/null 2>&1; then export AWS_PROFILE=tracedb
elif aws sts get-caller-identity --profile lab >/dev/null 2>&1; then export AWS_PROFILE=lab
else log "AWS credentials expired/missing: run  aws login --profile lab  (skipping cycle)"; exit 2; fi
log "profile $AWS_PROFILE"
export TRACEDB_STORE=s3://tracedb-all-618283574049-us-east-1/store VAULT_STORE=s3://tracedb-vault-618283574049-us-east-1/store
PY=venv/bin/python; T=traceDB/tools; LISTS="srcs.txt srcs2.txt srcs3.txt srcs4.txt"
STAMP=state/last-sync.stamp; NEWSTAMP=state/new-sync.stamp; touch "$NEWSTAMP"

withlock() { # withlock cmd... : hold the shared store-write lock only while the command runs
  local n=0; until mkdir logs/store-write.lock 2>/dev/null; do sleep 15; n=$((n+1)); [ $n -gt 480 ] && { log "lock wait > 2h"; return 3; }; done
  "$@"; local rc=$?; rmdir logs/store-write.lock 2>/dev/null; return $rc
}
step() { log "== $*"; }
fail=0

step snapshot; bash $T/system_snapshot.sh snap || fail=1
CH=$($PY $T/changed_srcs.py "$STAMP" $LISTS | tr '\n' ' ')
log "changed sources: $(echo $CH | wc -w)"
if [ -n "$CH" ]; then
  step ingest;  withlock $PY ingest.py $CH       || { log "ingest rc=$?"; fail=1; }
  step vault;   withlock $PY vault_ingest.py $CH || { log "vault rc=$?"; fail=1; }
  printf '%s\n' $CH > state/changed-srcs.txt
  step blobs;   withlock $PY ingest_blobs.py @state/changed-srcs.txt || { log "blobs rc=$?"; fail=1; }
fi
if [ -f srcs4-vault.txt ]; then step vault-extra; withlock $PY vault_ingest.py --all $(cat srcs4-vault.txt) || fail=1; fi
step git-capture;  $PY $T/git_capture.py ${GITCAP_ARGS:-} || { log "git_capture rc=$?"; fail=1; }   # takes the lock itself, per write
cat $LISTS | tr ' ' '\n' | grep = | awk '!s[$0]++' > srcs-all.txt
step rebuild;  withlock bash $T/rebuild_auto.sh || { log "rebuild rc=$?"; fail=1; }
if [ $fail = 0 ]; then mv "$NEWSTAMP" "$STAMP"; log "OK stamp advanced"; else log "finished WITH ERRORS; stamp not advanced (next run re-checks)"; fi
date -u +%FT%TZ > state/last-run.txt
