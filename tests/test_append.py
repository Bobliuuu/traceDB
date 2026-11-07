import json, os, random, shutil, threading, zlib
import pytest
from tracedb import Store, LocalBackend
from tracedb import codec as C
from tracedb.store import _sha

MB = 1 << 20


@pytest.fixture
def st(tmp_path):
    return Store(LocalBackend(tmp_path / "bucket"), cache_dir=str(tmp_path / "cache"), name="t")


def packbytes(st):
    return sum(sz for _, sz in st.b.list("packs/"))


def lines(n, seed):
    r = random.Random(seed)
    return "".join(f'{{"i":{j},"t":"{r.random()}"}}\n' for j in range(n)).encode()


def grow(st, path, chunks):
    cur = b""
    for c in chunks:
        cur += c
        st.put([(path, cur, None)])
    return cur


def test_append_roundtrip_and_extents(st):
    a, b, c = lines(5000, 1), lines(3000, 2), lines(10, 3)
    full = grow(st, "s.jsonl", [a, b, c])
    e = st.locate("s.jsonl")
    assert len(e["exts"]) == 3 and e["size"] == len(full) and e["sha"] == _sha(full)
    assert st.read("s.jsonl", max_bytes=-1) == full
    st2 = Store(st.b, cache_dir=st.dir + "2", name="t2")  # a fresh client sees the same thing
    st2.sync()
    assert st2.read("s.jsonl", max_bytes=-1) == full
    assert [x["size"] for x in st2.ls("s")] == [len(full)]


def test_range_across_extent_boundary_unaligned(st):
    parts = [os.urandom(2_500_000), os.urandom(1_300_000), os.urandom(700_001)]
    full = grow(st, "x.bin", parts)
    assert len(st.locate("x.bin")["exts"]) == 3
    bnd = [len(parts[0]), len(parts[0]) + len(parts[1])]
    for b in bnd:
        for s, e in [(b - 10, b + 10), (b - MB - 5, b + MB + 5), (b - 1, b + 1)]:
            assert st.read("x.bin", s, e) == full[s:e]
    r = random.Random(0)
    for _ in range(40):
        s = r.randrange(len(full)); e = min(len(full), s + r.randrange(1, 3 * MB))
        assert st.read("x.bin", s, e) == full[s:e]
    assert st.read("x.bin", bnd[1] - 3, None, max_bytes=-1) == full[bnd[1] - 3:]


def test_head_tail(st):
    full = grow(st, "g", [os.urandom(3 * MB + 17), os.urandom(5000), os.urandom(MB)])
    assert st.head("g", 100) == full[:100]
    assert st.tail("g", 100) == full[-100:]
    assert st.tail("g", MB + 5000 + 20) == full[-(MB + 5020):]


def test_plan_cost_equals_actual_fetch(st):
    full = grow(st, "p", [os.urandom(2 * MB + 5), os.urandom(3 * MB + 9), os.urandom(MB // 2)])
    for s, e in [(2 * MB, 2 * MB + 50), (0, 100), (2 * MB - 5, 5 * MB + 20), (len(full) - 10, len(full))]:
        shutil.rmtree(os.path.join(st.dir, "blocks"), ignore_errors=True)  # cold cache
        pl = st.plan([("p", s, e)])
        b0, r0 = st.stats["bytes"], st.stats["requests"]
        assert st.read("p", s, e, max_bytes=-1) == full[s:e]
        assert st.stats["bytes"] - b0 == pl.fetch_bytes
        assert st.stats["requests"] - r0 == pl.requests


def test_append_stores_only_tail(st):
    base = os.urandom(20 * MB)
    st.put([("big.jsonl", base, None)])
    before = packbytes(st)
    tail = os.urandom(100 << 10)
    r = st.put([("big.jsonl", base + tail, None)])
    added = packbytes(st) - before
    assert 100 << 10 <= added < (100 << 10) + 2048 and r["bytes"] == added
    assert st.read("big.jsonl", max_bytes=-1) == base + tail
    assert st.tail("big.jsonl", 1000) == tail[-1000:]


def test_non_prefix_rewrite_stores_whole(st):
    a = os.urandom(MB)
    st.put([("f", a, None)])
    b = os.urandom(10) + a[10:] + b"zz"
    st.put([("f", b, None)])
    assert len(st.locate("f")["exts"]) == 1 and st.read("f", max_bytes=-1) == b
    st.put([("f", b[:100], None)])  # shrink
    assert st.read("f") == b[:100] and len(st.locate("f")["exts"]) == 1


def test_old_format_segment_loads_and_can_grow(tmp_path):
    be = LocalBackend(tmp_path / "b")
    data = lines(4000, 9)
    blocks, codecs, parts = [], [], []
    for i in range(0, len(data), C.BLOCK):
        c, cb = C.compress(data[i:i + C.BLOCK]); blocks.append(len(cb)); codecs.append(c); parts.append(cb)
    pack = b"".join(parts); pid = _sha(pack)
    be.put_if_absent(f"packs/{pid}.pack", pack)
    row = {"p": "old.jsonl", "h": _sha(data), "n": len(data), "k": pid, "o": 0, "c": len(pack), "b": blocks,
           "x": codecs[0], "m": {"a": 1}}
    seg = zlib.compress(json.dumps(row).encode(), 1); sid = _sha(seg)
    be.put_if_absent(f"segments/{sid}.seg", seg)
    be.cas_head(json.dumps({"v": 1, "segments": [sid]}).encode(), None)
    st = Store(be, cache_dir=str(tmp_path / "c"), name="o")
    st.sync()
    assert st.read("old.jsonl", max_bytes=-1) == data and st.locate("old.jsonl")["meta"] == {"a": 1}
    more = data + lines(10, 4)
    st.put([("old.jsonl", more, None)])
    assert len(st.locate("old.jsonl")["exts"]) == 2 and st.locate("old.jsonl")["meta"] == {"a": 1}
    assert st.read("old.jsonl", max_bytes=-1) == more and st.verify(deep=True)["ok"]


def test_old_catalog_cache_is_rebuilt(tmp_path):
    import sqlite3
    be = LocalBackend(tmp_path / "b")
    d = tmp_path / "c" / "n"; d.mkdir(parents=True)
    db = sqlite3.connect(d / "catalog.sqlite")
    db.executescript("CREATE TABLE entries(path TEXT PRIMARY KEY, sha TEXT, size INTEGER, pack TEXT, off INTEGER,"
                     " clen INTEGER, blocks TEXT, codecs TEXT, meta TEXT) WITHOUT ROWID;"
                     "CREATE TABLE segs(id TEXT PRIMARY KEY, ord INTEGER);"); db.close()
    st = Store(be, cache_dir=str(tmp_path / "c"), name="n")
    st.put([("a", b"hi" * 100, None)])
    assert st.read("a") == b"hi" * 100


def test_compact_preserves_extents_and_verify_deep(st):
    full = grow(st, "s", [lines(4000, 1), lines(2000, 2), lines(5, 3)])
    other = os.urandom(3000); st.put([("o", other, {"k": 1})])
    st.compact()
    assert len(st.locate("s")["exts"]) == 3
    st3 = Store(st.b, cache_dir=st.dir + "3", name="t3")  # rebuild from the compacted segment
    st3.sync()
    assert len(st3.locate("s")["exts"]) == 3 and st3.read("s", max_bytes=-1) == full
    assert st3.read("o") == other
    v = st3.verify(deep=True)
    assert v["ok"] and v["bad_content"] == []
    assert [x["size"] for x in st3.export_catalog() if x["path"] == "s"] == [len(full)]


def test_verify_detects_missing_tail_pack(st):
    grow(st, "s", [os.urandom(5000), os.urandom(5000)])
    pk = st.locate("s")["exts"][1]["k"]
    os.unlink(os.path.join(st.b.root, f"packs/{pk}.pack"))
    assert pk in st.verify()["missing_packs"]


def test_many_appends_collapse_extents(st, monkeypatch):
    from tracedb import store as S
    monkeypatch.setattr(S, "MAX_EXTENTS", 4)
    full = grow(st, "m", [os.urandom(2000) for _ in range(12)])
    assert len(st.locate("m")["exts"]) <= 4 and st.read("m") == full


def test_concurrent_appenders(tmp_path):
    be = LocalBackend(tmp_path / "b")
    stores = [Store(be, cache_dir=str(tmp_path / f"c{i}"), name=f"w{i}") for i in range(6)]
    finals = {}

    def w(i):
        cur = b""
        for j in range(6):
            cur += lines(200, i * 100 + j)
            stores[i].put([(f"w{i}.jsonl", cur, None)])
            stores[i].put([("shared.jsonl", cur, None)])  # contended path: last writer wins, never corrupt
        finals[i] = cur
    ts = [threading.Thread(target=w, args=(i,)) for i in range(6)]
    [t.start() for t in ts]; [t.join() for t in ts]
    chk = Store(be, cache_dir=str(tmp_path / "chk"), name="chk"); chk.sync()
    for i in range(6):
        assert chk.read(f"w{i}.jsonl", max_bytes=-1) == finals[i]
    assert chk.read("shared.jsonl", max_bytes=-1) in finals.values()
    assert chk.verify(deep=True)["ok"]
