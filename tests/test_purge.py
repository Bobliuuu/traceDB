import os
import pytest
from tracedb import Store, LocalBackend, NotFound
from tracedb.gc import gc


@pytest.fixture
def st(tmp_path):
    return Store(LocalBackend(tmp_path / "bucket"), cache_dir=str(tmp_path / "cache"), name="t")


def files(n, prefix):
    return {f"{prefix}f{i}.txt": f"{prefix} file {i} ".encode() * 50 for i in range(n)}


def bucket_bytes(root):
    out = b""
    for dp, _, fs in os.walk(root):
        for f in fs:
            out += open(os.path.join(dp, f), "rb").read()
    return out


def test_dry_run_reports_and_changes_nothing(st):
    st.put((p, b, None) for p, b in {**files(5, "repos/gone/"), **files(5, "repos/keep/")}.items())
    before = st.ls("")
    r = st.purge(["repos/gone/"])
    assert (r["purged"], r["dry_run"]) == (5, True)
    assert st.ls("") == before


def test_purge_removes_entries_and_keeps_the_rest_readable(st, tmp_path):
    gone, keep = files(8, "repos/gone/"), files(8, "repos/keep/")
    st.put((p, b, None) for p, b in {**gone, **keep}.items())        # one put: gone and keep share packs
    r = st.purge(["repos/gone/"], dry_run=False)
    assert r["purged"] == 8 and r["moved"] >= 1, r
    assert [e["path"] for e in st.ls("repos/gone/")] == []
    with pytest.raises(NotFound):
        st.read("repos/gone/f0.txt")
    for p, b in keep.items():
        assert st.read(p) == b
    gc(st, grace_seconds=0, dry_run=False)
    for p, b in keep.items():                                          # still readable after gc
        assert st.read(p) == b
    raw = bucket_bytes(tmp_path / "bucket")
    assert b"repos/gone/" not in raw                                    # no segment names them
    fresh = Store(LocalBackend(tmp_path / "bucket"), cache_dir=str(tmp_path / "cache2"), name="t")
    fresh.sync()
    assert fresh.ls("repos/gone/") == [] and len(fresh.ls("repos/keep/")) == 8


def test_exact_path_and_prefix_boundaries(st):
    st.put([("repos/gone", b"a", None), ("repos/gone-other/x", b"b", None), ("repos/gone/y", b"c", None)])
    st.purge(["repos/gone/"], dry_run=False)
    assert sorted(e["path"] for e in st.ls("")) == ["repos/gone", "repos/gone-other/x"]
    st.purge(["repos/gone"], dry_run=False)
    assert [e["path"] for e in st.ls("")] == ["repos/gone-other/x"]


def test_refuses_empty_or_absolute(st):
    for bad in ([""], ["/repos/"]):
        with pytest.raises(ValueError):
            st.purge(bad)


def test_the_stores_policy_refuses_paths_at_put_and_drives_purge(st, tmp_path):
    from tracedb.policy import Excluded, Policy
    st.put([("repos/private-proj/a.md", b"secret plans", None), ("repos/open/a.md", b"public", None),
            ("traces/cc/-home-x-private-proj/s.jsonl", b"session", None)])
    st.set_policy(Policy([r"^repos/private-proj(/|$)", r"^traces/[^/]+/[^/]*private-proj"]))
    again = Store(LocalBackend(tmp_path / "bucket"), cache_dir=str(tmp_path / "c3"), name="t")   # another writer
    for p in ("repos/private-proj/b.md", "repos/private-proj", "traces/cc/-home-x-private-proj/t.jsonl"):
        with pytest.raises(Excluded):
            again.put([(p, b"x", None)])
    again.put([("repos/private-project-other/x", b"ok", None)])        # the pattern's boundary holds
    r = again.purge([], dry_run=False, by_policy=True)
    assert r["purged"] == 2, r
    assert sorted(e["path"] for e in again.ls("")) == ["repos/open/a.md", "repos/private-project-other/x"]
    with pytest.raises(ValueError):
        again.purge([])                                                # nothing named, no policy flag


def test_purge_index_drops_excluded_text(tmp_path):
    import subprocess, sys, sqlite3
    from tracedb.search import SearchIndex
    (tmp_path / "POLICY.json").write_text('{"exclude": ["^repos/private-proj/"]}')
    old = SearchIndex.create(str(tmp_path / "old.sqlite"))
    old.add("repos/private-proj/AGENTS.md", b"RecoverSSM boundary bug\n")
    old.add("repos/north-star/README.md", b"triton decode executor\n")
    old.close()
    tool = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools", "purge_index.py")
    done = subprocess.run([sys.executable, tool, str(tmp_path / "old.sqlite"), str(tmp_path / "new.sqlite")],
                          capture_output=True, text=True, env={**os.environ, "TRACEDB_POLICY": str(tmp_path / "POLICY.json")})
    assert done.returncode == 0, done.stderr
    new = SearchIndex.open(str(tmp_path / "new.sqlite"))
    assert new.search("RecoverSSM") == [] and new.search("triton")
    assert b"RecoverSSM" not in (tmp_path / "new.sqlite").read_bytes()
