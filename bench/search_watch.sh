#!/usr/bin/env bash
# search_watch.sh -- daily on the eval host (search-watch.timer): grow the search eval from new chats, score every
# set, keep the history, and file an RSI inbox item when the curated real-question score drops.
# Private data stays in $EVAL_DIR (default ~/tracedb-work/search-eval) and the store's eval/search/, never in this repo.
set -uo pipefail
set -a; . ~/.config/tracedb/env; set +a
E=${EVAL_DIR:-$HOME/tracedb-work/search-eval}; R=$(cd "$(dirname "$0")/.." && pwd)
PY=~/tracedb-work/venv/bin/python; T=~/tracedb-work/venv/bin/tracedb
SRC=${CHAT_DIRS:-$HOME/tracedb-work/laptop/claude-projects}
INBOX=${RSI_INBOX:-$HOME/rsi-ontology-wt/skills/rsi-ontology/inbox.py}
now=$(date -u +%FT%TZ); mkdir -p "$E/runs"
$PY -I "$R/bench/mine_questions.py" $(for s in $SRC; do printf -- '--src %s ' "$s"; done) --out "$E/auto.jsonl" \
  --known "$E/real.jsonl" --known "$E/written.jsonl"
row="{\"at\": \"$now\""
for set in real written auto; do
  [ -s "$E/$set.jsonl" ] || continue
  $PY -I "$R/bench/ask_eval.py" --methods find,ask --questions "$E/$set.jsonl" --json-out "$E/runs/$set.json" >/dev/null
  row="$row, \"$set\": $(cat "$E/runs/$set.json")"
done
echo "$row}" >> "$E/history.jsonl"; tail -1 "$E/history.jsonl"
flock -w 3500 /home/jerry/pb-intel/.ingest.lock timeout -k 30 600 $T put "$E" --prefix eval/search/ >/dev/null
# alert when the curated real-question score falls 2+ below its best so far
$PY -I - "$E/history.jsonl" <<'PYEOF' > "$E/runs/alert.md" && [ -f "$INBOX" ] && (cd "$(dirname "$INBOX")" && AWS_PROFILE=lab-boto $PY "$INBOX" add --title "search eval: tracedb ask real-question score dropped" --from search-watch --kind infra --body "$E/runs/alert.md")
import json, sys
H = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
S = [h["real"]["ask"]["hit"] for h in H if "real" in h]
if len(S) < 2 or S[-1] > max(S[:-1]) - 2:
    sys.exit(1)
print(f"`tracedb ask` top-5 on the curated real questions fell to {S[-1]} (best so far {max(S[:-1])}).\n\n"
      "Rerun on the eval host: `python ~/code/traceDB/bench/ask_eval.py --questions ~/tracedb-work/search-eval/real.jsonl`"
      " and compare with the last good commit of traceDB/ontolagni. History: traceDB eval/search/history.jsonl.")
PYEOF
exit 0
