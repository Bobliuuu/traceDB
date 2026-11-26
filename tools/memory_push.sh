#!/usr/bin/env bash
# Back up this laptop's Claude memory and chat transcripts to pb-orch, where ops-publish.timer publishes them:
#   memory      -> ~/tracedb-work/memory/laptop/        -> traceDB memory/<name>/README.md + ontology nodes memory/<name>
#   transcripts -> ~/tracedb-work/laptop/claude-projects/ -> traceDB traces/claude-code/wsl-jerry/ (redacted by tools/ingest.py)
# The laptop is volatile (ops/laptop-volatile): nothing may live only here.
set -euo pipefail
SSH="ssh -o BatchMode=yes -o ConnectTimeout=20"
rsync -a --delete -e "$SSH" "$HOME/.claude/projects/-home-jerry/memory/" pb-orch:tracedb-work/memory/laptop/
$SSH pb-orch "mkdir -p ~/tracedb-work/laptop/claude-projects ~/tracedb-work/logs"
exec rsync -a -e "$SSH" "$HOME/.claude/projects/" pb-orch:tracedb-work/laptop/claude-projects/
