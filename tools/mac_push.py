#!/usr/bin/env python3
"""mac_push.py -- thin Mac -> tracedb sync-box pusher. NO AWS credentials, NO local staging, works when `lab` is expired.
Streams new/changed files with rsync over ssh (alias `tracedb-sync-push`, a dedicated ed25519 key that the box restricts to an inbox dir
with authorized_keys command=rrsync) into <inbox>/in/<store-prefix>/..., then fetches the box's ack manifest (sha256 per ingested file)
and deletes local copies ONLY when the box acked the exact same sha256 and the file is older than the source's prune_days.
Config ~/.config/tracedb-push.tsv (created with defaults if absent): SRC<TAB>STORE_PREFIX<TAB>PRUNE_DAYS(0 = never; -1 = at once)<TAB>excludes
PRUNE_DAYS `since` = work directory: send only files changed after the cutover stamp ~/.cache/tracedb-push/baseline (older content was
already ingested by the old Mac autosync), never delete. Add --add-work DIR=PREFIX lines (or run --import-srcs LIST...) to register them.
Runs from launchd com.tracedb.push (nice, background IO). Status: ~/.cache/tracedb-push/status.json. stdlib only. No IPs here: the ssh alias hides it."""
import fcntl, hashlib, json, os, shutil, subprocess, sys, time

HOME = os.path.expanduser("~"); CACHE = os.path.join(HOME, ".cache", "tracedb-push"); CONF = os.path.join(HOME, ".config", "tracedb-push.tsv")
ALIAS = os.environ.get("TRACEDB_PUSH_ALIAS", "tracedb-sync-push")
DEFAULT = """# SRC<TAB>STORE_PREFIX<TAB>PRUNE_DAYS<TAB>extra rsync excludes (comma separated)
~/.claude/projects\ttraces/claude-code\t14\t
~/.codex/sessions\ttraces/codex/sessions\t14\t
~/.codex/archived_sessions\ttraces/codex/archived_sessions\t14\t
~/.cursor/projects\ttraces/cursor\t14\tnode_modules
~/.claude/history.jsonl\tconfig/claude\t0\t
~/.claude/settings.json\tconfig/claude\t0\t
~/tracedb-outbox\trepos/mac-outbox\t-1\t
"""
SINCE = -999   # work-directory mode (see module docstring)
MAXSIZE = 20 << 30
WORK_SKIP = {"target", "build-cache", ".mypy_cache", ".pytest_cache", ".ruff_cache", "site-packages", "dist-packages", "Pods", ".gradle", ".terraform", ".next"}
EXCL = [".git", "node_modules", "__pycache__", ".venv", "venv", ".DS_Store", "*.pem", "*.sock", "*.sqlite-wal", "*.sqlite-shm", ".cache"]


def rsync_bin():  # needs real rsync (brew install rsync): macOS openrsync cannot talk to rrsync
    for p in ("/opt/homebrew/bin/rsync", "/usr/local/bin/rsync", "/usr/bin/rsync"):
        if os.path.exists(p): return p
    return "rsync"


def run(cmd, **k):
    return subprocess.run(cmd, capture_output=True, text=True, **k)


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""): h.update(b)
    return h.hexdigest()


def notify(msg):
    stamp = os.path.join(CACHE, "last-notify")
    if os.path.exists(stamp) and time.time() - os.path.getmtime(stamp) < 6 * 3600: return
    open(stamp, "w").close()
    run(["osascript", "-e", f'display notification "{msg}" with title "tracedb push"'])


def load_conf():
    if not os.path.exists(CONF):
        os.makedirs(os.path.dirname(CONF), exist_ok=True); open(CONF, "w").write(DEFAULT)
    rows = []
    for line in open(CONF):
        if not line.strip() or line.startswith("#"): continue
        c = (line.rstrip("\n").split("\t") + ["", "", "", ""])[:4]
        days = SINCE if c[2] == "since" else int(c[2] or 0)
        rows.append((os.path.expanduser(c[0]), c[1].strip("/"), days, [x for x in c[3].split(",") if x]))
    return rows


def rsync_base(extra):
    ssh = "ssh -o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=30"
    return [rsync_bin(), "-rlt", "-O", "--partial-dir=.rsync-partial", "--chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r", "--timeout=300", "-e", ssh] + [f"--exclude={x}" for x in EXCL + extra]


def import_srcs(lists):
    """One-time cutover: register every existing SRC=PREFIX of the old autosync lists as a `since` row."""
    rows = load_conf(); have = [r[0] for r in rows]; add = []
    for fn in lists:
        for tok in open(os.path.expanduser(fn)).read().split():
            if "=" not in tok: continue
            s, pre = tok.split("=", 1); s = os.path.expanduser(s); pre = pre.strip("/")
            if not os.path.exists(s) or s in have or any(s == h or s.startswith(h + "/") for h in have): continue
            if len(pre.split("/")) < 2 and s != os.path.join(HOME, ".codex"): continue
            ex = "packages,plugins,cache,.tmp,auth.json,sessions,archived_sessions" if s == os.path.join(HOME, ".codex") else ""
            if "/.claude/projects" in s or "/Library/" in s: continue
            have.append(s); add.append(f"{s.replace(HOME, '~', 1)}\t{pre}\tsince\t{ex}\n")
    open(CONF, "a").writelines(add)
    base = os.path.join(CACHE, "baseline"); os.makedirs(CACHE, exist_ok=True)
    if not os.path.exists(base): open(base, "w").close()
    print(f"registered {len(add)} work sources")


def push(src, prefix, extra):
    if not os.path.exists(src): return 0, "missing"
    base = rsync_base(extra)
    if os.path.isfile(src):
        cmd = base + [src, f"{ALIAS}:in/{prefix}/{os.path.basename(src)}"]
    else:
        cmd = base + [src.rstrip("/") + "/", f"{ALIAS}:in/{prefix}/"]
    r = run(cmd)
    return r.returncode, r.stderr[-300:]


def changed(src, extra, since):
    """Relative paths of regular files under src modified after `since` (heavy/regenerable directories pruned)."""
    skip = WORK_SKIP | {x for x in EXCL + extra if "*" not in x}
    for dp, dn, fs in os.walk(src):
        dn[:] = [d for d in dn if d not in skip]
        for f in fs:
            full = os.path.join(dp, f)
            try: st = os.lstat(full)
            except OSError: continue
            if st.st_mtime > since and (st.st_mode & 0o170000) == 0o100000 and st.st_size <= MAXSIZE and not f.endswith((".pem", ".sock")) and f != ".DS_Store":
                yield os.path.relpath(full, src)


def push_since(src, prefix, extra):
    stamp = os.path.join(CACHE, "since", hashlib.sha1(src.encode()).hexdigest()); os.makedirs(os.path.dirname(stamp), exist_ok=True)
    base = os.path.join(CACHE, "baseline")
    since = os.path.getmtime(stamp) if os.path.exists(stamp) else os.path.getmtime(base)
    start = time.time()
    if os.path.isfile(src):
        if os.path.getmtime(src) <= since: return 0, ""
        rc, err = push(src, prefix, extra)
    elif os.path.isdir(src):
        try: os.listdir(src)
        except PermissionError: return 13, "macOS privacy settings block this folder for the launchd job (Full Disk Access)"
        lst = list(changed(src, extra, since - 60))
        if not lst: return 0, ""
        lf = os.path.join(CACHE, "files-from")
        open(lf, "w").write("\0".join(lst) + "\0")
        r = run(rsync_base(extra) + ["--from0", f"--files-from={lf}", src.rstrip("/") + "/", f"{ALIAS}:in/{prefix}/"])
        rc, err = r.returncode, r.stderr[-300:]
    else:
        return 0, "missing"
    if rc in (0, 24):
        open(stamp, "w").close(); os.utime(stamp, (start, start))
    return rc, err


def ensure_dirs(rows):
    """rrsync gives no shell; create in/<prefix>/ by syncing an empty skeleton."""
    sk = os.path.join(CACHE, "skel"); shutil.rmtree(sk, ignore_errors=True)
    for _, prefix, _, _ in rows: os.makedirs(os.path.join(sk, "in", prefix), exist_ok=True)
    return run([rsync_bin(), "-r", "-O", "--chmod=Du=rwx,Dgo=rx", "-e", "ssh -o BatchMode=yes -o ConnectTimeout=20", sk + "/", f"{ALIAS}:"])


def fetch_ack():
    os.makedirs(CACHE, exist_ok=True); dst = os.path.join(CACHE, "manifest.tsv")
    r = run([rsync_bin(), "-t", "--timeout=300", "-e", "ssh -o BatchMode=yes -o ConnectTimeout=20", f"{ALIAS}:ack/manifest.tsv", dst])
    acked = {}
    if os.path.exists(dst):
        for line in open(dst):
            sh, _, rel = line.rstrip("\n").partition("\t")
            if rel: acked[rel] = sh
    return acked, r.returncode


def prune(rows, acked):
    freed = n = 0
    for src, prefix, days, _ in rows:
        if days in (0, SINCE) or not os.path.isdir(src) and not os.path.isfile(src): continue
        cutoff = time.time() - max(days, 0) * 86400
        paths = [src] if os.path.isfile(src) else [os.path.join(dp, f) for dp, dn, fs in os.walk(src) for f in fs]
        for full in paths:
            rel = f"{prefix}/{os.path.basename(src) if os.path.isfile(src) else os.path.relpath(full, src)}"
            try:
                st = os.lstat(full)
                parts = rel.split("/")
                if "memory" in parts or full.endswith((".md", ".toml", ".yaml", ".yml")) and days > 0: continue   # live state, never pruned
                if not os.path.isfile(full) or os.path.islink(full) or (days > 0 and st.st_mtime > cutoff) or (days < 0 and time.time() - st.st_mtime < 120): continue
                if acked.get(rel) == sha256(full):
                    os.remove(full); freed += st.st_size; n += 1
            except OSError:
                continue
        if os.path.isdir(src):
            for dp, dn, fs in os.walk(src, topdown=False):
                if dp != src and not os.listdir(dp):
                    try: os.rmdir(dp)
                    except OSError: pass
    return n, freed


def main():
    os.makedirs(CACHE, exist_ok=True)
    if "--import-srcs" in sys.argv:
        import_srcs(sys.argv[sys.argv.index("--import-srcs") + 1:]); return 0
    lk = open(os.path.join(CACHE, "lock"), "w")
    try: fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError: return 0
    os.nice(15)
    rows = load_conf(); st = {"time": time.strftime("%FT%TZ", time.gmtime()), "pushed": {}, "errors": []}
    r = ensure_dirs(rows)
    if r.returncode:
        st["errors"].append("box unreachable: " + r.stderr[-200:]); json.dump(st, open(os.path.join(CACHE, "status.json"), "w"))
        notify("sync box unreachable; nothing pushed (will retry)"); return 2
    for src, prefix, days, extra in rows:
        rc, err = (push_since if days == SINCE else push)(src, prefix, extra); st["pushed"][src] = rc
        if rc not in (0, 24): st["errors"].append(f"{src}: rc={rc} {err}")   # 24 = files vanished mid-transfer
    acked, rc = fetch_ack()
    st["acked"] = len(acked)
    if rc == 0 and "--no-prune" not in sys.argv:
        st["pruned_files"], st["pruned_bytes"] = prune(rows, acked)
    json.dump(st, open(os.path.join(CACHE, "status.json"), "w"), indent=1)
    if st["errors"]: notify("push errors: see ~/.cache/tracedb-push/status.json"); return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
