"""Several processes on one machine share one catalog cache. A sync that must rebuild after a compaction elsewhere
waits for another process's write instead of failing with "database is locked" (sqlite's default wait is 5 s), and a
process that waited applies only what is still missing."""
import multiprocessing as mp
import sqlite3, time

from tracedb import Store, LocalBackend


def _hold_write_lock(catalog, seconds, ready):
    c = sqlite3.connect(catalog, isolation_level=None)
    c.execute("BEGIN IMMEDIATE")
    ready.set()
    time.sleep(seconds)
    c.execute("COMMIT")


def _sync(bucket, cache, q):
    try:
        st = Store(LocalBackend(bucket), cache_dir=cache, name="t")
        q.put(("ok", st.sync()["entries"]))
    except Exception as e:  # the failure tonight: sqlite3.OperationalError: database is locked
        q.put(("err", repr(e)))


def _setup(tmp_path):
    bucket, cache = str(tmp_path / "bucket"), str(tmp_path / "cache")
    writer = Store(LocalBackend(bucket), cache_dir=str(tmp_path / "writer"), name="t")
    for i in range(3):
        writer.put([(f"a/{i}.txt", f"v{i}".encode(), None)])
    reader = Store(LocalBackend(bucket), cache_dir=cache, name="t")
    assert reader.count() == 0 and reader.sync()["entries"] == 3
    writer.put([("a/3.txt", b"v3", None)])
    writer.compact()  # every segment the reader holds is replaced: its next sync rebuilds
    return bucket, cache, reader._stamp.replace("last_sync", "catalog.sqlite")


def test_rebuild_waits_for_another_writer(tmp_path):
    bucket, cache, catalog = _setup(tmp_path)
    ctx = mp.get_context("spawn")
    ready, q = ctx.Event(), ctx.Queue()
    holder = ctx.Process(target=_hold_write_lock, args=(catalog, 15, ready))
    holder.start(); assert ready.wait(30)
    syncer = ctx.Process(target=_sync, args=(bucket, cache, q))
    syncer.start(); syncer.join(60); holder.join(30)
    assert q.get(timeout=5) == ("ok", 4)


def test_concurrent_rebuilds_all_succeed(tmp_path):
    bucket, cache, _ = _setup(tmp_path)
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    ps = [ctx.Process(target=_sync, args=(bucket, cache, q)) for _ in range(6)]
    [p.start() for p in ps]; [p.join(60) for p in ps]
    assert sorted(q.get(timeout=5) for _ in ps) == [("ok", 4)] * 6
