#!/usr/bin/env python3
"""Second ingest pass: the *useful* binaries and large files that ingest.py skipped.
  <= SMALL (64 MiB): stored in normal packs (compressed, deduplicated), no redaction (non-text).
  >  SMALL: stored as blobs (tracedb.blob): blobs/<sha256> + pointer entry at the real path.
Skips reproducible/vendored binaries (.o .so .dll .dylib .a .whl .gguf .ptx, codex/plugin binaries), secrets, weights of other
people's models are NOT special-cased: if it is in a project dir and has a useful extension it is uploaded.
usage: ingest_blobs.py [--dry] SRC=PREFIX ..."""
import json, os, sys, time
sys.path.insert(0, os.path.expanduser("~/tracedb-work")); sys.path.insert(0, os.path.expanduser("~/tracedb-work/traceDB"))
import ingest as I
from tracedb.store import open_store
from tracedb.blob import put_blob, sha256_file

USEFUL = {".tpq", ".npy", ".npz", ".pkl", ".pickle", ".parquet", ".arrow", ".pt", ".pth", ".bin", ".ckpt", ".safetensors", ".h5",
          ".zst", ".gz", ".tgz", ".tar", ".zip", ".csv", ".tsv", ".jsonl", ".json", ".txt", ".log", ".md",
          ".ncu-rep", ".nsys-rep", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".mp4", ".mov", ".wav", ".onnx", ".sqlite", ".db"}
NEVER = {".o", ".so", ".dll", ".dylib", ".a", ".whl", ".gguf", ".ptx", ".iso", ".pyc", ".exe", ".class", ".sqlite-wal", ".sqlite-shm", ".db-wal", ".db-shm"}
NEVER_PATH = ("/.codex/packages/", "/.codex/plugins/", "/.codex/cache/", "/.codex/.tmp", "/Library/", "/node_modules/",
              "/links/conductor-app/bin/", "/claude-desktop/claude-code", "/Contents/MacOS/", "/l12-work/art/", "/flapping-airplanes/llm/baseline/golden/")
SMALL = 64 << 20


def main():
    a = sys.argv[1:]; dry = "--dry" in a; a = [x for x in a if x != "--dry"]
    a = [y for x in a for y in (open(x[1:]).read().split() if x.startswith("@") else [x])]  # @file = list of SRC=PREFIX
    st = None if dry else open_store(os.environ["TRACEDB_STORE"])
    log = open(os.path.expanduser("~/tracedb-work/logs/blob-ingest.jsonl"), "a")
    pend_small, pend_big = [], []
    seen = set()
    for spec in a:
        src, prefix = spec.split("=", 1); src = os.path.expanduser(src)
        base = src if os.path.isdir(src) else os.path.dirname(src)
        for dp, f in I.walk(src):
            full = os.path.join(dp, f); ext = os.path.splitext(f)[1].lower()
            if os.path.islink(full) or full in seen: continue
            if f in I.SKIP_NAMES or I.SKIP_PAT.search(full) and not f.endswith(".pub"): continue
            if ext in NEVER or any(n in full for n in NEVER_PATH): continue
            try: sz = os.path.getsize(full)
            except OSError: continue
            was_text_ingested = ext in (".json", ".jsonl", ".md", ".txt", ".log", ".csv", ".tsv") and sz <= 128 << 20
            if was_text_ingested: continue  # already covered by ingest.py
            if ext not in USEFUL and sz <= 128 << 20: continue
            if ext not in USEFUL and sz > 128 << 20 and (ext in I.SKIP_EXT): continue
            seen.add(full)
            (pend_small if sz <= SMALL else pend_big).append((sz, full, prefix + os.path.relpath(full, base), int(os.path.getmtime(full))))
    print(json.dumps({"small": len(pend_small), "small_gib": round(sum(x[0] for x in pend_small) / 2**30, 2),
                      "blobs": len(pend_big), "blob_gib": round(sum(x[0] for x in pend_big) / 2**30, 2)}), flush=True)
    if dry:
        for sz, full, path, _ in sorted(pend_big, reverse=True)[:40]: print(round(sz / 2**20), "MB", path)
        return
    t0 = time.time(); batch, bsz, n = [], 0, 0
    for sz, full, path, mt in pend_small:
        try: data = open(full, "rb").read()
        except OSError: continue
        batch.append((path, data, {"mtime": mt, "binary": True})); bsz += len(data); n += 1
        if bsz >= 256 << 20:
            st.put(batch); batch, bsz = [], 0; print("small", n, round(time.time() - t0), flush=True)
    if batch: st.put(batch)
    print(json.dumps({"small_done": n, "secs": round(time.time() - t0)}), flush=True)
    for sz, full, path, mt in sorted(pend_big):
        t1 = time.time()
        try: r = put_blob(st, path, full, {"mtime": mt})
        except Exception as e:
            log.write(json.dumps({"path": path, "error": repr(e)}) + "\n"); log.flush(); print("ERR", path, repr(e)[:200], flush=True); continue
        log.write(json.dumps({**r, "secs": round(time.time() - t1)}) + "\n"); log.flush()
        print(json.dumps({"blob": path, "mb": sz >> 20, "uploaded": r["uploaded"], "secs": round(time.time() - t1)}), flush=True)
main()
