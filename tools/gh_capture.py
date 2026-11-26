#!/usr/bin/env python3
"""Capture every GitHub repo of an owner (default Bobliuuu) into the tracedb store at repos/<name>/.git-bundle/ (restore_repo.sh layout).
usage: gh_capture.py [--owner O] [--force] [--only NAME] [--list FILE]
Source list: `gh repo list` (plus optional srcs-github.txt, one owner/name per line). A repo is re-captured only when its pushedAt
changed (state/gh-capture.json). Each repo is mirror-cloned into ~/tracedb-work/gh-cache/<name>.git, captured, and the clone deleted.
Portable: set TRACEDB_WORK to the dir holding ingest.py + traceDB/ (default ~/tracedb-work); state/clones live under it. Needs env TRACEDB_STORE, VAULT_STORE, a working `gh auth`. Exit 1 if any repo failed."""
import json, os, shutil, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import git_capture as G
from tracedb.store import open_store

def main():
    a = sys.argv[1:]
    owner = a[a.index("--owner") + 1] if "--owner" in a else "Bobliuuu"
    only = a[a.index("--only") + 1] if "--only" in a else None
    force = "--force" in a
    state_f = os.path.join(G.WORK, "state", "gh-capture.json")
    state = json.load(open(state_f)) if os.path.exists(state_f) else {}
    out = subprocess.run(["gh", "repo", "list", owner, "--limit", "1000", "--json", "name,pushedAt,nameWithOwner,isEmpty"], capture_output=True, text=True)
    if out.returncode: print("gh repo list failed:", out.stderr[-300:]); return 2
    repos = json.loads(out.stdout)
    extra = os.path.join(G.WORK, "srcs-github.txt")
    if os.path.exists(extra):
        for ln in open(extra).read().split():
            if "/" in ln: repos.append({"name": ln.split("/")[1], "nameWithOwner": ln, "pushedAt": "?", "isEmpty": False})
    st = open_store(os.environ["TRACEDB_STORE"]); vt = open_store(os.environ["VAULT_STORE"])
    st.sync()
    # sha of the manifest this tool wrote per repo: if another writer (e.g. git_capture on a stale local clone) replaced it, capture again
    man_f = os.path.join(G.WORK, "state", "gh-capture-manifest.json")
    mans = json.load(open(man_f)) if os.path.exists(man_f) else {}
    stored = lambda nm: (st.locate(f"repos/{nm}/.git-bundle/manifest.json") or {}).get("sha")
    cache = os.path.join(G.WORK, "gh-cache"); os.makedirs(cache, exist_ok=True)
    args = {"dry": False, "force": True, "scratch": False, "maxscan": 600, "only": None}
    bad = n = 0
    for r in repos:
        nm = r["name"]
        if only and nm != only: continue
        if r.get("isEmpty"): continue
        key = r["nameWithOwner"]
        overwritten = key in mans and stored(nm) != mans[key]
        if not force and not overwritten and state.get(key) == r["pushedAt"] and r["pushedAt"] != "?": continue
        d = os.path.join(cache, nm + ".git")
        shutil.rmtree(d, ignore_errors=True)
        try:
            c = subprocess.run(["git", "clone", "--mirror", "-q", f"https://github.com/{r['nameWithOwner']}.git", d], capture_output=True, text=True, timeout=1800)
            if c.returncode: raise RuntimeError(c.stderr[-200:])
            res = G.capture(d, [d], f"repos/{nm}/.git-bundle/", st, vt, args, {})
            print(json.dumps({"repo": nm, "ok": bool(res), "size": (res or {}).get("bundle_size")}), flush=True)
            if res:
                state[r["nameWithOwner"]] = r["pushedAt"]; n += 1
                st.sync(); mans[r["nameWithOwner"]] = stored(nm); json.dump(mans, open(man_f, "w"))
            else: bad += 1
        except Exception as e:
            bad += 1; print(json.dumps({"repo": nm, "error": repr(e)[:200]}), flush=True)
        finally:
            shutil.rmtree(d, ignore_errors=True)
        json.dump(state, open(state_f, "w"))
    print(json.dumps({"done": True, "captured": n, "failed": bad}))
    return 1 if bad else 0

if __name__ == "__main__":
    sys.exit(main())
