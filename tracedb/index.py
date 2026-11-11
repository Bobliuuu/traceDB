"""v3 search index: sharded, incremental, contentless FTS5, readable lazily over S3.

Why (measured on a 208k-entry / 87 GB store, see bench/results/opt-*.txt): the v1 index (search.py) is ONE 4 GB sqlite
file that every new machine had to download whole, and it was rebuilt from scratch. v3 changes four things:

* contentless FTS5 (content=''): only the inverted index is stored, not a second copy of every chunk.
  Snippets are produced at query time by re-reading the hit's bytes from the store (path + offset are exact).
* repeated text (system prompts / tool boilerplate repeated in every trace record, identical chunks) is indexed once.
* the index is a set of *generations* (shards) in the store under index/v3/: MANIFEST.json lists them with the path range
  each covers, so `search --prefix traces/codex/` opens only the generations that can contain it, and `update` adds a small
  delta generation (new/changed files + tombstones for removed ones) instead of rebuilding everything.
* a generation is read lazily with range reads (an apsw VFS over Store.read: only the 1 MiB blocks sqlite touches are
  fetched). Without apsw it falls back to a bounded-memory download of just the needed generations.

    build_index(src_store, dest_store, prefixes=[""], jobs=4)    # full build of the covered prefixes
    update_index(src_store, dest_store, ...)                      # incremental: only new/changed/removed entries
    IndexSet.open(dest_store, data_store).search("vllm oom", prefix="traces/")
Index covers every catalog entry: text files by content (head+tail capped), everything else (incl. blobs) by path.
Stdlib only; apsw is optional.
"""
from __future__ import annotations
import hashlib, json, os, re, sqlite3, threading, time, uuid, zlib
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor

from .search import TEXT_EXT, HEAD_CAP, TAIL_CAP, CHUNK
from .store import Store, open_store
from . import codec as C

PREFIX_KEY = "index/v3/"
MANIFEST = PREFIX_KEY + "MANIFEST.json"
DEDUP_MIN = 160          # strings/chunks shorter than this are never deduplicated
BLOB_HEAD = 64 << 10     # text blobs: index only this much of the head
SNIP_FETCH = 256 << 10   # max raw bytes re-read per hit to build a snippet
BATCH_RAW = 48 << 20     # raw bytes per read batch while building
_TOK = "unicode61 remove_diacritics 2 tokenchars '_'"

SCHEMA = f"""
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT UNIQUE, size INTEGER, sha TEXT, note TEXT, first INTEGER, last INTEGER, nchunks INTEGER);
CREATE TABLE cm(id INTEGER PRIMARY KEY, fid INTEGER, off INTEGER, rl INTEGER);
CREATE TABLE dups(orig INTEGER, fid INTEGER, off INTEGER, rl INTEGER);
CREATE INDEX dups_orig ON dups(orig);
CREATE VIRTUAL TABLE chunks USING fts5(text, content='', detail=full, tokenize="{_TOK}");
CREATE VIRTUAL TABLE paths USING fts5(path, content='', tokenize="unicode61 remove_diacritics 2");
"""


# ---------------------------------------------------------------- chunking (with dedupe)
def _dup(s: str, seen) -> bool:
    if seen is None or len(s) < DEDUP_MIN:
        return False
    h = hash(s[:4000])
    if h in seen:
        return True
    seen.add(h)
    return False


def _flat(o, out, seen, depth=0):
    if depth > 8:
        return
    if isinstance(o, str):
        if len(o) >= 3 and not _dup(o, seen):
            out.append(o[:4000])
    elif isinstance(o, dict):
        for k, v in o.items():
            if isinstance(v, (dict, list)):
                _flat(v, out, seen, depth + 1)
            elif isinstance(v, str) and len(v) >= 3:
                if _dup(v, seen):
                    continue
                out.append(f"{k}: {v[:4000]}" if len(k) <= 24 and len(v) < 200 else v[:4000])
            elif isinstance(v, (int, float)) and not isinstance(v, bool) and len(k) <= 40:
                out.append(f"{k}={v}")
    elif isinstance(o, list):
        for v in o[:200]:
            _flat(v, out, seen, depth + 1)


def chunks_of(path: str, data: bytes, base: int, seen=None, dedupe_chunks=True):
    """Yield (byte_offset, text, raw_len, hash): ~CHUNK-char line-aligned chunks; raw_len = bytes of the stored file the chunk spans;
    hash identifies identical chunks (the writer indexes the text once and records the other places in `dups`).
    JSONL is indexed on text-bearing values. `seen` (a set) enables dedupe of repeated long values inside records."""
    def emit(boff, end, parts):
        t = "\n".join(parts)[: CHUNK * 4]
        return boff, t, end - boff, (hash(t) if dedupe_chunks and len(t) >= DEDUP_MIN else None)
    if path.endswith(".jsonl"):
        parts, boff, blen, off, last_end = [], None, 0, base, base
        for line in data.split(b"\n"):
            n = len(line) + 1
            if line.strip():
                try:
                    p: list = []
                    _flat(json.loads(line), p, seen)
                    t = "\n".join(p)
                except Exception:
                    t = line.decode("utf8", "replace")[:4000]
                if t:
                    if boff is None:
                        boff = off
                    parts.append(t); blen += len(t); last_end = off + n
                    if blen >= CHUNK:
                        r = emit(boff, last_end, parts)
                        if r:
                            yield r
                        parts, boff, blen = [], None, 0
            off += n
        if parts:
            r = emit(boff, last_end, parts)
            if r:
                yield r
        return
    off = base
    parts, boff, blen = [], base, 0
    for line in data.split(b"\n"):
        n = len(line) + 1
        s = line.decode("utf8", "replace")[:2000]
        if blen + len(s) > CHUNK and parts:
            r = emit(boff, off, parts)
            if r:
                yield r
            parts, boff, blen = [], off, 0
        parts.append(s); blen += len(s) + 1
        off += n
    if parts:
        r = emit(boff, min(off, base + len(data)), parts)
        if r:
            yield r


def is_text(path: str, head: bytes) -> bool:
    return os.path.splitext(path)[1].lower() in TEXT_EXT and b"\0" not in head[:2048]


def text_ext(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in TEXT_EXT


# ---------------------------------------------------------------- generation writer
class GenWriter:
    def __init__(self, path: str):
        if os.path.exists(path):
            os.remove(path)
        self.path = path
        self.c = sqlite3.connect(path, isolation_level=None)
        self.c.execute("PRAGMA page_size=4096"); self.c.execute("PRAGMA journal_mode=OFF"); self.c.execute("PRAGMA synchronous=OFF")
        self.c.execute("PRAGMA cache_size=-262144")
        self.c.executescript(SCHEMA)
        self.c.execute("BEGIN")
        self.fid = 0
        self.rid = 0
        self.seen: set = set()
        self.chunk_rows: dict = {}
        self.ndups = 0
        self.nfiles = self.nchunks = 0

    def add_file(self, path, size, sha, note, chunks=()):
        self.fid += 1
        first = self.rid + 1
        n = 0
        for off, text, rl, h in chunks:
            if h is not None:
                o = self.chunk_rows.get(h)
                if o is not None:  # identical chunk already indexed: remember where else it occurs
                    self.c.execute("INSERT INTO dups VALUES (?,?,?,?)", (o, self.fid, off, rl)); self.ndups += 1
                    continue
            self.rid += 1
            if h is not None:
                self.chunk_rows[h] = self.rid
            self.c.execute("INSERT INTO chunks(rowid,text) VALUES (?,?)", (self.rid, text))
            self.c.execute("INSERT INTO cm VALUES (?,?,?,?)", (self.rid, self.fid, off, rl))
            n += 1
        self.c.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?,?)",
                       (self.fid, path, size, sha, note, first if n else None, self.rid if n else None, n))
        self.c.execute("INSERT INTO paths(rowid,path) VALUES (?,?)", (self.fid, path))
        self.nfiles += 1; self.nchunks += n
        if self.nfiles % 2000 == 0:
            self.c.execute("COMMIT"); self.c.execute("BEGIN")
        return n

    def close(self, meta: dict):
        for k, v in meta.items():
            self.c.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, json.dumps(v)))
        self.c.execute("COMMIT")
        self.c.execute("INSERT INTO chunks(chunks) VALUES('optimize')")
        self.c.execute("INSERT INTO paths(paths) VALUES('optimize')")
        self.c.execute("VACUUM")
        self.c.close()


def catalog_rows(st: Store, prefixes=("",)) -> list[tuple]:
    """(path, sha, size, meta_json) of every entry under the prefixes, sorted by path, excluding the index itself."""
    out = {}
    for pf in prefixes:
        hi = pf + "\U0010ffff"
        for p, h, n, m in st.db.execute("SELECT path,sha,size,meta FROM entries WHERE path>=? AND path<? ORDER BY path", (pf, hi)):
            if not p.startswith(PREFIX_KEY) and not p.startswith("index/fts"):
                out[p] = (p, h, n, m)
    return [out[k] for k in sorted(out)]


def digest(rows) -> str:
    h = hashlib.sha256()
    for p, sha, *_ in rows:
        h.update(p.encode() + b"\0" + sha.encode() + b"\n")
    return h.hexdigest()


def _read_text_parts(st: Store, rows: list, head_cap: int, tail_cap: int):
    """For text-ext rows: {path: (head, tail, tail_start, is_blob)}; reads only head+tail ranges. Blob pointers: the blob head."""
    reqs, kinds = [], {}
    for p, sha, size, m in rows:
        meta = json.loads(m) if isinstance(m, str) else m
        if not text_ext(p) or size == 0:
            continue
        if meta.get("blob"):
            kinds[p] = ("blob", meta.get("blob_size", 0)); continue
        if size <= head_cap + tail_cap:
            reqs.append((p, 0, size)); kinds[p] = ("whole", size)
        else:
            reqs.append((p, 0, head_cap)); reqs.append((p, size - tail_cap, size)); kinds[p] = ("split", size)
    got = st.read_many(reqs, max_bytes=-1) if reqs else {}
    out = {}
    for p, (k, size) in kinds.items():
        if k == "whole":
            out[p] = (got[(p, 0, size)], b"", 0, False)
        elif k == "split":
            head = got[(p, 0, head_cap)]
            nl = head.rfind(b"\n")
            head = head[: nl + 1] if nl > 0 else head
            t = got[(p, size - tail_cap, size)]
            ts = size - tail_cap
            nl = t.find(b"\n")
            if nl != -1:
                t, ts = t[nl + 1:], ts + nl + 1
            out[p] = (head, t, ts, False)
        else:
            try:
                from .blob import get_blob
                out[p] = (get_blob(st, p, 0, min(size, BLOB_HEAD), max_bytes=-1), b"", 0, True)
            except Exception:
                pass
    return out


def build_shard(args) -> dict:
    """Build one generation file from `rows` (already sorted). Runs in a worker process."""
    spec, cache_root, rows, out = args
    st = open_store(spec, cache_dir=cache_root) if cache_root else open_store(spec)
    st.sync_if_stale(ttl=3600)
    w = GenWriter(out)
    t0 = time.time()
    batches, cur, raw = [], [], 0
    for r in rows:
        cost = min(r[2], HEAD_CAP + TAIL_CAP) if text_ext(r[0]) else 0
        if cur and raw + cost > BATCH_RAW:
            batches.append(cur); cur, raw = [], 0
        cur.append(r); raw += cost
    if cur:
        batches.append(cur)
    ex = ThreadPoolExecutor(1)
    nxt = ex.submit(_read_text_parts, st, batches[0], HEAD_CAP, TAIL_CAP) if batches else None
    for i, b in enumerate(batches):
        try:
            parts = nxt.result()
        except Exception:
            parts = {}
        nxt = ex.submit(_read_text_parts, st, batches[i + 1], HEAD_CAP, TAIL_CAP) if i + 1 < len(batches) else None
        for p, sha, size, m in b:
            d = parts.get(p)
            if d is None:
                w.add_file(p, size, sha, "path-only")
                continue
            head, tail, ts, isblob = d
            if not is_text(p, head):
                w.add_file(p, size, sha, "non-text"); continue

            def gen(p=p, head=head, tail=tail, ts=ts):
                yield from chunks_of(p, head, 0, w.seen)
                if tail:
                    yield from chunks_of(p, tail, ts, w.seen)
            w.add_file(p, size, sha, "blob-head" if isblob else ("head+tail" if tail else "full"), gen())
    ex.shutdown()
    meta = {"lo": rows[0][0] if rows else "", "hi": rows[-1][0] if rows else "", "built": time.time()}
    w.close(meta)
    return {"file": out, "files": w.nfiles, "chunks": w.nchunks, "dups": w.ndups, "bytes": os.path.getsize(out),
            "secs": round(time.time() - t0, 1), "lo": meta["lo"], "hi": meta["hi"]}


def _shards(rows, n: int):
    """Split sorted rows into n contiguous parts of roughly equal indexing cost."""
    if n <= 1 or len(rows) < 2:
        return [rows] if rows else []
    cost = [1 + (min(r[2], HEAD_CAP + TAIL_CAP) if text_ext(r[0]) else 0) // 64 for r in rows]
    tot, acc, out, cur = sum(cost), 0, [], []
    for r, c in zip(rows, cost):
        cur.append(r); acc += c
        if acc >= tot / n and len(out) < n - 1:
            out.append(cur); cur, acc = [], 0
    if cur:
        out.append(cur)
    return out


def _load_manifest(dest: Store):
    dest.sync()
    if not dest.locate(MANIFEST):
        return None
    return json.loads(dest.read(MANIFEST, max_bytes=1 << 26))


def _load_state(dest: Store, gen: dict) -> dict:
    d = zlib.decompress(dest.read(gen["files_key"], max_bytes=-1)).decode()
    return dict(l.split("\t") for l in d.splitlines() if l)


def _stage_dir(work):
    d = work or os.path.join(os.path.expanduser("~/.cache/tracedb"), "index-build")
    os.makedirs(d, exist_ok=True)
    return d


def _run_build(spec, cache_root, rows, jobs, work, tag):
    parts = _shards(rows, jobs)
    argl = [(spec, cache_root, p, os.path.join(work, f"{tag}-{i}.sqlite")) for i, p in enumerate(parts)]
    if len(argl) == 1:
        return [build_shard(argl[0])]
    with ProcessPoolExecutor(len(argl)) as ex:
        return list(ex.map(build_shard, argl))


def _publish(dest: Store, res: list, state: dict, tag: str, kind: str, dead: list, manifest, scope, cat_digest):
    gens = list((manifest or {}).get("gens", [])) if kind == "delta" else []
    for r in res:
        sha = hashlib.sha256(open(r["file"], "rb").read()).hexdigest()[:12]
        key = f"{PREFIX_KEY}g-{tag}-{sha}.sqlite"
        fkey = key[:-len(".sqlite")] + ".files"
        sub = {p: s for p, s in state.items() if r["lo"] <= p <= r["hi"]} if kind == "base" else state
        blob = zlib.compress("".join(f"{p}\t{s}\n" for p, s in sorted(sub.items())).encode(), 6)
        dest.put([(key, r["file"], {"kind": "index-gen"}), (fkey, blob, {"kind": "index-state"})])
        gens.append({"key": key, "files_key": fkey, "kind": kind, "lo": r["lo"], "hi": r["hi"], "files": r["files"],
                     "chunks": r["chunks"], "bytes": r["bytes"], "dead": []})
        os.remove(r["file"])
    if dead:
        gens[-1]["dead"] = sorted(set(gens[-1].get("dead", [])) | set(dead))
    man = {"v": 3, "built": time.time(), "scope": list(scope), "catalog_digest": cat_digest, "gens": gens}
    dest.put([(MANIFEST, json.dumps(man).encode(), {"kind": "index-manifest"})])
    return man


def build_index(src: Store, dest: Store | None = None, prefixes=("",), jobs: int = 4, work=None) -> dict:
    """Full build of the covered prefixes: new base generations replace the manifest (old gens become orphan entries)."""
    dest = dest or src
    src.sync()
    rows = catalog_rows(src, prefixes)
    t0 = time.time()
    work = _stage_dir(work)
    tag = "b%d" % int(time.time())
    res = _run_build(src.spec, os.path.dirname(src.dir), rows, jobs, work, tag)
    state = {p: s for p, s, *_ in rows}
    _publish(dest, res, state, tag, "base", [], None, prefixes, digest(rows))
    return {"mode": "full", "entries": len(rows), "gens": len(res), "chunks": sum(r["chunks"] for r in res),
            "index_bytes": sum(r["bytes"] for r in res), "secs": round(time.time() - t0, 1)}


def update_index(src: Store, dest: Store | None = None, prefixes=("",), jobs: int = 2, work=None) -> dict:
    """Incremental: index only entries new/changed since the manifest, tombstone removed/changed ones. Falls back to full."""
    dest = dest or src
    src.sync()
    man = _load_manifest(dest)
    if man is None:
        return build_index(src, dest, prefixes, jobs, work)
    rows = catalog_rows(src, man["scope"] or prefixes)
    cd = digest(rows)
    if cd == man["catalog_digest"]:
        return {"mode": "noop", "entries": len(rows), "gens": len(man["gens"])}
    state: dict = {}
    for g in man["gens"]:  # oldest first: a gen's tombstones remove older entries, then its own files are added
        for p in g.get("dead", ()):
            state.pop(p, None)
        state.update(_load_state(dest, g))
    cur = {p: s for p, s, *_ in rows}
    gone = {p for p in state if p not in cur}
    changed = {p for p in cur if p in state and state[p] != cur[p]}
    todo = [r for r in rows if r[0] not in state or state[r[0]] != r[1]]
    work = _stage_dir(work)
    t0 = time.time()
    tag = "d%d" % int(time.time())
    res = _run_build(src.spec, os.path.dirname(src.dir), todo, jobs, work, tag) if todo else []
    if not res:  # only removals: publish an empty delta so the tombstones live somewhere
        ep = os.path.join(work, tag + "-e.sqlite")
        w = GenWriter(ep); w.close({"lo": "", "hi": ""})
        res = [{"file": ep, "files": 0, "chunks": 0, "bytes": os.path.getsize(ep), "lo": "", "hi": ""}]
    _publish(dest, res, {p: cur[p] for p, *_ in todo}, tag, "delta", sorted(gone | changed), man, man["scope"], cd)
    return {"mode": "incremental", "new_or_changed": len(todo), "removed": len(gone), "gens": len(man["gens"]) + len(res),
            "chunks": sum(r["chunks"] for r in res), "secs": round(time.time() - t0, 1)}


# ---------------------------------------------------------------- reading
class _Lazy:
    """Read-only sqlite file over Store.read: only the touched 1 MiB blocks are fetched (and disk-cached by Store)."""
    BS = C.BLOCK

    def __init__(self, st: Store, key: str, nblocks_cache: int = 48):
        self.st, self.key = st, key
        self.size = st.locate(key)["size"]
        self.cache: OrderedDict = OrderedDict()
        self.lock = threading.Lock()
        self.n = nblocks_cache
        self.fetches = 0

    def block(self, i: int) -> bytes:
        with self.lock:
            b = self.cache.get(i)
            if b is not None:
                self.cache.move_to_end(i)
                return b
        a = i * self.BS
        b = self.st.read(self.key, a, min(a + self.BS, self.size), max_bytes=-1)
        with self.lock:
            self.fetches += 1
            self.cache[i] = b
            while len(self.cache) > self.n:
                self.cache.popitem(last=False)
        return b

    def read(self, amount: int, offset: int) -> bytes:
        end = min(offset + amount, self.size)
        out, pos = [], offset
        while pos < end:
            i = pos // self.BS
            b = self.block(i)
            s = pos - i * self.BS
            e = min(end - i * self.BS, len(b))
            out.append(b[s:e]); pos += e - s
        return b"".join(out)


def _open_lazy_conn(st: Store, key: str):
    import apsw
    lz = _Lazy(st, key)
    name = "tdb_" + uuid.uuid4().hex[:8]

    class F:
        def xRead(s, amount, offset): return lz.read(amount, offset)
        def xFileSize(s): return lz.size
        def xClose(s): pass
        def xLock(s, level): pass
        def xUnlock(s, level): pass
        def xCheckReservedLock(s): return False
        def xFileControl(s, op, ptr): return False
        def xSectorSize(s): return 4096
        def xDeviceCharacteristics(s): return 0
        def xSync(s, flags): pass
        def xWrite(s, data, offset): raise IOError("read-only")
        def xTruncate(s, size): raise IOError("read-only")

    class V(apsw.VFS):
        def __init__(s): super().__init__(name, base="")
        def xAccess(s, pathname, flags): return False
        def xFullPathname(s, n): return n
        def xOpen(s, n, flags): return F()
    v = V()
    c = apsw.Connection(key, flags=apsw.SQLITE_OPEN_READONLY, vfs=name)
    c.cursor().execute("PRAGMA temp_store=MEMORY")
    lz.vfs = v  # keep the VFS alive as long as the connection
    return c, lz


def _have_apsw() -> bool:
    if os.environ.get("TRACEDB_INDEX_LAZY", "1") == "0":
        return False
    try:
        import apsw  # noqa
        return True
    except ImportError:
        return False


def _open_gen(st: Store, key: str):
    """-> (connection, lazy-or-None). Lazy via apsw if available, else bounded-memory download cached by content hash."""
    if _have_apsw():
        return _open_lazy_conn(st, key)
    e = st.locate(key)
    local = os.path.join(st.dir, "idx-" + e["sha"][:16] + ".sqlite")
    if not os.path.exists(local):
        st.read_to_file(key, local + ".tmp")
        os.replace(local + ".tmp", local)
    return sqlite3.connect(f"file:{local}?mode=ro", uri=True), None


def _terms(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9_]+", text)


def _q(text: str) -> str:
    if re.search(r'["*]|\b(AND|OR|NOT|NEAR)\b', text):
        return text
    ws = _terms(text)
    if not ws:
        return '""'
    return " ".join(f'"{w}"' for w in ws[:-1]) + (" " if len(ws) > 1 else "") + f'"{ws[-1]}"*'


def make_snippet(text: str, terms: list[str], width: int = 160) -> str:
    low = text.lower()
    pos = -1
    for t in terms:
        i = low.find(t.lower())
        if i != -1 and (pos == -1 or i < pos):
            pos = i
    if pos == -1:
        return text[:width].replace("\n", " ⏎ ")
    a = max(0, pos - width // 3)
    s = text[a:a + width]
    for t in sorted(set(terms), key=len, reverse=True):
        s = re.sub(re.escape(t), lambda m: "[" + m.group(0) + "]", s, count=3, flags=re.I)
    return (("… " if a else "") + s + (" …" if a + width < len(text) else "")).replace("\n", " ⏎ ")


class IndexSet:
    def __init__(self, manifest: dict, index_store: Store, data_store: Store):
        self.man, self.ist, self.dst = manifest, index_store, data_store
        self._conns: dict = {}

    @classmethod
    def open(cls, index_store: Store, data_store: Store | None = None) -> "IndexSet | None":
        man = _load_manifest_cached(index_store)
        return cls(man, index_store, data_store or index_store) if man else None

    @property
    def gens_opened(self) -> int:
        return len(self._conns)

    @property
    def block_fetches(self) -> int:
        return sum(c[1].fetches for c in self._conns.values() if c[1])

    def _conn(self, g):
        c = self._conns.get(g["key"])
        if c is None:
            c = self._conns[g["key"]] = _open_gen(self.ist, g["key"])
        return c[0]

    def _gens_for(self, prefix: str):
        hi = prefix + "\U0010ffff"
        return [(i, g) for i, g in enumerate(self.man["gens"]) if g["files"] and g["lo"] < hi and g["hi"] >= prefix]

    def _dead_after(self, i: int) -> set:
        s: set = set()
        for g in self.man["gens"][i + 1:]:
            s.update(g.get("dead", ()))
        return s

    def _gen_hits(self, g, q, prefix, n):
        c = self._conn(g)
        lo, hi, flo, fhi = 1, 1 << 62, 0, 1 << 62
        if prefix:
            r = c.execute("SELECT min(first), max(last), min(id), max(id) FROM files WHERE path>=? AND path<?",
                          (prefix, prefix + "\U0010ffff")).fetchall()[0]
            if r[2] is None:
                return []
            lo, hi, flo, fhi = (r[0] or 1), (r[1] or 0), r[2], r[3]
        out = []
        try:
            rows = c.execute("SELECT rowid, rank FROM chunks WHERE chunks MATCH ? AND rowid BETWEEN ? AND ? ORDER BY rank LIMIT ?",
                             (q, lo, hi, n)).fetchall() if hi >= lo else []
            if rows:
                ids = ",".join(str(r[0]) for r in rows)
                meta = {r[0]: r[1:] for r in c.execute(
                    f"SELECT cm.id, f.path, cm.off, cm.rl, f.note FROM cm JOIN files f ON f.id=cm.fid WHERE cm.id IN ({ids})").fetchall()}
                out = [(sc, *meta[rid]) for rid, sc in rows if rid in meta]
            # identical chunks: files holding a copy of an indexed chunk score like the indexed one (restricted to the prefix's files)
            drows = rows if not prefix else c.execute(  # prefix: the indexed copy may live outside it
                "SELECT rowid, rank FROM chunks WHERE chunks MATCH ? ORDER BY rank LIMIT ?", (q, n * 8)).fetchall()
        except Exception:
            return out
        if drows:
            sc_of = dict(drows)
            ids = ",".join(str(r[0]) for r in drows)
            for orig, path, off, rl, note in c.execute(
                    f"SELECT d.orig, f.path, d.off, d.rl, f.note FROM dups d JOIN files f ON f.id=d.fid "
                    f"WHERE d.orig IN ({ids}) AND d.fid BETWEEN {int(flo)} AND {int(fhi)} LIMIT {n * 4}").fetchall():
                out.append((sc_of[orig], path, off, rl, note))
        return out

    def search(self, text: str, prefix: str = "", limit: int = 10, paths_only: bool = False, snippets: bool = True) -> list[dict]:
        gens = self._gens_for(prefix)
        if paths_only:
            return self._search_paths(text, prefix, limit, gens)
        q = _q(text)
        for attempt in (0, 1):
            hits = []
            for i, g in gens:
                dead = self._dead_after(i)
                for sc, path, off, rl, note in self._gen_hits(g, q, prefix, limit * 6):
                    if path not in dead:
                        hits.append((sc, path, off, rl, note))
            if hits or attempt or q.count('"') <= 2 or "OR" in q:
                break
            q = q.replace('" "', '" OR "')
        hits.sort(key=lambda h: h[0])
        out, seen = [], set()
        for sc, path, off, rl, note in hits:
            if path in seen:
                continue
            seen.add(path)
            cmd = (f"tracedb blob-get {path} --start {off} --end {off + 4096}" if note == "blob-head"
                   else f"tracedb get {path} --start {off} --end {off + 4096}")
            out.append({"path": path, "offset": off, "score": round(-sc, 3), "snippet": "", "cmd": cmd, "_rl": rl, "_blob": note == "blob-head"})
            if len(out) >= limit:
                break
        if snippets:
            self._fill_snippets(out, _terms(text))
        for o in out:
            o.pop("_rl"); o.pop("_blob")
        return out

    def _fill_snippets(self, out, terms):
        """Re-read each hit's bytes (one parallel read_many for the whole page of hits) and cut a snippet around the terms."""
        reqs, raws = {}, {}
        for i, o in enumerate(out):
            n = min(max(o["_rl"], 1), SNIP_FETCH)
            if o["_blob"]:
                try:
                    from .blob import get_blob
                    raws[i] = get_blob(self.dst, o["path"], o["offset"], o["offset"] + n, max_bytes=-1)
                except Exception:
                    pass
            else:
                e = self.dst.locate(o["path"])
                if e:
                    reqs[i] = (o["path"], o["offset"], min(o["offset"] + n, e["size"]))
        if reqs:
            try:
                got = self.dst.read_many(list(reqs.values()), max_bytes=-1)
            except Exception:
                got = {}
            for i, rq in reqs.items():
                if rq in got:
                    raws[i] = got[rq]
        for i, o in enumerate(out):
            raw = raws.get(i)
            if raw is None:
                continue
            txt = "\n".join(t for _, t, _, _ in chunks_of(o["path"], raw, o["offset"], None, False)) or raw.decode("utf8", "replace")
            o["snippet"] = make_snippet(txt, terms)

    def _search_paths(self, text, prefix, limit, gens):
        q = _q(text)
        out, seen = [], set()
        for i, g in reversed(gens):
            dead = self._dead_after(i)
            c = self._conn(g)
            try:
                rows = c.execute("SELECT f.path FROM paths JOIN files f ON f.id=paths.rowid WHERE paths MATCH ? "
                                 "AND f.path>=? AND f.path<? ORDER BY rank LIMIT ?",
                                 (q, prefix, prefix + "\U0010ffff", limit * 4)).fetchall()
            except Exception:
                rows = []
            for (p,) in rows:
                if p not in dead and p not in seen:
                    seen.add(p); out.append({"path": p, "cmd": f"tracedb get {p} --head 4096"})
        return out[:limit]

    def describe(self) -> dict:
        g = self.man["gens"]
        return {"gens": len(g), "files": sum(x["files"] for x in g), "chunks": sum(x["chunks"] for x in g),
                "bytes": sum(x["bytes"] for x in g), "scope": self.man["scope"], "built": self.man["built"]}


_MANI_CACHE: dict = {}


def _load_manifest_cached(st: Store):
    e = st.locate(MANIFEST)
    if not e:
        return None
    k = (id(st), e["sha"])
    if k not in _MANI_CACHE:
        _MANI_CACHE[k] = json.loads(st.read(MANIFEST, max_bytes=1 << 26))
    return _MANI_CACHE[k]
