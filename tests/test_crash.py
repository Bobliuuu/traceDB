"""Crash / fuzz tests: failure injection at every mutating step, random multi-store sequences, corruption."""
import os, random, threading, glob
import pytest
from tracedb import Store, LocalBackend
from tracedb import store as store_mod
from tracedb.store import CorruptBlock


class Injected(Exception):
    pass


class FailingBackend(LocalBackend):
    """Fails the Nth mutating op (put_if_absent / cas_head), either before or after it takes effect."""

    def __init__(self, root, fail_at=None, after=False):
        super().__init__(root)
        self.fail_at, self.after, self.n, self._l = fail_at, after, 0, threading.Lock()
        self.hook = None

    def _step(self):
        with self._l:
            i = self.n
            self.n += 1
        return i == self.fail_at

    def put_if_absent(self, key, data):
        hit = self._step()
        if self.hook:
            self.hook(key)
        if hit and not self.after:
            raise Injected()
        r = super().put_if_absent(key, data)
        if hit:
            raise Injected()
        return r

    def cas_head(self, data, etag):
        hit = self._step()
        if hit and not self.after:
            raise Injected()
        r = super().cas_head(data, etag)
        if hit:
            raise Injected()
        return r


def mkstore(tmp, name, backend=None):
    return Store(backend or LocalBackend(tmp / "bucket"), cache_dir=str(tmp / f"cache-{name}"), name=name, workers=4)


def blob(seed, n=3000):
    return random.Random(seed).randbytes(n)


@pytest.fixture(autouse=True)
def small_packs(monkeypatch):
    monkeypatch.setattr(store_mod, "PACK_TARGET", 8000)  # several packs per put


BASE = {f"base/f{i}": blob(i) for i in range(4)}
NEW = {f"new/g{i}": blob(100 + i) for i in range(6)}


def triples(d):
    return [(p, b, {"m": 1}) for p, b in d.items()]


def check_consistent(tmp, expect_old, new, tag):
    """Fresh reader: HEAD valid, state is exactly old or old+new (atomic), deep verify ok, old data intact."""
    r = mkstore(tmp, "reader-" + tag)
    r.sync()
    paths = {x["path"] for x in r.ls("")}
    assert set(expect_old) <= paths, "acknowledged put lost"
    extra = paths - set(expect_old)
    assert extra in (set(), set(new)), f"partial put visible: {extra}"
    for p in paths:
        assert r.read(p) == (expect_old.get(p) or new[p])
    assert r.verify(deep=True)["ok"]
    return extra == set(new), r


def test_put_failure_at_every_step(tmp_path_factory):
    probe = tmp_path_factory.mktemp("probe")
    b = FailingBackend(probe / "bucket")
    s = mkstore(probe, "w", b)
    s.put(triples(BASE)); b.n = 0
    s.put(triples(NEW))
    total = b.n
    assert total >= 4  # >1 pack + segment + CAS
    for after in (False, True):
        for k in range(total):
            tmp = tmp_path_factory.mktemp(f"p{k}{int(after)}")
            b = FailingBackend(tmp / "bucket")
            w = mkstore(tmp, "w", b)
            w.put(triples(BASE))
            b.n, b.fail_at, b.after = 0, k, after
            with pytest.raises(Injected):
                w.put(triples(NEW))
            b.fail_at = None
            committed, _ = check_consistent(tmp, BASE, NEW, f"a{k}")
            if not after:
                assert not committed  # failure before the op never commits
            # retry converges (fresh writer), idempotent, and the original instance recovers too
            w2 = mkstore(tmp, "w2")
            w2.put(triples(NEW))
            committed, _ = check_consistent(tmp, BASE, NEW, f"b{k}")
            assert committed
            assert w2.put(triples(NEW))["added"] == 0
            w.put(triples(NEW))
            assert all(w.read(p) == v for p, v in {**BASE, **NEW}.items())


def test_compact_failure_at_every_step(tmp_path_factory):
    allf = {f"s{i}/x{j}": blob(i * 10 + j) for i in range(3) for j in range(3)}

    def setup(tmp, b):
        w = mkstore(tmp, "w", b)
        for i in range(3):
            w.put(triples({f"s{i}/x{j}": blob(i * 10 + j) for j in range(3)}))
        return w
    probe = tmp_path_factory.mktemp("cp")
    b = FailingBackend(probe / "bucket"); w = setup(probe, b); b.n = 0
    w.compact(); total = b.n
    assert total == 2
    for after in (False, True):
        for k in range(total):
            tmp = tmp_path_factory.mktemp(f"c{k}{int(after)}")
            b = FailingBackend(tmp / "bucket"); w = setup(tmp, b)
            b.n, b.fail_at, b.after = 0, k, after
            with pytest.raises(Injected):
                w.compact()
            b.fail_at = None
            _, r = check_consistent(tmp, allf, {}, f"c{k}")
            assert {x["path"] for x in r.ls("")} == set(allf)
            w.compact()
            assert mkstore(tmp, "z").verify(deep=True)["ok"]


def test_compact_does_not_drop_concurrent_put(tmp_path):
    """Regression: compact used a fresh HEAD etag, so a segment appended after its sync was lost."""
    b = FailingBackend(tmp_path / "bucket")
    a = mkstore(tmp_path, "a", b)
    a.put(triples({"a/1": blob(1)})); a.put(triples({"a/2": blob(2)}))
    other = mkstore(tmp_path, "o")
    fired = []

    def hook(key):
        if key.startswith("segments/") and not fired:
            fired.append(1)
            other.put(triples({"o/late": blob(9)}))
    b.hook = hook
    a.compact()
    b.hook = None
    r = mkstore(tmp_path, "r"); r.sync()
    assert r.read("o/late") == blob(9) and r.read("a/1") == blob(1)
    assert r.verify(deep=True)["ok"]


def test_random_multi_store_vs_model(tmp_path_factory):
    for seed in range(220):
        rnd = random.Random(seed)
        tmp = tmp_path_factory.mktemp(f"m{seed}")
        stores = [mkstore(tmp, f"s{i}") for i in range(3)]
        model, history = {}, {}
        paths = [f"d{i % 3}/f{i}" for i in range(8)]
        for step in range(rnd.randint(6, 14)):
            s = rnd.choice(stores)
            op = rnd.choices(["put", "read", "compact", "sync", "stale"], [5, 3, 1, 1, 2])[0]
            if op == "put":
                batch = {rnd.choice(paths): rnd.randbytes(rnd.choice([0, 10, 700, 5000])) for _ in range(rnd.randint(1, 4))}
                s.put([(p, d, None) for p, d in batch.items()])
                for p, d in batch.items():
                    model[p] = d; history.setdefault(p, set()).add(d)
            elif op == "read":
                s.sync()
                p = rnd.choice(paths)
                if p in model:
                    assert s.read(p) == model[p]
                else:
                    assert s.locate(p) is None
            elif op == "stale":  # no sync: must be some version that was acknowledged
                p = rnd.choice(paths)
                if s.locate(p):
                    assert s.read(p) in history[p]
            elif op == "compact":
                s.compact()
            else:
                s.sync()
        for s in stores:
            s.sync()
            assert {x["path"] for x in s.ls("")} == set(model)
            for p, d in model.items():
                assert s.read(p) == d
        assert stores[0].verify(deep=True)["ok"]


def _pack_files(tmp):
    return sorted(glob.glob(str(tmp / "bucket" / "packs" / "*.pack")))


@pytest.mark.parametrize("compressible", [False, True])
def test_corrupt_pack_detected(tmp_path, compressible):
    data = (b"hello trace line\n" * 500) if compressible else os.urandom(5000)
    w = mkstore(tmp_path, "w")
    w.put([("f", data, None), ("g", os.urandom(2000), None)])
    pk = _pack_files(tmp_path)[0]
    raw = bytearray(open(pk, "rb").read())
    e = w.locate("f")
    raw[e["off"] + e["clen"] // 2] ^= 0xFF
    open(pk, "wb").write(raw)
    r = mkstore(tmp_path, "r"); r.sync()
    v = r.verify(deep=True)
    assert not v["ok"] and "f" in v["bad_content"]
    with pytest.raises(CorruptBlock):
        r.read("f")
    assert r.read("g") is not None


def test_deep_verify_separates_transient_errors_from_corruption(tmp_path):
    """Regression (S3 run): a transient network error must be retried / reported as an error, never as bad_content."""
    from tracedb import Store, LocalBackend
    be = LocalBackend(tmp_path / "b"); st = Store(be, cache_dir=str(tmp_path / "c"), name="x")
    st.put([(f"f{i}", os.urandom(3000), None) for i in range(5)])
    real = be.get; calls = {"n": 0}

    def flaky(key, start=None, end=None):
        if key.startswith("packs/"):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionResetError("blip")
        return real(key, start, end)
    be.get = flaky
    shutil_rm = __import__("shutil").rmtree; shutil_rm(os.path.join(st.dir, "blocks"), ignore_errors=True); os.makedirs(os.path.join(st.dir, "blocks"))
    r = st.verify(deep=True)
    assert r["ok"] and r["bad_content"] == [] and r["errors"] == []  # one blip retried away

    def dead(key, start=None, end=None):
        if key.startswith("packs/"):
            raise ConnectionResetError("down")
        return real(key, start, end)
    be.get = dead; shutil_rm(os.path.join(st.dir, "blocks"), ignore_errors=True); os.makedirs(os.path.join(st.dir, "blocks"))
    r = st.verify(deep=True)
    assert not r["ok"] and r["bad_content"] == [] and len(r["errors"]) == 5  # reported as errors, not corruption


def test_one_store_shared_by_many_threads(tmp_path):
    """Regression: concurrent reads/lookups from many threads on ONE Store used to abort the process (sqlite)."""
    from concurrent.futures import ThreadPoolExecutor
    be = LocalBackend(tmp_path / "b"); st = Store(be, cache_dir=str(tmp_path / "c"), name="x")
    data = {f"d{i % 7}/f{i}": os.urandom(2000 + i) for i in range(300)}
    st.put([(p, b, None) for p, b in data.items()])
    paths = list(data)

    def work(i):
        p = paths[(i * 37) % len(paths)]
        assert st.locate(p)["size"] == len(data[p])
        assert st.read(p) == data[p]
        assert st.ls("d1/", limit=5)
        return True
    with ThreadPoolExecutor(32) as ex:
        assert all(ex.map(work, range(2000)))
