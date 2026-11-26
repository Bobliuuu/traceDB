# Sync box: permanent, keyless tracedb sync

An always-on EC2 instance keeps the tracedb stores current. It uses an EC2 **instance role** (temporary credentials from
IMDSv2, rotated by AWS), so it never needs `aws login`, has no static keys, and does not depend on any laptop.
No IPs or keys are recorded here: machines are reached only through `~/.ssh/config` aliases.

## What runs where

| Piece | Where | What it does |
|---|---|---|
| `tools/box_sync.sh` | box, `tracedb-sync.timer` (30 min after the last cycle) | self-update, Mac inbox ingest, GitHub capture, index update, verify, ontology (every 6 h), heartbeat |
| `tools/box_heartbeat.sh` | box, `tracedb-alive.timer` (10 min) | writes `health/heartbeat.json` in the main bucket |
| `tools/mac_push.py` | Mac, launchd `com.tracedb.push` (20 min) | rsync new files to the box inbox; prune files the box has confirmed |

- Instance: tag `Name=tracedb-sync`, m6i.xlarge, 300 GB encrypted gp3 (kept on termination), us-east-1, instance profile
  `tracedb-sync`, termination protection on. Security group `tracedb-sync`: port 22 only, key authentication only.
- Role `tracedb-sync`: read/write the main, vault and ontolagni buckets, read the experiment buckets, KMS through S3,
  SSM core, and `cloudwatch:PutMetricData` limited to namespace `Tracedb/Sync`.
- SSH aliases: `tracedb-sync` (admin, user `ubuntu`) and `tracedb-sync-push` (user `inbox`). Both private keys are also in the
  vault under `home/ssh/`. Without SSH, use SSM Run Command or Session Manager.

## Mac push

The push key can only run rsync inside the inbox directory (`authorized_keys`: `command="rrsync <inbox>",restrict`).
It needs real rsync (`brew install rsync`); macOS `openrsync` is rejected by `rrsync`. No AWS credentials are used.

Sources are listed in `~/.config/tracedb-push.tsv`: `SRC <TAB> STORE_PREFIX <TAB> PRUNE_DAYS <TAB> excludes`
(`0` never delete, `-1` delete as soon as confirmed). To hand any file to tracedb, drop it in `~/tracedb-outbox/`.

A local file is deleted only if all of these hold:

1. The box found the **sha256 of the original bytes** in a store: main entry, main blob, or vault entry. A clean ingest
   exit is not enough, because ingest skips some file types. Skipped files are never confirmed.
2. The local file still has that sha256.
3. It is older than the source's `PRUNE_DAYS`.
4. It is not under a `memory/` directory, and (for age-based sources) is not `.md`, `.toml` or `.yaml`.

Status: `~/.cache/tracedb-push/status.json`. Run now: `launchctl kickstart gui/$(id -u)/com.tracedb.push`.
Dry run without deleting: `python3 ~/.local/share/tracedb/mac_push.py --no-prune`.

## Health and alerts

- `tracedb health` (any machine with read access) prints the heartbeat; exit 0 fresh, 2 if the last good cycle or the
  heartbeat is older than 3 h, 3 if unreadable.
- CloudWatch alarm `tracedb-sync-stale`: no `Tracedb/Sync SyncOK` datapoint for 3 h, sends to SNS topic
  `tracedb-sync-alerts`. **The email subscription only works after the owner clicks the confirmation link.**
- Alarm `tracedb-sync-recover` recovers the instance on an EC2 system status failure.

## Runbook (on the box: `ssh tracedb-sync`)

    systemctl list-timers 'tracedb-*'
    sudo systemctl start tracedb-sync.service          # run a cycle now
    ls -t ~/tracedb-work/logs/box-*.log | head -1      # latest log; failures also in logs/ALERT.txt
    FORCE_ONTOLOGY=1 bash ~/tracedb-work/traceDB/tools/box_sync.sh
    touch ~/tracedb-work/state/gh.hold                 # pause GitHub capture (rm to resume)

Never put AWS credentials on the box (`~/.aws/credentials` would shadow the instance role), and do not run
`keys pull` or `bootstrap.sh` there.

### Rebuild from nothing

Launch Ubuntu 24.04 with instance profile `tracedb-sync` and at least 16 GB RAM and 200 GB disk, then run
`tools/box_bootstrap.sh` as `ubuntu`. It reads the push public key from `setup/tracedb-sync-push.pub` in the main bucket.
Update the `HostName` of both aliases, and the alarm's instance id.

### GitHub token

The box reads the owner's existing token from the vault (`home/gh-current/hosts.yml`) into one process environment for
the capture step. It is never written to the box's disk or logged. To replace it with a narrower one, create a
fine-grained token (all repositories, Contents and Metadata read-only) and store it at that vault path in the same
`hosts.yml` format.

### Other machines as sources (disabled by default)

`~/tracedb-work/box-pull.txt` lines `<ssh-alias>:<remote-dir>=<store-prefix>` make the box pull from another machine.
It needs SSH keys on the box, which `tools/box_ssh_pull.sh` can restore from the vault. That gives the box a route to
every machine, so it is **opt-in and needs the owner's approval**. Nothing is enabled today.

## Not covered

Modal and Vast artifacts are not pulled by the box. Work on other machines reaches tracedb only through GitHub, the
buckets, the Mac push, or an approved `box-pull.txt` entry.

## GitHub repo capture
`tools/gh_capture.py` captures every repo of github.com/Bobliuuu (`gh repo list`, plus `~/tracedb-work/srcs-github.txt`, one owner/name per line) to
`repos/<name>/.git-bundle/` (restore: `tools/restore_repo.sh repos/<name>`). It re-captures a repo only when `pushedAt` changes (`state/gh-capture.json`),
mirror-clones one repo at a time into `gh-cache/` and deletes the clone. First full run (2026-11-26): 194 non-empty repos, 0 failures.
`tools/auto-sync.mac.sh` is the retired Mac cycle (now with alerts in `logs/ALERT.txt` and `state/heartbeat.json`).
