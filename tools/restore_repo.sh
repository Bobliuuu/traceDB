#!/usr/bin/env bash
# restore_repo.sh PREFIX [DEST]   e.g.  restore_repo.sh repos/ten-percent  ~/restored/ten-percent
# Fetches repos/<project>/.git-bundle/repo.bundle from the tracedb store (main, else vault) and clones it with ALL branches/tags.
# Also prints uncommitted work: wip/*.diff (apply with `git apply`), stash.txt. Env: TRACEDB_STORE (default main store), AWS_PROFILE.
# Find a project: `tracedb --store $TRACEDB_STORE search --paths ".git-bundle/manifest.json"` or `tracedb ls repos/`.
set -euo pipefail
P="${1:?usage: restore_repo.sh repos/<project>[/sub] [dest]}"; P="${P%/}"; P="${P%/.git-bundle}"
DEST="${2:-./$(basename "$P")}"
export AWS_PROFILE="${AWS_PROFILE:-lab}"
MAIN="${TRACEDB_STORE:-s3://tracedb-all-618283574049-us-east-1/store}"
VAULT="${VAULT_STORE:-s3://tracedb-vault-618283574049-us-east-1/store}"
T="${TRACEDB:-tracedb}"; command -v "$T" >/dev/null || T=~/tracedb-work/venv/bin/tracedb
B="$P/.git-bundle"; TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
$T --store "$MAIN" get --max-bytes -1 "$B/manifest.json" > "$TMP/manifest.json"
LOC=$(python3 -c "import json;print(json.load(open('$TMP/manifest.json'))['bundle']['location'])")
STORE="$MAIN"; [ "$LOC" = vault ] && STORE="$VAULT"
echo "bundle in $LOC store; downloading..." >&2
$T --store "$STORE" blob-get --max-bytes -1 --out "$TMP/repo.bundle" "$B/repo.bundle"
git clone --mirror "$TMP/repo.bundle" "$TMP/mirror.git" >/dev/null 2>&1
git clone "$TMP/mirror.git" "$DEST" >/dev/null 2>&1
# make every bundled branch a local branch, restore original remote urls
git -C "$DEST" remote remove origin
python3 - "$TMP/manifest.json" "$DEST" <<'PY'
import json,subprocess,sys
m=json.load(open(sys.argv[1])); d=sys.argv[2]
for n,u in m["remotes"].items(): subprocess.run(["git","-C",d,"remote","add",n,u])
PY
git -C "$TMP/mirror.git" for-each-ref --format='%(refname:short)' refs/heads | while read -r b; do
  git -C "$DEST" rev-parse -q --verify "refs/heads/$b" >/dev/null || git -C "$DEST" fetch -q "$TMP/mirror.git" "refs/heads/$b:refs/heads/$b" || true
done
git -C "$DEST" fetch -q "$TMP/mirror.git" '+refs/tags/*:refs/tags/*' 2>/dev/null || true
for f in manifest.json commits.jsonl stash.txt; do $T --store "$MAIN" get --max-bytes -1 "$B/$f" > "$DEST/.git/gitcapture-$f" 2>/dev/null || true; done
echo "restored $P -> $DEST ($(git -C "$DEST" rev-list --all --count) commits). Uncommitted work: tracedb get $B/wip/<name>.diff ; stash: $B/stash.txt ; untracked tarballs live in the vault store." >&2
