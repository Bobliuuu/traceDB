#!/usr/bin/env python3
"""Capture git HISTORY + uncommitted work of relevant local repos into a tracedb store, so a repo can be fully restored.

usage: git_capture.py [--dry] [--force] [--scratch] [--only SUBSTR] [--maxscan SECS] [--list FILE ...]
Per repo (grouped by git common dir, so worktrees share one bundle), under <prefix>/.git-bundle/ :
  repo.bundle       `git bundle create --all` as a content-addressed blob (MAIN store), or VAULT only if the history scan found
                    secrets / timed out (manifest.json says bundle_location)
  commits.jsonl     sha, date, author, subject, refs; manifest.json: remotes (credentials stripped), refs, worktrees, bundle sha
  stash.txt         stash list + redacted `stash show -p`;  wip/<worktree>.diff  redacted `git diff HEAD`; wip/<worktree>.untracked.txt
  vault (same paths): original diffs, wip/<worktree>.untracked.tar.gz (small untracked files), bundles flagged by the scan
Only repos matching RELEVANT are captured; every skip is logged to logs/git-capture-skips.jsonl with a reason.
Mapping repo -> prefix uses the SRC=PREFIX lists (srcs*.txt); unmapped repos go to repos/git-extra/<relpath>/.
Needs env TRACEDB_STORE and VAULT_STORE. Holds mkdir lock logs/store-write.lock around store writes (not with --scratch).
State (skip unchanged repos): ~/tracedb-work/state/git-capture.json
"""
import atexit, hashlib, io, json, os, re, subprocess, sys, tarfile, tempfile, time
sys.path.insert(0, os.path.expanduser("~/tracedb-work")); sys.path.insert(0, os.path.expanduser("~/tracedb-work/traceDB"))
import ingest as I
from tracedb.store import open_store
from tracedb.blob import put_blob, sha256_file

HOME = os.path.expanduser("~")
WORK = os.path.join(HOME, "tracedb-work")
LOCK = os.path.join(WORK, "logs", "store-write.lock")
STATE = os.path.join(WORK, "state", "git-capture.json")
SKIPLOG = os.path.join(WORK, "logs", "git-capture-skips.jsonl")
TMPDIR = os.path.join(WORK, "tmp-bundles")
PRUNE = {"node_modules", "Library", ".cache", "venv", ".venv", "site-packages", ".Trash", "repo-backups", ".npm", ".cargo",
         ".rustup", ".pyenv", ".nvm", ".bun", ".local", "Pictures", "Movies", "Music", "Downloads", ".homebrew", ".git"}
# relevant = path relative to ~ matches (LLM inference/optimisation, RSI, tracedb, kvm, challenge, qwen*, etc.)
RELEVANT = re.compile(r"(^|/)(?:inference-experiment|ten-percent|tenpct|MMF|kittens-vm|kvm|qwen|north-star|northstar|okc|cuda-intel|"
                      r"ultraquant|uq-|uqm-|rdf2|daw-in-reaper|cakebench|video-readme-maker|personal-website|traceDB|ontolagni|"
                      r"recursive-dflash2|readme-lm|magni|muse-|neurosurgeon|deepswe|bundle-bench|b300-work|l12-work|bobliu-os|"
                      r"flapping-airplanes|all-git|amd-experiments|tgt|opt|wt|wt-[a-z0-9-]+|work|tp-[a-z0-9]+|bobliuuu|Bobliuuu|"
                      r"nvim-in-nvim|swarm|dflash)[^/]*(/|$)", re.I)
IRRELEVANT = re.compile(r"(^|/)(\.asdf|\.codex/\.tmp|\.claude/plugins|\.dlt|airflow|lage|doom-src|doomgeneric|DMC100-Doom|StudioProjects|"
                        r"Launcher|Brush|holyc-benchmark|CAAP-LLM|intern-challenge[^/]*|test|cutlass-ref|rustlings|boids|shopify|"
                        r"GitHub/(?!personal-website|amd-experiments)[^/]*|libuv|neovim|nvim-port|kilo-src|google-project-program-argocd|"
                        r"waterloo-voice|wl-work|src|2026-27-Bobliuuu)(/|$)")


def sh(args, cwd=None, timeout=300, text=True, check=False):
    r = subprocess.run(args, cwd=cwd, capture_output=True, timeout=timeout)
    out = r.stdout
    return out.decode("utf-8", "replace") if text else out


def strip_url(u):
    return re.sub(r"(://)[^/@\s]+@", r"\1", u)


def skip(path, reason):
    with open(SKIPLOG, "a") as f:
        f.write(json.dumps({"repo": path, "reason": reason, "t": int(time.time())}) + "\n")


def lock():
    deadline = time.time() + 3600
    while True:
        try:
            os.mkdir(LOCK); return
        except FileExistsError:
            if time.time() > deadline: raise SystemExit("store-write.lock held > 1h")
            time.sleep(10)


def unlock():
    try: os.rmdir(LOCK)
    except OSError: pass


def load_prefixes(lists):
    m = {}
    for fn in lists:
        for tok in open(fn).read().split():
            if "=" not in tok: continue
            s, p = tok.split("=", 1)
            s = os.path.expanduser(s).rstrip("/")
            if os.path.isdir(s) and p.startswith("repos/"): m[s] = p
    return m


def prefix_for(path, pmap):
    best = None
    for s, p in pmap.items():
        if path == s or path.startswith(s + "/"):
            if best is None or len(s) > len(best[0]): best = (s, p)
    if best:
        rel = os.path.relpath(path, best[0])
        return best[1] + ("" if rel == "." else rel + "/")
    return "repos/git-extra/" + os.path.relpath(path, HOME).strip("/") + "/"


def find_gits(maxdepth=8):
    out = []
    for dp, dns, fns in os.walk(HOME):
        depth = dp[len(HOME):].count("/")
        if ".git" in dns or ".git" in fns: out.append(dp)
        dns[:] = [d for d in dns if d not in PRUNE and not (dp == WORK and d.startswith(("stage", "venv", "tmp-")))]
        if depth >= maxdepth: dns[:] = []
    return out


def scan_history(repo, secs):
    """stream `git log --all -p` through the redaction patterns; returns hit count, or -1 on timeout."""
    p = subprocess.Popen(["git", "-C", repo, "log", "--all", "-p", "--no-color", "--no-ext-diff"], stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL)
    t0 = time.time(); hits = 0; tail = b""
    while True:
        chunk = p.stdout.read(4 << 20)
        if not chunk: break
        _, n = I.redact(tail + chunk); hits += n
        tail = chunk[-512:]
        if time.time() - t0 > secs:
            p.kill(); return -1
        if hits: p.kill(); break
    p.wait()
    return hits


def redact_text(b, cap=20 << 20):
    trunc = len(b) > cap
    b = b[:cap]
    b, n = I.redact(b)
    return b, n, trunc


def capture(common, wts, base, st, vt, args, state):
    """common: git common dir; wts: list of worktree paths; base: store prefix ending in .git-bundle/"""
    main = os.path.dirname(common) if common.endswith("/.git") else wts[0]
    g = lambda *a, **k: sh(["git", "-C", main, *a], **k)
    refs = g("for-each-ref", "--format=%(refname) %(objectname)")
    if not refs.strip():
        skip(main, "no refs (empty repo)"); return None
    stash = g("stash", "list", "--format=%gd %H %s")
    wtinfo = []
    for w in wts:
        try:
            head = sh(["git", "-C", w, "rev-parse", "HEAD"]).strip()
            br = sh(["git", "-C", w, "rev-parse", "--abbrev-ref", "HEAD"]).strip()
            status = sh(["git", "-C", w, "status", "--porcelain=v1", "-uall"])
            diff = sh(["git", "-C", w, "diff", "HEAD", "--binary", "--no-ext-diff"], text=False, timeout=600)
        except Exception as e:
            wtinfo.append({"path": w, "error": repr(e)[:100]}); continue
        wtinfo.append({"path": w, "head": head, "branch": br, "status": status, "diff": diff})
    fp = hashlib.sha1(json.dumps([refs, stash, [(x.get("head"), x.get("status"), hashlib.sha1(x.get("diff", b"")).hexdigest()) for x in wtinfo]]).encode()).hexdigest()
    if not args["force"] and state.get(main) == fp:
        return {"repo": main, "unchanged": True}
    if args["dry"]:
        print("would capture", main, "->", base, "worktrees", len(wts)); return {"repo": main, "dry": True}

    res = {"repo": main, "prefix": base}
    # --- history scan + bundle
    hits = scan_history(main, args["maxscan"])
    res["scan_hits"] = hits
    os.makedirs(TMPDIR, exist_ok=True)
    bpath = os.path.join(TMPDIR, hashlib.sha1(main.encode()).hexdigest() + ".bundle")
    r = subprocess.run(["git", "-C", main, "bundle", "create", bpath, "--all"], capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(bpath):
        skip(main, "bundle failed: " + r.stderr[-200:]); return None
    loc = "main" if hits == 0 else "vault"
    target = st if hits == 0 else vt
    if not args["scratch"]: lock()
    try:
        br_ = put_blob(target, base + "repo.bundle", bpath, {"git_bundle": True})
    finally:
        if not args["scratch"]: unlock()
        os.unlink(bpath)
    res.update(bundle_sha=br_["sha256"], bundle_size=br_["size"], bundle_location=loc)
    # --- text artefacts
    log = g("log", "--all", "--format=%H%x1f%aI%x1f%an%x1f%ae%x1f%P%x1f%D%x1f%s", timeout=600)
    commits = []
    for ln in log.splitlines()[:300000]:
        f = ln.split("\x1f")
        if len(f) == 7: commits.append(json.dumps({"sha": f[0], "date": f[1], "author": f[2], "email": f[3], "parents": f[4].split(), "refs": f[5], "subject": f[6]}))
    remotes = {}
    for ln in g("config", "--get-regexp", r"^remote\..*\.url$").splitlines():
        k, _, v = ln.partition(" "); remotes[k.split(".")[1]] = strip_url(v)
    manifest = {"format": "tracedb-gitcapture/1", "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "repo_path": main,
                "remotes": remotes, "refs": [x.split(" ") for x in refs.splitlines()], "commits": len(commits),
                "bundle": {"sha256": br_["sha256"], "size": br_["size"], "location": loc, "path": base + "repo.bundle"},
                "history_scan_hits": hits, "stash_count": len(stash.splitlines()),
                "worktrees": [{k: v for k, v in x.items() if k in ("path", "head", "branch", "error")} for x in wtinfo],
                "restore": "tools/restore_repo.sh " + base[:-len(".git-bundle/")]}
    main_items = [(base + "commits.jsonl", "\n".join(commits).encode(), {"gitcapture": True}),
                  (base + "manifest.json", json.dumps(manifest, indent=1).encode(), {"gitcapture": True})]
    vault_items = []
    stash_txt = stash
    for ln in stash.splitlines():
        ref = ln.split(" ")[0]
        stash_txt += "\n##### " + ref + "\n" + g("stash", "show", "-p", ref, timeout=120)[: 5 << 20]
    if stash_txt.strip():
        rb, n, _ = redact_text(stash_txt.encode())
        main_items.append((base + "stash.txt", rb, {"gitcapture": True}))
        if n: vault_items.append((base + "stash.txt", stash_txt.encode(), {"vault": True}))
    for x in wtinfo:
        if "head" not in x: continue
        nm = os.path.relpath(x["path"], main).replace("/", "_").replace(".", "main") if x["path"] != main else "main"
        if x["diff"]:
            rb, n, tr = redact_text(x["diff"])
            main_items.append((base + f"wip/{nm}.diff", rb, {"gitcapture": True, "redacted": n, "truncated": tr}))
            if n or tr: vault_items.append((base + f"wip/{nm}.diff", x["diff"], {"vault": True}))
        untracked = sh(["git", "-C", x["path"], "ls-files", "--others", "--exclude-standard"]).splitlines()
        if untracked:
            main_items.append((base + f"wip/{nm}.untracked.txt", "\n".join(untracked[:20000]).encode(), {"gitcapture": True}))
            buf, tot = io.BytesIO(), 0
            with tarfile.open(fileobj=buf, mode="w:gz") as tf:
                for u in untracked[:5000]:
                    fp_ = os.path.join(x["path"], u)
                    try:
                        sz = os.path.getsize(fp_)
                        if os.path.islink(fp_) or sz > (5 << 20) or tot + sz > (100 << 20): continue
                        if os.path.basename(u) in ("__pycache__",) or u.endswith((".pyc", ".o", ".so")): continue
                        tf.add(fp_, arcname=u, recursive=False); tot += sz
                    except OSError: pass
            if tot: vault_items.append((base + f"wip/{nm}.untracked.tar.gz", buf.getvalue(), {"vault": True}))
    if not args["scratch"]: lock()
    try:
        st.put(main_items)
        if vault_items: vt.put(vault_items)
    finally:
        if not args["scratch"]: unlock()
    state[main] = fp
    res.update(commits=len(commits), vault_items=len(vault_items))
    return res


def main():
    a = sys.argv[1:]
    args = {"dry": "--dry" in a, "force": "--force" in a, "scratch": "--scratch" in a, "maxscan": 600, "only": None}
    if "--maxscan" in a: args["maxscan"] = int(a[a.index("--maxscan") + 1])
    if "--only" in a: args["only"] = a[a.index("--only") + 1]
    lists = [a[i + 1] for i, x in enumerate(a) if x == "--list"] or [os.path.join(WORK, f) for f in ("srcs.txt", "srcs2.txt", "srcs3.txt", "srcs4.txt")]
    lists = [l for l in lists if os.path.exists(l)]
    pmap = load_prefixes(lists)
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    state_file = STATE + (".scratch" if args["scratch"] else "")
    state = json.load(open(state_file)) if os.path.exists(state_file) else {}
    st = vt = None
    if not args["dry"]:
        st = open_store(os.environ["TRACEDB_STORE"]); vt = open_store(os.environ["VAULT_STORE"])
    groups = {}
    for wt in find_gits():
        c = sh(["git", "-C", wt, "rev-parse", "--path-format=absolute", "--git-common-dir"]).strip()
        if not c or not os.path.isdir(c):
            skip(wt, "broken gitdir / worktree"); continue
        groups.setdefault(c, []).append(wt)
    rows = []
    for common, wts in sorted(groups.items()):
        main = os.path.dirname(common) if common.endswith("/.git") else wts[0]
        rel = os.path.relpath(main, HOME)
        if args["only"] and args["only"] not in rel: continue
        if IRRELEVANT.search(rel + "/"): skip(main, "irrelevant third-party/unrelated clone"); continue
        if not RELEVANT.search(rel + "/"): skip(main, "not in relevance list"); continue
        try:
            r = capture(common, sorted(set(wts)), prefix_for(main, pmap) + ".git-bundle/", st, vt, args, state)
        except Exception as e:
            r = {"repo": main, "error": repr(e)[:300]}
        if r:
            rows.append(r); print(json.dumps(r), flush=True)
        if not args["dry"]:
            json.dump(state, open(state_file, "w"))
    print(json.dumps({"done": True, "repos": len(rows), "errors": sum(1 for r in rows if "error" in r)}))


if __name__ == "__main__":
    atexit.register(lambda: None)
    main()
