#!/usr/bin/env bash
# bootstrap.sh -- the "button": make this machine identical to every other one, from the tracedb stores.
#
#   bash bootstrap.sh s3://<bucket>/setup        (first run; the URL is remembered in ~/.config/tracedb/env)
#   bash bootstrap.sh                            (re-run: idempotent, safe, fast when nothing changed)
#
# Steps (each one skips itself when already done):
#   1 AWS CLI v2 into ~/.local (no sudo)     2 AWS login (`aws login --profile lab`: the ONLY interactive step)
#   3 ~/.config/tracedb/env from <setup>/bootstrap.env (stores, profile; no secrets)
#   4 venv ~/tracedb-work/venv without sudo (python3 -m venv --without-pip + the pinned pip wheel, sha256-checked)
#   5 traceDB + ontolagni checkouts in ~/code (git bundles from <setup>/src/, origin -> GitHub; tarball if no git)
#   6 pip install -e both + extras    7 ~/.local/bin: tracedb ontolagni keys skills (+ extra CLIs)
#   8 a managed block in ~/.bashrc / ~/.zshrc (PATH + env)
#   9 keys pull: every SSH key, ~/.ssh/config, credentials, CLAUDE.md, settings.json, memory (exact bytes + modes;
#     anything replaced goes to ~/.keys-backup/<stamp>/)       10 skills pull: the user skill set into ~/.claude/skills
#   11 ontology -> ~/ontology        12 report: keys status, skills status, box registry, reachability (keys doctor)
# Env: BOOT_NO_DOCTOR=1 skips the reachability check; BOOT_SKIP_LOGIN=1 fails instead of prompting.
# macOS: AWS CLI comes from `brew install awscli` (or the .pkg); everything else is the same (zsh block in ~/.zshrc).
# This script holds no secrets, hosts or addresses: all of that comes from the stores after login.
set -euo pipefail
umask 022
step() { printf '\n=== [%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { echo "bootstrap: $*" >&2; exit 1; }
ENV_FILE="$HOME/.config/tracedb/env"
[ -f "$ENV_FILE" ] && { set -a; . "$ENV_FILE"; [ -f "$ENV_FILE.local" ] && . "$ENV_FILE.local"; set +a; }
SETUP_S3=${1:-${SETUP_S3:-}}
[ -n "$SETUP_S3" ] || die "usage: bash bootstrap.sh s3://<bucket>/setup"
SETUP_S3=${SETUP_S3%/}
PROFILE=${AWS_PROFILE:-lab}; REGION=${AWS_REGION:-us-east-1}
VENV=${TRACEDB_VENV:-$HOME/tracedb-work/venv}
# pinned pip wheel for venvs created without ensurepip (python3-venv not installed, no sudo); PyPI sha256
: "${PIP_WHEEL_NAME:=pip-26.3-py3-none-any.whl}"
: "${PIP_WHEEL_URL:=https://files.pythonhosted.org/packages/07/25/ee8c38ae72d66b844ce8eb9711ba905dba21ca1a8a0c960057c41cd23337/pip-26.3-py3-none-any.whl}"
: "${PIP_WHEEL_SHA256:=f3b60b20ffaadd4415ed7957212a38ac63fa4c5fa375bd0f4a797777940fad13}"
CODE=${TRACEDB_CODE:-$HOME/code}
export PATH="$HOME/.local/bin:$PATH"
OS=$(uname -s); ARCH=$(uname -m)
mkdir -p "$HOME/.local/bin" "$HOME/.config/tracedb"

step "1 AWS CLI"
if ! command -v aws >/dev/null; then
  if [ "$OS" = Darwin ]; then
    command -v brew >/dev/null && brew install awscli || die "install the AWS CLI v2 (brew install awscli), then re-run"
  else
    T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
    curl -fsSL -o "$T/awscli.zip" "https://awscli.amazonaws.com/awscli-exe-linux-${ARCH}.zip"
    python3 - "$T/awscli.zip" "$T" <<'PY'
import sys, zipfile, os
z = zipfile.ZipFile(sys.argv[1])
for i in z.infolist():                     # python zipfile drops exec bits; restore them from the zip's unix mode
    p = z.extract(i, sys.argv[2]); m = i.external_attr >> 16
    if m: os.chmod(p, m & 0o7777)
PY
    "$T/aws/install" -i "$HOME/.local/aws-cli" -b "$HOME/.local/bin" --update >/dev/null
  fi
fi
aws --version

step "2 AWS credentials (source: ${AWS_CRED_SOURCE:-login}, profile $PROFILE)"
# Pluggable credential source (AWS_CRED_SOURCE in bootstrap.env / env):
#   login               `aws login --profile $PROFILE` (browser; short-lived, re-login when it expires)
#   secret:<id>[@region] a long-lived access key kept in AWS Secrets Manager (JSON with AccessKeyId/SecretAccessKey).
#                       Read once (needs any working credential the first time, e.g. the login), written to profile
#                       ${PROFILE}-key in ~/.aws/credentials (0600, never printed), then that profile is used from then on.
KEYPROF="${PROFILE}-key"
ok() { aws sts get-caller-identity --profile "$1" --query Account --output text 2>/dev/null; }
aws configure get region --profile "$PROFILE" >/dev/null 2>&1 || aws configure set region "$REGION" --profile "$PROFILE"
SRC=${AWS_CRED_SOURCE:-login}
if [ "${SRC%%:*}" = secret ] && ok "$KEYPROF"; then PROFILE=$KEYPROF
else
  if ! ok "$PROFILE"; then
    [ "${BOOT_SKIP_LOGIN:-0}" = 1 ] && die "not logged in: aws login --profile $PROFILE"
    echo "opening the AWS sign-in (headless machine: aws login --profile $PROFILE --remote)"
    aws login --profile "$PROFILE"
    ok "$PROFILE" || die "login failed"
  fi
  if [ "${SRC%%:*}" = secret ]; then
    SID=${SRC#secret:}; SREG=${SID##*@}; [ "$SREG" = "$SID" ] && SREG=$REGION; SID=${SID%@*}
    aws secretsmanager get-secret-value --profile "$PROFILE" --region "$SREG" --secret-id "$SID" --query SecretString --output text \
      | python3 -c '
import configparser, json, os, sys
s = json.loads(sys.stdin.read()); g = lambda *ks: next((s[k] for k in ks if s.get(k)), None)
kid, sec = g("AccessKeyId", "aws_access_key_id", "access_key_id"), g("SecretAccessKey", "aws_secret_access_key", "secret_access_key")
assert kid and sec, "secret must hold AccessKeyId + SecretAccessKey"
p = os.path.expanduser("~/.aws/credentials"); c = configparser.RawConfigParser(); c.read(p)
c[sys.argv[1]] = {"aws_access_key_id": kid, "aws_secret_access_key": sec}
fd = os.open(p + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600); c.write(os.fdopen(fd, "w")); os.replace(p + ".tmp", p)
print("wrote profile", sys.argv[1], "(key not shown)")' "$KEYPROF"
    aws configure set region "$REGION" --profile "$KEYPROF"
    ok "$KEYPROF" >/dev/null && PROFILE=$KEYPROF || die "the key from secret $SID does not authenticate"
  fi
fi
export AWS_PROFILE=$PROFILE
echo "using AWS profile $AWS_PROFILE"

step "3 config"
NEW=$(mktemp); aws s3 cp "$SETUP_S3/bootstrap.env" "$NEW" --only-show-errors
if [ -f "$ENV_FILE" ] && ! cmp -s "$NEW" "$ENV_FILE"; then cp -p "$ENV_FILE" "$ENV_FILE.bak-$(date +%Y%m%dT%H%M%S)"; fi
mv "$NEW" "$ENV_FILE"; chmod 644 "$ENV_FILE"
set -a; . "$ENV_FILE"; set +a
export AWS_PROFILE=$PROFILE   # the profile step 2 settled on (the env file's default may be the login profile)
# machine-local overrides (sourced after env): the credential profile step 2 chose, if it is not the shared default
if [ "$PROFILE" != "$(sed -n 's/^AWS_PROFILE=//p' "$ENV_FILE")" ]; then echo "AWS_PROFILE=$PROFILE" > "$ENV_FILE.local"; else rm -f "$ENV_FILE.local"; fi
grep -v '^#' "$ENV_FILE" | grep .

step "4 python venv ($VENV)"
python3 -c 'import sys; assert sys.version_info >= (3, 10), sys.version' || die "need python >= 3.10"
[ -x "$VENV/bin/python" ] || python3 -m venv --without-pip "$VENV"
if ! "$VENV/bin/python" -m pip --version >/dev/null 2>&1; then
  W=$(mktemp -d); whl="$W/$PIP_WHEEL_NAME"
  curl -fsSL -o "$whl" "$PIP_WHEEL_URL"
  echo "$PIP_WHEEL_SHA256  $whl" | sha256sum -c - 2>/dev/null || echo "$PIP_WHEEL_SHA256  $whl" | shasum -a 256 -c -
  "$VENV/bin/python" "$whl/pip" install --quiet "$whl"; rm -rf "$W"
fi
"$VENV/bin/python" -m pip --version

step "5 code ($CODE)"
mkdir -p "$CODE"
for R in traceDB ontolagni; do
  D="$CODE/$R"; U=$(eval echo "\${GIT_$(echo "$R" | tr '[:lower:]' '[:upper:]'):-}")  # bash 3.2 (macOS) safe
  if command -v git >/dev/null; then
    B=$(mktemp); aws s3 cp "$SETUP_S3/src/$R.bundle" "$B" --only-show-errors
    BR=$(git bundle list-heads "$B" | awk '{print $2}' | sed 's#refs/heads/##' | head -1)
    if [ ! -d "$D/.git" ]; then
      git clone -q -b "$BR" "$B" "$D" && git -C "$D" remote set-url origin "${U:-$B}"
      echo "$R: cloned ($BR $(git -C "$D" rev-parse --short HEAD))"
    else
      git -C "$D" fetch -q "$B" "+refs/heads/*:refs/remotes/setup/*"
      if [ "$(git -C "$D" rev-parse --abbrev-ref HEAD)" = "$BR" ] && [ -z "$(git -C "$D" status --porcelain --untracked-files=no)" ] \
         && git -C "$D" merge-base --is-ancestor HEAD "setup/$BR"; then
        git -C "$D" merge -q --ff-only "setup/$BR"; echo "$R: up to date with setup ($BR $(git -C "$D" rev-parse --short HEAD))"
      else
        echo "$R: kept your checkout ($(git -C "$D" rev-parse --abbrev-ref HEAD), local changes or ahead); setup copy is refs/remotes/setup/$BR"
      fi
    fi
    rm -f "$B"
  elif [ ! -d "$D" ]; then
    mkdir -p "$D"; aws s3 cp "$SETUP_S3/src/$R.tar.gz" - --only-show-errors | tar -xz -C "$D"; echo "$R: unpacked (no git)"
  fi
done

step "6 packages"
REQ="-e $CODE/traceDB[s3,zstd] -e $CODE/ontolagni botocore[crt] ${EXTRA_PIP:-}"
STAMP="$VENV/.bootstrap-req"
if [ "$(cat "$STAMP" 2>/dev/null)" != "$REQ" ] || ! "$VENV/bin/python" -c 'import tracedb, ontolagni, boto3, awscrt' 2>/dev/null; then
  # shellcheck disable=SC2086
  "$VENV/bin/python" -m pip install --quiet --disable-pip-version-check $REQ && echo "$REQ" > "$STAMP"
fi
"$VENV/bin/python" -c 'import tracedb, ontolagni, boto3; print("tracedb, ontolagni, boto3 ok")'

step "7 commands in ~/.local/bin"
for c in tracedb ontolagni ${EXTRA_BIN:-}; do [ -x "$VENV/bin/$c" ] && ln -sfn "$VENV/bin/$c" "$HOME/.local/bin/$c"; done
for c in keys skills; do
  F="$HOME/.local/bin/$c"; printf '#!/bin/sh\n# %s: wrapper (source: traceDB tools/%s.py). Written by bootstrap.\nexec "$HOME/%s/bin/python" "$HOME/%s/traceDB/tools/%s.py" "$@"\n' \
    "$c" "$c" "${VENV#$HOME/}" "${CODE#$HOME/}" "$c" > "$F.tmp"; chmod 755 "$F.tmp"; mv "$F.tmp" "$F"
done
ls "$HOME/.local/bin"

step "8 shell"
BLOCK='# >>> tracedb bootstrap >>> (managed by traceDB tools/bootstrap.sh; edits inside are overwritten)
export PATH="$HOME/.local/bin:$PATH"
[ -f "$HOME/.config/tracedb/env" ] && { set -a; . "$HOME/.config/tracedb/env"; [ -f "$HOME/.config/tracedb/env.local" ] && . "$HOME/.config/tracedb/env.local"; set +a; }
# <<< tracedb bootstrap <<<'
RCS="$HOME/.bashrc"; { [ "$OS" = Darwin ] || [ -f "$HOME/.zshrc" ]; } && RCS="$RCS $HOME/.zshrc"
for rc in $RCS; do
  touch "$rc"
  python3 - "$rc" "$BLOCK" <<'PY'
import sys, re, os
p, block = sys.argv[1], sys.argv[2]
s = open(p).read()
new = re.sub(r"(?s)# >>> tracedb bootstrap >>>.*?# <<< tracedb bootstrap <<<\n?", lambda m: block + "\n", s) if "# >>> tracedb bootstrap >>>" in s \
      else s + ("" if s.endswith("\n") or not s else "\n") + block + "\n"
if new != s:
    open(p + ".tmp", "w").write(new); os.replace(p + ".tmp", p); print("updated", p)
else:
    print("ok", p)
PY
done

step "9 keys pull (vault canonical keys/)"
"$HOME/.local/bin/keys" pull
"$HOME/.local/bin/keys" status || true

step "10 skills pull"
"$HOME/.local/bin/skills" pull
"$HOME/.local/bin/skills" status || true

step "11 ontology -> ${ONTOLOGY_DIR:-$HOME/ontology}"
O=${ONTOLOGY_DIR:-$HOME/ontology}; mkdir -p "$O"
aws s3 sync "${TRACEDB_STORE%/*}/ontology/" "$O/" --only-show-errors --exclude 'catalog.jsonl.gz'
ONTOLOGY_DIR="$O" "$HOME/.local/bin/ontolagni" --dir "$O" find skills --limit 3 | head -c 300; echo

step "12 boxes"
"$HOME/.local/bin/keys" boxes show
[ "${BOOT_NO_DOCTOR:-0}" = 1 ] || "$HOME/.local/bin/keys" doctor || true
echo
echo "bootstrap done. New shells get PATH + env; this one: . ~/.config/tracedb/env. Agents: read $O/AGENT.md and $SETUP_S3/SETUP.md."
