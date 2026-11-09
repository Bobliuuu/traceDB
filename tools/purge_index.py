#!/usr/bin/env python3
"""Copy a search index without the paths the store's POLICY.json excludes (TRACEDB_POLICY / TRACEDB_STORE, see
tracedb/policy.py) or --prefix ones, so their text is gone, not
just hidden: a fresh FTS5 file is written row by row, then optimized and vacuumed.

usage: purge_index.py OLD.sqlite NEW.sqlite [--prefix P ...]
Then put NEW as index/fts.sqlite (tracedb put), and purge the old entry's packs with the next `tracedb purge`/`gc`."""
import argparse, json, os, sqlite3, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tracedb.policy import from_env as _policy  # noqa: E402
from tracedb.search import SearchIndex  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("old"); ap.add_argument("new"); ap.add_argument("--prefix", action="append", default=[])
    a = ap.parse_args(argv)
    policy = _policy()
    gone = lambda p: policy.excluded(p) or any(p.startswith(x) for x in a.prefix)  # noqa: E731
    old = sqlite3.connect(f"file:{a.old}?mode=ro", uri=True)
    new = SearchIndex.create(a.new)
    kept = dropped = 0
    for table, cols in (("files", "path,size,indexed,note"), ("chunks", "text,path,off"), ("paths", "path")):
        rows = old.execute(f"SELECT {cols} FROM {table}")
        while batch := rows.fetchmany(50_000):
            keep = [r for r in batch if not gone(r[cols.split(',').index('path')])]
            kept += len(keep); dropped += len(batch) - len(keep)
            new.c.executemany(f"INSERT INTO {table}({cols}) VALUES ({','.join('?' * len(cols.split(',')))})", keep)
    new.close()
    con = sqlite3.connect(a.new); con.execute("VACUUM"); con.close()
    check = sqlite3.connect(a.new)
    left = [p for (p,) in check.execute("SELECT path FROM paths") if gone(p)]
    print(json.dumps({"kept_rows": kept, "dropped_rows": dropped, "excluded_left": len(left),
                      "bytes": os.path.getsize(a.new)}))
    return 1 if left else 0


if __name__ == "__main__":
    sys.exit(main())
