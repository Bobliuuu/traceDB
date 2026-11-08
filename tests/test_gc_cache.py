import os, threading, time
from tracedb import Store, LocalBackend

OP = "packs/" + "ab" * 32 + ".pack"
OS = "segments/" + "cd" * 32 + ".seg"


def mkstore(tmp_path, **kw):
    return Store(LocalBackend(tmp_path / "bucket"), cache_dir=str(tmp_path / "cache"), name="t", **kw)


def keys(st, p):
    return {k for k, _ in st.b.list(p)}


def age(st, key, secs):
    t = time.time() - secs
    os.utime(st.b._p(key), (t, t))


def test_dry_run_reports_without_deleting(tmp_path):
    st = mkstore(tmp_path)
    st.put([("a", os.urandom(1000), None)])
    st.b.put_if_absent(OP, b"x" * 100); age(st, OP, 7200)
    r = st.gc(grace_seconds=3600)
    assert r["dry_run"] and r["orphans"] == 1 and OP in r["orphan_keys"] and r["deleted"] == 0
    assert OP in keys(st, "packs/")


def test_grace_protects_fresh_pack(tmp_path):
    st = mkstore(tmp_path)
    st.put([("a", b"hello", None)])
    st.b.put_if_absent(OP, b"x" * 100)  # fresh, no segment yet: a concurrent writer mid-flight
    r = st.gc(grace_seconds=3600, dry_run=False)
    assert r["orphans"] == 0 and r["skipped_young"] == 1 and OP in keys(st, "packs/")


def test_orphans_collected_live_intact(tmp_path):
    st = mkstore(tmp_path)
    d = {f"f{i}": os.urandom(5000) for i in range(20)}
    st.put([(p, b, None) for p, b in d.items()])
    st.put([("g", b"second", None)])
    st.b.put_if_absent(OP, b"x" * 100); st.b.put_if_absent(OS, b"junk")
    age(st, OP, 9999); age(st, OS, 9999)
    live_before = keys(st, "packs/") | keys(st, "segments/")
    r = st.gc(grace_seconds=60, dry_run=False)
    assert r["deleted"] == 2
    assert live_before - {OP, OS} == keys(st, "packs/") | keys(st, "segments/")
    assert os.path.exists(st.b._p("HEAD"))
    st2 = Store(st.b, cache_dir=str(tmp_path / "c2"), name="u")
    st2.sync()
    for p, b in d.items():
        assert st2.read(p) == b
    assert st2.verify(deep=True)["ok"]


def test_gc_after_compact_keeps_reads(tmp_path):
    st = mkstore(tmp_path)
    for i in range(3):
        st.put([(f"k{i}", os.urandom(300), None)])
    st.compact()
    for k in keys(st, "segments/"):
        age(st, k, 9999)
    r = st.gc(grace_seconds=60, dry_run=False)
    assert r["deleted"] == 3 and len(keys(st, "segments/")) == 1
    assert st.verify(deep=True)["ok"]


def test_concurrent_put_during_gc(tmp_path):
    st = mkstore(tmp_path)
    st.put([("seed", b"s" * 100, None)])
    stop = threading.Event()
    errs = []

    def gcloop():
        s2 = Store(st.b, cache_dir=str(tmp_path / "cg"), name="g")
        while not stop.is_set():
            try:
                s2.gc(grace_seconds=3600, dry_run=False)
            except Exception as e:
                errs.append(e)

    t = threading.Thread(target=gcloop); t.start()
    w = Store(st.b, cache_dir=str(tmp_path / "cw"), name="w")
    data = {f"n{i}": os.urandom(2000) for i in range(30)}
    for p, b in data.items():
        w.put([(p, b, None)])
    stop.set(); t.join()
    assert not errs
    r = Store(st.b, cache_dir=str(tmp_path / "cr"), name="r")
    r.sync()
    for p, b in data.items():
        assert r.read(p) == b
    assert r.verify(deep=True)["ok"]


def test_cache_bounded_and_reads_correct(tmp_path):
    cap = 600_000
    st = mkstore(tmp_path, cache_bytes=cap)
    data = {f"f{i}": os.urandom(100_000) for i in range(30)}
    st.put([(p, b, None) for p, b in data.items()])
    mx = 0
    for _ in range(2):
        for p, b in data.items():
            assert st.read(p) == b
            mx = max(mx, sum(x[1] for x in st._cache_walk()))
    assert mx <= cap * 1.1 + 110_000  # at most one read's worth over the 110% trigger
    st.evict_cache()
    assert sum(x[1] for x in st._cache_walk()) <= cap * 0.8
    for p, b in data.items():
        assert st.read(p) == b


def test_reupload_of_old_orphan_pack_is_protected_from_gc(tmp_path):
    """Race fix: identical content re-put onto an old orphan pack must refresh its age so GC cannot reap it."""
    import json
    st = mkstore(tmp_path)
    data = os.urandom(5000)
    st.put([("a", data, None)])
    pk = st.locate("a")["pack"]
    pkey = f"packs/{pk}.pack"
    st.b.cas_head(json.dumps({"v": 1, "segments": []}).encode(), st.b.get_head()[1])  # crash: pack now orphaned
    age(st, pkey, 10 * 86400)
    st2 = mkstore(tmp_path)  # fresh client, empty catalog
    st2.db.execute("DELETE FROM entries"); st2.db.execute("DELETE FROM segs"); st2.db.commit()
    st2.put([("a", data, None)])  # dedup hit on the old orphan pack -> touch
    assert time.time() - os.path.getmtime(st.b._p(pkey)) < 60
    st2.gc(grace_seconds=3600, dry_run=False)
    assert st2.read("a") == data and st2.verify(deep=True)["ok"]


def test_gc_keeps_every_extent_pack_of_appended_files(tmp_path):
    """Regression (found on real S3): gc must treat packs of ALL extents as live. grace=0 so only liveness protects."""
    st = mkstore(tmp_path)
    cur = os.urandom(300_000)
    st.put([("s.jsonl", cur, None)])
    for _ in range(3):
        cur += os.urandom(50_000)
        st.put([("s.jsonl", cur, None)])
    assert len(st.locate("s.jsonl")["exts"]) == 4
    before = keys(st, "packs/")
    r = st.gc(grace_seconds=0, dry_run=False)
    assert keys(st, "packs/") == before, r
    st2 = mkstore(tmp_path / "other")  # (fresh cache) reads must still work
    st2.b = st.b; st2.sync()
    assert st2.read("s.jsonl", max_bytes=-1) == cur and st.verify(deep=True)["ok"]
