#!/usr/bin/env python3
"""Build the FTS5 search index for the same sources/filters/redaction as ingest.py, then upload it as index/fts.sqlite.
usage: build_index.py [--no-upload] OUT.sqlite SRC=PREFIX ...   (SRC may be a dir or file; PREFIX must match ingest's)"""
import json, os, sys, time
sys.path.insert(0, os.path.expanduser("~/tracedb-work")); sys.path.insert(0, os.path.expanduser("~/tracedb-work/traceDB"))
import ingest as I
from tracedb.search import SearchIndex
from tracedb.store import open_store

def main():
    a = sys.argv[1:]; up = "--no-upload" not in a; a = [x for x in a if x != "--no-upload"]
    out, specs = a[0], a[1:]
    idx = SearchIndex.create(out); t0 = time.time(); n = 0
    for spec in specs:
        src, prefix = spec.split("=", 1); src = os.path.expanduser(src)
        base = src if os.path.isdir(src) else os.path.dirname(src)
        for dp, f in I.walk(src):
            full = os.path.join(dp, f); rel = os.path.relpath(full, base); ext = os.path.splitext(f)[1].lower()
            if os.path.islink(full) or f in I.SKIP_NAMES or (I.SKIP_PAT.search(full) and not f.endswith(".pub")) or ext in I.SKIP_EXT:
                continue
            try:
                sz = os.path.getsize(full)
                if sz > 128 << 20: continue
                head = open(full, "rb").read(I.__dict__.get("IDX_READ", 0) or sz) if sz <= (4 << 20) else None
                if head is None:  # big file: head + tail only (indexer caps to the same), keep byte offsets honest
                    with open(full, "rb") as fh:
                        fh.seek(0); h = fh.read(192 << 10); fh.seek(max(0, sz - (32 << 10))); t = fh.read()
                    # build a sparse buffer: head, gap (not indexed), tail -> pass whole size via padding-free path
                    data = h + b"\0" * 0
                    idx.add(prefix + rel, h, size=sz)
                    # tail chunks at true offsets
                    tail_start = max(0, sz - (32 << 10)); nl = t.find(b"\n"); 
                    if nl != -1 and tail_start: t, tail_start = t[nl + 1:], tail_start + nl + 1
                    from tracedb.search import _chunks, _is_text
                    if _is_text(rel, h):
                        t, _ = I.redact(t)
                        for off, tx in _chunks(rel, t, tail_start):
                            idx.c.execute("INSERT INTO chunks(text,path,off) VALUES (?,?,?)", (tx, prefix + rel, off))
                    n += 1; continue
                data, _ = I.redact(head)
                idx.add(prefix + rel, data, size=len(data)); n += 1
            except OSError:
                continue
            if n % 5000 == 0: print(n, round(time.time() - t0), flush=True); idx.c.commit()
    s = idx.stats(); idx.close(); print(json.dumps({"index": out, **s, "secs": round(time.time() - t0, 1), "mb": os.path.getsize(out) >> 20}))
    if up:
        st = open_store(os.environ["TRACEDB_STORE"]); print(st.put([("index/fts.sqlite", out, {"kind": "search-index", **s})]))
main()
