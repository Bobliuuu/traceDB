#!/usr/bin/env python3
"""Vault pass: store ORIGINAL secret-bearing bytes in a separate, KMS-encrypted traceDB store.
Same relative paths/prefixes as the main (redacted) store, so  main: traces/codex/x.jsonl  <->  vault: traces/codex/x.jsonl
usage: vault_ingest.py [--all] SRC=PREFIX ...
  --all   (before a SRC) every file under that SRC is secret by definition (ssh dir, keys dir, cloud creds): store all.
  default only files the main ingest skipped as secret-name, or that contained redactable secrets, are stored.
Never prints secret content. Log: logs/vault-ingest.jsonl (paths + counts only)."""
import json, os, sys, time
sys.path.insert(0, os.path.expanduser("~/tracedb-work")); sys.path.insert(0, os.path.expanduser("~/tracedb-work/traceDB"))
import ingest as I
from tracedb.store import open_store

def main():
    st = open_store(os.environ["VAULT_STORE"]); log = open(os.path.expanduser("~/tracedb-work/logs/vault-ingest.jsonl"), "a")
    allmode = False
    for spec in sys.argv[1:]:
        if spec == "--all": allmode = True; continue
        src, prefix = spec.split("=", 1); src = os.path.expanduser(src)
        base = src if os.path.isdir(src) else os.path.dirname(src)
        batch, bsz, nf, t0 = [], 0, 0, time.time()
        def flush():
            nonlocal batch, bsz
            if batch: st.put(batch)
            batch, bsz = [], 0
        for dp, f in I.walk(src):
            full = os.path.join(dp, f)
            if os.path.islink(full) or os.path.splitext(f)[1].lower() in (".sock",): continue
            if allmode and "/cache/" in full + "/": continue
            rel = os.path.relpath(full, base); ext = os.path.splitext(f)[1].lower()
            try:
                sz = os.path.getsize(full)
                if sz > 32 << 20: continue
                named = f in I.SKIP_NAMES or bool(I.SKIP_PAT.search(full)) and not f.endswith(".pub")
                if not (allmode or named or ext in (".pem", ".key", ".p12", ".pfx", ".keystore")):
                    if ext in I.SKIP_EXT: continue
                    data = open(full, "rb").read()
                    if b"\0" in data[:4096] and ext not in (".jsonl", ".json", ".md", ".txt", ".log"): continue
                    if I.redact(data)[1] == 0: continue
                else:
                    data = open(full, "rb").read()
            except OSError:
                continue
            batch.append((prefix + rel, data, {"vault": True, "mtime": int(os.path.getmtime(full))})); bsz += len(data); nf += 1
            if bsz >= 128 << 20: flush()
        flush()
        print(json.dumps({"src": src, "prefix": prefix, "secret_files": nf, "secs": round(time.time() - t0, 1)}), flush=True)
        log.write(json.dumps({"src": src, "prefix": prefix, "secret_files": nf}) + "\n"); log.flush(); allmode = False
main()
