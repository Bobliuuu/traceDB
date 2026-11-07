"""Growing append-only file: re-put after each append. Local backend, no network.
usage: python bench/bench_append.py [base_mib=20] [append_kib=100] [rounds=10]
Compares pack bytes added and put time per append vs. storing the whole file again."""
import os, random, shutil, sys, tempfile, time
from tracedb import Store, LocalBackend

base_mib = int(sys.argv[1]) if len(sys.argv) > 1 else 20
app_kib = int(sys.argv[2]) if len(sys.argv) > 2 else 100
rounds = int(sys.argv[3]) if len(sys.argv) > 3 else 10
r = random.Random(0)


def jsonl(nbytes):  # session-like text: compressible, not trivially repetitive
    out, n = [], 0
    while n < nbytes:
        l = ('{"role":"assistant","ts":%d,"text":"%s"}\n' % (r.randrange(1 << 40), "".join(r.choice("abcdef ghij klmnop") for _ in range(r.randrange(40, 400))))).encode()
        out.append(l); n += len(l)
    return b"".join(out)


tmp = tempfile.mkdtemp()
st = Store(LocalBackend(os.path.join(tmp, "b")), cache_dir=os.path.join(tmp, "c"), name="a")
packs = lambda: sum(s for _, s in st.b.list("packs/"))
cur = jsonl(base_mib << 20)
t = time.time(); st.put([("sess.jsonl", cur, None)]); t0 = time.time() - t
p0 = packs()
print(f"BASE raw={len(cur)/2**20:.1f}MiB packs={p0/2**20:.2f}MiB put={t0:.2f}s")
tot_t, tot_b, tot_raw = 0, 0, 0
for i in range(rounds):
    tail = jsonl(app_kib << 10); cur += tail
    before = packs(); t = time.time(); st.put([("sess.jsonl", cur, None)]); dt = time.time() - t
    added = packs() - before; tot_t += dt; tot_b += added; tot_raw += len(tail)
    print(f"APPEND {i} tail={len(tail)/1024:.0f}KiB added_pack={added/1024:.0f}KiB put={dt*1e3:.0f}ms")
assert st.read("sess.jsonl", max_bytes=-1) == cur and st.verify(deep=True)["ok"]
e = st.locate("sess.jsonl")
t = time.time(); st.tail("sess.jsonl", 4096); tt = time.time() - t
print(f"SUMMARY appends={rounds} avg_added={tot_b/rounds/1024:.0f}KiB (whole-file re-store would add ~{p0/2**20:.1f}MiB each, "
      f"{p0*rounds/2**20:.0f}MiB total vs {tot_b/2**20:.2f}MiB) avg_put={tot_t/rounds*1e3:.0f}ms (base put {t0*1e3:.0f}ms) "
      f"extents={len(e['exts'])} total_packs={packs()/2**20:.2f}MiB tail4k_cold={tt*1e3:.1f}ms verify_deep=ok")
shutil.rmtree(tmp)
