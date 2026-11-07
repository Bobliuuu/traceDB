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
  blocks TEXT, codecs TEXT, meta TEXT) WITHOUT ROWID;
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
        self.db = sqlite3.connect(os.path.join(self.dir, "catalog.sqlite"), check_same_thread=False)
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

    def _apply(self, rows):
        self.db.executemany(
            "INSERT OR REPLACE INTO entries VALUES(?,?,?,?,?,?,?,?,?)",
            [(r["p"], r["h"], r["n"], r["k"], r["o"], r["c"], json.dumps(r["b"], separators=(",", ":")),
              r["x"], json.dumps(r.get("m") or {}, separators=(",", ":"))) for r in rows])

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
            row = self.db.execute("SELECT sha FROM entries WHERE path=?", (path,)).fetchone()
            if row and row[0] == h:
                continue
            todo.append((path, data, h, meta))
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
                r["k"] = pid
            packs.append((pid, blob)); rows.extend(pack_rows)
            cur, cur_len, pack_rows = [], 0, []

        for path, data, h, meta in todo:
            blocks, codecs, parts = [], [], []
            for i in range(0, max(len(data), 1), C.BLOCK):
                cdc, cb = C.compress(data[i:i + C.BLOCK])
                blocks.append(len(cb)); codecs.append(cdc); parts.append(cb)
            enc = b"".join(parts)
            if cur and cur_len + len(enc) > PACK_TARGET:
                flush()
            pack_rows.append({"p": path, "h": h, "n": len(data), "k": "", "o": cur_len, "c": len(enc),
                              "b": blocks, "x": ",".join(sorted(set(codecs))) if len(set(codecs)) == 1 else "|".join(codecs),
                              "m": meta})
            cur.append(enc); cur_len += len(enc)
        flush()
        list(self._pool.map(lambda pb: self.b.put_if_absent(f"packs/{pb[0]}.pack", pb[1]), packs))
        wire = sum(len(b) for _, b in packs)
        seg = zlib.compress("\n".join(json.dumps(r, separators=(",", ":")) for r in rows).encode(), 1)
        sid = _sha(seg)
        self.b.put_if_absent(f"segments/{sid}.seg", seg)
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
            self.compact()
        return {"added": len(rows), "packs": len(packs), "bytes": wire, "segment": sid}

    def compact(self) -> dict:
        """Merge all segments into one. Old segments stay (orphans) until GC; HEAD flip is atomic."""
        self.sync()
        rows = []
        for r in self.db.execute("SELECT path,sha,size,pack,off,clen,blocks,codecs,meta FROM entries ORDER BY path"):
            rows.append({"p": r[0], "h": r[1], "n": r[2], "k": r[3], "o": r[4], "c": r[5],
                         "b": json.loads(r[6]), "x": r[7], "m": json.loads(r[8])})
        seg = zlib.compress("\n".join(json.dumps(r, separators=(",", ":")) for r in rows).encode(), 1)
        sid = _sha(seg)
        self.b.put_if_absent(f"segments/{sid}.seg", seg)
        raw, etag = self.b.get_head()
        self.b.cas_head(json.dumps({"v": 1, "segments": [sid], "ts": time.time()}).encode(), etag)
        self.sync()
        return {"entries": len(rows), "segment": sid}

    # ------------------------------------------------------------------ read
    def locate(self, path: str) -> Optional[dict]:
        r = self.db.execute("SELECT path,sha,size,pack,off,clen,blocks,codecs,meta FROM entries WHERE path=?",
                            (path,)).fetchone()
        return self._row(r) if r else None

    def locate_sha(self, sha: str) -> list[dict]:
        return [self._row(r) for r in self.db.execute(
            "SELECT path,sha,size,pack,off,clen,blocks,codecs,meta FROM entries WHERE sha=?", (sha,))]

    @staticmethod
    def _row(r):
        return {"path": r[0], "sha": r[1], "size": r[2], "pack": r[3], "off": r[4], "clen": r[5],
                "blocks": json.loads(r[6]), "codecs": r[7], "meta": json.loads(r[8])}

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
            for bi, off, cl in self._blocks_for(e, start, end):
                spans.append((e["pack"], off, off + cl))
        pl.fetch_bytes, pl.requests = self._coalesce_stats(spans)
        return pl

    @staticmethod
    def _blocks_for(e, start, end):
        """yield (block_index, abs_pack_offset, clen) covering raw [start,end)."""
        if e["size"] == 0 or end <= start:
            return
        offs, o = [], e["off"]
        for cl in e["blocks"]:
            offs.append((o, cl)); o += cl
        b0, b1 = start // C.BLOCK, max(start, end - 1) // C.BLOCK
        for bi in range(b0, min(b1, len(offs) - 1) + 1):
            yield bi, offs[bi][0], offs[bi][1]

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
                return f.read()
        except FileNotFoundError:
            pass
        data = self.b.get(f"packs/{pk}.pack", a, b)
        self.stats["requests"] += 1; self.stats["bytes"] += len(data)
        os.makedirs(os.path.dirname(cp), exist_ok=True)
        tmp = cp + f".{threading.get_ident()}.tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, cp)
        return data

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
            spans += [(e["pack"], off, off + cl) for _, off, cl in bl]
        merged = self._merge(spans)
        blobs = dict(zip(map(tuple, merged), self._pool.map(self._fetch_span, map(tuple, merged))))

        def cut(pk, a, b):
            for (mp, ma, mb), d in blobs.items():
                if mp == pk and ma <= a and b <= mb:
                    return d[a - ma:b - ma]
            raise AssertionError("span missing")
        out = {}
        for e, start, end, bl in per_item:
            codecs = e["codecs"].split("|") if "|" in e["codecs"] else [e["codecs"]] * len(e["blocks"])
            raw = bytearray()
            for bi, off, cl in bl:
                raw += C.decompress(codecs[bi], cut(e["pack"], off, off + cl))
            base = (start // C.BLOCK) * C.BLOCK
            out[(e["path"], start, end)] = bytes(raw[start - base:end - base])
        return out

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
        for pk, mx in self.db.execute("SELECT pack, MAX(off+clen) FROM entries GROUP BY pack"):
            packs[pk] = mx
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
                if _sha(self.read(p, max_bytes=-1)) != h:
                    bad.append(p)
        return {"entries": self.count(), "packs": len(packs), "missing_packs": missing,
                "short_packs": short, "bad_content": bad, "ok": not (missing or short or bad)}

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
