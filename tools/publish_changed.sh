#!/usr/bin/env bash
# publish_changed.sh -- run by ops-publish.timer on pb-orch every 10 min: republish the ops runbook and the user's
# memory (tools/publish_ops.py) to traceDB + the ontology when their source pages changed since the last publish.
# Sources: ~/tracedb-work/ops/<topic>/README.md, ~/tracedb-work/memory/laptop/*.md (pushed by the laptop's
# memory-push.timer). Takes the team ingest lock (ops/tracedb-ingest). Idempotent; a failed publish retries next run.
set -uo pipefail
set -a; . ~/.config/tracedb/env; set +a
PY=~/tracedb-work/venv/bin/python
TOOL=~/code/traceDB/tools/publish_ops.py
STATE=~/.local/state/ops-publish; mkdir -p "$STATE"
declare -A SRC=([ops]="${OPS_DIR:-$HOME/tracedb-work/ops}" [memory]="${MEMORY_DIR:-$HOME/tracedb-work/memory/laptop}")
rc=0
for root in ops memory; do
  d=${SRC[$root]}
  [ -d "$d" ] || { echo "$root: no $d"; continue; }
  dig=$(cd "$d" && find . -name '*.md' -type f -print0 | sort -z | xargs -0 sha1sum | sha1sum | cut -c1-40)
  if [ "$dig" = "$(cat "$STATE/$root.digest" 2>/dev/null)" ]; then echo "$root: unchanged"; continue; fi
  out=$(flock -w 3500 /home/jerry/pb-intel/.ingest.lock timeout -k 30 900 $PY "$TOOL" --root "$root" 2>&1); st=$?
  printf '%s\n' "$out" | grep -v 'README.md: '
  if [ $st = 0 ]; then
    echo "$dig" > "$STATE/$root.digest"; echo "$root: published $(date -u +%FT%TZ)"
  else
    echo "$root: publish failed"; rc=1
  fi
done
# laptop chat transcripts (pushed by the laptop's memory-push.timer): redacting ingest of what changed since the last run
T=~/tracedb-work/laptop/claude-projects
if [ -d "$T" ]; then
  stamp="$STATE/transcripts.stamp"; next="$STATE/transcripts.next"; touch "$next"
  if [ ! -f "$stamp" ] || [ -n "$(find "$T" -type f -newer "$stamp" -print -quit)" ]; then
    if flock -w 3500 /home/jerry/pb-intel/.ingest.lock timeout -k 30 1800 $PY ~/code/traceDB/tools/ingest.py "$T=traces/claude-code/wsl-jerry/" | tail -2; then
      mv "$next" "$stamp"; echo "transcripts: ingested $(date -u +%FT%TZ)"
    else
      echo "transcripts: ingest failed"; rc=1
    fi
  else echo "transcripts: unchanged"; fi
fi
exit $rc
