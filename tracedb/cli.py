"""tracedb CLI. Store from --store or $TRACEDB_STORE (dir or s3://bucket/prefix)."""
import argparse, json, os, sys, time
from .store import open_store, BudgetExceeded, NotFound


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
    p = sp.add_parser("export-catalog"); p.add_argument("--out", default="-")
    a = ap.parse_args(argv)
    if not a.store:
        print("set --store or TRACEDB_STORE", file=sys.stderr); return 1
    st = open_store(a.store)
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
                    yield (a.prefix + os.path.basename(root), root, meta); return
                for dp, _, fs in os.walk(root):
                    for f in fs:
                        full = os.path.join(dp, f)
                        yield (a.prefix + os.path.relpath(full, root), full, meta)
            t = time.time(); r = st.put(gen()); r["secs"] = round(time.time() - t, 2); out(r)
        elif a.cmd == "locate":
            st.sync(); out(st.locate(a.path) or {"error": "not found"})
        elif a.cmd == "ls":
            st.sync(); out(st.ls(a.prefix, a.limit, a.glob))
        elif a.cmd == "summary":
            st.sync(); out(st.summary(a.prefix, a.depth))
        elif a.cmd == "plan":
            st.sync(); out(st.plan(a.paths).as_dict())
        elif a.cmd == "get":
            st.sync()
            if a.head: d = st.head(a.path, a.head)
            elif a.tail: d = st.tail(a.path, a.tail)
            else: d = st.read(a.path, a.start, a.end, a.max_bytes)
            sys.stdout.buffer.write(d)
        elif a.cmd == "verify":
            r = st.verify(a.deep); out(r); return 0 if r["ok"] else 2
        elif a.cmd == "compact":
            out(st.compact())
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
