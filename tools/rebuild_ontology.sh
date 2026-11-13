#!/usr/bin/env bash
# rebuild_ontology.sh -- rebuild + publish the ontology from ANY machine (no Mac-only inputs needed). Idempotent.
#   1. export the main store catalog
#   2. fetch the published ontology (carried over: other writers' nodes and fields are merged, never clobbered)
#   3. inputs from the stores: canonical ssh config (vault keys/files/.ssh/config, temp file, removed after),
#      box registry (config/boxes.json), skills manifest (skills/MANIFEST.json)
#   4. ontolagni build --carry --boxes --skills, verify (must pass), render, wiki
#   5. publish: s3://<bucket>/ontology/ via `ontolagni publish` (compare-and-swap), then the store prefix ontology/
# Other writers (push-boxes, rsi, ...) add nodes with `ontolagni overlay <nodes.jsonl> --name <writer> --to s3://<bucket>/ontology/`;
# their overlays/<writer>.jsonl files are re-applied by every publish, so a rebuild never drops them.
# Config: ~/.config/tracedb/env (TRACEDB_STORE, VAULT_STORE, AWS_PROFILE). Work dir: $ONTO_WORK (default ~/tracedb-work/onto-build).
# Env: NO_PUBLISH=1 builds and verifies only. DESCRIBE=describe.json (README summaries), DOCS=LOCALDIR=STOREPREFIX (box mentions). Extra docs to publish next to the ontology: put them in $ONTO_WORK/extra/.
set -euo pipefail
[ -f ~/.config/tracedb/env ] && { set -a; . ~/.config/tracedb/env; set +a; }
: "${TRACEDB_STORE:?set TRACEDB_STORE}" "${VAULT_STORE:?set VAULT_STORE}"
export AWS_PROFILE=${AWS_PROFILE:-lab}
BIN=${TRACEDB_VENV:-$HOME/tracedb-work/venv}/bin
W=${ONTO_WORK:-$HOME/tracedb-work/onto-build}
BUCKET_URL=${TRACEDB_STORE%/*}            # s3://bucket
# where the ontology is published: first = canonical, the rest mirrors (all get the same CAS publish)
DESTS=${ONTOLOGY_DESTS:-$BUCKET_URL/ontology/}; CANON=${DESTS%% *}
mkdir -p "$W/prev" "$W/out" "$W/extra"; cd "$W"
step() { echo "=== $(date -u +%H:%M:%S) $*"; }
T="$BIN/tracedb"; O="$BIN/ontolagni"

step "1 catalog"
"$T" export-catalog --out catalog.jsonl; wc -l < catalog.jsonl

step "2 previous ontology (carried)"
aws s3 cp "${CANON}ontology.jsonl" prev/ontology.jsonl --only-show-errors || : > prev/ontology.jsonl
aws s3 cp "${CANON}meta.json" prev/meta.json --only-show-errors || echo '{}' > prev/meta.json

step "3 inputs"
SSHCFG=$(mktemp); chmod 600 "$SSHCFG"; trap 'rm -f "$SSHCFG"' EXIT
"$T" --store "$VAULT_STORE" get --max-bytes -1 keys/files/.ssh/config > "$SSHCFG"
"$T" get --max-bytes -1 config/boxes.json > boxes.json
"$T" get --max-bytes -1 skills/MANIFEST.json > skills.json

step "4 build + verify"
rm -rf ontology out; mkdir -p out
"$O" --dir ontology build catalog.jsonl --store "$TRACEDB_STORE" --carry prev/ontology.jsonl \
  --ssh-config "$SSHCFG:keys/files/.ssh/config" --boxes boxes.json:config/boxes.json --skills skills.json --max-depth 4 \
  ${DESCRIBE:+--describe "$DESCRIBE"} ${DOCS:+--docs "$DOCS"}
"$O" --dir ontology verify catalog.jsonl
"$O" --dir ontology render --out out          # local preview; publish re-renders with the overlays applied
gzip -c catalog.jsonl > out/catalog.jsonl.gz
ONTOLOGY_DIR="$W/ontology" "$O" --dir ontology find arc b390 --limit 3 | head -c 600; echo

if [ "${NO_PUBLISH:-0}" != 1 ]; then
  step "5 publish (compare-and-swap; carries + re-applies every overlays/*.jsonl written by other tools)"
  # shellcheck disable=SC2086
  "$O" --dir ontology publish --to "$DESTS"
  for d in $DESTS; do for f in out/catalog.jsonl.gz extra/*; do [ -f "$f" ] && aws s3 cp "$f" "$d$(basename "$f")" --only-show-errors; done; done
  # store copy = exactly what was published to canonical (incl. other writers' overlay nodes)
  rm -rf pub; mkdir -p pub; aws s3 sync "$CANON" pub/ --only-show-errors --exclude 'overlays/*'
  "$T" put pub --prefix ontology/
fi
step "done ($W/out)"
