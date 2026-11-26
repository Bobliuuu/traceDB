#!/usr/bin/env python3
"""publish_ops.py -- publish the operations runbook (how this user's machines, credentials and agent teams work) and
the user's Claude memory to traceDB and the ontology, so every session on every machine finds them with
`ontolagni find` / `tracedb ask` without being told.

Sources (private, never in this repo):
  ops     one README.md per topic under $OPS_DIR (default ~/tracedb-work/ops/<topic>/README.md) -> ops/<topic>/README.md
  memory  one <name>.md per memory under $MEMORY_DIR (default ~/tracedb-work/memory/laptop, a copy of the laptop's
          ~/.claude/projects/-home-jerry/memory pushed by its memory-push timer) -> memory/<name>/README.md
Each page is redacted, put in the main store and becomes ontology node <root>/<topic> (kind ops|memory) through
ontolagni's overlay protocol (overlays/<root>.jsonl, compare-and-swap on ontology.jsonl, every $ONTOLOGY_DESTS).
The node's summary is the page's first lines (a memory's frontmatter description first); its `text` holds the page
body, which `ontolagni find` indexes at low weight, so a word deep in a page still finds it. Idempotent.

    python publish_ops.py [--root ops|memory|all] [--dry-run]
"""
import argparse, json, os, pathlib, re, sys

STORE = os.environ.get("TRACEDB_STORE")
DSTS = (os.environ.get("ONTOLOGY_DESTS") or "").split()
SOURCES = {  # root -> (source dir, glob, keywords added to every node)
    "ops": (pathlib.Path(os.environ.get("OPS_DIR", pathlib.Path.home() / "tracedb-work/ops")), "*/README.md",
            "ops runbook operations setup how to credentials login vault keys push timer team seats orchestrator never ask"),
    "memory": (pathlib.Path(os.environ.get("MEMORY_DIR", pathlib.Path.home() / "tracedb-work/memory/laptop")), "*.md",
               "memory rule user feedback project lane decision what the user said"),
}
TEXT_CAP = 4000

REDACT = [(re.compile(p, f), r) for p, f, r in [
    (rb"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S, b"[REDACTED:PRIVATE_KEY]"),
    (rb"\b(AKIA|ASIA)[0-9A-Z]{16}\b", 0, b"[REDACTED:AWS_KEY_ID]"),
    (rb"\bsk-(ant-|proj-|or-v1-)?[A-Za-z0-9_-]{20,}", 0, b"[REDACTED:API_KEY]"),
    (rb"\bgh[pousr]_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}\b", 0, b"[REDACTED:GH_TOKEN]"),
    (rb"\bhf_[A-Za-z0-9]{30,}\b", 0, b"[REDACTED:HF_TOKEN]"),
    (rb"(?i)((?:api[_-]?key|apikey|secret|passwd|password|token)[\"']?\s*[:=]\s*[\"']?)[A-Za-z0-9_\-./+=]{24,}", 0,
     rb"\1[REDACTED:CRED]"),
]]


def redact(b):
    for rx, rep in REDACT:
        b = rx.sub(rep, b)
    return b


def one_line(s, n):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def split_front(t: str):
    """'---\nk: v\n---\nbody' -> ({k: v}, body). Pages without frontmatter -> ({}, t)."""
    m = re.match(r"---\n(.*?)\n---\n?", t, re.S)
    if not m:
        return {}, t
    fm = {}
    for l in m.group(1).splitlines():
        k, _, v = l.partition(":")
        if v and not l.startswith(" "):
            fm[k.strip()] = v.strip().strip('"')
    return fm, t[m.end():]


def describe_page(t: str) -> dict:
    fm, body = split_front(t)
    lines = [l.replace("**", "").strip("-* ").strip() for l in body.splitlines() if l.strip() and not l.startswith(("#", "|"))]
    head = ([fm["description"]] if fm.get("description") else []) + lines[:3]
    return {"summary": one_line(" ".join(head), 600), "status": head[:3],
            "text": one_line(" ".join(([fm.get("name", "")] if fm else []) + lines), TEXT_CAP)}


def publish_root(root: str, dry_run: bool):
    src, pattern, words = SOURCES[root]
    pages = sorted(p for p in src.glob(pattern) if p.name != "MEMORY.md")  # MEMORY.md is the index, not a memory
    if not pages:
        sys.exit(f"no pages under {src}")
    topic = (lambda p: p.parent.name) if pattern.endswith("README.md") else (lambda p: p.stem)
    items = [(f"{root}/{topic(p)}/README.md", redact(p.read_bytes()), {"project": root, "kind": root}) for p in pages]
    for path, body, _ in items:
        print(f"{path}: {len(body)} B")
    if dry_run:
        return
    from tracedb.store import open_store
    from ontolagni.build import build
    from ontolagni.publish import publish
    st = open_store(STORE)
    st.sync()
    print("traceDB put:", json.dumps(st.put(items)))
    ents = st.ls(root + "/", limit=10 ** 7)
    desc = {}
    for e in ents:
        if e["path"].endswith("/README.md"):
            d = describe_page(st.read(e["path"]).decode(errors="replace"))
            desc[e["path"].rsplit("/", 1)[0]] = dict(d, readme=e["path"])
    lines = [json.dumps({"path": e["path"], "sha": e["sha"], "size": e["size"], "meta": e.get("meta") or {}}) for e in ents]
    nodes, _ = build(lines, max_depth=3, store_label=STORE, describe=desc)
    nodes = [n for n in nodes if n["id"].split("/")[0] == root]
    for n in nodes:
        n["project"] = n["campaign"] = n["kind"] = root
        page = desc.get(n["id"], {})
        n["text"] = one_line(" ".join(filter(None, [n.get("text", ""), words, n.get("summary", ""), page.get("text", "")])), TEXT_CAP)
    for dst in DSTS:
        res = publish(dst, overlay=(root, nodes), render_docs=True)
        print(f"overlay {root} -> {dst}: {len(nodes)} nodes, ontology now {res['nodes']} ({res['attempts']} attempts)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", choices=[*SOURCES, "all"], default="ops")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not STORE or not DSTS:
        sys.exit("set TRACEDB_STORE and ONTOLOGY_DESTS (. ~/.config/tracedb/env)")
    for root in (SOURCES if a.root == "all" else [a.root]):
        publish_root(root, a.dry_run)


if __name__ == "__main__":
    main()
