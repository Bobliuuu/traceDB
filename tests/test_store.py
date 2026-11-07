import json, os, random, threading, time
import pytest
from tracedb import Store, LocalBackend, BudgetExceeded, NotFound


@pytest.fixture
def st(tmp_path):
    return Store(LocalBackend(tmp_path / "bucket"), cache_dir=str(tmp_path / "cache"), name="t")


def mk(n=50, seed=0):
    r = random.Random(seed)
    return {f"c/seat{i%5}/job{i}.jsonl": ("".join(f'{{"i":{j},"t":"{r.random()}"}}\n' for j in range(30))).encode()
            for i in range(n)}


def test_roundtrip_and_ls(st):
    d = mk()
    st.put((p, b, {"k": "x"}) for p, b in d.items())
    for p, b in d.items():
        assert st.read(p) == b
    assert [x["path"] for x in st.ls("c/seat1/")] == sorted(p for p in d if p.startswith("c/seat1/"))
    assert st.locate("c/seat0/job0.jsonl")["meta"] == {"k": "x"}


def test_idempotent_put(st):
    d = mk(10)
    r1 = st.put((p, b, None) for p, b in d.items())
    r2 = st.put((p, b, None) for p, b in d.items())
    assert r1["added"] == 10 and r2["added"] == 0
    assert len(list(st.b.list("segments/"))) == 1


def test_budget_blocks_big_pull_and_plan_is_free(st):
    big = os.urandom(3 << 20)
    st.put([("big.bin", big, None)])
    before = st.stats["requests"]
    pl = st.plan(["big.bin"])
    assert pl.raw_bytes == 3 << 20 and st.stats["requests"] == before
    with pytest.raises(BudgetExceeded):
        st.read("big.bin", max_bytes=1 << 20)
    assert st.read("big.bin", max_bytes=-1) == big


def test_range_reads_fetch_only_needed_blocks(st):
    big = os.urandom(6 << 20)
    st.put([("big.bin", big, None)])
    b0 = st.stats["bytes"]
    got = st.read("big.bin", 4_500_000, 4_500_100)
    assert got == big[4_500_000:4_500_100]
    assert st.stats["bytes"] - b0 <= (1 << 20) + 16  # one block, not 6 MiB
    assert st.tail("big.bin", 10) == big[-10:]
    assert st.head("big.bin", 10) == big[:10]


def test_cache_serves_repeat(st):
    d = mk(5); st.put((p, b, None) for p, b in d.items())
    p = next(iter(d)); st.read(p); r = st.stats["requests"]
    st.read(p)
    assert st.stats["requests"] == r


def test_empty_and_missing(st):
    st.put([("e", b"", None)])
    assert st.read("e") == b""
    with pytest.raises(NotFound):
        st.read("nope")


def test_orphan_pack_invisible_until_head_flip(st):
    st.b.put_if_absent("packs/" + "a" * 64 + ".pack", b"junk")
    st.b.put_if_absent("segments/" + "b" * 64 + ".seg", b"junk")  # crash after seg, before HEAD
    st.sync()
    assert st.count() == 0
    st.put([("x", b"hello" * 100, None)])
    assert st.read("x") == b"hello" * 100


def test_second_client_syncs_incrementally(tmp_path):
    be = LocalBackend(tmp_path / "b")
    a = Store(be, cache_dir=str(tmp_path / "ca"), name="a")
    b = Store(be, cache_dir=str(tmp_path / "cb"), name="b")
    a.put([("one", b"1" * 1000, None)])
    assert b.sync()["new"] == 1
    a.put([("two", b"2" * 1000, None)])
    r = b.sync()
    assert r["new"] == 1 and r["entries"] == 2
    assert b.read("two") == b"2" * 1000


def test_concurrent_writers_lose_nothing(tmp_path):
    be = LocalBackend(tmp_path / "b")
    stores = [Store(be, cache_dir=str(tmp_path / f"c{i}"), name=f"w{i}") for i in range(8)]
    def w(i):
        for j in range(5):
            stores[i].put([(f"w{i}/f{j}", f"{i}-{j}".encode() * 50, None)])
    ts = [threading.Thread(target=w, args=(i,)) for i in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    chk = Store(be, cache_dir=str(tmp_path / "chk"), name="chk"); chk.sync()
    assert chk.count() == 40
    assert all(chk.read(f"w{i}/f{j}") == f"{i}-{j}".encode() * 50 for i in range(8) for j in range(5))


def test_compact_preserves_everything(st):
    d = mk(20)
    for i, (p, b) in enumerate(d.items()):
        st.put([(p, b, None)])
    n = len(json.loads(st.b.get("HEAD"))["segments"])
    assert n == 20
    st.compact()
    assert len(json.loads(st.b.get("HEAD"))["segments"]) == 1
    assert st.verify(deep=True)["ok"]
    assert all(st.read(p) == b for p, b in d.items())


def test_verify_detects_missing_pack(st):
    st.put([("x", os.urandom(5000), None)])
    pk = st.locate("x")["pack"]
    os.unlink(os.path.join(st.b.root, f"packs/{pk}.pack"))
    r = st.verify()
    assert not r["ok"] and r["missing_packs"] == [pk]


def test_lookup_is_fast(st):
    paths = [f"d{i%100}/f{i}" for i in range(5000)]
    st.put((p, b"x" * 200, None) for p in paths)
    t = time.perf_counter()
    for i in range(2000):
        assert st.locate(paths[(i * 7) % 5000]) is not None
    per = (time.perf_counter() - t) / 2000
    assert per < 0.001  # <1 ms (typically ~20us)


def test_corrupt_segment_rejected(tmp_path):
    be = LocalBackend(tmp_path / "b")
    a = Store(be, cache_dir=str(tmp_path / "ca"), name="a")
    a.put([("one", b"1" * 100, None)])
    sid = json.loads(be.get("HEAD"))["segments"][0]
    with open(os.path.join(be.root, f"segments/{sid}.seg"), "ab") as f:
        f.write(b"x")
    b = Store(be, cache_dir=str(tmp_path / "cb"), name="b")
    with pytest.raises(IOError):
        b.sync()
