#!/bin/bash
# full refresh: re-ingest every source (idempotent), vault originals, blobs, then rebuild_all.sh
cd ~/tracedb-work
export AWS_PROFILE=lab TRACEDB_STORE=s3://tracedb-all-618283574049-us-east-1/store VAULT_STORE=s3://tracedb-vault-618283574049-us-east-1/store
PY=venv/bin/python
ALL="$(cat srcs.txt srcs2.txt srcs3.txt | tr '\n' ' ')"
$PY ingest.py $ALL > logs/refresh-ingest.out 2>&1
$PY vault_ingest.py $ALL > logs/refresh-vault.out 2>&1
printf '%s\n' $ALL > srcs-all.txt
$PY ingest_blobs.py @srcs-all.txt > logs/refresh-blobs.out 2>&1
bash rebuild_all.sh > logs/refresh-rebuild.out 2>&1
echo DONE > logs/refresh-done.flag
