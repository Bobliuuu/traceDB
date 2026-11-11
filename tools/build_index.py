#!/usr/bin/env python3
"""Build or refresh the v3 search index of a traceDB store (reads text from the store itself, writes index/v3/*).

usage: build_index.py [--full] [--jobs N] [--prefix P ...] [--dest-store SPEC]
  default: INCREMENTAL (indexes only entries new/changed/removed since the last manifest; falls back to a full build when
  there is no manifest). --full forces a rebuild. Store comes from $TRACEDB_STORE. Same as `tracedb index build|update`.

Replaces the v1 script that re-walked local source dirs and rebuilt one 4 GB sqlite file (~30 min): the index now follows the
catalog (so new binaries/blobs are covered by path, text blobs by head), shards by path range, and updates in minutes.
"""
import argparse, json, os, sys
from tracedb.store import open_store
from tracedb.index import build_index, update_index


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true"); ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--prefix", action="append"); ap.add_argument("--dest-store"); ap.add_argument("--work")
    a = ap.parse_args()
    st = open_store(os.environ["TRACEDB_STORE"])
    dest = open_store(a.dest_store) if a.dest_store else st
    f = build_index if a.full else update_index
    print(json.dumps(f(st, dest, tuple(a.prefix or [""]), a.jobs, a.work)))


main()
