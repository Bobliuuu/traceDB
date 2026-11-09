"""Large files ("blobs"): content-addressed objects outside the pack format, with a small pointer entry in the catalog.

Why: packs hold whole files in RAM and are tuned for many small files; a 13 GB model artifact belongs in one object.
    put_blob(store, "repos/x/model.bin", "/local/model.bin")
      -> object  blobs/<sha256>        (create-only, multipart, streamed; identical files upload once)
      -> entry   repos/x/model.bin     (small JSON pointer; meta {"blob": <sha256>, "blob_size": N})
    get_blob(store, "repos/x/model.bin", start, end, max_bytes) -> bytes   (ranged GET; budgeted like every read)
    blob_info(store, path) -> {"sha256","size","key"} or None
The pointer is an ordinary entry, so ls/locate/search-by-path/ontology see the file at its real path and real size (blob_size).
"""
from __future__ import annotations
import hashlib, json, os
from typing import Optional

from .store import BudgetExceeded, DEFAULT_BUDGET, NotFound, Store

KEY = "blobs/{}"


def sha256_file(path: str, chunk: int = 8 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def put_blob(st: Store, path: str, local: str, meta: Optional[dict] = None, sha: Optional[str] = None) -> dict:
    size = os.path.getsize(local)
    sha = sha or sha256_file(local)
    uploaded = st.b.put_file(KEY.format(sha), local)
    ptr = {"format": "tracedb-blob/1", "sha256": sha, "size": size, "key": KEY.format(sha)}
    m = {**(meta or {}), "blob": sha, "blob_size": size}
    r = st.put([(path, json.dumps(ptr, sort_keys=True).encode(), m)])
    return {"path": path, "sha256": sha, "size": size, "uploaded": uploaded, **{k: r[k] for k in ("added",) if k in r}}


def blob_info(st: Store, path: str) -> Optional[dict]:
    e = st.locate(path)
    if not e or "blob" not in (e.get("meta") or {}):
        return None
    return json.loads(st.read(path, max_bytes=1 << 20))


def get_blob(st: Store, path: str, start: int = 0, end: Optional[int] = None, max_bytes: Optional[int] = None) -> bytes:
    info = blob_info(st, path)
    if info is None:
        raise NotFound(path)
    end = info["size"] if end is None else min(end, info["size"])
    budget = DEFAULT_BUDGET if max_bytes is None else max_bytes
    if budget >= 0 and end - start > budget:
        raise BudgetExceeded(_BlobPlan(end - start), budget)
    return st.b.get(info["key"], start, end)


class _BlobPlan:
    def __init__(self, n):
        self.raw_bytes = self.fetch_bytes = n; self.requests = 1

    def as_dict(self):
        return {"raw_bytes": self.raw_bytes, "fetch_bytes": self.fetch_bytes, "requests": 1, "blob": True}
