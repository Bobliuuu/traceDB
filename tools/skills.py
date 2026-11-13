#!/usr/bin/env python3
"""skills -- every Claude Code / agent skill saved in traceDB, and one user-level set installed identically on every machine.

Store layout (main store $TRACEDB_STORE; secret-bearing files: redacted there, original bytes in $VAULT_STORE, same path):
  skills/MANIFEST.json                 {"skills": {name: {"user": <variant>|null, "variants": {<variant>: {...}}}}}
  skills/<name>/<variant>/<files...>   variant = first 12 hex of the skill's content hash (sorted relpath + file sha256)
Each variant records its sources (repo root, path in repo, git commit, branch, remote) and the SKILL.md description.
`user` marks the variant installed to ~/.claude/skills/<name> on every machine. Other variants are saved and
listed in the ontology (repo-local .agents/skills copies, older forks) but not installed globally.

  skills scan                 what push would save (read-only): skill dirs under ~/.claude/skills and $SKILLS_ROOTS (default ~/code)
  skills push                 save every skill variant found; skills present in ~/.claude/skills become the user set
  skills promote NAME [SUBSTR]  make the variant whose source path contains SUBSTR (default: newest) user-level
  skills demote NAME          keep it saved, stop installing it
  skills pull [--dry-run]     install the user set into ~/.claude/skills (anything replaced -> ~/.keys-backup/<stamp>/)
  skills status | list
Config: env (or ~/.config/tracedb/env) TRACEDB_STORE, VAULT_STORE, AWS_PROFILE.
"""
from __future__ import annotations
import hashlib, json, os, re, shutil, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import keys as K  # noqa: E402  (env loading, vault/main store handles)

HOME = K.HOME
SK = os.path.join(HOME, ".claude", "skills")
MAN = "skills/MANIFEST.json"
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".DS_Store"}
SKIP_FILE = re.compile(r"(\.pyc|\.DS_Store|~)$")
EXCLUDE_TOP = {"synced"}  # ~/.claude/skills/synced: account-synced skills, managed by Claude Code itself

_M = None


def main_store():
    global _M
    if _M is None:
        _M = K._store("TRACEDB_STORE")
    return _M


def redact(b):
    import ingest  # traceDB tools/ingest.py: the same secret patterns as the main-store ingest
    return ingest.redact(b)


def skill_files(d):
    out = []
    for dp, dns, fns in os.walk(d, followlinks=True):
        dns[:] = sorted(x for x in dns if x not in SKIP_DIRS)
        for f in sorted(fns):
            p = os.path.join(dp, f)
            if os.path.isfile(p) and not SKIP_FILE.search(f):
                out.append(os.path.relpath(p, d))
    return out


def content_hash(d):
    h = hashlib.sha256(); files = {}
    for rel in skill_files(d):
        fs = hashlib.sha256(open(os.path.join(d, rel), "rb").read()).hexdigest()
        files[rel] = fs; h.update(f"{rel}\0{fs}\n".encode())
    return h.hexdigest(), files


def description(d):
    try:
        t = open(os.path.join(d, "SKILL.md"), encoding="utf-8", errors="replace").read(8192)
    except OSError:
        return ""
    m = re.search(r"(?m)^description:\s*(.+(?:\n[ \t]+.+)*)", t)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip().strip("'\">|")[:400]
    body = [l for l in t.splitlines() if l.strip() and not l.startswith(("#", "---"))]
    return body[0][:300] if body else ""


def git_info(d):
    def g(*a):
        r = subprocess.run(["git", "-C", d, *a], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""
    top = g("rev-parse", "--show-toplevel")
    if not top:
        return {"path": d.replace(HOME, "~", 1)}
    rel = os.path.relpath(d, top)
    dirty = bool(g("status", "--porcelain", "--", rel))
    return {"repo": top.replace(HOME, "~", 1), "path_in_repo": rel, "commit": g("rev-parse", "HEAD")[:12],
            "branch": g("rev-parse", "--abbrev-ref", "HEAD"), "remote": g("remote", "get-url", "origin"),
            "dirty": dirty, "path": d.replace(HOME, "~", 1)}


def roots():
    env = os.environ.get("SKILLS_ROOTS")
    return [os.path.expanduser(x) for x in env.split(":")] if env else [os.path.join(HOME, "code")]


def scan():
    """-> list of (name, dir, installed_in_user_dir)."""
    found = []
    if os.path.isdir(SK):
        for n in sorted(os.listdir(SK)):
            p = os.path.join(SK, n)
            if n not in EXCLUDE_TOP and os.path.isdir(p) and os.path.exists(os.path.join(p, "SKILL.md")):
                found.append((n, os.path.realpath(p), True))
    seen = {d for _, d, _ in found}
    for r in roots():
        for dp, dns, fns in os.walk(r):
            dns[:] = [x for x in dns if x not in SKIP_DIRS and not x.startswith(".git")]
            if "SKILL.md" in fns:
                d = os.path.realpath(dp)
                if d not in seen:
                    seen.add(d)
                    name = os.path.basename(dp) if os.path.basename(dp) not in ("skill", "skills") else os.path.basename(os.path.dirname(dp))
                    found.append((name, d, False))
                dns[:] = []  # a skill's own sub-dirs are part of it
    return found


def manifest():
    b = K.read_or_none(main_store(), MAN)
    return json.loads(b) if b is not None else {"version": 1, "skills": {}}  # None only before the first push


def save(m, items_main, items_vault):
    m["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    m["by"] = f"{os.environ.get('USER', '?')}@{os.uname().nodename}"
    if items_vault:
        K.vault().put(items_vault)
    main_store().put(items_main + [(MAN, json.dumps(m, indent=1, sort_keys=True).encode(), {"skills": "manifest"})])


def cmd_scan():
    for n, d, u in scan():
        print(f"{'user ' if u else '     '}{n:48} {d.replace(HOME, '~', 1)}")


def push():
    m = manifest(); S = m.setdefault("skills", {})
    items_main, items_vault, new = [], [], []
    for name, d, user in scan():
        full, files = content_hash(d)
        v = full[:12]
        s = S.setdefault(name, {"user": None, "variants": {}})
        var = s["variants"].get(v)
        src = git_info(d)
        if var is None:
            secret = []
            for rel in files:
                b = open(os.path.join(d, rel), "rb").read()
                red, hits = redact(b)
                path = f"skills/{name}/{v}/{rel}"
                items_main.append((path, red, {"skill": name, "variant": v, **({"redacted": hits} if hits else {})}))
                if hits:
                    secret.append(rel); items_vault.append((path, b, {"vault": True, "skill": name, "variant": v}))
            var = s["variants"][v] = {"sha256": full, "files": files, "bytes": sum(os.path.getsize(os.path.join(d, r)) for r in files),
                                      "description": description(d), "sources": [], "first_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            if secret: var["secret_files"] = secret
            new.append(f"{name}@{v}")
        key = (src.get("repo"), src.get("path_in_repo"), src.get("path"))
        var["sources"] = [x for x in var["sources"] if (x.get("repo"), x.get("path_in_repo"), x.get("path")) != key] + [src]
        if user:
            s["user"] = v
        s["installs_to"] = "~/.claude/skills/" + name if s["user"] else "repo-local (installed with its repo)"
    save(m, items_main, items_vault)
    nv = sum(len(s["variants"]) for s in S.values())
    print(f"skills push: {len(S)} skills, {nv} variants ({len(new)} new{': ' + ' '.join(new) if new else ''}); "
          f"user set: {sum(1 for s in S.values() if s['user'])}")


def promote(name, sub=None):
    m = manifest(); s = m["skills"].get(name) or sys.exit(f"skills: no saved skill {name!r} (skills push first)")
    vs = s["variants"]
    cand = [v for v, x in vs.items() if not sub or v.startswith(sub) or any(sub in (y.get("path") or "") for y in x["sources"])]
    if not cand: sys.exit(f"skills promote: no variant of {name} matches {sub!r}")
    v = max(cand, key=lambda v: vs[v].get("first_seen", ""))
    s["user"] = v; s["installs_to"] = "~/.claude/skills/" + name
    save(m, [], []); print(f"{name}: user variant {v} ({vs[v]['sources'][-1].get('path')})")


def demote(name):
    m = manifest(); s = m["skills"].get(name) or sys.exit(f"skills: no saved skill {name!r}")
    s["user"] = None; s["installs_to"] = "repo-local (installed with its repo)"
    save(m, [], []); print(f"{name}: no longer installed globally (still saved)")


def installed_hash(name):
    p = os.path.join(SK, name)
    return content_hash(os.path.realpath(p))[0][:12] if os.path.isdir(p) else None


def pull(dry=False):
    m = manifest()
    if not m.get("skills"):
        sys.exit(f"skills pull: no {MAN} in {os.environ['TRACEDB_STORE']} (run skills push on a set-up machine first)")
    stamp = time.strftime("%Y%m%dT%H%M%S")
    bk = os.path.join(HOME, ".keys-backup", stamp, ".claude", "skills")
    done = same = 0
    for name, s in sorted(m.get("skills", {}).items()):
        v = s.get("user")
        if not v: continue
        if installed_hash(name) == v:
            same += 1; continue
        var = s["variants"][v]
        print(("would install " if dry else "install ") + f"~/.claude/skills/{name}  ({v})")
        if dry: continue
        os.makedirs(SK, exist_ok=True)
        tmp = os.path.join(SK, f".{name}.tmp-{stamp}")
        shutil.rmtree(tmp, ignore_errors=True)
        secret = set(var.get("secret_files", []))
        for rel, fs in var["files"].items():
            st = K.vault() if rel in secret else main_store()
            b = st.read(f"skills/{name}/{v}/{rel}", max_bytes=1 << 30)
            if hashlib.sha256(b).hexdigest() != fs:
                sys.exit(f"skills: {name}/{rel} bytes do not match the manifest; refusing")
            p = os.path.join(tmp, rel); os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "wb").write(b)
            if rel.endswith((".py", ".sh")) and b.startswith(b"#!"): os.chmod(p, 0o755)
        dst = os.path.join(SK, name)
        if os.path.lexists(dst):
            os.makedirs(bk, exist_ok=True); shutil.move(dst, os.path.join(bk, name))
        os.replace(tmp, dst); done += 1
    print(f"skills pull: {done} installed, {same} already identical" + (f"; replaced copies in {bk}" if os.path.isdir(bk) else ""))


def status():
    m = manifest(); S = m.get("skills", {})
    user = {n: s["user"] for n, s in S.items() if s.get("user")}
    bad = [n for n, v in user.items() if installed_hash(n) != v]
    local = {n for n in os.listdir(SK) if n not in EXCLUDE_TOP and not n.startswith(".")} if os.path.isdir(SK) else set()
    extra = sorted(local - set(user))
    print(f"saved: {len(S)} skills, {sum(len(s['variants']) for s in S.values())} variants (updated {m.get('updated', '?')} by {m.get('by', '?')})")
    print(f"user set: {len(user)}; installed identically: {len(user) - len(bad)}" + (f"; differ/missing: {' '.join(bad)}  (skills pull)" if bad else ""))
    if extra: print(f"in ~/.claude/skills but not in the user set: {' '.join(extra)}  (skills push saves them)")
    return 1 if bad or extra else 0


def list_():
    m = manifest()
    for n, s in sorted(m.get("skills", {}).items()):
        vs = s["variants"]
        print(f"{'U' if s.get('user') else ' '} {n:48} {len(vs)} variant(s)  " + (vs[s['user']]['description'][:70] if s.get('user') else
              next(iter(vs.values()))['description'][:70]))


def main(a):
    c = a[0] if a else "status"
    if c == "scan": return cmd_scan()
    if c == "push": return push()
    if c == "promote" and a[1:]: return promote(a[1], a[2] if a[2:] else None)
    if c == "demote" and a[1:]: return demote(a[1])
    if c == "pull": return pull("--dry-run" in a)
    if c == "status": return status()
    if c == "list": return list_()
    print(__doc__); return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) or 0)
