"""Full-text search over a traceDB store, kept as ONE SQLite FTS5 file stored in the store itself (index/fts.sqlite).

Why a separate pass: the catalog knows *where* bytes are; this answers *what is inside* without pulling packs.
Index content (bounded, deterministic): text files are cut into ~1.5 KB line-aligned chunks, capped per file
(HEAD_CAP from the start, TAIL_CAP from the end) so a 500 MB trace costs the same as a 50 KB doc.
JSONL agent traces are indexed on their *text-bearing values* (message text, commands, outputs) rather than raw JSON,
so hits are readable and the index is smaller.

    idx = SearchIndex.create("fts.sqlite"); idx.add(path, data_bytes); idx.close()
    SearchIndex.open("fts.sqlite").search("g7e vllm oom", prefix="traces/", limit=10)
Each hit carries path, byte offset (start of the chunk in the stored file, for `tracedb get --start/--end`), a snippet,
and the exact follow-up command. Stdlib only (sqlite3 FTS5).
"""
from __future__ import annotations
import json, os, re, sqlite3

HEAD_CAP = int(os.environ.get("TRACEDB_IDX_HEAD", 192 << 10))
TAIL_CAP = int(os.environ.get("TRACEDB_IDX_TAIL", 32 << 10))
CHUNK = 1500
TEXT_EXT = {".md", ".txt", ".py", ".sh", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".conf", ".json", ".jsonl", ".log",
            ".csv", ".tsv", ".rs", ".c", ".cc", ".cpp", ".h", ".cu", ".go", ".js", ".ts", ".tsx", ".html", ".sql",
            ".lua", ".tf", ".env.example", ".rst", ".out", ".err", ""}
_SAFE = re.compile(r"[A-Za-z0-9_.\-/]+")


def _is_text(path: str, data: bytes) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in TEXT_EXT and b"\0" not in data[:2048]


def _flatten(o, out, depth=0):
    """Collect text-bearing leaves of a JSON record, skipping huge/blobby values and short keys-only noise."""
    if depth > 8:
        return
    if isinstance(o, str):
        if len(o) >= 3:
            out.append(o[:4000])
    elif isinstance(o, dict):
        for k, v in o.items():
            if isinstance(v, (dict, list)):
                _flatten(v, out, depth + 1)
            elif isinstance(v, str) and len(v) >= 3:
                out.append(f"{k}: {v[:4000]}" if len(k) <= 24 and len(v) < 200 else v[:4000])
            elif isinstance(v, (int, float)) and not isinstance(v, bool) and len(k) <= 40:
                out.append(f"{k}={v}")
    elif isinstance(o, list):
        for v in o[:200]:
            _flatten(v, out, depth + 1)


def _jsonl_text(data: bytes, base: int):
    """Yield (offset, text) per JSONL line; offset = byte offset of the line in the stored file."""
    off = base
    for line in data.split(b"\n"):
        n = len(line) + 1
        if line.strip():
            try:
                o = json.loads(line)
                parts: list = []
                _flatten(o, parts)
                t = "\n".join(parts)
            except Exception:
                t = line.decode("utf8", "replace")
            if t:
                yield off, t
        off += n


def _chunks(path: str, data: bytes, base: int):
    """Yield (byte_offset, text) chunks of ~CHUNK chars."""
    if path.endswith(".jsonl"):
        buf, boff, blen = [], None, 0
        for off, t in _jsonl_text(data, base):
            if boff is None:
                boff = off
            buf.append(t); blen += len(t)
            if blen >= CHUNK:
                yield boff, "\n".join(buf)[: CHUNK * 4]; buf, boff, blen = [], None, 0
        if buf:
            yield boff, "\n".join(buf)[: CHUNK * 4]
        return
    off = base
    buf, boff, blen = [], base, 0
    for line in data.split(b"\n"):
        n = len(line) + 1
        s = line.decode("utf8", "replace")[:2000]
        if blen + len(s) > CHUNK and buf:
            yield boff, "\n".join(buf); buf, boff, blen = [], off, 0
        buf.append(s); blen += len(s) + 1
        off += n
    if buf:
        yield boff, "\n".join(buf)


class SearchIndex:
    def __init__(self, conn: sqlite3.Connection, path: str):
        self.c, self.path = conn, path

    @classmethod
    def create(cls, path: str) -> "SearchIndex":
        if os.path.exists(path):
            os.remove(path)
        c = sqlite3.connect(path)
        c.executescript("""
            PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA page_size=8192;
            CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT UNIQUE, size INTEGER, indexed INTEGER, note TEXT);
            CREATE VIRTUAL TABLE chunks USING fts5(text, path UNINDEXED, off UNINDEXED, tokenize="unicode61 remove_diacritics 2 tokenchars '_'", detail=column);
            CREATE VIRTUAL TABLE paths USING fts5(path, tokenize="unicode61 remove_diacritics 2");
        """)
        return cls(c, path)

    @classmethod
    def open(cls, path: str) -> "SearchIndex":
        return cls(sqlite3.connect(f"file:{path}?mode=ro", uri=True), path)

    def add(self, path: str, data: bytes, size: int | None = None) -> int:
        size = size if size is not None else len(data)
        n = 0
        # path is always searchable (even for binaries / skipped content)
        self.c.execute("INSERT INTO paths(path) VALUES (?)", (path,))
        if not _is_text(path, data):
            self.c.execute("INSERT OR REPLACE INTO files(path,size,indexed,note) VALUES (?,?,0,'non-text')", (path, size))
            return 0
        head, tail = data[:HEAD_CAP], b""
        if len(data) > HEAD_CAP + TAIL_CAP:
            tail_start = len(data) - TAIL_CAP
            nl = data.find(b"\n", tail_start)
            tail_start = nl + 1 if nl != -1 else tail_start
            tail = data[tail_start:]
            nlh = head.rfind(b"\n")
            head = head[: nlh + 1] if nlh > 0 else head
        else:
            head = data
            tail_start = 0
        for off, t in _chunks(path, head, 0):
            self.c.execute("INSERT INTO chunks(text,path,off) VALUES (?,?,?)", (t, path, off)); n += 1
        if tail:
            for off, t in _chunks(path, tail, tail_start):
                self.c.execute("INSERT INTO chunks(text,path,off) VALUES (?,?,?)", (t, path, off)); n += 1
        self.c.execute("INSERT OR REPLACE INTO files(path,size,indexed,note) VALUES (?,?,?,?)",
                       (path, size, n, "head+tail" if tail else "full"))
        return n

    def close(self):
        self.c.commit()
        try:
            self.c.execute("INSERT INTO chunks(chunks) VALUES('optimize')")
            self.c.execute("INSERT INTO paths(paths) VALUES('optimize')")
            self.c.commit()
        except Exception:
            pass
        self.c.close()

    @staticmethod
    def _q(text: str) -> str:
        """Plain words -> AND of quoted terms (prefix on the last); leave explicit FTS syntax (quotes, AND/OR/NEAR, *) alone."""
        if re.search(r'["*]|\b(AND|OR|NOT|NEAR)\b', text):
            return text
        ws = re.findall(r"[A-Za-z0-9_]+", text)  # hyphen/dot/slash split like the tokenizer; AND of terms (no phrase: detail=column)
        if not ws:
            return '""'
        return " ".join(f'"{w}"' for w in ws[:-1]) + (" " if len(ws) > 1 else "") + f'"{ws[-1]}"*'

    def search(self, text: str, prefix: str = "", limit: int = 10, paths_only: bool = False) -> list[dict]:
        out = []
        q = self._q(text)
        if paths_only:
            rows = self.c.execute("SELECT path FROM paths WHERE paths MATCH ? ORDER BY rank LIMIT ?", (q, limit * 4)).fetchall()
            return [{"path": r[0], "cmd": f"tracedb get {r[0]} --head 4096"} for r in rows if r[0].startswith(prefix)][:limit]
        rows = self.c.execute(
            "SELECT path, off, snippet(chunks, 0, '[', ']', ' … ', 24), bm25(chunks) AS s FROM chunks "
            "WHERE chunks MATCH ? " + ("AND path GLOB ? " if prefix else "") + "ORDER BY s LIMIT ?",
            (q, prefix + "*", limit * 3) if prefix else (q, limit * 3)).fetchall()
        if not rows and q.count('"') > 2 and "OR" not in q:  # AND found nothing: fall back to any-term ranking (bm25 still favours more matches)
            q = q.replace('" "', '" OR "')
            rows = self.c.execute(
                "SELECT path, off, snippet(chunks, 0, '[', ']', ' … ', 24), bm25(chunks) AS s FROM chunks "
                "WHERE chunks MATCH ? " + ("AND path GLOB ? " if prefix else "") + "ORDER BY s LIMIT ?",
                (q, prefix + "*", limit * 3) if prefix else (q, limit * 3)).fetchall()
        seen = {}
        for path, off, snip, score in rows:  # best chunk per file first, then fill with more chunks
            if path in seen:
                continue
            seen[path] = 1
            out.append({"path": path, "offset": off, "score": round(-score, 3), "snippet": snip.replace("\n", " ⏎ "),
                        "cmd": f"tracedb get {path} --start {off} --end {off + 4096}"})
            if len(out) >= limit:
                break
        return out

    def stats(self) -> dict:
        f = self.c.execute("SELECT count(*), sum(indexed>0), sum(size) FROM files").fetchone()
        n = self.c.execute("SELECT count(*) FROM chunks").fetchone()[0]
        return {"files": f[0], "text_files_indexed": f[1], "chunks": n, "bytes_covered": f[2]}
