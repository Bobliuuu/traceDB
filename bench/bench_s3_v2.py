"""Real-S3 re-validation of append/GC/integrity/touch + core. usage: SRC BUCKET PREFIX PROFILE. Cleans up its own prefix."""
import json, os, random, shutil, sys, threading, time, uuid, hashlib
import tracedb.store as S
from tracedb import Store, S3Backend
from tracedb.backend import CASConflict
from tracedb.cli import sniff
SRC, BUCKET, PFX, PROFILE = sys.argv[1:5]
run = f"{PFX.strip('/')}/{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:6]}"
cache = os.path.expanduser("~/tracedb-work/bench-out/s3cache3"); shutil.rmtree(cache, ignore_errors=True)
mk = lambda n: Store(S3Backend(BUCKET, run, profile=PROFILE), cache_dir=cache, name=n)
P = lambda *a: print(*a, flush=True)
files = []
for d in ["logs", "research", "experiments", "data", "docs/archives", "reports", "bugs", "evals"]:
    for dp, _, fs in os.walk(os.path.join(SRC, d)):
        for f in fs:
            full = os.path.join(dp, f)
            if os.path.isfile(full) and not os.path.islink(full): files.append((os.path.relpath(full, SRC), full, sniff(full, {})))
P(f"RUN s3://<bucket>/<bench-prefix>/ files={len(files)} (client = laptop, not in-region)")
packbytes = lambda be: sum(sz for k, sz in be.list("packs/"))
try:
    st = mk("w"); be = st.b
    assert be.put_if_absent("probe", b"a") and not be.put_if_absent("probe", b"b"); P("COND create-only PUT ok")
    e1 = be.cas_head(b'{"segments":[]}', None); be.cas_head(b'{"segments":[],"x":1}', e1)
    try: be.cas_head(b'{"segments":[],"x":2}', e1); P("COND stale CAS NOT REFUSED (BAD)")
    except CASConflict: P("COND stale CAS refused ok")
    t = time.time(); r = st.put(iter(files)); dt = time.time() - t
    P(f"INGEST secs={dt:.1f} packs={r['packs']} wire={r['bytes']/2**20:.0f}MiB")
    cl = mk("c"); t = time.time(); cl.sync(); P(f"COLD_SYNC entries={cl.count()} secs={time.time()-t:.2f}")
    t = time.perf_counter(); cl.sync(); P(f"WARM_SYNC ms={(time.perf_counter()-t)*1e3:.0f}")
    # ---- APPEND on real S3: 20 MiB jsonl-ish session, then 3 appends of 100 KiB
    rnd = random.Random(7); line = lambda i: (json.dumps({"i": i, "t": rnd.random(), "m": "x" * 60}) + "\n").encode()
    base = b"".join(line(i) for i in range(200000)); sess = "sessions/s1.jsonl"
    t = time.time(); st.put([(sess, base, None)]); P(f"APPEND base put {len(base)/2**20:.1f}MiB secs={time.time()-t:.2f}")
    cur = base
    for k in range(3):
        add = b"".join(line(10**6 + k * 2000 + i) for i in range(1000)); cur += add
        b0 = packbytes(be); t = time.time(); st.put([(sess, cur, None)]); dt = time.time() - t
        P(f"APPEND +{len(add)/1024:.0f}KiB: packs grew {(packbytes(be)-b0)/1024:.0f}KiB secs={dt:.2f}")
    c2 = mk("c2"); c2.sync()
    t = time.perf_counter(); tail = c2.tail(sess, 4096); P(f"APPEND cold tail-4KiB read ms={(time.perf_counter()-t)*1e3:.0f} correct={tail==cur[-4096:]}")
    t = time.perf_counter(); whole = c2.read(sess, max_bytes=-1); P(f"APPEND whole read {len(cur)/2**20:.1f}MiB ms={(time.perf_counter()-t)*1e3:.0f} correct={whole==cur} (sha-checked)")
    # ---- INTEGRITY: corrupt a real pack object on S3 copy? (don't touch live data): corrupt via a separate tiny store
    ci = Store(S3Backend(BUCKET, run + "/corrupt", profile=PROFILE), cache_dir=cache, name="ci")
    ci.put([("v", os.urandom(300000), None)]); pk = ci.locate("v")["pack"]
    blob = bytearray(ci.b.get(f"packs/{pk}.pack")); blob[1000] ^= 0xFF
    ci.b.c.put_object(Bucket=BUCKET, Key=ci.b._k(f"packs/{pk}.pack"), Body=bytes(blob))
    shutil.rmtree(os.path.join(ci.dir, "blocks"), ignore_errors=True); os.makedirs(os.path.join(ci.dir, "blocks"))
    try: ci.read("v", max_bytes=-1); P("INTEGRITY corrupted pack read: NOT DETECTED (BAD)")
    except IOError as e: P(f"INTEGRITY corrupted pack read raises {type(e).__name__} ok")
    P("INTEGRITY deep verify flags it:", ci.verify(deep=True)["ok"] is False)
    # ---- GC + touch on real S3
    orphan = "packs/" + "ab" * 32 + ".pack"; be.put_if_absent(orphan, b"junk" * 100); be.put_if_absent("segments/" + "cd" * 32 + ".seg", b"junk")
    live = next(k for k, _ in be.list("packs/") if k != orphan)
    m0 = dict((k, m) for k, _, m in be.list_mtime("packs/"))[live]; time.sleep(2); be.touch(live)
    m1 = dict((k, m) for k, _, m in be.list_mtime("packs/"))[live]; P(f"TOUCH real S3: mtime advanced={m1 > m0} object_intact={be.size(live) is not None}")
    r = st.gc(grace_seconds=3600, dry_run=False); P("GC grace=3600 apply (orphans are fresh):", {k: (v if not isinstance(v, list) else len(v)) for k, v in r.items()})
    r = st.gc(grace_seconds=0, dry_run=True); P("GC grace=0 dry-run:", {k: (v if not isinstance(v, list) else len(v)) for k, v in r.items()})
    r = st.gc(grace_seconds=0, dry_run=False); P("GC grace=0 apply:", {k: (v if not isinstance(v, list) else len(v)) for k, v in r.items()})
    P("GC orphan gone:", be.size(orphan) is None, "| live data intact:", c2.sync() is not None and c2.read(sess, max_bytes=-1) == cur)
    t = time.time(); v = st.verify(deep=True); P(f"VERIFY deep (all {v['entries']} entries re-hashed from S3) secs={time.time()-t:.1f} ok={v['ok']}")
    # ---- concurrent writers
    ws = [mk(f"cw{i}") for i in range(6)]; errs = []
    def w(i):
        try:
            for j in range(3): ws[i].put([(f"cw/{i}/{j}", os.urandom(2000), None)])
        except Exception as e: errs.append(repr(e))
    t = time.time(); ts = [threading.Thread(target=w, args=(i,)) for i in range(6)]; [x.start() for x in ts]; [x.join() for x in ts]
    chk = mk("chk"); chk.sync(); P(f"CONCURRENT 6x3 puts secs={time.time()-t:.1f} entries={len(chk.ls('cw/', limit=1000))}/18 errors={errs[:2]}")
    t = time.time(); P("COMPACT", st.compact()["entries"], f"secs={time.time()-t:.1f}"); P("VERIFY after compact ok:", mk("v2").verify()["ok"])
finally:
    be = S3Backend(BUCKET, run, profile=PROFILE); keys = [k for k, _ in be.list("")]
    for i in range(0, len(keys), 1000):
        be.c.delete_objects(Bucket=BUCKET, Delete={"Objects": [{"Key": be._k(k)} for k in keys[i:i+1000]]})
    P(f"CLEANUP deleted={len(keys)} remaining={len(list(be.list('')))}")
