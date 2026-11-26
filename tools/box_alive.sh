#!/bin/bash
# every 10 min (systemd tracedb-alive.timer): refresh heartbeat time without touching last_ok. Skipped while a cycle holds the lock? no: harmless.
exec bash "$(dirname "$0")/box_heartbeat.sh" alive
