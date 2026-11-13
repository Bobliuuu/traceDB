#!/usr/bin/env python3
"""keys -- every SSH key, the ssh config and every service credential, restored byte-exact from the tracedb vault.

Canonical copy (written ONLY by this tool), in the vault store ($VAULT_STORE):
  keys/MANIFEST.json      {"files": [{dest, path, mode, sha256, size, kind}, ...]}   dest is relative to $HOME
  keys/files/<dest>       the bytes of each file
Other vault prefixes (home/ssh/, home/..., config/...) are machine snapshots written by auto-sync ingests. They are
NOT read by `pull`; `status` only reports files a snapshot has that canonical lacks, so new keys are noticed.

  keys status                 local vs canonical, plus snapshot drift (read-only)
  keys list                   the manifest: dest, mode, size, sha256 prefix, ssh key fingerprint
  keys pull [--dry-run]       canonical -> $HOME (exact bytes + modes; overwritten files go to ~/.keys-backup/<stamp>/)
  keys push                   tracked files + new files in ~/.ssh -> canonical (only changed files are uploaded)
  keys add PATH...            track more files (any file under $HOME), uploaded by the next push (done at once)
  keys forget DEST...         stop tracking DEST (the vault bytes stay)
  keys doctor [ALIAS...]      per ssh alias: route, key(s), present locally?, reachable?, which key the box accepted
  keys boxes probe [--publish] [ALIAS...]   read-only probe of every box -> box registry (config/boxes.json, main store)
  keys boxes show | publish [FILE] | path
  keys install HOST...|all    append ~/.ssh/id_ed25519_main.pub to HOST's authorized_keys (WRITES to the remote box)

Config: env VAULT_STORE, TRACEDB_STORE, AWS_PROFILE, else ~/.config/tracedb/env (KEY=VALUE lines, written by bootstrap).
Never prints key or token contents: only names, sizes, sha256 prefixes and public-key fingerprints.
"""
from __future__ import annotations
import base64, hashlib, json, os, re, shutil, socket, stat, subprocess, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor

HOME = os.path.expanduser("~")
ENV_FILE = os.path.join(HOME, ".config", "tracedb", "env")
PFX, MANIFEST = "keys/files/", "keys/MANIFEST.json"
REGISTRY = "config/boxes.json"
MAIN_KEY = ".ssh/id_ed25519_main"
SSH_SKIP = re.compile(r"(\.bak|\.old|\.tmp|\.lock|\.tar|\.swp|~)$|\.bak-|^config\.pre|^\.|^known_hosts\.old$|\.sock$")
UNION = {"known-hosts"}  # pull merges (canonical wins per host); push uploads the local file as is
HOMEKEY = "@HOMEKEY@"  # Claude Code keys per-project dirs by path (/home/u -> -home-u); re-keyed per machine


def _load_env():
    for fn in (ENV_FILE + ".local", ENV_FILE):  # machine-local overrides first (setdefault: first one wins)
        if not os.path.exists(fn):
            continue
        for line in open(fn):
            m = re.match(r"\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)=(.*)$", line)
            if m:
                os.environ.setdefault(m.group(1), os.path.expandvars(m.group(2).strip().strip("'\"")))
    os.environ.setdefault("AWS_PROFILE", "lab")


_load_env()


_STORES = {}


def _store(var):
    if var in _STORES:
        return _STORES[var]
    spec = os.environ.get(var)
    if not spec:
        sys.exit(f"keys: set {var} (or run bootstrap, which writes {ENV_FILE})")
    from tracedb.store import open_store
    st = open_store(spec)
    st.sync()  # the local catalog may be empty or stale (fresh machine): reads only see what sync loaded
    _STORES[var] = st
    return st


def read_or_none(st, path):
    """Bytes of path, or None if the store has no such entry. Any other failure (auth, network) raises."""
    if st.locate(path) is None:
        return None
    return st.read(path, max_bytes=1 << 30)


_V = None


def vault():
    global _V
    if _V is None:
        _V = _store("VAULT_STORE")
    return _V


def sha(b):
    return hashlib.sha256(b).hexdigest()


def homekey():
    return re.sub(r"[^A-Za-z0-9]", "-", HOME)


def real(dest):
    """manifest dest -> path relative to this $HOME (expands the per-machine Claude project key)."""
    return dest.replace(HOMEKEY, homekey())


def logical(rel):
    """path relative to $HOME -> manifest dest (inverse of real)."""
    pre = f".claude/projects/{homekey()}/"
    return f".claude/projects/{HOMEKEY}/" + rel[len(pre):] if rel.startswith(pre) else rel


def kind_of(dest):
    n = os.path.basename(dest)
    if dest.startswith(".claude/"): return "claude-memory" if "/memory/" in dest else "claude-config"
    if dest.startswith(".ssh/"):
        if n == "config": return "ssh-config"
        if n.startswith("known_hosts") or n.endswith("known_hosts"): return "known-hosts"
        if n == "authorized_keys": return "authorized-keys"
        if n.endswith(".pub"): return "ssh-pub"
        return "ssh-key"
    if dest.startswith(".aws/"): return "aws"
    return "credential"


def default_mode(dest, local_mode=None):
    k = kind_of(dest)
    if k == "ssh-pub": return 0o644
    if k in ("claude-config", "claude-memory"): return (local_mode or 0o644) & 0o755 or 0o644
    if k in ("ssh-key", "ssh-config", "authorized-keys", "known-hosts", "aws", "credential"):
        return 0o600 if local_mode is None else (local_mode & 0o700) or 0o600  # never group/world readable
    return local_mode or 0o600


def manifest():
    b = read_or_none(vault(), MANIFEST)
    return json.loads(b) if b is not None else {"version": 1, "files": []}  # None only before the first push


def entries(m):
    return {e["dest"]: e for e in m["files"]}


def local_ssh_files():
    d = os.path.join(HOME, ".ssh")
    out = []
    if os.path.isdir(d):
        for n in sorted(os.listdir(d)):
            p = os.path.join(d, n)
            if os.path.isfile(p) and not os.path.islink(p) and not SSH_SKIP.search(n):
                out.append(".ssh/" + n)
    return out


def local_claude_files():
    """Session config that makes every Claude Code session identical: global CLAUDE.md, settings.json, home memory."""
    out = [d for d in (".claude/CLAUDE.md", ".claude/settings.json") if os.path.isfile(os.path.join(HOME, d))]
    mem = os.path.join(HOME, ".claude", "projects", homekey(), "memory")
    if os.path.isdir(mem):
        out += [logical(os.path.relpath(os.path.join(mem, n), HOME)) for n in sorted(os.listdir(mem))
                if n.endswith(".md") and os.path.isfile(os.path.join(mem, n))]
    return out


def auto_tracked():
    return local_ssh_files() + local_claude_files()


def lread(dest):
    p = os.path.join(HOME, real(dest))
    try:
        return open(p, "rb").read()
    except OSError:
        return None


def union(a: bytes, b: bytes) -> bytes:
    """known_hosts merge on pull: canonical lines a, plus local lines of b for hosts canonical does not mention.
    A host canonical knows keeps only canonical's key, so a stale key removed on the pushing machine stays removed."""
    ca = [l for l in (a or b"").splitlines() if l.strip()]
    hosts = {l.split()[0] for l in ca if not l.startswith(b"#")}
    out = ca + [l for l in (b or b"").splitlines() if l.strip() and l not in ca and l.split()[0] not in hosts]
    return b"\n".join(out) + b"\n" if out else b""


def fingerprint(dest):
    """Public-key fingerprint (from the .pub next to it, or derived from the private key by ssh-keygen). Never content."""
    p = os.path.join(HOME, real(dest))
    k = kind_of(dest)
    if k not in ("ssh-key", "ssh-pub"): return ""
    src = p if k == "ssh-pub" else (p + ".pub" if os.path.exists(p + ".pub") else p)
    try:
        r = subprocess.run(["ssh-keygen", "-l", "-f", src], capture_output=True, text=True, timeout=5)
        return " ".join(r.stdout.split()[1:2] + r.stdout.split()[-1:]) if r.returncode == 0 else ""
    except Exception:
        return ""


# ------------------------------------------------------------------------------------------------ status / list
def status():
    m = manifest(); E = entries(m)
    tracked = set(E)
    same, differ, missing = [], [], []
    for dest, e in sorted(E.items()):
        b = lread(dest)
        if b is None: missing.append(dest)
        elif sha(b) != e["sha256"]: differ.append(dest)
        else: same.append(dest)
    untracked = [d for d in auto_tracked() if d not in tracked]
    print(f"canonical: {len(E)} files in {os.environ['VAULT_STORE']} {MANIFEST} (updated {m.get('updated', '?')} by {m.get('by', '?')})")
    print(f"  in sync: {len(same)}")
    for t, xs, hint in (("local differs", differ, "push to publish, pull to restore canonical"),
                        ("missing locally", missing, "pull restores"), ("untracked (~/.ssh, ~/.claude)", untracked, "push adds")):
        print(f"  {t}: {len(xs)}" + (f"   ({hint})\n    " + "\n    ".join(xs) if xs else ""))
    # snapshot drift: files the auto-sync snapshot (home/ssh/) has that canonical lacks or holds with other bytes
    snap = {r["path"][len("home/ssh/"):]: r["sha"] for r in vault().ls("home/ssh/", limit=100000) if "/" not in r["path"][9:]}
    cano = {d[5:]: e["sha256"] for d, e in E.items() if d.startswith(".ssh/")}
    new = sorted(n for n in snap if n not in cano and not SSH_SKIP.search(n) and not n.startswith("config"))
    mine = set(aliases()) if os.path.exists(ssh_cfg()) else set()
    lacking = set()
    for n in snap:
        if n == "config" or n.startswith("config.") and not SSH_SKIP.search(n):
            txt = vault().read("home/ssh/" + n, max_bytes=1 << 24).decode(errors="replace")
            for line in txt.splitlines():
                w = line.split("#", 1)[0].split()
                if w and w[0].lower() == "host":
                    lacking |= {h for h in w[1:] if not any(c in h for c in "*?!") and h not in mine}
    if lacking:
        print(f"  snapshot ssh config(s) in home/ssh/ define {len(lacking)} Host alias(es) your config lacks: {' '.join(sorted(lacking))}\n"
              f"    merge them into ~/.ssh/config (paths ~/.ssh/...), check with ssh -G, then keys push")
    if new:
        print(f"  snapshot home/ssh/ has {len(new)} file(s) canonical lacks (a key made on another machine?): {' '.join(new)}\n"
              f"    adopt one: tracedb --store $VAULT_STORE get --max-bytes -1 home/ssh/<name> > ~/.ssh/<name>; chmod 600 ...; keys push")
    return 0 if not (differ or missing or untracked) else 1


def list_():
    m = manifest()
    print(f"{'dest (~/...)':44} {'mode':>4} {'bytes':>7}  sha256       kind             fingerprint")
    for e in sorted(m["files"], key=lambda e: e["dest"]):
        print(f"{e['dest']:44} {e['mode']:>4} {e['size']:>7}  {e['sha256'][:12]} {e['kind']:16} {fingerprint(e['dest'])}")
    print(f"{len(m['files'])} files; updated {m.get('updated', '?')} by {m.get('by', '?')}")


# ------------------------------------------------------------------------------------------------ pull
def _mkdirs(d):
    """Create missing parents with 0700 (existing dirs untouched, except ~/.ssh which is forced to 0700)."""
    parts = os.path.relpath(d, HOME).split(os.sep)
    cur = HOME
    for p in parts:
        cur = os.path.join(cur, p)
        if not os.path.isdir(cur):
            os.mkdir(cur, 0o700)
    ssh = os.path.join(HOME, ".ssh")
    if os.path.isdir(ssh): os.chmod(ssh, 0o700)


def pull(dry=False):
    m = manifest()
    if not m["files"]:
        sys.exit(f"keys pull: no canonical manifest {MANIFEST} in {os.environ['VAULT_STORE']} (run keys push on a set-up machine first)")
    stamp = time.strftime("%Y%m%dT%H%M%S")
    bk = os.path.join(HOME, ".keys-backup", stamp)
    n_new = n_upd = n_same = n_mode = 0
    for e in sorted(m["files"], key=lambda e: e["dest"]):
        dest, p = e["dest"], os.path.join(HOME, real(e["dest"]))
        cur = lread(dest)
        want = None
        if e["kind"] in UNION:
            want = union(vault().read(e["path"], max_bytes=1 << 28), cur)  # canonical lines first, local extras kept
            if cur is not None and set(want.splitlines()) == set(cur.splitlines()): want = None
        elif cur is None or sha(cur) != e["sha256"]:
            want = vault().read(e["path"], max_bytes=1 << 28)
            if sha(want) != e["sha256"]:
                sys.exit(f"keys: vault bytes for {dest} do not match the manifest sha; refusing (re-run keys push on the source machine)")
        mode = int(e["mode"], 8)
        if want is None:
            if stat.S_IMODE(os.stat(p).st_mode) != mode:
                n_mode += 1
                if not dry: os.chmod(p, mode)
            n_same += 1; continue
        print(("would " if dry else "") + ("update " if cur is not None else "restore ") + f"~/{real(dest)}")
        if dry: continue
        _mkdirs(os.path.dirname(p))
        if cur is not None:
            b = os.path.join(bk, real(dest)); os.makedirs(os.path.dirname(b), mode=0o700, exist_ok=True)
            shutil.copy2(p, b); os.chmod(b, 0o600); n_upd += 1
        else:
            n_new += 1
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix=".keys-")
        with os.fdopen(fd, "wb") as f: f.write(want)
        os.chmod(tmp, mode); os.replace(tmp, p)
    print(f"pull: {n_new} restored, {n_upd} updated, {n_same} already identical ({n_mode} mode fixes)"
          + (f"; previous versions in {bk}" if os.path.isdir(bk) else ""))


# ------------------------------------------------------------------------------------------------ push / add / forget
def _save(m, uploads):
    m["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    m["by"] = f"{os.environ.get('USER', '?')}@{socket.gethostname()}"
    m["files"].sort(key=lambda e: e["dest"])
    items = [(path, data, {"vault": True, "keys": True}) for path, data in uploads]
    items.append((MANIFEST, json.dumps(m, indent=1, sort_keys=True).encode(), {"vault": True, "keys": True}))
    vault().put(items)


def push(extra=()):
    m = manifest(); E = entries(m)
    todo = sorted(set(E) | set(auto_tracked()) | set(extra))
    uploads, changed = [], []
    for dest in todo:
        b = lread(dest)
        if b is None: continue  # missing here: keep canonical as is (a machine without a file never deletes it)
        e = E.get(dest)
        k = kind_of(dest)
        mode = default_mode(dest, stat.S_IMODE(os.stat(os.path.join(HOME, real(dest))).st_mode))
        h = sha(b)
        if e and e["sha256"] == h and e["mode"] == f"{mode:o}": continue
        ne = {"dest": dest, "path": PFX + dest, "mode": f"{mode:o}", "sha256": h, "size": len(b), "kind": k}
        if e: e.update(ne)
        else: m["files"].append(ne)
        uploads.append((ne["path"], b)); changed.append(dest)
    if not changed:
        print("push: canonical already up to date"); return
    _save(m, uploads)
    print(f"push: {len(changed)} file(s) -> {os.environ['VAULT_STORE']} {PFX}:\n  " + "\n  ".join(changed))


def add(paths):
    dests = []
    for p in paths:
        ap = os.path.abspath(os.path.expanduser(p))
        if not ap.startswith(HOME + os.sep) or not os.path.isfile(ap):
            sys.exit(f"keys add: {p} is not a file under $HOME")
        dests.append(logical(os.path.relpath(ap, HOME)))
    push(extra=dests)


def forget(dests):
    m = manifest()
    ds = {d[2:] if d.startswith("~/") else d for d in dests}
    before = len(m["files"]); m["files"] = [e for e in m["files"] if e["dest"] not in ds]
    _save(m, []); print(f"forget: {before - len(m['files'])} entr(ies) removed from the manifest")


# ------------------------------------------------------------------------------------------------ ssh helpers
def ssh_cfg():
    return os.path.join(HOME, ".ssh", "config")


def aliases(cfg=None):
    hs = []
    for line in open(cfg or ssh_cfg()):
        w = line.split("#", 1)[0].split()
        if w and w[0].lower() == "host":
            hs += [h for h in w[1:] if not any(c in h for c in "*?!") and h not in hs]
    return hs


def ssh_G(alias):
    r = subprocess.run(["ssh", "-G", "-F", ssh_cfg(), alias], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    o = {}
    for line in r.stdout.splitlines():
        k, _, v = line.partition(" ")
        o.setdefault(k, []).append(v)
    return o


def _pw_home():
    import pwd
    return pwd.getpwuid(os.getuid()).pw_dir


def _expand(p):
    """ssh expands ~ to the passwd home; map it to $HOME so a relocated HOME (tests) checks its own files."""
    if p.startswith("~/"): return os.path.join(HOME, p[2:])
    pw = _pw_home()
    if pw != HOME and p.startswith(pw + "/"): return HOME + p[len(pw):]
    return p


def ssh_base():
    kh = os.path.join(HOME, ".ssh", "known_hosts")
    return ["ssh", "-F", ssh_cfg(), "-n", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ForwardAgent=no",
            "-o", "IdentityAgent=none", "-o", "ClearAllForwardings=yes", "-o", "RequestTTY=no",
            "-o", f"UserKnownHostsFile={kh}", "-o", "StrictHostKeyChecking=accept-new", "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=2"]


def classify(rc, err):
    e = err.lower()
    if rc == 0: return "alive"
    if "permission denied" in e or "too many authentication failures" in e: return "auth-failed"
    if "host key verification failed" in e or "remote host identification has changed" in e: return "hostkey-changed"
    if "could not resolve" in e: return "dns-failed"
    if "timed out" in e or "timeout" in e: return "unreachable"
    if "no route" in e or "connection refused" in e or "network is unreachable" in e or "connection closed" in e \
            or "connection reset" in e or "kex_exchange" in e or "closed by remote" in e: return "unreachable"
    if "targetnotconnected" in e or "start-session" in e or "ssm" in e or "token" in e: return "unreachable"
    return "error"


def probe(alias, cmd="exit", timeout=30):
    """One read-only ssh run. Returns dict(status, accepted_key, rc, out, err_tail)."""
    try:
        r = subprocess.run(ssh_base() + ["-v", alias, cmd], capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
        out, err, rc = r.stdout.decode(errors="replace"), r.stderr.decode(errors="replace"), r.returncode
    except subprocess.TimeoutExpired:
        return {"status": "unreachable", "detail": f"timeout {timeout}s", "rc": None, "out": "", "accepted_key": None}
    acc = re.findall(r"Server accepts key: (\S+)", err)
    tail = [l for l in err.splitlines() if not l.startswith("debug") and "Pseudo-terminal" not in l and l.strip()]
    st = classify(rc, "\n".join(tail) if rc != 0 else "")
    if rc != 0 and acc and st == "error":  # authenticated, but the probe command itself failed (e.g. cmd.exe): still alive
        st = "alive"
    if rc == 255 and st == "error":
        st = classify(1, err)
    return {"status": st, "detail": (tail[-1][:160] if tail else "") if st != "alive" else "", "rc": rc, "out": out,
            "accepted_key": os.path.basename(acc[-1]) if acc else None}


# ------------------------------------------------------------------------------------------------ doctor
def route(alias, G=None):
    G = G or ssh_G(alias)
    via = (G.get("proxyjump") or [""])[0]
    pc = (G.get("proxycommand") or [""])[0]
    if pc and pc != "none":
        via = "ssm" if "ssm" in pc else "proxycommand"
    keys = [os.path.relpath(_expand(k), HOME) if _expand(k).startswith(HOME) else k for k in G.get("identityfile", [])]
    return {"hostname": (G.get("hostname") or [alias])[0], "user": (G.get("user") or [""])[0],
            "port": (G.get("port") or ["22"])[0], "via": via if via != "none" else "", "keys": keys}


def doctor(only=()):
    al = list(only) or aliases()
    have_main = os.path.exists(os.path.join(HOME, MAIN_KEY))
    with ThreadPoolExecutor(8) as ex:
        routes = dict(zip(al, ex.map(route, al)))
    with ThreadPoolExecutor(24) as ex:
        res = dict(zip(al, ex.map(lambda a: probe(a), al)))
    print(f"{'alias':22} {'user@host':34} {'via':12} {'keys (+ present / ! missing)':44} {'status':15} accepted")
    lack, missing_keys = [], set()
    for a in al:
        r, p = routes[a], res[a]
        ks = []
        for k in r["keys"]:
            ok = os.path.exists(os.path.join(HOME, k)) if not k.startswith("/") else os.path.exists(k)
            ks.append(("+" if ok else "!") + k.replace(".ssh/", ""))
            if not ok: missing_keys.add(k)
        uh = f"{r['user']}@{r['hostname']}" + (f":{r['port']}" if r["port"] != "22" else "")
        acc = p["accepted_key"] or ""
        main = acc == os.path.basename(MAIN_KEY)
        if p["status"] == "alive" and not main: lack.append(a)
        print(f"{a:22} {uh[:34]:34} {r['via'][:12]:12} {' '.join(ks)[:44]:44} {p['status']:15} {acc}"
              + ("" if p["status"] == "alive" else f"  {p['detail'][:60]}"))
    alive = sum(1 for p in res.values() if p["status"] == "alive")
    print(f"\n{alive}/{len(al)} reachable from {socket.gethostname()}; universal key {MAIN_KEY}: {'present' if have_main else 'MISSING (keys pull)'}")
    if missing_keys: print("key files referenced but missing locally: " + " ".join(sorted(missing_keys)))
    if lack: print("reachable but NOT via id_ed25519_main (add its .pub there, then re-run): " + " ".join(lack))
    return res, routes


# ------------------------------------------------------------------------------------------------ box registry
LINUX_PROBE = r"""uname -srm; . /etc/os-release 2>/dev/null && echo "os=$PRETTY_NAME"; [ "$(uname -s)" = Darwin ] && echo "os=macOS $(sw_vers -productVersion)";
echo "cpu=$(grep -m1 'model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2 | sed 's/^ *//')$(sysctl -n machdep.cpu.brand_string 2>/dev/null)";
echo "ncpu=$(nproc 2>/dev/null || sysctl -n hw.ncpu)"; echo "mem_gb=$(awk '/MemTotal/{printf "%.0f", $2/1048576}' /proc/meminfo 2>/dev/null)";
command -v nvidia-smi >/dev/null && nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | sed 's/^/gpu=/';
if command -v amd-smi >/dev/null; then amd-smi static --asic 2>/dev/null | grep -i 'MARKET_NAME' | head -16 | sed 's/^[^:]*: */rocm=/';
elif command -v rocm-smi >/dev/null; then rocm-smi --showproductname 2>/dev/null | grep -i 'card series' | head -16 | sed 's/^.*Series:[[:space:]]*/rocm=/'; fi;
command -v lspci >/dev/null && lspci 2>/dev/null | grep -iE 'vga|3d controller|display|processing accelerators' | head -16 | sed 's/^/pci=/'; true"""
WIN_PROBE = r"""$ErrorActionPreference='SilentlyContinue'; $o=Get-CimInstance Win32_OperatingSystem; 'os='+$o.Caption+' '+$o.Version;
'cpu='+((Get-CimInstance Win32_Processor | Select-Object -First 1).Name); 'ncpu='+[Environment]::ProcessorCount;
'mem_gb='+[math]::Round($o.TotalVisibleMemorySize/1MB);
Get-CimInstance Win32_VideoController | ForEach-Object { 'gpu='+$_.Name+' | status='+$_.Status+' | err='+$_.ConfigManagerErrorCode+' | driver='+$_.DriverVersion }"""


def _facts(alias):
    p = probe(alias, "uname -s")
    if p["status"] != "alive": return p, {}
    facts = {}
    if p["rc"] == 0 and p["out"].strip():
        q = probe(alias, LINUX_PROBE, timeout=40); facts["shell"] = "bash"
    else:  # cmd.exe / powershell remote shell: 'uname' is not recognized
        enc = base64.b64encode(WIN_PROBE.encode("utf-16-le")).decode()
        q = probe(alias, f"powershell -NoProfile -NonInteractive -EncodedCommand {enc}", timeout=60); facts["shell"] = "cmd (use powershell)"
    lines = q["out"].splitlines()
    if not any("=" in l for l in lines):
        facts["shell"] = "restricted (no command shell; jump/git host)"
    gpus = []
    for l in lines:
        if l.startswith(("gpu=", "rocm=")) and l.split("=", 1)[1].strip():
            g = l.split("=", 1)[1].strip()
            if " | status=" in g:  # windows: name | status=OK | err=0 | driver=X  ->  name (driver X)  or  name [ERROR code N]
                f = dict(x.split("=", 1) for x in g.split(" | ")[1:] if "=" in x)
                g = g.split(" | ")[0] + (f" (driver {f.get('driver')})" if f.get("err", "0") in ("0", "") else
                                         f" [DEVICE ERROR code {f.get('err')}, status {f.get('status')}, driver {f.get('driver')}]")
            gpus.append(g)
    if not gpus:
        gpus = [re.sub(r"^\S+\s+", "", l.split("=", 1)[1]).strip() for l in lines if l.startswith("pci=")]
    for k in ("os", "cpu", "ncpu", "mem_gb"):
        v = next((l.split("=", 1)[1].strip() for l in lines if l.startswith(k + "=")), "")
        if v: facts[k] = int(v) if k in ("ncpu", "mem_gb") and v.isdigit() else v
    if not facts.get("os") and lines: facts["os"] = lines[0].strip()
    if gpus:
        from collections import Counter
        facts["gpu"] = [f"{n}x {g}" if n > 1 else g for g, n in Counter(gpus).items()]
    else:
        facts["gpu"] = []
    return p, facts


def registry_load(prefer_local=False):
    """The published registry; with prefer_local, an unpublished local copy that is newer wins (probe ALIAS... merges)."""
    b = read_or_none(_store("TRACEDB_STORE"), REGISTRY)
    reg = json.loads(b) if b is not None else {"version": 1, "boxes": {}}
    if prefer_local and os.path.exists(reg_path()):
        loc = json.load(open(reg_path()))
        if loc.get("updated", "") > reg.get("updated", ""):
            return loc
    return reg


def reg_path():
    return os.path.join(HOME, ".cache", "tracedb", "boxes.json")


def boxes_probe(only=(), publish=False):
    reg = registry_load(prefer_local=True)
    B = reg.setdefault("boxes", {})
    al = list(only) or aliases()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    host = socket.gethostname()
    with ThreadPoolExecutor(16) as ex:
        rs = dict(zip(al, ex.map(lambda a: (route(a), *_facts(a)), al)))
    for a in al:
        r, p, facts = rs[a]
        b = B.setdefault(a, {"alias": a})
        b.update({"hostname": r["hostname"], "user": r["user"], "port": int(r["port"]) if r["port"].isdigit() else r["port"],
                  "jump": r["via"] or None, "keys": [k.replace(".ssh/", "") for k in r["keys"]],
                  "status": p["status"], "status_detail": p["detail"] or None, "last_verified": now, "verified_from": host,
                  "accepted_key": p["accepted_key"]})
        b["has_main_key"] = (p["accepted_key"] == os.path.basename(MAIN_KEY)) if p["status"] == "alive" else b.get("has_main_key")
        if p["status"] == "alive":
            b["last_alive"] = now
            for k, v in facts.items():
                if v or k not in b: b[k] = v
            if facts.get("shell", "").startswith("restricted"):
                for k in ("os", "cpu", "ncpu", "mem_gb", "gpu"): b.pop(k, None)
    reg["updated"] = now
    os.makedirs(os.path.dirname(reg_path()), exist_ok=True)
    json.dump(reg, open(reg_path(), "w"), indent=1, sort_keys=True)
    boxes_show(reg)
    print(f"registry written to {reg_path()}" + ("" if publish else "  (keys boxes publish  uploads it)"))
    if publish: boxes_publish(reg_path())


def boxes_publish(path):
    reg = json.load(open(path))
    for b in reg.get("boxes", {}).values():  # guard: the registry is plain metadata, never key material
        blob = json.dumps(b)
        if "PRIVATE KEY" in blob or re.search(r"(?i)(token|secret|password)\s*[:=]", blob):
            sys.exit(f"keys boxes publish: refusing, {b.get('alias')} looks like it contains a secret")
    _store("TRACEDB_STORE").put([(REGISTRY, json.dumps(reg, indent=1, sort_keys=True).encode(), {"registry": "boxes"})])
    print(f"published {len(reg.get('boxes', {}))} boxes -> {os.environ['TRACEDB_STORE']} {REGISTRY}")


def boxes_show(reg=None):
    reg = reg or registry_load()
    B = reg.get("boxes", {})
    print(f"{'alias':20} {'status':13} {'provider':16} {'gpu':40} {'os':24} {'shell':6} main")
    for a in sorted(B, key=lambda a: (B[a].get("status") != "alive", a)):
        b = B[a]
        g = "; ".join(b.get("gpu") or [])
        main = {True: "yes", False: "NO", None: "?"}[b.get("has_main_key")]
        print(f"{a:20} {b.get('status', '?'):13} {(b.get('provider') or '')[:16]:16} {g[:40]:40} {(b.get('os') or '')[:24]:24} "
              f"{(b.get('shell') or '')[:6]:6} {main}")
    c = {}
    for b in B.values(): c[b.get("status", "?")] = c.get(b.get("status", "?"), 0) + 1
    print("  " + ", ".join(f"{k}: {v}" for k, v in sorted(c.items())) + f"   (registry updated {reg.get('updated', '?')})")


# ------------------------------------------------------------------------------------------------ install (remote write)
WIN_ADD = r"""$k = '__PUB__'; $out = @()
$files = @("$env:USERPROFILE\.ssh\authorized_keys")
$admin = (New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($admin) { $files += 'C:\ProgramData\ssh\administrators_authorized_keys' }
foreach ($f in $files) {
  if (-not (Test-Path $f)) { if ($f -like '*ProgramData*') { continue }; New-Item -ItemType Directory -Force (Split-Path $f) | Out-Null; New-Item -ItemType File $f | Out-Null }
  if ((Get-Content $f) -contains $k) { $out += 'already' } else {
    $t = [IO.File]::ReadAllText($f); if ($t.Length -gt 0 -and -not $t.EndsWith([string][char]10)) { [IO.File]::AppendAllText($f, "`r`n") }
    [IO.File]::AppendAllText($f, $k + "`r`n"); $out += 'added' } }
'win:' + ($out -join ',')"""


def install(targets):
    """Add the universal public key to each HOST's authorized_keys (idempotent), then VERIFY by logging in with it.
    Linux/macOS: ~/.ssh/authorized_keys. Windows OpenSSH (cmd remote shell): %USERPROFILE%\.ssh\authorized_keys and, for
    administrators, C:\ProgramData\ssh\administrators_authorized_keys (which file sshd reads depends on its sshd_config).
    A host only counts as done when a login offering the universal key FIRST is accepted with it; restricted shells
    (jump/git hosts whose account has no command shell) report as 'not accepted' instead of a false OK."""
    pub = open(os.path.join(HOME, MAIN_KEY + ".pub")).read().strip()
    keyname = os.path.basename(MAIN_KEY)
    if targets == ["all"]: targets = aliases()
    unix = (f"umask 077; mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && "
            f"(grep -qxF '{pub}' ~/.ssh/authorized_keys && echo already || (echo '{pub}' >> ~/.ssh/authorized_keys && echo added))")
    win = "powershell -NoProfile -NonInteractive -EncodedCommand " + base64.b64encode(WIN_ADD.replace("__PUB__", pub).encode("utf-16-le")).decode()

    def one(h):
        p = probe(h, "uname -s")
        if p["status"] != "alive": return h, False, f"unreachable: {p.get('detail', '')[:70]}"
        if keyname in (p.get("accepted_key") or ""): return h, True, "already accepted"
        is_unix = p["rc"] == 0 and p["out"].strip() != ""
        q = probe(h, unix if is_unix else win, timeout=60)
        did = (q["out"].strip().splitlines() or ["(no output: restricted shell?)"])[-1][:40]
        v = probe(h, "exit")
        if keyname in (v.get("accepted_key") or ""): return h, True, f"{did}; verified"
        return h, False, f"{did}; NOT accepted with {keyname} (restricted shell or sshd reads another AuthorizedKeysFile)"
    with ThreadPoolExecutor(16) as ex:
        res = list(ex.map(one, targets))
    for h, ok, msg in sorted(res, key=lambda x: (not x[1], x[0])):
        print(f"{'OK  ' if ok else 'FAIL'}  {h}: {msg}")
    print(f"{sum(r[1] for r in res)}/{len(res)} hosts accept {keyname}")
    return 0 if all(r[1] for r in res) else 2


def main(a):
    cmd = a[0] if a else "status"
    if cmd == "status": return status()
    if cmd == "list": return list_()
    if cmd == "pull": return pull(dry="--dry-run" in a)
    if cmd == "push": return push()
    if cmd == "add" and a[1:]: return add(a[1:])
    if cmd == "forget" and a[1:]: return forget(a[1:])
    if cmd == "doctor": doctor(a[1:]); return 0
    if cmd == "boxes":
        sub = a[1] if a[1:] else "show"
        rest = [x for x in a[2:] if not x.startswith("--")]
        if sub == "probe": return boxes_probe(rest, publish="--publish" in a)
        if sub == "show": return boxes_show()
        if sub == "publish": return boxes_publish(rest[0] if rest else reg_path())
        if sub == "path": print(reg_path()); return 0
    if cmd == "install" and a[1:]: return install(a[1:])
    print(__doc__); return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) or 0)
