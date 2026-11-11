import json, os
import pytest
from tracedb.store import open_store
from tracedb.index import build_index, update_index, IndexSet, chunks_of, make_snippet, _have_apsw


def mk(tmp_path):
    root = tmp_path / "store"
    st = open_store(str(root), cache_dir=str(tmp_path / "cache"))
    line = lambda t: json.dumps({"role": "user", "content": t}).encode() + b"\n"
    boiler = "SYSTEM PROMPT " + "you are a helpful agent with many rules " * 10
    st.put([
        ("traces/codex/a.jsonl", line(boiler) + line("why did vllm OOM on the g7e box") + line("lower gpu memory utilization"), {}),
        ("traces/codex/b.jsonl", line(boiler) + line("second session about nccl timeouts"), {}),
        ("repos/x/README.md", b"# x\nsetup notes for tenpct-g1 access\n", {}),
        ("repos/x/blob.bin", b"\0\1\2 needle_in_binary", {}),
        ("config/ssh.txt", b"Host tenpct-g2\n  User ubuntu\n", {}),
    ])
    return st, str(tmp_path / "work")


def test_build_search_prefix_and_cmd(tmp_path):
    st, work = mk(tmp_path)
    r = build_index(st, st, jobs=2, work=work)
    assert r["entries"] == 5 and r["gens"] == 2
    st.sync()
    ix = IndexSet.open(st)
    h = ix.search("vllm oom g7e")
    assert h and h[0]["path"] == "traces/codex/a.jsonl" and "get traces/codex/a.jsonl --start" in h[0]["cmd"]
    assert "[vllm]" in h[0]["snippet"].lower() or "[vllm" in h[0]["snippet"].lower()
    assert ix.search("tenpct-g1", prefix="repos/")[0]["path"] == "repos/x/README.md"
    assert ix.search("vllm", prefix="repos/") == []
    assert ix.search("needle_in_binary") == []                      # binary: path-only
    assert ix.search("blob", paths_only=True)[0]["path"] == "repos/x/blob.bin"
    assert ix.search("alpha zzzz nccl")[0]["path"] == "traces/codex/b.jsonl"   # OR fallback


def test_offset_is_exact(tmp_path):
    st, work = mk(tmp_path)
    build_index(st, st, jobs=1, work=work); st.sync()
    h = IndexSet.open(st).search("nccl timeouts")[0]
    raw = st.read(h["path"], h["offset"], h["offset"] + 4096)
    assert b"nccl timeouts" in raw


def test_dedupe_boilerplate(tmp_path):
    boiler = "SYSTEM PROMPT " + "you are a helpful agent with many rules " * 10
    data = b"".join(json.dumps({"c": boiler}).encode() + b"\n" for _ in range(50))
    seen = set()
    n1 = sum(1 for _ in chunks_of("x.jsonl", data, 0, seen))
    n2 = sum(1 for _ in chunks_of("x.jsonl", data, 0, None))
    assert n1 <= 1 < n2


def test_incremental(tmp_path):
    st, work = mk(tmp_path)
    build_index(st, st, jobs=1, work=work)
    assert update_index(st, st, jobs=1, work=work)["mode"] == "noop"
    st.put([("traces/codex/c.jsonl", json.dumps({"content": "brand new zebra topic"}).encode() + b"\n", {}),
            ("repos/x/README.md", b"# x\nrewritten, no more tenpct stuff, now quokka\n", {})])
    r = update_index(st, st, jobs=1, work=work)
    assert r["mode"] == "incremental" and r["new_or_changed"] == 2
    st.sync(); ix = IndexSet.open(st)
    assert ix.search("zebra")[0]["path"] == "traces/codex/c.jsonl"
    assert ix.search("quokka")[0]["path"] == "repos/x/README.md"
    assert ix.search("setup", prefix="repos/") == []            # old content tombstoned
    assert ix.search("tenpct-g2")[0]["path"] == "config/ssh.txt"    # untouched base still served
    assert update_index(st, st, jobs=1, work=work)["mode"] == "noop"


def test_scope_prefix_and_snippet():
    s = make_snippet("alpha beta gamma delta " * 20 + "needle here", ["needle"])
    assert "[needle]" in s


def test_blob_head_indexed(tmp_path):
    from tracedb.blob import put_blob
    st, work = mk(tmp_path)
    f = tmp_path / "big.log"; f.write_bytes(b"line about quokka_blob\n" * 10)
    put_blob(st, "repos/x/big.log", str(f))
    build_index(st, st, jobs=1, work=work); st.sync()
    h = IndexSet.open(st).search("quokka_blob")
    assert h and h[0]["path"] == "repos/x/big.log" and "blob-get" in h[0]["cmd"]


@pytest.mark.skipif(not _have_apsw(), reason="apsw not installed")
def test_lazy_matches_download(tmp_path, monkeypatch):
    st, work = mk(tmp_path)
    build_index(st, st, jobs=1, work=work); st.sync()
    a = IndexSet.open(st).search("vllm oom")
    monkeypatch.setenv("TRACEDB_INDEX_LAZY", "0")
    b = IndexSet.open(st).search("vllm oom")
    assert [x["path"] for x in a] == [x["path"] for x in b] and a[0]["offset"] == b[0]["offset"]
