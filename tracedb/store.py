"""traceDB: packs + immutable catalog segments + CAS HEAD, with a local SQLite catalog.

Write path (all-git's ordering, minus the multi-writer machinery):
  1. pack(s)    packs/<sha256>.pack   create-only PUT   (invisible until a segment names them)
  2. segment    segments/<sha256>.seg create-only PUT   (immutable catalog delta)
  3. HEAD       compare-and-swap       (the commit point; crash before it leaves harmless orphans)
Read path: local catalog (SQLite, point lookup ~10-50us) -> coalesced ranged GETs of only the blocks needed.
Every read is byte-budgeted so an agent can never accidentally pull a huge amount.
"""
from __future__ import annotations
import hashlib, json, os, sqlite3, threading, time, zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .backend import Backend, CASConflict, LocalBackend, S3Backend
from . import codec as C

PACK_TARGET = 32 << 20
SEG_COMPACT_AT = 64
MAX_EXTENTS = 64  # past this an append rewrites the file as one extent (bounds per-read request fan-out)
DEFAULT_BUDGET = int(os.environ.get("TRACEDB_MAX_BYTES", 8 << 20))
COALESCE_GAP = int(os.environ.get("TRACEDB_GAP", 64 << 10))


class BudgetExceeded(Exception):
    def __init__(self, plan: "Plan", budget: int):
        self.plan, self.budget = plan, budget
        super().__init__(f"read would fetch {plan.raw_bytes} raw bytes ({plan.fetch_bytes} on the wire, "
                         f"{plan.requests} requests) over budget {budget}; narrow with start/end, head/tail, "
                         f"or pass max_bytes=")


class NotFound(KeyError):
    pass


class CorruptBlock(IOError):
    pass


@dataclass
class Plan:
    paths: int = 0
    raw_bytes: int = 0
    fetch_bytes: int = 0
    requests: int = 0
    items: list = field(default_factory=list)  # (path, start, end)

    def as_dict(self):
        return {"paths": self.paths, "raw_bytes": self.raw_bytes, "fetch_bytes": self.fetch_bytes,
                "requests": self.requests}


SCHEMA = """
CREATE TABLE IF NOT EXISTS entries(
  path TEXT PRIMARY KEY, sha TEXT, size INTEGER, pack TEXT, off INTEGER, clen INTEGER,
  blocks TEXT, codecs TEXT, meta TEXT, exts TEXT) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS entries_sha ON entries(sha);
CREATE TABLE IF NOT EXISTS segs(id TEXT PRIMARY KEY, ord INTEGER);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class Store:
    def __init__(self, backend: Backend, cache_dir: Optional[str] = None, name: str = "default",
                 cache_bytes: int = 2 << 30, workers: int = 64):
        self.b = backend
        self.name = name
        self.workers = workers
        root = cache_dir or os.environ.get("TRACEDB_CACHE") or os.path.expanduser("~/.cache/tracedb")
        self.dir = os.path.join(root, name)
        os.makedirs(os.path.join(self.dir, "blocks"), exist_ok=True)
        self.cache_bytes = cache_bytes
        self._csize: Optional[int] = None  # running cache size; lazily initialised, persisted lazily
        self._cdirty = 0
        self.db = sqlite3.connect(os.path.join(self.dir, "catalog.sqlite"), check_same_thread=False)
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(entries)")]
        if cols and "exts" not in cols:  # catalog cache from before multi-extent: rebuildable from segments
            self.db.executescript("DROP TABLE entries; DROP TABLE segs;")
        self.db.executescript(SCHEMA)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=OFF")  # catalog is a rebuildable cache of immutable segments
        self.db.execute("PRAGMA mmap_size=268435456")
        self._pool = ThreadPoolExecutor(workers)
        self._head_etag: Optional[str] = None
        self._warmed = False
        self._lock = threading.RLock()
        self.stats = {"requests": 0, "bytes": 0, "cache_hits": 0}

    # ------------------------------------------------------------------ sync
    def _segment_ids(self) -> list[str]:
        raw, etag = self.b.get_head()
        self._head_etag = etag
        return json.loads(raw)["segments"] if raw else []

    def sync_if_stale(self, ttl: Optional[float] = None) -> Optional[dict]:
        """Skip the ~70 ms HEAD round trip if we synced within ttl seconds (env TRACEDB_SYNC_TTL, default 0 = always)."""
        ttl = float(os.environ.get("TRACEDB_SYNC_TTL", 0)) if ttl is None else ttl
        if ttl > 0 and time.time() - getattr(self, "_last_sync", 0) < ttl:
            return None
        return self.sync()

    def sync(self) -> dict:
        """Pull only segments we do not have. One HEAD GET when nothing changed."""
        self._last_sync = time.time()
        if not self._warmed:  # TLS handshakes in parallel, once, so first reads do not pay them serially
            self._warmed = True
            self._pool.submit(self.b.warm, self.workers)
        with self._lock:
            ids = self._segment_ids()
            have = {r[0] for r in self.db.execute("SELECT id FROM segs")}
            if ids and not have.issubset(set(ids)):  # compaction replaced segments -> rebuild
                self.db.execute("DELETE FROM entries"); self.db.execute("DELETE FROM segs")
                have = set()
            new = [i for i in ids if i not in have]
            blobs = list(self._pool.map(lambda i: self.b.get(f"segments/{i}.seg"), new))
            self.stats["requests"] += len(new) + 1
            for i, blob in zip(new, blobs):
                if _sha(blob) != i:
                    raise IOError(f"segment {i} corrupt")
                rows = [json.loads(l) for l in zlib.decompress(blob).splitlines()]
                self._apply(rows)
                self.db.execute("INSERT INTO segs VALUES(?,?)", (i, ids.index(i)))
            self.db.commit()
            return {"segments": len(ids), "new": len(new), "entries": self.count()}

    @staticmethod
    def _exts_of(r) -> list:
        """Segment row -> extent list. Old rows (flat k/o/c/b/x) are one extent; new multi-extent rows carry "e"."""
        if "e" in r:
            return r["e"]
        return [{"k": r["k"], "o": r["o"], "c": r["c"], "b": r["b"], "x": r["x"], "r": r["n"]}]

    @staticmethod
    def _wire_row(r) -> dict:
        """Internal row {p,h,n,m,e} -> segment JSON. One extent stays in the original flat format (old readers work)."""
        if len(r["e"]) == 1:
            x = r["e"][0]
            return {"p": r["p"], "h": r["h"], "n": r["n"], "k": x["k"], "o": x["o"], "c": x["c"],
                    "b": x["b"], "x": x["x"], "m": r["m"]}
        return {"p": r["p"], "h": r["h"], "n": r["n"], "e": r["e"], "m": r["m"]}

    def _apply(self, rows):
        out = []
        for r in rows:
            ex = self._exts_of(r)
            f = ex[0]
            out.append((r["p"], r["h"], r["n"], f["k"], f["o"], f["c"], json.dumps(f["b"], separators=(",", ":")),
                        f["x"], json.dumps(r.get("m") or {}, separators=(",", ":")),
                        json.dumps(ex, separators=(",", ":"))))
        self.db.executemany("INSERT OR REPLACE INTO entries VALUES(?,?,?,?,?,?,?,?,?,?)", out)

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM entries").fetchone()[0]

    # ----------------------------------------------------------------- write
    def put(self, items: Iterable[tuple[str, "bytes | str", Optional[dict]]], retries: int = 20) -> dict:
        """items: (path, bytes | local-file-path, meta). Idempotent: same path+content is a no-op."""
        self.sync()
        known = {}
        todo = []
        for path, data, meta in items:
            if isinstance(data, str):
                with open(data, "rb") as f:
                    data = f.read()
            h = _sha(data)
            row = self.db.execute("SELECT sha,size,exts,meta FROM entries WHERE path=?", (path,)).fetchone()
            if row and row[0] == h:
                continue
            base = None  # (old_size, old_exts, old_meta) when data strictly extends the stored content
            if row and 0 < row[1] < len(data) and len(json.loads(row[2])) < MAX_EXTENTS \
                    and _sha(memoryview(data)[:row[1]]) == row[0]:
                base = (row[1], json.loads(row[2]), json.loads(row[3]))
            todo.append((path, data, h, meta, base))
        if not todo:
            return {"added": 0, "packs": 0, "bytes": 0}
        todo.sort(key=lambda t: t[0])  # path order = locality (same dir adjacent in a pack)
        rows, packs, cur, cur_len, wire = [], [], [], 0, 0
        pack_rows: list[dict] = []

        def flush():
            nonlocal cur, cur_len, pack_rows
            if not cur:
                return
            blob = b"".join(cur)
            pid = _sha(blob)
            for r in pack_rows:
                r.pop("_new")["k"] = pid
            packs.append((pid, blob)); rows.extend(pack_rows)
            cur, cur_len, pack_rows = [], 0, []

        for path, data, h, meta, base in todo:
            skip = base[0] if base else 0  # append: only the new tail is encoded and stored
            blocks, codecs, parts = [], [], []
            mv = memoryview(data)
            for i in range(skip, max(len(data), 1), C.BLOCK):
                cdc, cb = C.compress(bytes(mv[i:i + C.BLOCK]))
                blocks.append(len(cb)); codecs.append(cdc); parts.append(cb)
            enc = b"".join(parts)
            if cur and cur_len + len(enc) > PACK_TARGET:
                flush()
            ext = {"k": "", "o": cur_len, "c": len(enc), "b": blocks,
                   "x": ",".join(sorted(set(codecs))) if len(set(codecs)) == 1 else "|".join(codecs),
                   "r": len(data) - skip}
            pack_rows.append({"p": path, "h": h, "n": len(data), "m": meta if meta is not None or not base else base[2],
                              "e": (base[1] if base else []) + [ext], "_new": ext})
            cur.append(enc); cur_len += len(enc)
        flush()
        def up(pb):  # already-existing pack (dedup / old orphan): refresh its age so GC grace protects it
            k = f"packs/{pb[0]}.pack"
            if not self.b.put_if_absent(k, pb[1]):
                self.b.touch(k)
        list(self._pool.map(up, packs))
        wire = sum(len(b) for _, b in packs)
        seg = zlib.compress("\n".join(json.dumps(self._wire_row(r), separators=(",", ":")) for r in rows).encode(), 1)
        sid = _sha(seg)
        if not self.b.put_if_absent(f"segments/{sid}.seg", seg):
            self.b.touch(f"segments/{sid}.seg")
        for _ in range(retries):
            raw, etag = self.b.get_head()
            segs = json.loads(raw)["segments"] if raw else []
            if sid not in segs:
                segs.append(sid)
            try:
                self.b.cas_head(json.dumps({"v": 1, "segments": segs, "ts": time.time()}).encode(), etag)
                break
            except CASConflict:
                time.sleep(0.01)
        else:
            raise RuntimeError("HEAD CAS kept conflicting")
        self.sync()
        if len(segs) >= SEG_COMPACT_AT:
            try:
                self.compact()
            except CASConflict:
                pass  # someone committed meanwhile; a later put will compact
        return {"added": len(rows), "packs": len(packs), "bytes": wire, "segment": sid}

    def compact(self, retries: int = 20) -> dict:
        """Merge all segments into one. Old segments stay (orphans) until GC; HEAD flip is atomic.
        The CAS uses the etag seen at the sync that built the snapshot, so a segment committed meanwhile
        makes the CAS fail and we re-merge, instead of silently dropping that segment."""
        for _ in range(retries):
            with self._lock:
                self.sync()
                etag = self._head_etag
                rows = []
                for r in self.db.execute("SELECT path,sha,size,meta,exts FROM entries ORDER BY path"):
                    rows.append({"p": r[0], "h": r[1], "n": r[2], "m": json.loads(r[3]), "e": json.loads(r[4])})
            seg = zlib.compress("\n".join(json.dumps(self._wire_row(r), separators=(",", ":")) for r in rows).encode(), 1)
            sid = _sha(seg)
            self.b.put_if_absent(f"segments/{sid}.seg", seg)
            try:
                self.b.cas_head(json.dumps({"v": 1, "segments": [sid], "ts": time.time()}).encode(), etag)
                break
            except CASConflict:
                time.sleep(0.01)
        else:
            raise RuntimeError("HEAD CAS kept conflicting")
        self.sync()
        return {"entries": len(rows), "segment": sid}

    # ------------------------------------------------------------------ read
    def locate(self, path: str) -> Optional[dict]:
        r = self.db.execute("SELECT path,sha,size,pack,off,clen,blocks,codecs,meta,exts FROM entries WHERE path=?",
                            (path,)).fetchone()
        return self._row(r) if r else None

    def locate_sha(self, sha: str) -> list[dict]:
        return [self._row(r) for r in self.db.execute(
            "SELECT path,sha,size,pack,off,clen,blocks,codecs,meta,exts FROM entries WHERE sha=?", (sha,))]

    @staticmethod
    def _row(r):
        return {"path": r[0], "sha": r[1], "size": r[2], "pack": r[3], "off": r[4], "clen": r[5],
                "blocks": json.loads(r[6]), "codecs": r[7], "meta": json.loads(r[8]), "exts": json.loads(r[9])}

    def ls(self, prefix: str = "", limit: int = 1000, glob: Optional[str] = None) -> list[dict]:
        hi = prefix + "\U0010ffff"
        q = "SELECT path,size,sha,meta FROM entries WHERE path>=? AND path<?"
        a = [prefix, hi]
        if glob:
            q += " AND path GLOB ?"; a.append(glob)
        q += " ORDER BY path LIMIT ?"; a.append(limit)
        return [{"path": p, "size": s, "sha": h, "meta": json.loads(m)} for p, s, h, m in self.db.execute(q, a)]

    def summary(self, prefix: str = "", depth: int = 1) -> list[dict]:
        """Subtree aggregates (files, bytes, newest meta ts) at `depth` path segments below prefix."""
        out: dict[str, list] = {}
        hi = prefix + "\U0010ffff"
        for p, n in self.db.execute("SELECT path,size FROM entries WHERE path>=? AND path<?", (prefix, hi)):
            rest = p[len(prefix):].lstrip("/")
            parts = rest.split("/")
            key = prefix + ("/" if prefix and not prefix.endswith("/") else "") + "/".join(parts[:depth]) \
                if len(parts) > depth else p
            e = out.setdefault(key, [0, 0]); e[0] += 1; e[1] += n
        return [{"prefix": k, "files": v[0], "bytes": v[1]} for k, v in sorted(out.items())]

    def plan(self, reqs: list, ) -> Plan:
        """reqs: list of path | (path, start, end). Computes cost without fetching."""
        pl = Plan()
        spans = []  # (pack, a, b) wire spans
        for rq in reqs:
            path, start, end = (rq, 0, None) if isinstance(rq, str) else (rq[0], rq[1] or 0, rq[2] if len(rq) > 2 else None)
            e = self.locate(path)
            if e is None:
                raise NotFound(path)
            if end is None or end > e["size"]:
                end = e["size"]
            if start < 0:
                start = max(0, e["size"] + start)
            start = min(start, end)
            pl.paths += 1; pl.raw_bytes += end - start
            pl.items.append((path, start, end))
            for pk, off, cl, *_ in self._blocks_for(e, start, end):
                spans.append((pk, off, off + cl))
        pl.fetch_bytes, pl.requests = self._coalesce_stats(spans)
        return pl

    @staticmethod
    def _blocks_for(e, start, end):
        """yield (pack, abs_pack_offset, clen, extent_idx, block_idx) for every block covering raw [start,end).
        An entry is a list of extents laid end to end in raw space; blocks are per extent (1 MiB from the extent's
        own start), so an extent boundary need not be block aligned."""
        if e["size"] == 0 or end <= start:
            return
        rb = 0
        for xi, x in enumerate(e["exts"]):
            rl = x["r"]
            s, t = max(start, rb) - rb, min(end, rb + rl) - rb
            rb += rl
            if t <= s:
                continue
            offs, o = [], x["o"]
            for cl in x["b"]:
                offs.append((o, cl)); o += cl
            for bi in range(s // C.BLOCK, min((t - 1) // C.BLOCK, len(offs) - 1) + 1):
                yield x["k"], offs[bi][0], offs[bi][1], xi, bi

    @staticmethod
    def _merge(spans):
        spans = sorted(set(spans))
        merged: list[list] = []
        for pk, a, b in spans:
            if merged and merged[-1][0] == pk and a - merged[-1][2] <= COALESCE_GAP:
                merged[-1][2] = max(merged[-1][2], b)
            else:
                merged.append([pk, a, b])
        return merged

    def _coalesce_stats(self, spans):
        m = self._merge(spans)
        uncached = [x for x in m if not self._cached(x)]
        return sum(b - a for _, a, b in uncached), len(uncached)

    # block-range cache: key = pack/off-end
    def _cpath(self, pk, a, b):
        return os.path.join(self.dir, "blocks", pk[:2], f"{pk}.{a}-{b}")

    def _cached(self, x):
        return os.path.exists(self._cpath(*x))

    def _fetch_span(self, x):
        pk, a, b = x
        cp = self._cpath(pk, a, b)
        try:
            with open(cp, "rb") as f:
                self.stats["cache_hits"] += 1
                d = f.read()
            try:
                os.utime(cp, None)  # LRU: refresh atime explicitly (noatime/relatime mounts)
            except OSError:
                pass
            return d
        except FileNotFoundError:
            pass
        data = self.b.get(f"packs/{pk}.pack", a, b)
        self.stats["requests"] += 1; self.stats["bytes"] += len(data)
        os.makedirs(os.path.dirname(cp), exist_ok=True)
        tmp = cp + f".{threading.get_ident()}.tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, cp)
        self._cache_added(len(data))
        return data

    # ---- bounded block cache: running counter + batch LRU eviction (no directory walk per read)
    def _csize_file(self):
        return os.path.join(self.dir, "blocks", ".size")

    def _cache_walk(self):
        out = []
        for dp, _, fs in os.walk(os.path.join(self.dir, "blocks")):
            for f in fs:
                if f.startswith(".") or f.endswith(".tmp"):
                    continue
                full = os.path.join(dp, f)
                try:
                    st = os.stat(full)
                except FileNotFoundError:
                    continue
                out.append((max(st.st_atime, st.st_mtime), st.st_size, full))
        return out

    def _cache_added(self, n: int) -> None:
        if not self.cache_bytes or self.cache_bytes < 0:
            return
        with self._lock:
            if self._csize is None:
                try:
                    with open(self._csize_file()) as f:
                        self._csize = int(f.read()) + n
                except (OSError, ValueError):
                    self._csize = sum(x[1] for x in self._cache_walk())
            else:
                self._csize += n
            self._cdirty += 1
            if self._csize > self.cache_bytes * 1.1:
                self.evict_cache()
            elif self._cdirty >= 256:
                self._cache_persist()

    def _cache_persist(self):
        self._cdirty = 0
        try:
            with open(self._csize_file(), "w") as f:
                f.write(str(self._csize or 0))
        except OSError:
            pass

    def evict_cache(self, target: float = 0.8) -> int:
        """Delete least-recently-used cached ranges until size <= target*cache_bytes. Returns bytes freed."""
        with self._lock:
            files = sorted(self._cache_walk())
            total = sum(x[1] for x in files)
            goal = int(self.cache_bytes * target)
            freed = 0
            for _, sz, full in files:
                if total - freed <= goal:
                    break
                try:
                    os.unlink(full); freed += sz
                except FileNotFoundError:
                    pass
            self._csize = total - freed
            self._cache_persist()
            return freed

    def read_many(self, reqs: list, max_bytes: Optional[int] = None) -> dict:
        """Parallel, coalesced, budgeted read. Returns {(path,start,end)->bytes}. Budget applies to raw bytes."""
        budget = DEFAULT_BUDGET if max_bytes is None else max_bytes
        pl = self.plan(reqs)
        if budget >= 0 and pl.raw_bytes > budget:
            raise BudgetExceeded(pl, budget)
        spans = []
        per_item = []
        for path, start, end in pl.items:
            e = self.locate(path)
            bl = list(self._blocks_for(e, start, end))
            per_item.append((e, start, end, bl))
            spans += [(pk, off, off + cl) for pk, off, cl, _, _ in bl]
        merged = self._merge(spans)
        blobs = dict(zip(map(tuple, merged), self._pool.map(self._fetch_span, map(tuple, merged))))

        def cut(pk, a, b):
            for (mp, ma, mb), d in blobs.items():
                if mp == pk and ma <= a and b <= mb:
                    return d[a - ma:b - ma]
            raise AssertionError("span missing")
        out = {}
        for e, start, end, bl in per_item:
            raw = {}  # (extent, block) -> decompressed
            for pk, off, cl, xi, bi in bl:
                x = e["exts"][xi]
                cdc = x["x"].split("|") if "|" in x["x"] else [x["x"]] * len(x["b"])
                try:
                    blk = C.decompress(cdc[bi], cut(pk, off, off + cl))
                    want = min(C.BLOCK, x["r"] - bi * C.BLOCK)
                    if len(blk) != want:
                        raise CorruptBlock(f"block {bi} of {e['path']}: {len(blk)} bytes, expected {want}")
                except Exception as ex:
                    self._evict(pk, off, off + cl)
                    if isinstance(ex, CorruptBlock):
                        raise
                    raise CorruptBlock(f"block {bi} of {e['path']} does not decompress: {ex}") from ex
                raw[(xi, bi)] = blk
            buf, rb = bytearray(), 0
            for xi, x in enumerate(e["exts"]):
                s, t = max(start, rb) - rb, min(end, rb + x["r"]) - rb
                rb += x["r"]
                if t <= s:
                    continue
                piece = b"".join(raw[(xi, bi)] for bi in range(s // C.BLOCK, (t - 1) // C.BLOCK + 1))
                base = (s // C.BLOCK) * C.BLOCK
                buf += piece[s - base:t - base]
            data = bytes(buf)
            if start == 0 and end == e["size"] and _sha(data) != e["sha"]:  # whole file: content hash is cheap
                for pk, off, cl, _, _ in bl:
                    self._evict(pk, off, off + cl)
                raise CorruptBlock(f"{e['path']}: content hash mismatch")
            out[(e["path"], start, end)] = data
        return out

    def _evict(self, pk, a, b):
        """Drop cached ranges overlapping a bad block so a repaired pack is re-fetched."""
        d = os.path.join(self.dir, "blocks", pk[:2])
        try:
            for f in os.listdir(d):
                if f.startswith(pk + ".") and f.endswith(tuple("0123456789")):
                    x, y = f[len(pk) + 1:].split("-")
                    if int(x) < b and a < int(y):
                        os.unlink(os.path.join(d, f))
        except (OSError, ValueError):
            pass

    def read(self, path: str, start: int = 0, end: Optional[int] = None, max_bytes: Optional[int] = None) -> bytes:
        r = self.read_many([(path, start, end)], max_bytes)
        return next(iter(r.values()))

    def head(self, path: str, n: int = 4096) -> bytes:
        return self.read(path, 0, n, max_bytes=max(n, 1))

    def tail(self, path: str, n: int = 4096) -> bytes:
        return self.read(path, -n, None, max_bytes=max(n, 1))

    # ---------------------------------------------------------------- verify
    def verify(self, deep: bool = False) -> dict:
        """Every catalog entry's pack exists and is large enough; deep also re-hashes content."""
        self.sync()
        packs = {}
        for (ex,) in self.db.execute("SELECT exts FROM entries"):
            for x in json.loads(ex):
                packs[x["k"]] = max(packs.get(x["k"], 0), x["o"] + x["c"])
        missing, short = [], []
        for pk, need in packs.items():
            sz = self.b.size(f"packs/{pk}.pack")
            if sz is None:
                missing.append(pk)
            elif sz < need:
                short.append(pk)
        bad = []
        if deep:
            for (p, h) in self.db.execute("SELECT path,sha FROM entries"):
                try:
                    if _sha(self.read(p, max_bytes=-1)) != h:
                        bad.append(p)
                except (IOError, ValueError, RuntimeError):
                    bad.append(p)
        return {"entries": self.count(), "packs": len(packs), "missing_packs": missing,
                "short_packs": short, "bad_content": bad, "ok": not (missing or short or bad)}

    def gc(self, grace_seconds: float = 3600, dry_run: bool = True) -> dict:
        """Delete unreferenced packs/segments older than grace. See tracedb/gc.py."""
        from .gc import gc
        return gc(self, grace_seconds, dry_run)

    def export_catalog(self) -> Iterable[dict]:
        """Plain listing for siloed consumers (ontolagni): path, sha, size, meta. No tracedb code needed to read."""
        for p, h, n, m in self.db.execute("SELECT path,sha,size,meta FROM entries ORDER BY path"):
            yield {"path": p, "sha": h, "size": n, "meta": json.loads(m)}


def open_store(spec: str, name: Optional[str] = None, **kw) -> Store:
    """spec: local dir path, or s3://bucket/prefix[?profile=x&region=y]."""
    if spec.startswith("s3://"):
        rest, _, q = spec[5:].partition("?")
        bucket, _, prefix = rest.partition("/")
        opts = dict(x.split("=", 1) for x in q.split("&") if x)
        be = S3Backend(bucket, prefix, profile=opts.get("profile"), region=opts.get("region"))
        nm = name or f"s3-{bucket}-{prefix.strip('/').replace('/', '_')}"
    else:
        be = LocalBackend(spec)
        nm = name or "local-" + hashlib.sha256(os.path.abspath(spec).encode()).hexdigest()[:10]
    return Store(be, name=nm, **kw)
