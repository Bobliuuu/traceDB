#!/usr/bin/env python3
"""box_inbox.py -- sync-box side of the Mac -> box push. The Mac rsyncs into <inbox>/in/<store-prefix>/... (rrsync-restricted key).
  stage   : diff <inbox>/in against state/inbox-state.json, hardlink changed files into $W/stage-in/<rel>, record sha256 (at stage
            time, so a later edit can never be acked by mistake), print one `SRC=PREFIX` token per top-level source dir.
  commit  : after ingest+vault+blobs all succeeded: append `sha256<TAB>rel` to <inbox>/ack/manifest.tsv, advance the state, drop the stage.
  abort   : drop the stage, state unchanged (next run retries).
rel = path under in/ = the store path (e.g. traces/claude-code/<project>/<session>.jsonl). Source dirs are 2 levels: <kind>/<name>/.
Env: TRACEDB_WORK (default ~/tracedb-work), INBOX (default /srv/tracedb-inbox/mac)."""
import hashlib, json, os, shutil, sys

W = os.environ.get("TRACEDB_WORK", os.path.expanduser("~/tracedb-work"))
INBOX = os.environ.get("INBOX", "/srv/tracedb-inbox/mac")
IN, ACK = os.path.join(INBOX, "in"), os.path.join(INBOX, "ack", "manifest.tsv")
STATE, STAGE, PEND = (os.path.join(W, "state", "inbox-state.json"), os.path.join(W, "stage-in"), os.path.join(W, "state", "inbox-pending.json"))
# files that must never reach the stores via the inbox (the vault tooling handles credentials separately)
SKIP_SUFFIX = (".partial", ".tmp")


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def stage():
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    shutil.rmtree(STAGE, ignore_errors=True)
    pend, srcs = {}, set()
    for dp, dn, fs in os.walk(IN):
        dn[:] = [d for d in dn if not d.startswith(".~tmp~") and d != ".rsync-partial"]
        for f in fs:
            if f.startswith(".") and f.count(".") > 2 or f.endswith(SKIP_SUFFIX):
                continue  # rsync temp files
            full = os.path.join(dp, f); rel = os.path.relpath(full, IN)
            try:
                st = os.stat(full)
            except OSError:
                continue
            sig = [st.st_size, int(st.st_mtime_ns)]
            if state.get(rel) == sig:
                continue
            parts = rel.split("/")
            if len(parts) < 3:
                continue  # need <kind>/<name>/file
            dst = os.path.join(STAGE, rel); os.makedirs(os.path.dirname(dst), exist_ok=True)
            try:
                shutil.copy2(full, dst)  # a private snapshot (protected_hardlinks forbids linking another user's file)
                pend[rel] = [sig, sha256(dst)]
            except OSError as e:
                print(f"stage failed: {type(e).__name__} for one file", file=sys.stderr); continue
            srcs.add("/".join(parts[:2]))
    json.dump(pend, open(PEND, "w"))
    for s in sorted(srcs):
        print(f"{STAGE}/{s}={s}/")
    print(f"staged {len(pend)} files in {len(srcs)} sources", file=sys.stderr)


def confirmer():
    """True only if the ORIGINAL bytes (sha256) are really in a store: main entry, main blob, or vault entry at that path."""
    from tracedb.store import open_store
    from tracedb.blob import blob_info
    stores = [open_store(os.environ["TRACEDB_STORE"]), open_store(os.environ["VAULT_STORE"])]
    for s in stores:
        s.sync()

    def stored(rel, sh):
        for s in stores:
            e = s.locate(rel)
            if e and (e["sha"] == sh or (e.get("meta") or {}).get("blob") == sh and blob_info(s, rel)):
                return True
        return False
    return stored


def commit():
    if not os.path.exists(PEND):
        return
    pend = json.load(open(PEND)); state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    stored = confirmer()
    os.makedirs(os.path.dirname(ACK), exist_ok=True)
    n = 0
    with open(ACK, "a") as a:
        for rel, (sig, sh) in pend.items():
            if stored(rel, sh):
                a.write(f"{sh}\t{rel}\n"); n += 1
            state[rel] = sig  # unconfirmed (skipped by ingest rules): not acked, so the Mac never deletes it
    json.dump(state, open(STATE + ".tmp", "w")); os.replace(STATE + ".tmp", STATE)
    os.remove(PEND); shutil.rmtree(STAGE, ignore_errors=True)
    print(f"acked {n} of {len(pend)} files (sha256 confirmed in a store)", file=sys.stderr)


def abort():
    shutil.rmtree(STAGE, ignore_errors=True)
    if os.path.exists(PEND): os.remove(PEND)


if __name__ == "__main__":
    {"stage": stage, "commit": commit, "abort": abort}[sys.argv[1]]()
