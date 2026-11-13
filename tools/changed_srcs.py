#!/usr/bin/env python3
"""changed_srcs.py STAMP_FILE LIST... : print the SRC=PREFIX tokens (from the lists) that hold a file newer than STAMP (all if no stamp).
Used by auto-sync.sh for incremental ingest. Prunes the same heavy dirs ingest.py skips."""
import os, subprocess, sys
stamp, lists = sys.argv[1], sys.argv[2:]
seen = set()
for fn in lists:
    for tok in open(os.path.expanduser(fn)).read().split():
        if "=" not in tok or tok in seen: continue
        seen.add(tok)
        src = os.path.expanduser(tok.split("=", 1)[0])
        if not os.path.exists(src): continue
        if not os.path.exists(stamp) or os.path.isfile(src):
            if os.path.isfile(src) and os.path.exists(stamp) and os.path.getmtime(src) <= os.path.getmtime(stamp): continue
            print(tok); continue
        cmd = ["find", src, "(", "-name", ".git", "-o", "-name", "node_modules", "-o", "-name", "__pycache__", "-o", "-name", "venv", ")", "-prune",
               "-o", "-type", "f", "-newer", stamp, "-print", "-quit"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.stdout.strip(): print(tok)
