#!/usr/bin/env bash
# install_keys_push_timer.sh -- keep the tracedb vault from going stale: a systemd user timer runs `keys push`
# every 15 min (it uploads only changed tracked files, plus new files in ~/.ssh). Log: ~/tracedb-work/logs/keys-push.log
# Run on the ONE machine that is the vault's writer (the laptop). Other boxes only pull, so two writers never flap.
# Remove: systemctl --user disable --now keys-push.timer
set -euo pipefail
mkdir -p ~/.config/systemd/user ~/tracedb-work/logs
cat > ~/.config/systemd/user/keys-push.service <<'EOF'
[Unit]
Description=Push changed keys/credentials/config to the tracedb vault (keys push)

[Service]
Type=oneshot
Environment=AWS_PROFILE=lab
ExecStart=/bin/sh -c '{ date -u +%%FT%%TZ; %h/.local/bin/keys push; } >> %h/tracedb-work/logs/keys-push.log 2>&1'
EOF
cat > ~/.config/systemd/user/keys-push.timer <<'EOF'
[Unit]
Description=keys push every 15 min (vault never goes stale)

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
Persistent=true

[Install]
WantedBy=timers.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now keys-push.timer
systemctl --user start keys-push.service
tail -3 ~/tracedb-work/logs/keys-push.log
systemctl --user list-timers | grep keys-push
