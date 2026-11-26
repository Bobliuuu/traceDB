#!/usr/bin/env bash
# ontology_sync.sh -- keep this machine's read copies current, so a search never waits on a download.
# Installed as ~/.local/bin/ontology-sync, run by ontology-sync.timer every 5 min (laptop and pb-orch).
#   1. pull the published ontology into $ONTOLOGY_DIR (what `ontolagni find` and `tracedb ask` read)
#   2. tracedb index prefetch: download new search-index generations, delete ones the manifest dropped
set -euo pipefail
[ -f ~/.config/tracedb/env ] && { set -a; . ~/.config/tracedb/env; set +a; }
O=${ONTOLOGY_DIR:-$HOME/ontology}; mkdir -p "$O"
CANON=${ONTOLOGY_DESTS:-${TRACEDB_STORE%/*}/ontology/}; CANON=${CANON%% *}
AWS=$(command -v aws || echo "$HOME/.local/bin/aws")
"$AWS" s3 sync "$CANON" "$O/" --only-show-errors --exclude 'catalog.jsonl.gz'
TDB=$(command -v tracedb || echo "$HOME/tracedb-work/venv/bin/tracedb")
nice "$TDB" index prefetch
