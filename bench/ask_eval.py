#!/usr/bin/env python3
"""ask_eval.py -- how fast and how well do our search entry points answer real questions?

Each line of a question file is {"q": question as the user asks it, "want": [id/path substrings]}. The question
sets are private (they quote the user's chats and name internal projects) and never go in this public repo: they live
on pb-orch in ~/tracedb-work/search-eval/ (written.jsonl, real.jsonl) and in the store under eval/search/. A method scores a
hit when any of its top-K result ids/paths contains any `want` substring. Every method is timed as a fresh CLI process,
the way an agent calls it. Run on pb-orch (never the laptop):

    python bench/ask_eval.py [--methods find,search,ask] [--k 5] [--questions ~/tracedb-work/search-eval/real.jsonl]
"""
import argparse, json, os, pathlib, subprocess, sys, time

HERE = pathlib.Path(__file__).resolve().parent
BIN = pathlib.Path(sys.executable).parent


def run(cmd):
    t = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    return time.time() - t, p.stdout


def ids_find(q, k):
    dt, out = run([str(BIN / "ontolagni"), "find", q, "--limit", str(k)])
    try:
        return dt, [x["id"] for x in json.loads(out) if "id" in x]
    except Exception:
        return dt, []


def ids_search(q, k):
    dt, out = run([str(BIN / "tracedb"), "search", q, "--limit", str(k), "--per-file", "1", "--no-snippets"])
    try:
        return dt, [x["path"] for x in json.loads(out)]
    except Exception:
        return dt, []


def ids_ask(q, k):
    dt, out = run([str(BIN / "tracedb"), "ask", q, "--json", "--limit", str(k)])
    try:
        return dt, [x["id"] for x in json.loads(out)["hits"]]
    except Exception:
        return dt, []


METHODS = {"find": ids_find, "search": ids_search, "ask": ids_ask}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--methods", default="find,search,ask")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--questions", default=os.path.expanduser("~/tracedb-work/search-eval/real.jsonl"))
    a = ap.parse_args()
    qs = [json.loads(l) for l in open(a.questions) if l.strip()]
    ms = a.methods.split(",")
    tot = {m: [0, 0.0, 0.0] for m in ms}  # hits, total s, max s
    for x in qs:
        row = []
        for m in ms:
            dt, ids = METHODS[m](x["q"], a.k)
            ok = any(w in i for i in ids for w in x["want"])
            tot[m][0] += ok; tot[m][1] += dt; tot[m][2] = max(tot[m][2], dt)
            row.append(f"{m}:{'Y' if ok else '.'} {dt:4.1f}s")
        print(f"{' | '.join(row)} | {x['q']}", flush=True)
    print()
    for m, (h, s, mx) in tot.items():
        print(f"{m:7s} top-{a.k} hit {h}/{len(qs)}  mean {s / len(qs):.1f}s  max {mx:.1f}s")


if __name__ == "__main__":
    main()
