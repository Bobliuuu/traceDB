import os, sys, time, shutil
from tracedb import open_store
from tracedb.cli import sniff
SRC, OUT = sys.argv[1:3]
shutil.rmtree(OUT, ignore_errors=True); os.makedirs(OUT)
st = open_store(os.path.join(OUT, "b"), cache_dir=os.path.join(OUT, "c"), name="x")
files = []
for d in ["logs", "research", "experiments", "data", "docs/archives", "reports", "bugs", "evals"]:
    for dp, _, fs in os.walk(os.path.join(SRC, d)):
        for f in fs:
            full = os.path.join(dp, f)
            if os.path.isfile(full) and not os.path.islink(full): files.append((os.path.relpath(full, SRC), full, sniff(full, {})))
st.put(iter(files)); t = time.time(); v = st.verify(deep=True)
print("secs", round(time.time() - t, 1), {k: (v[k] if not isinstance(v[k], list) else (len(v[k]), v[k][:5])) for k in v})
