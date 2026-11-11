#!/usr/bin/env python3
"""Bulk, filtered, secret-redacting ingest into a tracedb store.

usage: ingest.py [--dry] [--maxfile MB] SRC=PREFIX [SRC=PREFIX ...]
Files are read, secrets redacted in-memory, and put in ~256 MiB batches (Store.put holds a batch in RAM).
Everything skipped or redacted is logged to logs/ingest-skips.jsonl so nothing silently vanishes.
Never uploads: private keys, auth/credential files, .env, weights/checkpoints/binaries (see SKIP_*).
"""
import json, os, re, sys, time
sys.path.insert(0, os.path.expanduser("~/tracedb-work/traceDB"))
from tracedb.store import Store  # noqa
from tracedb.cli import sniff, open_store  # noqa

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".cache", ".next",
             "target", ".gradle", "site-packages", ".tox", "dist-packages", ".ruff_cache", "Pods", ".terraform"}
SKIP_EXT = {".safetensors", ".pt", ".pth", ".bin", ".gguf", ".ckpt", ".npy", ".npz", ".onnx", ".so", ".dylib", ".whl",
            ".iso", ".o", ".a", ".pyc", ".mp4", ".mov", ".mkv", ".wav", ".mp3", ".zip", ".tar", ".gz", ".tgz", ".zst",
            ".pem", ".key", ".p12", ".pfx", ".keystore", ".sqlite-wal", ".sqlite-shm", ".db-wal", ".db-shm", ".nsys-rep",
            ".ncu-rep", ".parquet", ".arrow", ".h5", ".pkl", ".pickle", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".pdf"}
SKIP_NAMES = {"auth.json", ".env", "credentials", "credentials.json", "id_rsa", "id_ed25519", "known_hosts", ".netrc",
              ".DS_Store", "slam-gcp-sa.json", "installation_id", "api_key", "vast_api_key"}
SKIP_PAT = re.compile(r"(^|/)(id_(rsa|ed25519|ecdsa)[^/]*|\.env[^/]*|[^/]*\.pem|[^/]*secret[^/]*key[^/]*|[^/]*_key|[^/]*token[^/]*\.json)$", re.I)
# keep the .pub of ssh keys (public) but not private; handled: id_* without .pub skipped, .pub allowed
SECRETS = [
    (re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), b"[REDACTED:PRIVATE_KEY]"),
    (re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----[A-Za-z0-9+/=\s]{40,}"), b"[REDACTED:PRIVATE_KEY]"),
    (re.compile(rb"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), b"[REDACTED:AWS_KEY_ID]"),
    (re.compile(rb"(?i)(aws_secret_access_key|secret_access_key|SecretAccessKey)([\"'\s:=]+)[A-Za-z0-9/+=]{30,}"), rb"\1\2[REDACTED:AWS_SECRET]"),
    (re.compile(rb"(?i)(aws_session_token|SessionToken)([\"'\s:=]+)[A-Za-z0-9/+=]{80,}"), rb"\1\2[REDACTED:AWS_TOKEN]"),
    (re.compile(rb"\bsk-(ant-|proj-|or-v1-)?[A-Za-z0-9_-]{20,}"), b"[REDACTED:API_KEY]"),
    (re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), b"[REDACTED:GH_TOKEN]"),
    (re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{40,}\b"), b"[REDACTED:GH_TOKEN]"),
    (re.compile(rb"\bhf_[A-Za-z0-9]{30,}\b"), b"[REDACTED:HF_TOKEN]"),
    (re.compile(rb"\bxox[abprs]-[A-Za-z0-9-]{20,}"), b"[REDACTED:SLACK_TOKEN]"),
    (re.compile(rb"\bAIza[0-9A-Za-z_-]{35}\b"), b"[REDACTED:GOOGLE_KEY]"),
    (re.compile(rb"\beyJ[A-Za-z0-9_-]{15,}\.eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}"), b"[REDACTED:JWT]"),
    (re.compile(rb"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{30,}"), rb"\1[REDACTED:BEARER]"),
    (re.compile(rb"(?i)((?:api[_-]?key|apikey|secret|passwd|password|token)[\"']?\s*[:=]\s*[\"']?)[A-Za-z0-9_\-./+=]{24,}"), rb"\1[REDACTED:CRED]"),
]
TOOL_BIN = ("/links/conductor-app/bin/", "/claude-desktop/claude-code", "/Contents/MacOS/", "/.local/share/mise/")
BATCH = 256 << 20


def redact(data: bytes):
    hits = 0
    for rx, rep in SECRETS:
        data, n = rx.subn(rep, data)
        hits += n
    return data, hits


def walk(src):
    if os.path.isfile(src):
        yield os.path.dirname(src), os.path.basename(src)
        return
    for dp, dns, fns in os.walk(src):
        dns[:] = [d for d in dns if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        for f in fns:
            yield dp, f


def main():
    args = sys.argv[1:]
    dry = "--dry" in args
    maxfile = 128
    if "--maxfile" in args:
        i = args.index("--maxfile"); maxfile = int(args[i + 1]); del args[i:i + 2]
    args = [a for a in args if a != "--dry"]
    st = None if dry else open_store(os.environ["TRACEDB_STORE"])
    skiplog = open(os.path.expanduser("~/tracedb-work/logs/ingest-skips.jsonl"), "a")
    for spec in args:
        src, prefix = spec.split("=", 1)
        src = os.path.expanduser(src)
        t0 = time.time(); nfile = nbytes = nskip = nred = 0
        batch, bsz = [], 0
        flushed = {"added": 0, "bytes": 0}

        def flush():
            nonlocal batch, bsz
            if batch and st:
                r = st.put(batch)
                flushed["added"] += r.get("added", 0); flushed["bytes"] += r.get("bytes", 0)
            batch, bsz = [], 0

        base = src if os.path.isdir(src) else os.path.dirname(src)
        for dp, f in walk(src):
            full = os.path.join(dp, f)
            rel = os.path.relpath(full, base)
            ext = os.path.splitext(f)[1].lower()
            reason = None
            if os.path.islink(full):
                reason = "symlink"
            elif f in SKIP_NAMES or SKIP_PAT.search(full) and not f.endswith(".pub"):
                reason = "secret-name"
            elif ext in SKIP_EXT:
                reason = "binary-ext"
            elif any(n in full for n in TOOL_BIN):
                reason = "tool-binary"
            if not reason:
                try:
                    sz = os.path.getsize(full)
                except OSError:
                    reason = "stat-error"; sz = 0
                if not reason and sz > maxfile << 20:
                    reason = f"gt{maxfile}MB"
            if reason:
                nskip += 1
                skiplog.write(json.dumps({"src": src, "path": rel, "reason": reason}) + "\n")
                continue
            if dry:
                nfile += 1; nbytes += sz; continue
            try:
                with open(full, "rb") as fh:
                    data = fh.read()
            except OSError as e:
                skiplog.write(json.dumps({"src": src, "path": rel, "reason": f"read-error {e}"}) + "\n"); nskip += 1
                continue
            if b"\0" not in data[:4096] or ext in (".jsonl", ".json", ".md", ".txt", ".log"):
                data, hits = redact(data)
                if hits:
                    nred += 1
                    skiplog.write(json.dumps({"src": src, "path": rel, "reason": "redacted", "hits": hits}) + "\n")
            meta = {"src": os.path.basename(src.rstrip("/")), "mtime": int(os.path.getmtime(full))}
            batch.append((prefix + rel, data, meta)); bsz += len(data)
            nfile += 1; nbytes += len(data)
            if bsz >= BATCH:
                flush()
        flush()
        skiplog.flush()
        print(json.dumps({"src": src, "prefix": prefix, "files": nfile, "bytes": nbytes, "skipped": nskip,
                          "redacted_files": nred, "put": flushed, "secs": round(time.time() - t0, 1)}), flush=True)


if __name__ == "__main__":
    main()
