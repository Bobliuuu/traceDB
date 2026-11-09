import os, pytest
from tracedb.store import open_store, BudgetExceeded
from tracedb.blob import put_blob, get_blob, blob_info, sha256_file


def test_blob_roundtrip_dedupe_range_budget(tmp_path):
    st = open_store(str(tmp_path / "s"), cache_dir=str(tmp_path / "c"))
    f = tmp_path / "big.bin"; f.write_bytes(os.urandom(3 << 20) + b"tail-marker")
    g = tmp_path / "copy.bin"; g.write_bytes(f.read_bytes())
    r1 = put_blob(st, "repos/x/big.bin", str(f)); r2 = put_blob(st, "repos/y/copy.bin", str(g))
    assert r1["uploaded"] and not r2["uploaded"] and r1["sha256"] == r2["sha256"] == sha256_file(str(f))  # identical content uploads once
    e = st.locate("repos/x/big.bin")
    assert e["meta"]["blob_size"] == f.stat().st_size and e["size"] < 1000  # pointer is tiny, real size in meta
    assert blob_info(st, "repos/x/big.bin")["size"] == f.stat().st_size
    n = f.stat().st_size
    assert get_blob(st, "repos/x/big.bin", n - 11, n) == b"tail-marker"
    with pytest.raises(BudgetExceeded):
        get_blob(st, "repos/x/big.bin", 0, None, max_bytes=1 << 20)
    assert get_blob(st, "repos/x/big.bin", max_bytes=-1) == f.read_bytes()
    assert blob_info(st, "nope") is None
