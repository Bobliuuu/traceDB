import os, sys, json, random, shutil, time
from tracedb import open_store, Store, LocalBackend
from tracedb.cli import sniff
SRC, OUT = sys.argv[1:3]
shutil.rmtree(OUT, ignore_errors=True); os.makedirs(OUT)
be = LocalBackend(os.path.join(OUT, "b")); st = Store(be, cache_dir=os.path.join(OUT, "c"), name="x")
files = []
for d in ["research", "bugs", "evals", "reports"]:
    for dp, _, fs in os.walk(os.path.join(SRC, d)):
        for f in fs:
            full = os.path.join(dp, f)
            if os.path.isfile(full) and not os.path.islink(full): files.append((os.path.relpath(full, SRC), full, sniff(full, {})))
be.put_if_absent("probe", b"a"); e1 = be.cas_head(b'{"segments":[]}', None); be.cas_head(b'{"segments":[],"x":1}', e1)
st.put(iter(files)); print("ingested", len(files))
rnd = random.Random(7); line = lambda i: (json.dumps({"i": i, "t": rnd.random(), "m": "x" * 60}) + "\n").encode()
cur = b"".join(line(i) for i in range(200000)); st.put([("sessions/s1.jsonl", cur, None)])
for k in range(3):
    cur += b"".join(line(10**6 + k * 2000 + i) for i in range(1000)); st.put([("sessions/s1.jsonl", cur, None)])
def v(tag):
    r = st.verify(deep=True); print(tag, {k: (r[k] if not isinstance(r[k], list) else (len(r[k]), r[k][:3])) for k in r}, flush=True)
v("after-append")
live = next(k for k, _ in be.list("packs/")); be.touch(live); v("after-touch")
be.put_if_absent("packs/" + "ab" * 32 + ".pack", b"junk" * 100); be.put_if_absent("segments/" + "cd" * 32 + ".seg", b"junk")
print(st.gc(grace_seconds=0, dry_run=False)); v("after-gc")
st.compact(); v("after-compact")
