#!/usr/bin/env bash
# publish_setup.sh -- publish the new-machine entry point to <setup> (= $SETUP_S3, e.g. s3://<bucket>/setup) and the store prefix setup/:
#   SETUP.md        rendered from tools/SETUP.md.tmpl (agent-facing instructions; read first on a new machine)
#   bootstrap.sh    tools/bootstrap.sh (the button)
#   bootstrap.env   this machine's ~/.config/tracedb/env (stores, profile, git URLs, extras; no secrets)
#   src/<repo>.bundle + src/<repo>.tar.gz   committed HEAD of ~/code/traceDB and ~/code/ontolagni (bootstrap clones these)
# Also drops SETUP.md into the ontology build's extra/ dir so rebuild_ontology.sh publishes it next to AGENT.md.
# Run after committing changes to either repo. Idempotent.
set -euo pipefail
[ -f ~/.config/tracedb/env ] && { set -a; . ~/.config/tracedb/env; set +a; }
: "${SETUP_S3:?set SETUP_S3}" "${TRACEDB_STORE:?}" "${VAULT_STORE:?}"
export AWS_PROFILE=${AWS_PROFILE:-lab}
HERE=$(cd "$(dirname "$0")" && pwd); CODE=${TRACEDB_CODE:-$HOME/code}
T=${TRACEDB_VENV:-$HOME/tracedb-work/venv}/bin/tracedb
S=$(mktemp -d); trap 'rm -rf "$S"' EXIT
mkdir -p "$S/setup/src"
sed -e "s#{SETUP_S3}#$SETUP_S3#g" -e "s#{TRACEDB_STORE}#$TRACEDB_STORE#g" -e "s#{VAULT_STORE}#$VAULT_STORE#g" \
    -e "s#{BUCKET}#${TRACEDB_STORE%/*}#g" -e "s#{PROFILE}#${AWS_PROFILE_LOGIN:-lab}#g" -e "s#{REGION}#${AWS_REGION:-us-east-1}#g" \
    "$HERE/SETUP.md.tmpl" > "$S/setup/SETUP.md"
cp "$HERE/bootstrap.sh" "$S/setup/bootstrap.sh"
grep -v '^AWS_PROFILE=lab-' ~/.config/tracedb/env > "$S/setup/bootstrap.env"
if grep -v '^#' "$S/setup/bootstrap.env" | grep -qiE 'token|password|aws_access|aws_secret|PRIVATE KEY|=.*secret[^:]'; then echo "bootstrap.env looks like it holds a secret; refusing" >&2; exit 1; fi
for R in traceDB ontolagni; do
  D="$CODE/$R"; BR=$(git -C "$D" rev-parse --abbrev-ref HEAD)
  [ -n "$(git -C "$D" status --porcelain --untracked-files=no)" ] && echo "note: $R has uncommitted changes; publishing the committed HEAD only"
  git -C "$D" bundle create -q "$S/setup/src/$R.bundle" "$BR"
  git -C "$D" archive --format=tar.gz -o "$S/setup/src/$R.tar.gz" HEAD
  echo "$R: $BR $(git -C "$D" rev-parse --short HEAD)"
done
aws s3 cp --recursive "$S/setup/" "$SETUP_S3/" --only-show-errors
"$T" put "$S/setup" --prefix setup/
mkdir -p "${ONTO_WORK:-$HOME/tracedb-work/onto-build}/extra"
cp "$S/setup/SETUP.md" "${ONTO_WORK:-$HOME/tracedb-work/onto-build}/extra/SETUP.md"
aws s3 ls --recursive "$SETUP_S3/"
