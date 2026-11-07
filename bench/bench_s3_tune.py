"""S3 tuning: prefix pulls + coalesce-gap sweep. usage: SRC BUCKET PREFIX PROFILE. Cleans up its prefix."""
import os, random, shutil, sys, time, uuid
from collections import defaultdict
import tracedb.store as S
from tracedb import Store, S3Backend
from tracedb.cli import sniff
SRC, BUCKET, PFX, PROFILE = sys.argv[1:5]
run = f"{PFX.strip('/')}/{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:6]}"
cache = os.path.expanduser("~/tracedb-work/bench-out/s3cache2"); shutil.rmtree(cache, ignore_errors=True)
mk = lambda n: Store(S3Backend(BUCKET, run, profile=PROFILE), cache_dir=cache, name=n)
P = lambda *a: print(*a, flush=True)
files = []
for d in ["logs", "research", "experiments", "data", "docs/archives", "reports", "bugs", "evals"]:
    for dp, _, fs in os.walk(os.path.join(SRC, d)):
        for f in fs:
            full = os.path.join(dp, f)
            if os.path.isfile(full) and not os.path.islink(full): files.append((os.path.relpath(full, SRC), full, sniff(full, {})))
P("RUN files=%d (client = laptop, not in-region)" % len(files))
try:
    mk("w").put(iter(files))
    cl = mk("c"); t = time.time(); cl.sync(); P(f"COLD_SYNC secs={time.time()-t:.2f}")
    time.sleep(1.5)  # let background warm finish, as it would during agent think time
    # ontolagni-style nodes: depth-3 dirs with 100..1000 files
    g = defaultdict(list)
    for p, _, _ in files:
        s = p.split("/")
        if len(s) > 3: g["/".join(s[:3])].append(p)
    nodes = sorted([k for k, v in g.items() if 100 <= len(v) <= 1000], key=lambda k: -len(g[k]))
    random.seed(2); picks = random.sample(nodes, 4)
    paths = [p for p, _, _ in files]; small = [p for p in paths if cl.locate(p)["size"] < 1 << 16]
    def run_wl(label, pick):
        shutil.rmtree(os.path.join(cl.dir, "blocks"), ignore_errors=True); os.makedirs(os.path.join(cl.dir, "blocks"))
        pl = cl.plan(pick); b0, r0 = cl.stats["bytes"], cl.stats["requests"]
        t = time.perf_counter(); d = cl.read_many(pick, max_bytes=-1); dt = time.perf_counter() - t
        nb = sum(len(v) for v in d.values()); wb = cl.stats["bytes"] - b0
        P(f"  gap={S.COALESCE_GAP>>10:>4}K {label:<34} files={len(pick):>4} raw={nb/2**20:6.1f}MiB wire={wb/2**20:6.1f}MiB amp={wb/max(nb,1):4.1f}x reqs={cl.stats['requests']-r0:>3} ms={dt*1e3:6.0f}")
    from concurrent.futures import ThreadPoolExecutor
    random.seed(5)
    wl = {"100 random small": random.sample(small, 100), "1000 random small": random.sample(small, 1000)}
    for workers in [16, 64, 128]:
        cl._pool = ThreadPoolExecutor(workers)
        for gap in [16 << 10, 64 << 10, 256 << 10, 1 << 20]:
            S.COALESCE_GAP = gap
            for label, pick in wl.items():
                cl_label = f"w={workers:<3} {label}"
                run_wl(cl_label, pick)
finally:
    be = S3Backend(BUCKET, run, profile=PROFILE); keys = [k for k, _ in be.list("")]
    for i in range(0, len(keys), 1000):
        be.c.delete_objects(Bucket=BUCKET, Delete={"Objects": [{"Key": be._k(k)} for k in keys[i:i+1000]]})
    P(f"CLEANUP deleted={len(keys)} remaining={len(list(be.list('')))}")
