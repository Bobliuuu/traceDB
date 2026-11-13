#!/usr/bin/env python3
"""describe.json for ontolagni: {node_id: {summary, readme}} derived from each source's own README (first heading + first paragraph).
Nothing invented: no README -> no summary. usage: describe.py OUT.json SRC=PREFIX ..."""
import json, os, re, sys
out, specs = sys.argv[1], sys.argv[2:]
SEC = re.compile(r"status|result|progress|current|headline|findings|state of|where we are|what works|next|done|outcome", re.I)
NUM = re.compile(r"\d[\d,.]*\s*(%|x\b|×|tok/s|\$/h|ms\b|GB\b|MB\b|tokens?\b|passed|pass\b|tests?\b|/\d)", re.I)


def clean(l):
    l = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", l)
    return re.sub(r"[*_`>]|^[-+]\s+|^\d+\.\s+", "", l).strip()


def status_metrics(t):
    """status = first lines under headings like Status/Result/Progress/Next; metrics = lines carrying a number+unit. Both are README text, verbatim-trimmed."""
    t = re.sub(r"<!--.*?-->", "", t, flags=re.S)
    lines = t.splitlines()
    status, i = [], 0
    while i < len(lines) and len(status) < 6:
        l = lines[i]
        if l.startswith("#") and SEC.search(l):
            j = i + 1
            while j < len(lines) and not lines[j].startswith("#") and len(status) < 6:
                c = clean(lines[j])
                if c and not c.startswith(("|", "```", "---")) and len(c) > 12:
                    status.append(c.lstrip("- ")[:240])
                j += 1
            i = j
        else:
            i += 1
    metrics = []
    for l in lines[:400]:
        c = clean(l)
        if l.lstrip().startswith("|") and not set(c) <= set("|-: "):
            c = " · ".join(x.strip() for x in l.strip().strip("|").split("|") if x.strip())
        if c and c not in status and NUM.search(c) and 12 < len(c) < 220 and not l.startswith("#") and "http" not in c[:8]:
            metrics.append(c)
        if len(metrics) >= 6:
            break
    return status[:6], metrics


res = {}
for spec in specs:
    src, prefix = spec.split("=", 1); src = os.path.expanduser(src); node = prefix.strip("/")
    for name in ("README.md", "readme.md", "README", "AGENTS.md", "CLAUDE.md", "README.txt"):
        p = os.path.join(src, name)
        if os.path.isfile(p):
            t = open(p, errors="replace").read()
            t = re.sub(r"<!--.*?-->", "", t, flags=re.S)
            lines = [l.strip() for l in t.splitlines()]
            title = next((l.lstrip("# ").strip() for l in lines if l.startswith("#")), "")
            para = next((l for l in lines if l and not l.startswith(("#", "![", "[!", "<", "|", "```", "---"))), "")
            s = (title + ". " if title else "") + re.sub(r"[*_`]", "", para)
            res[node] = {"summary": s[:600], "readme": prefix + name}
            st, me = status_metrics(t)
            if st: res[node]["status"] = st
            if me: res[node]["metrics"] = me
            break
    if node not in res:  # no README: newest doc's title + first paragraph (still derived, never invented)
        docs = []
        for sub in ("docs", "doc", "notes", "research"):
            d = os.path.join(src, sub)
            if os.path.isdir(d):
                docs += [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".md")]
        if docs:
            p = max(docs, key=os.path.getmtime); t = open(p, errors="replace").read()
            lines = [l.strip() for l in t.splitlines()]
            title = next((l.lstrip("# ").strip() for l in lines if l.startswith("#")), "")
            para = next((l for l in lines if l and not l.startswith(("#", "![", "[!", "<", "|", "```", "---"))), "")
            res[node] = {"summary": ((title + ". ") if title else "") + re.sub(r"[*_`]", "", para)[:500], "readme": prefix + os.path.relpath(p, src)}
            st, me = status_metrics(t)
            if st: res[node]["status"] = st
            if me: res[node]["metrics"] = me
json.dump(res, open(out, "w"), indent=1)
print(len(res), "described of", len(specs))
