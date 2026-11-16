#!/usr/bin/env python3
"""publish_ops.py -- publish the operations runbook (how this user's machines, credentials and agent teams work) to
traceDB and the ontology, so every new session finds it with `ontolagni find ops` without being told.

Source: one README.md per topic under $OPS_DIR (default ~/tracedb-work/ops/<topic>/README.md; private, never in this
repo). Each goes to the main store as ops/<topic>/README.md (redacted) and becomes ontology node ops/<topic> (kind
"ops") through ontolagni's overlay protocol (overlays/ops.jsonl, compare-and-swap on ontology.jsonl, every destination
in $ONTOLOGY_DESTS). Idempotent: re-run after editing any page.

    python publish_ops.py [--dry-run]
"""
import argparse, json, os, pathlib, re, sys

STORE = os.environ.get("TRACEDB_STORE")
DSTS = (os.environ.get("ONTOLOGY_DESTS") or "").split()
OPS = pathlib.Path(os.environ.get("OPS_DIR", pathlib.Path.home() / "tracedb-work/ops"))
ROOT = "ops"
WORDS = "ops runbook operations setup how to credentials login vault keys push timer team seats orchestrator never ask"

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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not STORE or not DSTS:
        sys.exit("set TRACEDB_STORE and ONTOLOGY_DESTS (. ~/.config/tracedb/env)")
    pages = sorted(OPS.glob("*/README.md"))
    if not pages:
        sys.exit(f"no pages under {OPS}")
    items = [(f"{ROOT}/{p.parent.name}/README.md", redact(p.read_bytes()), {"project": "ops", "kind": "ops"}) for p in pages]
    for path, body, _ in items:
        print(f"{path}: {len(body)} B")
    if a.dry_run:
        return
    from tracedb.store import open_store
    from ontolagni.build import build
    from ontolagni.publish import publish
    st = open_store(STORE)
    st.sync()
    print("traceDB put:", json.dumps(st.put(items)))
    ents = st.ls(ROOT + "/", limit=10 ** 7)
    desc = {}
    for e in ents:
        if e["path"].endswith("/README.md"):
            t = st.read(e["path"]).decode(errors="replace")
            body = [l.replace("**", "").strip("-* ").strip() for l in t.splitlines() if l.strip() and not l.startswith(("#", "|"))]
            desc[e["path"].rsplit("/", 1)[0]] = {"summary": one_line(" ".join(body[:3]), 600), "readme": e["path"], "status": body[:3]}
    lines = [json.dumps({"path": e["path"], "sha": e["sha"], "size": e["size"], "meta": e.get("meta") or {}}) for e in ents]
    nodes, _ = build(lines, max_depth=3, store_label=STORE, describe=desc)
    nodes = [n for n in nodes if n["id"].split("/")[0] == ROOT]
    for n in nodes:
        n["project"] = n["campaign"] = "ops"
        n["kind"] = "ops"
        n["text"] = one_line(" ".join(filter(None, [n.get("text", ""), WORDS, n.get("summary", "")])), 4000)
    for dst in DSTS:
        res = publish(dst, overlay=(ROOT, nodes), render_docs=True)
        print(f"overlay {ROOT} -> {dst}: {len(nodes)} nodes, ontology now {res['nodes']} ({res['attempts']} attempts)")


if __name__ == "__main__":
    main()
