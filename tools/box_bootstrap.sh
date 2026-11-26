#!/bin/bash
# box_bootstrap.sh -- turn a fresh Ubuntu 24.04 EC2 instance (instance profile tracedb-sync attached) into the tracedb sync box.
# Idempotent; re-run any time. Run as the default sudo user (ubuntu):
#   curl -fsSL https://raw.githubusercontent.com/Bobliuuu/traceDB/main/tools/box_bootstrap.sh | bash     (or from a checkout)
# Needs only the instance role (keyless). No secrets, IPs or keys in this file. See docs/SYNC_BOX.md.
set -euo pipefail
step() { printf '\n=== %s\n' "$*"; }
B=tracedb-all-618283574049-us-east-1; W=$HOME/tracedb-work; INBOX=/srv/tracedb-inbox/mac
step "1 packages"
sudo DEBIAN_FRONTEND=noninteractive apt-get update -y -q
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3-venv python3-pip git rsync unzip curl jq sqlite3 fail2ban gh
if ! command -v aws >/dev/null; then
  curl -fsSLo /tmp/awscli.zip "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip"; unzip -q -o /tmp/awscli.zip -d /tmp; sudo /tmp/aws/install --update; rm -rf /tmp/aws /tmp/awscli.zip
fi
aws sts get-caller-identity --query Arn --output text   # must be the instance role, not a user
step "2 code + venv ($W)"
mkdir -p "$W"/{logs,state} ~/.local/bin ~/.config/tracedb; cd "$W"
git config --global credential.https://github.com.helper '!gh auth git-credential'
[ -d traceDB/.git ] || git clone -q https://github.com/Bobliuuu/traceDB.git traceDB
[ -d ontolagni/.git ] || git clone -q https://github.com/Bobliuuu/ontolagni.git ontolagni
git -C traceDB pull -q --ff-only || true; git -C ontolagni pull -q --ff-only || true
[ -x venv/bin/python ] || python3 -m venv venv
venv/bin/pip install -q -U pip
venv/bin/pip install -q -e "./traceDB[s3,zstd,lazy]" -e ./ontolagni boto3 zstandard apsw
for f in ingest.py vault_ingest.py ingest_blobs.py describe.py; do ln -sf traceDB/tools/$f $f; done
ln -sf "$W/venv/bin/tracedb" ~/.local/bin/tracedb; ln -sf "$W/venv/bin/ontolagni" ~/.local/bin/ontolagni
step "3 env (keyless: no AWS_PROFILE)"
aws s3 cp "s3://$B/setup/bootstrap.env" - --only-show-errors | grep -v -E '^(AWS_PROFILE|ONTOLOGY_DIR|ONTOLAGNI_DIR)=' > ~/.config/tracedb/env
echo "ONTOLOGY_DIR=$HOME/ontology" >> ~/.config/tracedb/env; echo "TRACEDB_KEYLESS=1" >> ~/.config/tracedb/env
grep -q 'tracedb/env' ~/.bashrc || printf '\nset -a; . ~/.config/tracedb/env; set +a\nexport PATH=$HOME/.local/bin:$PATH\n' >> ~/.bashrc
step "4 inbox user (rrsync-restricted push key from s3://$B/setup/tracedb-sync-push.pub)"
sudo id inbox >/dev/null 2>&1 || sudo useradd -m -s /bin/bash inbox
sudo mkdir -p $INBOX/in $INBOX/ack /home/inbox/.ssh
PUB=$(aws s3 cp "s3://$B/setup/tracedb-sync-push.pub" - --only-show-errors)
echo "command=\"/usr/bin/rrsync $INBOX\",restrict $PUB" | sudo tee /home/inbox/.ssh/authorized_keys >/dev/null
sudo chown -R inbox:inbox /home/inbox/.ssh $INBOX; sudo chmod 700 /home/inbox/.ssh; sudo chmod 600 /home/inbox/.ssh/authorized_keys
sudo chmod -R a+rX $INBOX; sudo chown ubuntu $INBOX/ack   # ubuntu (ingest user) reads in/, writes ack/
sudo chmod 755 $INBOX $INBOX/ack
sudo usermod -aG inbox ubuntu || true
printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\n' | sudo tee /etc/ssh/sshd_config.d/99-tracedb.conf >/dev/null
sudo systemctl reload ssh || sudo systemctl reload sshd || true
sudo systemctl enable --now fail2ban >/dev/null 2>&1 || true
step "5 ssh keys for other boxes: OPT-IN only (TRACEDB_SSH_PULL=1, user approval needed; see docs/SYNC_BOX.md)"
if [ "${TRACEDB_SSH_PULL:-0}" = 1 ]; then bash traceDB/tools/box_ssh_pull.sh || echo "ssh pull failed"; else echo skipped; fi
step "6 systemd timers"
sudo tee /etc/systemd/system/tracedb-sync.service >/dev/null <<UNIT
[Unit]
Description=tracedb autosync cycle (inbox, github, index, ontology)
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
User=$USER
Nice=10
TimeoutStartSec=6h
ExecStart=/bin/bash $W/traceDB/tools/box_sync.sh
UNIT
sudo tee /etc/systemd/system/tracedb-sync.timer >/dev/null <<UNIT
[Unit]
Description=tracedb autosync every 30 min
[Timer]
OnBootSec=3min
OnUnitInactiveSec=30min
RandomizedDelaySec=60
Persistent=true
[Install]
WantedBy=timers.target
UNIT
sudo tee /etc/systemd/system/tracedb-alive.service >/dev/null <<UNIT
[Unit]
Description=tracedb heartbeat refresh
[Service]
Type=oneshot
User=$USER
ExecStart=/bin/bash $W/traceDB/tools/box_alive.sh
UNIT
sudo tee /etc/systemd/system/tracedb-alive.timer >/dev/null <<UNIT
[Unit]
Description=tracedb heartbeat every 10 min
[Timer]
OnBootSec=1min
OnUnitActiveSec=10min
[Install]
WantedBy=timers.target
UNIT
sudo systemctl daemon-reload; sudo systemctl enable --now tracedb-sync.timer tracedb-alive.timer
systemctl list-timers 'tracedb-*' --no-pager | head -6
step "done"
