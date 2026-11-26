# systemd user units for the search mirrors and publishers

Install: copy the pair into `~/.config/systemd/user/`, then `systemctl --user daemon-reload && systemctl --user enable --now <name>.timer`.

| unit | where | runs |
|---|---|---|
| ontology-sync | every machine that searches | `~/.local/bin/ontology-sync` = `tools/ontology_sync.sh`, every 5 min: pull the ontology, sync the catalog, prefetch index generations |
| ops-publish | the ops-page host only | `tools/publish_changed.sh`, every 10 min: publish changed ops/memory pages, ingest new chat transcripts |
| memory-push | a workstation with a Claude memory dir | `~/.local/bin/memory-push` = `tools/memory_push.sh`, every 10 min: copy memory + transcripts to the ops-page host |
