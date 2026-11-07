"""Real-S3 benchmark. usage: bench_s3.py SRC_REPO BUCKET PREFIX PROFILE. Cleans up its own prefix."""
import json, os, random, shutil, sys, threading, time, uuid
from tracedb import Store, S3Backend
from tracedb.cli import sniff
from tracedb.backend import CASConflict

SRC, BUCKET, PFX, PROFILE = sys.argv[1:5]
run = f"{PFX.strip('/')}/{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:6]}"
cache = os.path.expanduser("~/tracedb-work/bench-out/s3cache"); shutil.rmtree(cache, ignore_errors=True)
mk = lambda n: Store(S3Backend(BUCKET, run, profile=PROFILE), cache_dir=cache, name=n)
P = lambda *a: print(*a, flush=True)
DIRS = ["logs", "research", "experiments", "data", "docs/archives", "reports", "bugs", "evals"]
files = []
for d in DIRS:
    for dp, _, fs in os.walk(os.path.join(SRC, d)):
        for f in fs:
            full = os.path.join(dp, f)
            if os.path.isfile(full) and not os.path.islink(full):
                files.append((os.path.relpath(full, SRC), full, sniff(full, {})))
raw = sum(os.path.getsize(f[1]) for f in files)
P(f"RUN s3://{BUCKET}/{run}/ files={len(files)} raw={raw/2**20:.0f}MiB (client = laptop, not in-region)")
try:
    st = mk("w")
    # conditional-write sanity
    be = st.b
    assert be.put_if_absent("probe", b"a") and not be.put_if_absent("probe", b"b"); P("COND create-only PUT: ok (2nd refused)")
    _, e1 = be.get_head(); e1 = be.cas_head(b'{"segments":[]}', None); P("COND HEAD create ok")
    be.cas_head(b'{"segments":[],"x":1}', e1)
    try: be.cas_head(b'{"segments":[],"x":2}', e1); P("COND CAS stale-etag: NOT REFUSED (BAD)")
    except CASConflict: P("COND CAS stale-etag refused: ok")
    t = time.time(); r = st.put(iter(files)); dt = time.time() - t
    P(f"INGEST secs={dt:.1f} packs={r['packs']} wire={r['bytes']/2**20:.0f}MiB ({r['bytes']/2**20/dt:.1f} MiB/s upload incl. hashing/compress)")
    cl = mk("c"); t = time.time(); cl.sync(); P(f"COLD_SYNC entries={cl.count()} secs={time.time()-t:.2f}")
    lat = []
    for _ in range(10):
        t = time.perf_counter(); cl.b.get_head(); lat.append((time.perf_counter() - t) * 1e3)
    lat.sort(); P(f"HEAD_GET ms p50={lat[5]:.0f} max={lat[-1]:.0f}  (= warm sync cost when nothing changed)")
    paths = [f[0] for f in files]; random.seed(1)
    def pull(label, k, lo, hi):
        pool = [p for p in paths if lo <= cl.locate(p)["size"] < hi]
        pick = random.sample(pool, min(k, len(pool)))
        shutil.rmtree(os.path.join(cl.dir, "blocks"), ignore_errors=True); os.makedirs(os.path.join(cl.dir, "blocks"))
        pl = cl.plan(pick); b0, r0 = cl.stats["bytes"], cl.stats["requests"]
        t = time.perf_counter(); d = cl.read_many(pick, max_bytes=-1); dt = time.perf_counter() - t
        nb = sum(len(v) for v in d.values())
        P(f"PULL {label}: files={len(pick)} raw={nb/2**20:.1f}MiB wire={(cl.stats['bytes']-b0)/2**20:.1f}MiB reqs={cl.stats['requests']-r0} "
          f"(plan said wire={pl.fetch_bytes/2**20:.1f}MiB reqs={pl.requests}) ms={dt*1e3:.0f} ({nb/2**20/dt:.1f} MiB/s)")
    pull("1 small file", 1, 0, 1 << 16); pull("10 small", 10, 0, 1 << 16); pull("100 small", 100, 0, 1 << 16)
    pull("1000 small", 1000, 0, 1 << 16); pull("20 mid(64K-1M)", 20, 1 << 16, 1 << 20); pull("5 big(>2M)", 5, 2 << 20, 1 << 40)
    # single-range latency: 4 KiB from the middle of a big file
    big = max(paths, key=lambda p: cl.locate(p)["size"]); sz = cl.locate(big)["size"]
    ls = []
    for i in range(8):
        shutil.rmtree(os.path.join(cl.dir, "blocks"), ignore_errors=True); os.makedirs(os.path.join(cl.dir, "blocks"))
        o = random.randrange(0, sz - 4096); t = time.perf_counter(); cl.read(big, o, o + 4096); ls.append((time.perf_counter() - t) * 1e3)
    ls.sort(); P(f"RANGE 4KiB from {sz/2**20:.1f}MiB file (cold, 1 block<=1MiB wire): ms p50={ls[4]:.0f} max={ls[-1]:.0f}")
    # concurrent writers via real conditional writes
    ws = [mk(f"cw{i}") for i in range(6)]; errs = []
    def w(i):
        try:
            for j in range(3): ws[i].put([(f"cw/{i}/{j}", os.urandom(2000), None)])
        except Exception as e: errs.append(repr(e))
    t = time.time(); ts = [threading.Thread(target=w, args=(i,)) for i in range(6)]; [x.start() for x in ts]; [x.join() for x in ts]
    chk = mk("chk"); chk.sync(); n = len(chk.ls("cw/", limit=1000))
    P(f"CONCURRENT 6 writers x3 puts: secs={time.time()-t:.1f} entries={n}/18 errors={errs[:2]}")
    P("VERIFY", chk.verify())
finally:
    be = S3Backend(BUCKET, run, profile=PROFILE); keys = [k for k, _ in be.list("")]
    for i in range(0, len(keys), 1000):
        be.c.delete_objects(Bucket=BUCKET, Delete={"Objects": [{"Key": be._k(k)} for k in keys[i:i+1000]]})
    left = list(be.list("")); P(f"CLEANUP deleted={len(keys)} remaining={len(left)}")
