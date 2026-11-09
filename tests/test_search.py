import json, os
from tracedb.search import SearchIndex


def test_index_and_search(tmp_path):
    p = str(tmp_path / "f.sqlite")
    ix = SearchIndex.create(p)
    ix.add("traces/codex/a.jsonl", b'{"role":"user","content":"why did vllm OOM on the g7e box"}\n{"role":"assistant","content":"lower gpu memory utilization"}\n')
    ix.add("repos/x/README.md", b"# x\nsetup notes for tenpct-g1 access\n")
    ix.add("repos/x/blob.bin", b"\0\1\2")
    ix.close()
    s = SearchIndex.open(p)
    r = s.search("vllm oom g7e")
    assert r and r[0]["path"] == "traces/codex/a.jsonl" and "get traces/codex/a.jsonl --start" in r[0]["cmd"]
    assert s.search("tenpct-g1", prefix="repos/")[0]["path"] == "repos/x/README.md"
    assert s.search("vllm", prefix="repos/") == []
    assert s.search("blob", paths_only=True)[0]["path"] == "repos/x/blob.bin"
    assert s.stats()["files"] == 3


def test_big_file_head_tail(tmp_path):
    ix = SearchIndex.create(str(tmp_path / "g.sqlite"))
    body = b"".join(b"filler line %d\n" % i for i in range(60000)) + b"needle_at_the_end\n"
    ix.add("logs/big.log", body); ix.close()
    assert SearchIndex.open(str(tmp_path / "g.sqlite")).search("needle_at_the_end")


def test_or_fallback(tmp_path):
    ix = SearchIndex.create(str(tmp_path / "h.sqlite"))
    ix.add("a.md", b"alpha beta\n"); ix.add("b.md", b"gamma delta\n"); ix.close()
    r = SearchIndex.open(str(tmp_path / "h.sqlite")).search("alpha zzzz")
    assert r and r[0]["path"] == "a.md"
