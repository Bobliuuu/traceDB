#!/usr/bin/env python3
"""Harvest relevant remote GitHub repos into a staging dir, then (after refresh flag) ingest into a tracedb store.
  github_harvest.py relevance          -> logs/github-relevance.json (include/skip + reasons)
  github_harvest.py harvest            -> mirror, bundle, tree snapshot, issues/PRs/comments jsonl (mirror deleted after)
  github_harvest.py ingest             -> put tree/jsonl (redacted via ingest.redact) + bundle blobs into store (lock dir)
Layout in store: repos-remote/<owner>/<repo>/{tree/..., issues.jsonl, pulls.jsonl, issue_comments.jsonl, review_comments.jsonl, repo.bundle, meta.json}
Tokens are never written; git auth via gh credential helper."""
import json, os, re, subprocess, sys, shutil, tarfile, time, io
from concurrent.futures import ThreadPoolExecutor
HOME = os.path.expanduser("~"); WORK = f"{HOME}/boxharvest/github-20261112"; STAGE = f"{WORK}/stage"
LOGS = f"{HOME}/tracedb-work/logs"; MAXKB = 5 * 1024 * 1024
sys.path.insert(0, f"{HOME}/tracedb-work"); sys.path.insert(0, f"{HOME}/tracedb-work/traceDB")

INCLUDE = {  # explicit: owner/repo -> reason
 "Bobliuuu": "MMF amd-experiments allgit cakebench daw-in-daw deepswe-north homelab-config kvm neurosurgeon-bench north-star ontolagni open-flapping-airplanes personal-website personal-website-old personal-website-old-changes qwen-os qwenagent qwengl qweninqwen qwentensor qwentpu readme-lm recursive-dflash2 smol-ram ten-percent-challenge traceDB ultraquant-more video-readme-maker Bobliuuu jerryzhu.github.io".split(),
 "DeusExperiments": "*", "legendyxu": "*", "me50": ["Bobliuuu"],
 "amw720386": "MMF RewindGPU block-gtq-webgpu cuda-for-intel forge gemma4-inference-repro llm.c north-star okc-decode-layer openwebui-forge qrllm recursive-dflash2 ten-percent-challenge ten-percent-challenge-lab Portfolio InteractivePortfolio amw720386".split(),
 "arnoldxuuu": "inference-experiment magazine magazine-pre-clean okc-muse-spark rsi-system magni-website".split(),
}
KW = re.compile(r"llm|inference|kernel|cuda|speculative|quant|rsi\b|qwen|tracedb|ontolagni", re.I)

def sh(cmd, **k): return subprocess.run(cmd, capture_output=True, text=True, **k)

def relevance():
    rows = json.load(open(f"{WORK}/repos-full.json")); inc, skip = [], []
    for r in rows:
        o, n = r["full"].split("/"); sel = INCLUDE.get(o)
        if sel == "*" or (sel and n in sel): inc.append({**r, "reason": "named relevant project/theme"})
        else:
            why = "unrelated org/third-party/coursework/hackathon/fork" if o in ("WATonomous", "uwaggs") else "not on the user's themes (inference/RSI/tracedb/kvm/10pct/qwen*/etc)"
            skip.append({"full": r["full"], "fork": r["fork"], "size_kb": r["size"], "reason": why, "kw_hint": bool(KW.search(r["full"] + " " + (r.get("desc") or "")))})
    json.dump({"include": inc, "skip": skip}, open(f"{LOGS}/github-relevance.json", "w"), indent=1)
    print(len(inc), "include", len(skip), "skip")

def pages(path):
    r = sh(["gh", "api", "--paginate", path + ("&" if "?" in path else "?") + "per_page=100", "--jq", ".[]"])
    return [l for l in r.stdout.splitlines() if l.strip()], r.returncode

def one(r):
    full = r["full"]; o, n = full.split("/"); d = f"{STAGE}/{o}/{n}"
    if os.path.exists(f"{d}/meta.json"): return {"repo": full, "status": "done-already"}
    if r["size"] > MAXKB: return {"repo": full, "status": "skipped-gt5GB"}
    os.makedirs(d, exist_ok=True); mir = f"{WORK}/mirror/{o}__{n}.git"; shutil.rmtree(mir, ignore_errors=True)
    meta = {"repo": full, "fork": r["fork"], "private": r.get("priv", r.get("private"))}
    for acct in ("Bobliuuu", "jerry-prism"):
        tok = sh(["gh", "auth", "token", "-u", acct]).stdout.strip()
        c = subprocess.run(["git", "-c", "credential.helper=", "-c", f"http.extraheader=Authorization: Basic " + __import__("base64").b64encode(f"x-access-token:{tok}".encode()).decode(),
                            "clone", "--mirror", "-q", f"https://github.com/{full}.git", mir], capture_output=True, text=True)
        if c.returncode == 0: break
    else:
        return {"repo": full, "status": "clone-failed", "err": c.stderr[:200].replace(tok, "")}
    try:
        if sh(["git", "-C", mir, "rev-parse", "--verify", "-q", "HEAD"]).returncode == 0:
            b = sh(["git", "-C", mir, "bundle", "create", f"{d}/repo.bundle", "--all"]); meta["bundle_ok"] = b.returncode == 0
            tr = f"{d}/tree"; os.makedirs(tr, exist_ok=True)
            p = subprocess.Popen(["git", "-C", mir, "archive", "HEAD"], stdout=subprocess.PIPE)
            try:
                with tarfile.open(fileobj=p.stdout, mode="r|") as t:
                    for m in t:
                        if m.isfile() and m.size <= 20 << 20 and ".." not in m.name:
                            t.extract(m, tr, filter="data") if sys.version_info >= (3, 12) else t.extract(m, tr)
            except Exception as e: meta["tree_err"] = repr(e)[:100]
            p.wait()
            meta["commits"] = sh(["git", "-C", mir, "rev-list", "--all", "--count"]).stdout.strip()
        else: meta["empty"] = True
    finally: shutil.rmtree(mir, ignore_errors=True)
    for name, path in (("issues", "issues?state=all"), ("pulls", "pulls?state=all"), ("issue_comments", "issues/comments"), ("review_comments", "pulls/comments")):
        ls, rc = pages(f"repos/{full}/{path}"); meta[name] = len(ls)
        if rc and not ls: meta[name + "_err"] = "api-error"
        open(f"{d}/{name}.jsonl", "w").write("\n".join(ls) + ("\n" if ls else ""))
    meta["tree_bytes"] = sum(os.path.getsize(os.path.join(a, f)) for a, _, fs in os.walk(f"{d}/tree") for f in fs) if os.path.isdir(f"{d}/tree") else 0
    json.dump(meta, open(f"{d}/meta.json", "w")); return {"repo": full, "status": "ok", **meta}

def harvest():
    inc = json.load(open(f"{LOGS}/github-relevance.json"))["include"]; os.makedirs(f"{WORK}/mirror", exist_ok=True)
    with ThreadPoolExecutor(3) as ex, open(f"{WORK}/harvest.jsonl", "a") as log:
        for res in ex.map(one, inc):
            log.write(json.dumps(res) + "\n"); log.flush(); print(res["repo"], res["status"], flush=True)

def ingest():
    import ingest as I
    from tracedb.store import open_store
    from tracedb.blob import put_blob
    lock = f"{LOGS}/store-write.lock"
    while True:
        try: os.mkdir(lock); break
        except FileExistsError: time.sleep(30)
    try:
        st = open_store(os.environ["TRACEDB_STORE"]); batch, bsz = [], 0
        for o in sorted(os.listdir(STAGE)):
            for n in sorted(os.listdir(f"{STAGE}/{o}")):
                d = f"{STAGE}/{o}/{n}"; pre = f"repos-remote/{o}/{n}/"
                for dp, dns, fns in os.walk(d):
                    dns[:] = [x for x in dns if x not in (".git",)]
                    for f in fns:
                        full = os.path.join(dp, f); rel = os.path.relpath(full, d)
                        if f == "repo.bundle": continue
                        ext = os.path.splitext(f)[1].lower()
                        if f in I.SKIP_NAMES or (I.SKIP_PAT.search(full) and not f.endswith(".pub")) or (rel.startswith("tree/") and ext in I.SKIP_EXT): continue
                        data = open(full, "rb").read()
                        if b"\0" not in data[:4096]: data, _ = I.redact(data)
                        batch.append((pre + rel, data, {"src": "github", "repo": f"{o}/{n}"})); bsz += len(data)
                        if bsz > 256 << 20: st.put(batch); batch, bsz = [], 0
                if os.path.exists(f"{d}/repo.bundle"):
                    if batch: st.put(batch); batch, bsz = [], 0
                    r = put_blob(st, pre + "repo.bundle", f"{d}/repo.bundle", {"src": "github", "repo": f"{o}/{n}"}); print("bundle", o, n, r["size"], flush=True)
        if batch: st.put(batch)
        print("ingest done")
    finally: os.rmdir(lock)

if __name__ == "__main__": {"relevance": relevance, "harvest": harvest, "ingest": ingest}[sys.argv[1]]()
