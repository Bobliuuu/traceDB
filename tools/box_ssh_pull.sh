#!/bin/bash
# box_ssh_pull.sh -- give the box the user's ssh keys + config from the vault (keyless: instance role reads the vault) so it can reach
# every other box by alias. Deliberately NOT `keys pull`: that also writes ~/.aws/credentials (static MFA-denied keys would shadow the instance role).
# OPT-IN: copies every vault ssh key to this box, i.e. gives it a route to every box. Run only with the user's approval.
# Never overwrites authorized_keys. Never prints key material.
set -u
W=${TRACEDB_WORK:-$HOME/tracedb-work}; . ~/.config/tracedb/env; unset AWS_PROFILE
T=$W/venv/bin/tracedb; mkdir -p ~/.ssh; chmod 700 ~/.ssh
$T --store "$VAULT_STORE" ls keys/files/.ssh/ --limit 1000 | python3 -c '
import json,sys
for e in json.load(sys.stdin): print(e["path"])' | while read -r p; do
  n=${p#keys/files/.ssh/}; case "$n" in authorized_keys|known_hosts*|tracedb-sync-admin*) continue;; esac
  $T --store "$VAULT_STORE" get --max-bytes -1 "$p" > ~/.ssh/"$n.new" && mv ~/.ssh/"$n.new" ~/.ssh/"$n"
  case "$n" in *.pub|config*) chmod 644 ~/.ssh/"$n";; *) chmod 600 ~/.ssh/"$n";; esac
done
grep -q StrictHostKeyChecking ~/.ssh/config 2>/dev/null || printf '\nHost *\n  StrictHostKeyChecking accept-new\n  BatchMode yes\n' >> ~/.ssh/config
echo "ssh keys: $(ls ~/.ssh | grep -vc -E 'pub|config|known|authorized')  config lines: $(wc -l < ~/.ssh/config)"
