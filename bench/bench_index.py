#!/usr/bin/env python3
"""Index benchmark: legacy single-file FTS (search.py) vs v3 sharded/lazy index (index.py), recall on 20 fixed queries.

usage: bench_index.py --store S3 --index-store S3 --old-sqlite old-fts.sqlite --prefix P [--prefix P2 ...] [--cache DIR]
Cold = fresh empty cache dir (fresh process). Warm = same process, repeated query. Recall = mean over (query, prefix) of
|old top-10 paths & new top-10 paths| / |old top-10 paths| for pairs where the old index returned anything."""
import argparse, json, os, shutil, statistics, sys, tempfile, time

QUERIES = ["vllm oom", "tenpct-g1", "kv cache", "CUDA out of memory", "ssh_config Host", "tokenizer", "bm25", "g7e",
           "modal run detach", "gpu memory utilization", "pytest failed", "nccl", "quantization", "lambda-final",
           "verify ok", "judging rules", "aws login", "KMS vault", "sqlite fts5", "deadline"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", required=True); ap.add_argument("--index-store", required=True)
    ap.add_argument("--old-sqlite"); ap.add_argument("--prefix", action="append", required=True)
    ap.add_argument("--cache"); ap.add_argument("--out")
    a = ap.parse_args()
    from tracedb.store import open_store
    from tracedb.index import IndexSet
    res = {}
    cache = a.cache or tempfile.mkdtemp(prefix="idxbench-")
    shutil.rmtree(cache, ignore_errors=True)
    t = time.time()
    ds, ist = open_store(a.store, cache_dir=cache), open_store(a.index_store, cache_dir=cache)
    ds.sync(); ist.sync()
    ix = IndexSet.open(ist, ds)
    t1 = time.time()
    r = ix.search("vllm oom", prefix=a.prefix[0])
    res["cold_first_search_s"] = round(time.time() - t, 2)
    res["cold_first_search_net_of_catalog_sync_s"] = round(time.time() - t1, 2)
    st = ix.describe(); res["index_bytes"] = st["bytes"]; res["index_gens"] = st["gens"]; res["index_chunks"] = st["chunks"]
    res["cold_gens_opened"] = ix.gens_opened; res["cold_block_fetches"] = ix.block_fetches
    lat = []
    for q in QUERIES:
        for p in a.prefix:
            t = time.time(); ix.search(q, prefix=p); lat.append(time.time() - t)
    res["warm_search_p50_ms"] = round(statistics.median(lat) * 1000, 1)
    res["warm_search_p95_ms"] = round(sorted(lat)[int(len(lat) * .95)] * 1000, 1)
    lat = []
    for q in QUERIES:
        for p in a.prefix:
            t = time.time(); ix.search(q, prefix=p, snippets=False); lat.append(time.time() - t)
    res["warm_search_nosnippet_p50_ms"] = round(statistics.median(lat) * 1000, 1)
    # a cold prefix search on a fresh process-level cache for a different prefix
    if a.old_sqlite:
        from tracedb.search import SearchIndex
        old = SearchIndex.open(a.old_sqlite)
        lat, recs, top1 = [], [], []
        for q in QUERIES:
            for p in a.prefix:
                t = time.time(); o = old.search(q, prefix=p, limit=10); lat.append(time.time() - t)
                n = ix.search(q, prefix=p, limit=10, snippets=False)
                if o:
                    op, np_ = [x["path"] for x in o], [x["path"] for x in n]
                    recs.append(len(set(op) & set(np_)) / len(op)); top1.append(op[0] in np_[:3])
        res["old_warm_search_p50_ms"] = round(statistics.median(lat) * 1000, 1)
        res["recall_at_10_vs_old"] = round(statistics.mean(recs), 3)
        res["old_top1_in_new_top3"] = round(statistics.mean(top1), 3)
        res["pairs_compared"] = len(recs)
    print(json.dumps(res, indent=1))
    if a.out:
        json.dump(res, open(a.out, "w"), indent=1)


main()
