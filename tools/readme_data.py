#!/usr/bin/env python3
"""Derive README/video numbers from the real benchmark result files in bench/results/.

Writes bench/results/derived/readme_data.json. Nothing here is hand-typed: every value is parsed from a result file.
Run from the repo root: python3 tools/readme_data.py
"""
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
R = os.path.join(ROOT, "bench", "results")
S3 = "s3-20261107.txt"
SWEEP = "s3-sweep-20261107.txt"
APPEND = "append-local-20261107.txt"
OPT = "opt-20261111.txt"
IDX_FULL = "opt-index-full-20261111.json"


def read(name):
    with open(os.path.join(R, name)) as f:
        return f.read()


def kv(line):
    return {k: float(v) for k, v in re.findall(r"(\w+)=\s*([\d.]+)", line)}


out = {}

# --- real S3 pull latency by access pattern (laptop -> us-east-1, one request per coalesced range)
pulls = []
for m in re.finditer(r"^PULL (.+?): (files=.+)$", read(S3), re.M):
    d = kv(m.group(2))
    label = m.group(1)
    pulls.append({"what": label, "files": int(d["files"]), "raw_mib": d["raw"], "wire_mib": d["wire"], "reqs": int(d["reqs"]), "ms": d["ms"]})
out["s3_pulls"] = pulls
pm = {p["what"]: p for p in pulls}
out["pull_1000"] = pm["1000 small"]
out["pull_5big"] = pm["5 big(>2M)"]
out["pull_1"] = pm["1 small file"]
m = re.search(r"^HEAD_GET ms p50=(\d+)", read(S3), re.M)
out["head_get_ms"] = float(m.group(1))
m = re.search(r"^COLD_SYNC entries=(\d+) secs=([\d.]+)", read(S3), re.M)
out["cold_sync_entries"], out["cold_sync_s"] = int(m.group(1)), float(m.group(2))
m = re.search(r"^RANGE 4KiB.*ms p50=(\d+)", read(S3), re.M)
out["range_4k_ms"] = float(m.group(1))
m = re.search(r"^CONCURRENT (\d+) writers x(\d+) puts: .*entries=(\d+)/(\d+) errors=\[\]", read(S3), re.M)
out["writers"], out["writer_puts"], out["writer_entries"] = int(m.group(1)), int(m.group(2)), int(m.group(3))
m = re.search(r"^RUN .*files=(\d+) raw=(\d+)MiB", read(S3), re.M)
out["files"], out["raw_mib"] = int(m.group(1)), int(m.group(2))
m = re.search(r"^INGEST .*packs=(\d+) wire=(\d+)MiB", read(S3), re.M)
out["packs"], out["wire_mib"] = int(m.group(1)), int(m.group(2))
out["wire_pct"] = round(100 * out["wire_mib"] / out["raw_mib"])
out["verify_ok"] = "'ok': True" in read(S3)

# --- tuning sweep (1000 random small files): gap x workers
rows = []
for m in re.finditer(r"gap=\s*(\d+)K w=\s*(\d+)\s+1000 random small\s+(files=.+)$", read(SWEEP), re.M):
    d = kv(m.group(3))
    rows.append({"gap": f"{m.group(1)}K", "workers": f"{m.group(2)} workers", "ms": d["ms"], "amp": d["amp"], "reqs": int(d["reqs"])})
out["sweep"] = [r for r in rows if r["workers"] in ("16 workers", "64 workers")]
best = min(rows, key=lambda r: r["ms"])
out["sweep_best"] = best
base = [r for r in rows if r["gap"] == "256K" and r["workers"] == "16 workers"][0]
out["sweep_before_ms"], out["sweep_before_amp"] = base["ms"], base["amp"]
out["sweep_after_ms"], out["sweep_after_amp"] = best["ms"], best["amp"]
out["sweep_speedup"] = round(base["ms"] / best["ms"], 2)

# --- append-only growth (local backend)
txt = read(APPEND)
m = re.search(r"^BASE raw=([\d.]+)MiB packs=([\d.]+)MiB", txt, re.M)
whole_kib = float(m.group(2)) * 1024
adds = [float(x) for x in re.findall(r"^APPEND \d+ tail=\d+KiB added_pack=(\d+)KiB", txt, re.M)]
puts = [float(x) for x in re.findall(r"^APPEND \d+ .* put=(\d+)ms", txt, re.M)]
out["append"] = {"base_raw_mib": float(m.group(1)), "whole_file_kib": round(whole_kib), "added_kib": sum(adds) / len(adds), "n": len(adds),
                 "put_ms": round(sum(puts) / len(puts)), "ratio": round(whole_kib / (sum(adds) / len(adds)))}
out["append_rows"] = [{"what": "append 100 KiB tail (extent)", "kib": sum(adds) / len(adds)}, {"what": "re-store whole file", "kib": round(whole_kib)}]

# --- search index v1 -> v3 on the real store
opt = read(OPT)
idx = json.loads(read(IDX_FULL))
m = re.search(r"index size \(raw\)\s+([\d.]+) GB", opt)
old_gb = float(m.group(1))
out["index"] = {"old_gb": old_gb, "new_gb": idx["index_bytes"] / 1e9, "shrink": old_gb / (idx["index_bytes"] / 1e9),
                "chunks": idx["index_chunks"], "cold_s": idx["cold_first_search_s"], "cold_net_s": idx["cold_first_search_net_of_catalog_sync_s"],
                "warm_nosnip_ms": idx["warm_search_nosnippet_p50_ms"], "old_warm_ms": idx["old_warm_search_p50_ms"],
                "warm_snip_ms": idx["warm_search_p50_ms"], "recall10": idx["recall_at_10_vs_old"]}
out["index_size"] = [{"index": "v1 (single sqlite file)", "gb": old_gb}, {"index": "v3 (sharded, contentless FTS5)", "gb": round(idx["index_bytes"] / 1e9, 3)}]
m = re.search(r"cold first search\s+~(\d+) min", opt)
out["index"]["old_cold_min"] = int(m.group(1))

# --- repo facts (counted, not typed)
tests = 0
for fn in os.listdir(os.path.join(ROOT, "tests")):
    if fn.startswith("test_"):
        tests += len(re.findall(r"^\s*def test_", open(os.path.join(ROOT, "tests", fn)).read(), re.M))
out["tests"] = tests
src = 0
for fn in os.listdir(os.path.join(ROOT, "tracedb")):
    if fn.endswith(".py"):
        src += sum(1 for _ in open(os.path.join(ROOT, "tracedb", fn)))
out["src_lines"] = src
cli = open(os.path.join(ROOT, "tracedb", "cli.py")).read()
out["cli_commands"] = len(re.findall(r"sp\.add_parser\(", cli))
out["zero_deps"] = "dependencies = []" in open(os.path.join(ROOT, "pyproject.toml")).read()

os.makedirs(os.path.join(R, "derived"), exist_ok=True)
with open(os.path.join(R, "derived", "readme_data.json"), "w") as f:
    json.dump(out, f, indent=1)
print("wrote bench/results/derived/readme_data.json")
