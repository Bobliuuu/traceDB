#!/bin/bash
# waits for the one-off refresh to finish, then runs the first full auto-sync (git bundles etc.)
cd ~/tracedb-work
while [ ! -f logs/refresh-done.flag ]; do sleep 60; done
bash auto-sync.sh
echo done > logs/first-autosync-done.flag
