#!/usr/bin/env python3
"""mac_push.py -- thin Mac -> tracedb sync-box pusher. NO AWS credentials, NO local staging, works when `lab` is expired.
Streams new/changed files with rsync over ssh (alias `tracedb-sync-push`, a dedicated ed25519 key that the box restricts to an inbox dir
with authorized_keys command=rrsync) into <inbox>/in/<store-prefix>/..., then fetches the box's ack manifest (sha256 per ingested file)
and deletes local copies ONLY when the box acked the exact same sha256 and the file is older than the source's prune_days.
Config ~/.config/tracedb-push.tsv (created with defaults if absent): SRC<TAB>STORE_PREFIX<TAB>PRUNE_DAYS(0 = never; -1 = at once)<TAB>flags
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
        rows.append((os.path.expanduser(c[0]), c[1].strip("/"), int(c[2] or 0), [x for x in c[3].split(",") if x]))
    return rows


def push(src, prefix, extra):
    if not os.path.exists(src): return 0, "missing"
    ssh = "ssh -o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=30"
    base = [rsync_bin(), "-rlt", "-O", "--partial-dir=.rsync-partial", "--chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r", "--timeout=300", "-e", ssh] + [f"--exclude={x}" for x in EXCL + extra]
    if os.path.isfile(src):
        cmd = base + [src, f"{ALIAS}:in/{prefix}/{os.path.basename(src)}"]
    else:
        cmd = base + [src.rstrip("/") + "/", f"{ALIAS}:in/{prefix}/"]
    r = run(cmd)
    return r.returncode, r.stderr[-300:]


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
        if days == 0 or not os.path.isdir(src) and not os.path.isfile(src): continue
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
        rc, err = push(src, prefix, extra); st["pushed"][src] = rc
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
