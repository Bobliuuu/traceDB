#!/usr/bin/env python3
"""mine_questions.py -- grow the search eval from real chats: find the user's "where / what / find X" questions in
Claude Code transcripts and label each with the traceDB paths the chat read before the next user message.

    python bench/mine_questions.py --src DIR [--src DIR ...] --out candidates.jsonl

Appends only questions not already in --out (or in any --known file). Labels are automatic (what the chat read, cut to
node ids such as ops/<topic> or projects/<name>), so they are noisy: bench/ask_eval.py scores them as a separate
"auto" set to watch the trend, while the curated set stays the measure. Output is private (it quotes the user): write
it outside this repo.
"""
import argparse, glob, json, os, re

QW = re.compile(r"\b(where|find|look (in|for|up)|what('s| is| are| was)|which|do we have|is there|how (do|does|did) "
                r"(we|i|it)|show me|status of|did we|have we)\b", re.I)
READ = re.compile(r"(?:tracedb (?:get|ls|summary|locate|search|ask)(?: --?\S+)*|ontolagni (?:show|find|where))\s+"
                  r"['\"]?([A-Za-z0-9_][\w.\-/]*)")
DEEP = {"ops": 2, "memory": 2, "projects": 2, "recipes": 2, "policy": 2, "skills": 2, "boxes": 2, "rsi": 2,
        "magni": 2, "traces": 2, "repos": 2, "repos-remote": 3, "config": 2, "results": 2}


def node_of(path: str):
    """traceDB path -> the node id a search should return (None for a bare root or a search word)."""
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) < 2 or parts[0] not in DEEP:
        return None
    if parts[-1] == "README.md":
        parts = parts[:-1]
    return "/".join(parts[:DEEP[parts[0]]]) if len(parts) >= DEEP[parts[0]] else None


def mine(files):
    for f in files:
        cur = None
        for line in open(f, errors="replace"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("isSidechain"):
                continue
            m = r.get("message") or {}
            if r.get("type") == "user" and isinstance(m.get("content"), str):
                t = m["content"].strip()
                if t.startswith("<") or len(t) < 8:
                    continue
                if cur and cur["want"]:
                    yield cur
                cur = {"q": t[:400], "want": [], "set": "auto"} if QW.search(t) and len(t) < 600 else None
            elif r.get("type") == "assistant" and cur is not None:
                for c in m.get("content") or []:
                    if c.get("type") == "tool_use":
                        for p in READ.findall(json.dumps(c.get("input", {}))):
                            n = node_of(p)
                            if n and n not in cur["want"]:
                                cur["want"].append(n)
        if cur and cur["want"]:
            yield cur


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", action="append", required=True, help="a Claude projects dir (holds */*.jsonl)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--known", action="append", default=[], help="question files whose questions are not re-added")
    a = ap.parse_args()
    seen = set()
    for k in a.known + [a.out]:
        if os.path.exists(k):
            seen |= {json.loads(l)["q"].strip().lower() for l in open(k) if l.strip()}
    files = sorted(f for s in a.src for f in glob.glob(os.path.join(s, "*", "*.jsonl")))
    new = []
    for c in mine(files):
        k = c["q"].strip().lower()
        if k not in seen:
            seen.add(k); new.append(c)
    with open(a.out, "a") as fo:
        for c in new:
            fo.write(json.dumps(c) + "\n")
    print(json.dumps({"files": len(files), "new": len(new)}))


if __name__ == "__main__":
    main()
