#!/bin/bash
# background: retry spark2 harvest until reachable; once refresh-done.flag exists and S3 mirror is done, run the ingest.
cd ~/tracedb-work; pulled=0
while :; do
  if [ $pulled = 0 ]; then bash harvest-spark2.sh >> logs/spark2-harvest.out 2>&1 && pulled=1; fi
  if [ -f logs/refresh-done.flag ] && [ -f ~/boxharvest/s3-mirror-20261112/.mirror-done ]; then
    bash logs/spark2-ingest-cmd.sh
    [ $pulled = 1 ] && break
    # spark2 still unreachable: keep retrying; if it arrives, re-run idempotent ingest
    while [ $pulled = 0 ]; do sleep 600; bash harvest-spark2.sh >> logs/spark2-harvest.out 2>&1 && pulled=1; done
    bash logs/spark2-ingest-cmd.sh; break
  fi
  sleep 300
done
echo finished > logs/spark2-driver.done
