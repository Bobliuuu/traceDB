"""tracedb CLI. Store from --store or $TRACEDB_STORE (dir or s3://bucket/prefix)."""
import argparse, json, os, sys, time
from .store import open_store, BudgetExceeded, NotFound


def sniff(full: str, meta: dict) -> dict:
    """Cheap, deterministic metadata at ingest: top-level keys of the first json/jsonl record."""
    import json as _j
    try:
        if full.endswith((".jsonl", ".json")) and os.path.getsize(full) < (64 << 20):
            with open(full, "rb") as f:
                line = f.readline(1 << 16) if full.endswith(".jsonl") else f.read(1 << 16)
            o = _j.loads(line)
            if isinstance(o, dict):
                return {**meta, "keys": list(o)[:24]}
    except Exception:
        pass
    return meta


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tracedb")
    ap.add_argument("--store", default=os.environ.get("TRACEDB_STORE"))
    ap.add_argument("--json", action="store_true")
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("put"); p.add_argument("root"); p.add_argument("--prefix", default="")
    p.add_argument("--meta", default="{}")
    p = sp.add_parser("sync")
    p = sp.add_parser("locate"); p.add_argument("path")
    p = sp.add_parser("ls"); p.add_argument("prefix", nargs="?", default=""); p.add_argument("--limit", type=int, default=100)
    p.add_argument("--glob")
    p = sp.add_parser("summary"); p.add_argument("prefix", nargs="?", default=""); p.add_argument("--depth", type=int, default=1)
    p = sp.add_parser("plan"); p.add_argument("paths", nargs="+")
    p = sp.add_parser("get"); p.add_argument("path"); p.add_argument("--start", type=int, default=0)
    p.add_argument("--end", type=int); p.add_argument("--max-bytes", type=int)
    p.add_argument("--head", type=int); p.add_argument("--tail", type=int)
    p = sp.add_parser("verify"); p.add_argument("--deep", action="store_true")
    sp.add_parser("compact")
    p = sp.add_parser("gc"); p.add_argument("--apply", action="store_true"); p.add_argument("--grace", type=float, default=3600)
    p = sp.add_parser("export-catalog"); p.add_argument("--out", default="-")
    p = sp.add_parser("blob-put", help="upload a large local file as a content-addressed blob + pointer entry")
    p.add_argument("path"); p.add_argument("file"); p.add_argument("--meta", default="{}")
    p = sp.add_parser("blob-get", help="ranged, budgeted read of a blob (default stdout)")
    p.add_argument("path"); p.add_argument("--start", type=int, default=0); p.add_argument("--end", type=int)
    p.add_argument("--max-bytes", type=int); p.add_argument("--out")
    p = sp.add_parser("blob-info"); p.add_argument("path")
    p = sp.add_parser("search", help="full-text search over the store's FTS index (index/fts.sqlite)")
    p.add_argument("text", nargs="+"); p.add_argument("--prefix", default=""); p.add_argument("--limit", type=int, default=10)
    p.add_argument("--paths", action="store_true", help="search file paths only")
    p.add_argument("--index", default="index/fts.sqlite", help="legacy single-file index key (used only if no v3 index exists)")
    p.add_argument("--index-store", help="store holding the v3 index (default: the data store)")
    p.add_argument("--no-snippets", action="store_true", help="skip re-reading hit bytes for snippets")
    p = sp.add_parser("index", help="build/update the v3 sharded search index (reads text from the store, writes index/v3/*)")
    p.add_argument("action", choices=["build", "update", "stats"])
    p.add_argument("--prefix", action="append", default=None, help="limit coverage (repeatable); default whole store")
    p.add_argument("--dest-store", help="write the index here instead of into the data store")
    p.add_argument("--jobs", type=int, default=4); p.add_argument("--work")
    a = ap.parse_args(argv)
    if not a.store:
        print("set --store or TRACEDB_STORE", file=sys.stderr); return 1
    st = open_store(a.store)
    os.environ.setdefault("TRACEDB_SYNC_TTL", "0")
    out = lambda o: print(json.dumps(o) if a.json or not isinstance(o, str) else o)
    try:
        if a.cmd == "sync":
            out(st.sync())
        elif a.cmd == "put":
            st.sync()
            root = os.path.abspath(a.root)
            meta = json.loads(a.meta)
            def gen():
                if os.path.isfile(root):
                    yield (a.prefix + os.path.basename(root), root, sniff(root, meta)); return
                for dp, _, fs in os.walk(root):
                    for f in fs:
                        full = os.path.join(dp, f)
                        yield (a.prefix + os.path.relpath(full, root), full, sniff(full, meta))
            t = time.time(); r = st.put(gen()); r["secs"] = round(time.time() - t, 2); out(r)
        elif a.cmd == "locate":
            st.sync_if_stale(); out(st.locate(a.path) or {"error": "not found"})
        elif a.cmd == "ls":
            st.sync_if_stale(); out(st.ls(a.prefix, a.limit, a.glob))
        elif a.cmd == "summary":
            st.sync(); out(st.summary(a.prefix, a.depth))
        elif a.cmd == "plan":
            st.sync_if_stale(); out(st.plan(a.paths).as_dict())
        elif a.cmd == "get":
            st.sync_if_stale()
            if a.head: d = st.head(a.path, a.head)
            elif a.tail: d = st.tail(a.path, a.tail)
            else: d = st.read(a.path, a.start, a.end, a.max_bytes)
            sys.stdout.buffer.write(d)
        elif a.cmd == "verify":
            r = st.verify(a.deep); out(r); return 0 if r["ok"] else 2
        elif a.cmd == "gc":
            out(st.gc(a.grace, dry_run=not a.apply))
        elif a.cmd == "compact":
            out(st.compact())
        elif a.cmd == "blob-put":
            from .blob import put_blob
            st.sync(); out(put_blob(st, a.path, a.file, json.loads(a.meta)))
        elif a.cmd in ("blob-get", "blob-info"):
            from .blob import get_blob, blob_info
            st.sync_if_stale()
            if a.cmd == "blob-info":
                out(blob_info(st, a.path) or {"error": "not a blob"})
            else:
                d = get_blob(st, a.path, a.start, a.end, a.max_bytes)
                (open(a.out, "wb") if a.out else sys.stdout.buffer).write(d)
        elif a.cmd == "search":
            from .search import SearchIndex
            from .index import IndexSet
            st.sync_if_stale()
            ist = open_store(a.index_store) if a.index_store else st
            if ist is not st:
                ist.sync_if_stale()
            ix = IndexSet.open(ist, st)
            if ix is not None:
                out(ix.search(" ".join(a.text), a.prefix, a.limit, a.paths, snippets=not a.no_snippets)); return 0
            e = st.locate(a.index)
            if not e:
                print(f"no index at {a.index}; build with build_index.py", file=sys.stderr); return 4
            local = os.path.join(st.dir, f"fts-{e['sha'][:16]}.sqlite")
            if not os.path.exists(local):  # one-time download of the (single-file) index, cached by content hash
                st.read_to_file(a.index, local + ".tmp")  # bounded memory (read() of 4 GB held ~3 copies in RAM)
                os.replace(local + ".tmp", local)
            out(SearchIndex.open(local).search(" ".join(a.text), a.prefix, a.limit, a.paths))
        elif a.cmd == "index":
            from .index import build_index, update_index, IndexSet
            dest = open_store(a.dest_store) if a.dest_store else st
            pf = tuple(a.prefix or [""])
            if a.action == "build":
                out(build_index(st, dest, pf, a.jobs, a.work))
            elif a.action == "update":
                out(update_index(st, dest, pf, a.jobs, a.work))
            else:
                dest.sync(); ix = IndexSet.open(dest, st); out(ix.describe() if ix else {"error": "no v3 index"})
        elif a.cmd == "export-catalog":
            st.sync()
            f = sys.stdout if a.out == "-" else open(a.out, "w")
            for r in st.export_catalog():
                f.write(json.dumps(r, separators=(",", ":")) + "\n")
    except BudgetExceeded as e:
        print(f"BUDGET: {e}", file=sys.stderr); print(json.dumps(e.plan.as_dict()), file=sys.stderr); return 3
    except NotFound as e:
        print(f"not found: {e}", file=sys.stderr); return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
