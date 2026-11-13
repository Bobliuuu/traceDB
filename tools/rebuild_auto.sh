#!/usr/bin/env bash
# rebuild_auto.sh (copy of rebuild_all.sh for auto_sync: non-fatal VM_SETUP, profile from env, SSM wiki redeploy) -- run ONCE after ingest into the main store has finished. Refreshes everything derived from the store.
#
# What it does (each step is idempotent; safe to re-run):
#   0. preflight: AWS login, no ingest process running, free disk.
#   1. tracedb export-catalog -> catalog.jsonl            (parallel segment sync + one pass; ~2 s warm)
#   2. tracedb index update   -> index/v3/* in the store  (INCREMENTAL: only entries new/changed/removed since the last
#        MANIFEST get indexed, as one small delta generation. If no v3 manifest exists yet it does a full build:
#        ~10-25 min on this Mac with JOBS=4, reads only head+tail of text files, never whole blobs.)
#        The legacy index/fts.sqlite stays in the store untouched (old clients keep working); `tracedb search` prefers v3.
#        Index coverage now includes every catalog entry: text by content, binaries/blobs by path (+64 KiB head if text).
#   3. smoke-test search (+ prefix search) and `tracedb verify`
#   4. rebuild the ontology (HANDOFF "Remaining" step 2 command), render, copy VM_SETUP.md/ontology.jsonl/meta.json/catalog.gz,
#        upload to s3://<bucket>/ontology/ and into the store under ontology/.
#   5. optional cleanup of scratch-opt/ (CLEAN_SCRATCH=1; this is the only S3 delete and is limited to that prefix).
# Env overrides: JOBS (index workers, default 4), SKIP_ONTOLOGY=1, FORCE_FULL=1 (full rebuild instead of incremental).
# Logs: ~/tracedb-work/logs/rebuild-auto-<UTC stamp>.log
set -euo pipefail
cd ~/tracedb-work
export AWS_PROFILE=${AWS_PROFILE:-lab}
BUCKET=tracedb-all-618283574049-us-east-1
export TRACEDB_STORE=s3://$BUCKET/store
export TRACEDB_SYNC_TTL=0
JOBS=${JOBS:-4}
T=venv/bin/tracedb
LOG=logs/rebuild-auto-$(date -u +%Y%m%dT%H%M%SZ).log
mkdir -p logs
exec > >(tee -a "$LOG") 2>&1
step() { echo; echo "=== $(date -u +%H:%M:%S) $*"; }

step "0 preflight"
aws sts get-caller-identity --query Account --output text >/dev/null || { echo "AWS expired: aws login --profile lab"; exit 1; }
if pgrep -f "ingest(_blobs)?\.py|vault_ingest\.py" >/dev/null; then echo "an ingest is still running; wait for it"; exit 1; fi
df -h ~ | tail -1

step "1 export catalog"
$T sync
$T export-catalog --out catalog.jsonl
wc -l catalog.jsonl

step "2 index (incremental if a v3 manifest exists)"
if [ "${FORCE_FULL:-0}" = 1 ]; then $T index build --jobs "$JOBS"; else $T index update --jobs "$JOBS"; fi
$T index stats

step "3 smoke + verify"
$T search --limit 3 "vllm oom" | head -c 1500; echo
$T search --limit 3 --prefix config/ "ssh host" | head -c 800; echo
$T search --paths --limit 3 "model.tpq"
$T verify

if [ "${SKIP_ONTOLOGY:-0}" != 1 ]; then
  step "4 ontology rebuild + upload"
  venv/bin/python describe.py describe-new.json $(cat srcs.txt srcs2.txt srcs3.txt $( [ -f srcs4.txt ] && echo srcs4.txt ) | tr ' ' '\n' | grep "=repos/" | sort -u) || true
  [ -f describe-new.json ] && venv/bin/python - <<'PY'
import json
a=json.load(open("describe.json")); b=json.load(open("describe-new.json"))
for k,v in a.items(): b.setdefault(k,v)
json.dump(b,open("describe.json","w"),indent=1)
PY
  # build + verify + CAS publish to every $ONTOLOGY_DESTS (box registry, skills, other writers' overlays kept)
  DESCRIBE=$PWD/describe.json DOCS=$PWD/stage/config=config/ bash traceDB/tools/rebuild_ontology.sh
  # best effort: redeploy the human wiki on the wiki box via SSM (no inbound port)
  aws ssm send-command --region us-east-2 --instance-ids i-0c3596d24601850cf --document-name AWS-RunShellScript \
    --parameters "commands=[\"aws s3 cp s3://$BUCKET/ontology/wiki/index.html /srv/ontolagni-wiki/index.html\"]" --query Command.CommandId --output text || echo "wiki redeploy skipped"
fi

if [ "${CLEAN_SCRATCH:-0}" = 1 ]; then
  step "5 remove scratch-opt/"
  aws s3 rm --recursive "s3://$BUCKET/scratch-opt/" --only-show-errors
fi
step "done"
