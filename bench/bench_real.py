"""End-to-end on real data: ingest -> catalog -> ontolagni -> budgeted pull. Local backend (no network)."""
import json, os, random, shutil, subprocess, sys, time
from tracedb import open_store
from tracedb.cli import sniff

SRC = sys.argv[1]; OUT = sys.argv[2]
DIRS = ["logs", "research", "experiments", "data", "docs/archives", "reports", "bugs", "evals"]
shutil.rmtree(OUT, ignore_errors=True); os.makedirs(OUT)
st = open_store(os.path.join(OUT, "bucket"), cache_dir=os.path.join(OUT, "cache"), name="real")

def gen():
    for d in DIRS:
        for dp, _, fs in os.walk(os.path.join(SRC, d)):
            for f in fs:
                full = os.path.join(dp, f)
                if os.path.islink(full) or not os.path.isfile(full): continue
                yield os.path.relpath(full, SRC), full, sniff(full, {})

files = list(gen()); raw = sum(os.path.getsize(f[1]) for f in files)
t = time.time(); r = st.put(iter(files)); ing = time.time() - t
wire = sum(sz for _, sz in st.b.list("packs/"))
print(f"INGEST files={len(files)} raw={raw/2**20:.0f}MiB packs={r['packs']} wire={wire/2**20:.0f}MiB "
      f"ratio={wire/raw:.2f} secs={ing:.1f} ({raw/2**20/ing:.0f} MiB/s) segments={len(list(st.b.list('segments/')))}")

# cold catalog rebuild on a fresh client
st2 = open_store(os.path.join(OUT, "bucket"), cache_dir=os.path.join(OUT, "cache2"), name="cold")
t = time.time(); st2.sync(); print(f"COLD_SYNC entries={st2.count()} secs={time.time()-t:.2f}")
t = time.time(); st2.sync(); print(f"WARM_SYNC (nothing new) ms={(time.time()-t)*1e3:.2f}")

paths = [p for p, _, _ in files]; random.seed(1); samp = random.sample(paths, 5000)
lat = []
for p in samp:
    t = time.perf_counter(); e = st2.locate(p); lat.append((time.perf_counter() - t) * 1e6); assert e
lat.sort(); print(f"LOCATE us p50={lat[2500]:.0f} p99={lat[4950]:.0f} max={lat[-1]:.0f}")
t = time.perf_counter(); n = len(st2.ls("logs/qwen-g7e-20261021/", limit=100000)); print(f"LS prefix ms={(time.perf_counter()-t)*1e3:.1f} n={n}")

# cold-cache pull latency, small files then big, local backend = CPU + disk floor
def pull(label, k, size_lo, size_hi):
    pool = [p for p in paths if size_lo <= st2.locate(p)["size"] < size_hi]
    pick = random.sample(pool, min(k, len(pool)))
    shutil.rmtree(os.path.join(st2.dir, "blocks"), ignore_errors=True); os.makedirs(os.path.join(st2.dir, "blocks"))
    t = time.perf_counter(); d = st2.read_many(pick, max_bytes=-1); dt = time.perf_counter() - t
    nb = sum(len(v) for v in d.values()); print(f"PULL {label} files={len(pick)} bytes={nb/2**20:.1f}MiB ms={dt*1e3:.0f} ({nb/2**20/dt:.0f} MiB/s)")
pull("100 small(<64K)", 100, 0, 1 << 16); pull("1000 small", 1000, 0, 1 << 16); pull("20 mid(64K-1M)", 20, 1 << 16, 1 << 20); pull("5 big(>2M)", 5, 2 << 20, 1 << 40)

# ontolagni
cat = os.path.join(OUT, "catalog.jsonl")
with open(cat, "w") as f:
    for r in st2.export_catalog(): f.write(json.dumps(r, separators=(",", ":")) + "\n")
print("CATALOG_MB", round(os.path.getsize(cat)/2**20, 1))
od = os.path.join(OUT, "onto")
env = {**os.environ, "PYTHONPATH": os.path.expanduser("~/tracedb-work/ontolagni")}
t = time.time(); b = subprocess.run([sys.executable, "-m", "ontolagni.cli", "--dir", od, "build", cat], capture_output=True, text=True, env=env); print("ONTO_BUILD", b.stdout.strip(), f"secs={time.time()-t:.2f}")
print("ONTO_SIZE_KB", sum(os.path.getsize(os.path.join(od, x)) for x in os.listdir(od)) // 1024)
t = time.time(); v = subprocess.run([sys.executable, "-m", "ontolagni.cli", "--dir", od, "verify", cat], capture_output=True, text=True, env=env); print("ONTO_VERIFY", v.stdout.strip()[:200], f"rc={v.returncode} secs={time.time()-t:.2f}")
sys.path.insert(0, os.path.expanduser("~/tracedb-work/ontolagni"))
from ontolagni import Onto
t = time.perf_counter(); o = Onto(od); print(f"ONTO_LOAD ms={(time.perf_counter()-t)*1e3:.0f} nodes={len(o.nodes)}")
for q in ["qwen g7e vllm", "kimi b300 profile", "glm gpqa", "dialogs vega", "census nodes csv"]:
    t = time.perf_counter(); r = o.find(q, limit=3); ms = (time.perf_counter() - t) * 1e3
    print(f"FIND '{q}' ms={ms:.2f} ->", [(x['id'], x['files'], x['bytes']) for x in r[:2]])
