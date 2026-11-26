import json, os
import pytest
from tracedb.store import open_store
from tracedb.index import build_index, update_index, merge_deltas, IndexSet, chunks_of, make_snippet, _have_apsw


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


def test_identical_chunks_stay_findable(tmp_path):
    from tracedb.store import open_store
    st = open_store(str(tmp_path / "s"), cache_dir=str(tmp_path / "c"))
    body = b"shared notes about quokka rewilding " * 8 + b"\n"
    st.put([("a/one.md", body, {}), ("b/two.md", body, {})])
    build_index(st, st, jobs=1, work=str(tmp_path / "w")); st.sync()
    ix = IndexSet.open(st)
    assert {h["path"] for h in ix.search("quokka")} == {"a/one.md", "b/two.md"}
    assert [h["path"] for h in ix.search("quokka", prefix="b/")] == ["b/two.md"]


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


def test_limit_per_file_and_or_fill(tmp_path):
    root = tmp_path / "store"
    st = open_store(str(root), cache_dir=str(tmp_path / "cache"))
    line = lambda t: json.dumps({"role": "user", "content": t}).encode() + b"\n"
    # one big file with many distinct matching chunks + many small files
    big = b"".join(line(f"zebra stripes variant {i} " + "filler word " * 200 + f"uniq{i}") for i in range(60))
    files = [("traces/big.jsonl", big, {})]
    files += [(f"traces/s{i}.jsonl", line(f"zebra herd number {i} " + "other words " * 5 + f"u{i}"), {}) for i in range(40)]
    files += [("traces/only_apple.jsonl", line("apple orchard notes"), {})]
    st.put(files)
    build_index(st, st, jobs=2, work=str(tmp_path / "work"))
    st.sync()
    ix = IndexSet.open(st)
    # limit honoured beyond the old 10 / one big file can't starve the rest
    r = ix.search("zebra", limit=45, snippets=False)
    assert len(r) == 41 and len({h["path"] for h in r}) == 41      # 1 per file by default
    # per_file lets one file contribute several chunks (distinct offsets)
    r = ix.search("zebra", limit=50, per_file=5, snippets=False)
    big_hits = [h for h in r if h["path"] == "traces/big.jsonl"]
    assert len(big_hits) == 5 and len({h["offset"] for h in big_hits}) == 5
    # limit 0 = max; AND hits come first, OR fills the remainder
    r = ix.search("zebra apple", limit=0, snippets=False)
    assert len(r) >= 42
    r = ix.search("zebra stripes", limit=3, snippets=False)
    assert r[0]["path"] == "traces/big.jsonl"


def test_changed_file_in_first_shard_stays_findable(tmp_path):
    """An update split into shards used to put its tombstones on the last shard, which hid a changed file re-indexed
    into an earlier shard of the same update."""
    st, work = mk(tmp_path)
    build_index(st, st, jobs=1, work=work)
    st.put([("config/ssh.txt", b"Host tenpct-g2\n  User walrus\n", {}),                      # sorts first: shard 0
            ("traces/codex/b.jsonl", json.dumps({"content": "nccl timeouts fixed by narwhal"}).encode() + b"\n", {})])
    update_index(st, st, jobs=2, work=work)
    st.sync(); ix = IndexSet.open(st)
    assert [h["path"] for h in ix.search("walrus")] == ["config/ssh.txt"]
    assert [h["path"] for h in ix.search("narwhal")] == ["traces/codex/b.jsonl"]
    assert ix.search("ubuntu") == []                                   # the old copy is tombstoned


def test_merge_deltas(tmp_path):
    st, work = mk(tmp_path)
    build_index(st, st, jobs=1, work=work)
    st.put([("traces/codex/c.jsonl", json.dumps({"content": "brand new zebra topic"}).encode() + b"\n", {})])
    update_index(st, st, jobs=1, work=work)
    st.put([("repos/x/README.md", b"# x\nrewritten, now quokka\n", {})])
    update_index(st, st, jobs=1, work=work)
    st.put([("traces/codex/c.jsonl", json.dumps({"content": "zebra topic, second take with okapi"}).encode() + b"\n", {})])
    update_index(st, st, jobs=1, work=work)
    st.sync(); assert len(IndexSet.open(st).man["gens"]) == 4
    r = merge_deltas(st, st, jobs=2, work=work)
    assert r["mode"] == "merged" and r["deltas_before"] == 3
    st.sync(); ix = IndexSet.open(st)
    assert len(ix.man["gens"]) <= 3
    assert ix.search("okapi")[0]["path"] == "traces/codex/c.jsonl"
    assert ix.search("quokka")[0]["path"] == "repos/x/README.md"
    assert ix.search("setup", prefix="repos/") == []                  # base copy still tombstoned
    assert ix.search("tenpct-g2")[0]["path"] == "config/ssh.txt"      # base still served
    assert ix.search("nccl")[0]["path"] == "traces/codex/b.jsonl"
    assert update_index(st, st, jobs=1, work=work)["mode"] == "noop"  # merged state matches the catalog
