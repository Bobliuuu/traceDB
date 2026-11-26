"""tracedb ask: answer "where is / what is / how do we X" in one call, in seconds.

Runs the search runbook's first steps at once instead of one by one: the ontology (ops pages, memory, every node's
summary) and the full-text index are queried in parallel with the question's content words, the two rankings are
fused (reciprocal rank, curated ops/memory pages first), and the best page is printed inline, so most questions need
no second command. Every other hit carries its exact follow-up command.

    tracedb ask "where is the rsi vis"            # text: best page inline + ranked hits
    tracedb ask "fnx lane" --json --limit 5       # for scripts and bench/ask_eval.py

The ontology is optional (ontolagni importable and $ONTOLAGNI_DIR / $ONTOLOGY_DIR / ~/ontology present); without it
only the full-text index answers.
"""
from __future__ import annotations
import os, re, time
from concurrent.futures import ThreadPoolExecutor

# question words that only dilute an AND query ("where is the rsi vis" -> "rsi vis")
STOP = set("""a an the is are was were be been being am of to in on at for and or nor not no do does did done how what
which who whom whose where when why can could should would will shall may might must we i me my you your our ours it its
this that these those with from by about into onto as any there their them they he she his her have has had get got
use used using make made find show tell give need want let s t please one some all just only also than then so if
whats wheres hows does doesn dont don go goes much many
yet really actually exactly whatever able trying working happened look looking search tracedb ontology ontolagni
ve re ll aren isn wasn weren didn haven hasn won wouldn couldn shouldn e.g eg i.e ie etc nad teh adn thorugh thru""".split())
# rank boost for curated pages by top-level id; small: ontolagni find already ranks ops pages up (x1.6)
CURATED = {"ops": 1.3, "memory": 1.5, "policy": 1.3, "skills": 1.2, "projects": 1.3}
RRF_K = 10
PAGE_BUDGET = 6000


def content_words(question: str) -> list[str]:
    ws = re.findall(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*", question)
    keep = [w for w in ws if w.lower().strip(".-") not in STOP]
    return keep or ws


TEXT_TERMS = 5


def text_terms(words: list[str], onto) -> list[str]:
    """The full-text index ANDs every term, then ORs them: a long question ANDs to nothing and its OR over common words
    scans every generation (7-12 s). Keep the TEXT_TERMS most selective words, rarest first by ontology frequency
    (a word the ontology does not know, a typo or a name, comes after the known rare ones), in question order."""
    if len(words) <= TEXT_TERMS:
        return words
    inv = getattr(onto, "_inv", None) or {}
    df = {w: len(inv.get(w.lower().strip("."), ())) for w in words}
    # rarest known words first; words the ontology never saw (typos, or names) after them
    keep = set(sorted(dict.fromkeys(words), key=lambda w: (df[w] == 0, df[w], -len(w)))[:TEXT_TERMS])
    return [w for w in dict.fromkeys(words) if w in keep]


def plain_query(text: str) -> str:
    """`tracedb search` input: a question of more than 3 words without FTS syntax loses its question words
    ("how do I get aws credentials without logging in" -> "aws credentials logging"); anything else is kept as typed."""
    if re.search(r'["*]|\b(AND|OR|NOT|NEAR)\b', text):
        return text
    ws = re.findall(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*", text)
    if len(ws) <= 3:
        return text
    keep = [w for w in ws if w.lower().strip(".-") not in STOP]
    return " ".join(keep[:TEXT_TERMS * 2]) if keep else text


def _onto_dir() -> str | None:
    for d in (os.environ.get("ONTOLAGNI_DIR"), os.environ.get("ONTOLOGY_DIR"), os.path.expanduser("~/ontology")):
        if d and os.path.exists(os.path.join(d, "ontology.jsonl")):
            return d
    return None


def _load_onto():
    d = _onto_dir()
    if not d:
        return None
    try:
        from ontolagni.query import Onto
    except ImportError:
        return None
    return Onto(d)


def _owner(onto, path: str) -> str:
    """The ontology node a file belongs to (deepest one), else the file's directory."""
    if onto is not None:
        chain = onto.where(path)
        if chain:
            return chain[0]["id"]
    return path.rsplit("/", 1)[0] if "/" in path else path


def ask(store, question: str, limit: int = 8, page: bool = True, index_store=None) -> dict:
    from .index import IndexSet
    t0 = time.time()
    words = content_words(question)
    q = " ".join(words)
    timing = {}

    def open_index():  # I/O (catalog freshness, index manifest) overlaps with the ontology load
        store.sync_if_stale(float(os.environ.get("TRACEDB_ASK_SYNC_TTL", 300)))
        return IndexSet.open(index_store or store, store)

    with ThreadPoolExecutor(1) as ex:
        f_ix = ex.submit(open_index)
        t = time.time()
        onto = _load_onto()
        found = [r for r in (onto.find(q, limit=limit * 2) if onto else []) if "id" in r]
        timing["find"] = round(time.time() - t, 2)
        t = time.time()
        tq = " ".join(text_terms(words, onto))
        try:
            ix = f_ix.result()
            hits = ix.search(tq, limit=limit * 3, per_file=1, snippets=True) if ix else []
        except Exception as e:  # the ontology alone still answers
            hits, timing["search_error"] = [], repr(e)[:200]
        timing["search"] = round(time.time() - t, 2)

    cand: dict[str, dict] = {}

    def slot(id_):
        return cand.setdefault(id_, {"id": id_, "score": 0.0, "via": []})

    for r, n in enumerate(found):
        c = slot(n["id"])
        c["score"] += 1.0 / (RRF_K + r)
        c["via"].append("ontology")
        c.update({k: n[k] for k in ("kind", "summary", "readme") if n.get(k)})
    for r, h in enumerate(hits):
        c = slot(_owner(onto, h["path"]))
        if "text" not in c["via"]:  # an owner counts its best text hit only: 500 matching files in one dir is one answer
            c["score"] += 1.0 / (RRF_K + r)
            c["via"].append("text")
            c.setdefault("snippet", h.get("snippet", ""))
            c.setdefault("path", h["path"])
            c.setdefault("cmd", h["cmd"])
    for c in cand.values():
        top = c["id"].split("/")
        page = CURATED.get(top[0], 1.0) if len(top) <= 2 else 1.0  # the curated pages themselves, not data under them
        c["score"] *= page * (1.25 if len(c["via"]) > 1 else 1.0)
        if onto is not None and c["id"] in onto.nodes:
            n = onto.nodes[c["id"]]
            c.setdefault("summary", n.get("summary", ""))
            if n.get("readme"):
                c.setdefault("readme", n["readme"])
        if "cmd" not in c:
            c["cmd"] = f"tracedb get {c['readme']}" if c.get("readme") else f"tracedb ls {c['id']}/ --limit 50"
        c["score"] = round(c["score"], 4)
    ranked = sorted(cand.values(), key=lambda c: -c["score"])[:limit]

    out = {"question": question, "query": q, "hits": ranked, "timing": timing}
    if page:
        for c in ranked[:3]:  # inline the best hit that has a page: the answer is usually on it
            if c.get("readme"):
                try:
                    out["page"] = {"id": c["id"], "path": c["readme"],
                                   "text": store.read(c["readme"], 0, PAGE_BUDGET, max_bytes=-1).decode("utf8", "replace")}
                except Exception:
                    continue
                break
    out["timing"]["total"] = round(time.time() - t0, 2)
    return out


def render(r: dict) -> str:
    L = [f"# ask: {r['question']}   (query: {r['query']}; {r['timing'].get('total')} s)"]
    p = r.get("page")
    if p:
        L += ["", f"## best page: {p['id']}   (tracedb get {p['path']})", "", p["text"].rstrip()]
        if len(p["text"]) >= PAGE_BUDGET:
            L.append(f"... (truncated at {PAGE_BUDGET} B: tracedb get {p['path']})")
    L += ["", "## hits"]
    for i, c in enumerate(r["hits"], 1):
        what = c.get("summary") or c.get("snippet") or ""
        what = " ".join(what.split())[:220]
        L.append(f"{i}. {c['id']}  [{'+'.join(c['via'])}]  {what}")
        L.append(f"   -> {c['cmd']}")
    if not r["hits"]:
        L.append("nothing found: try other words (a path fragment, a phrase from its output), then `ontolagni find ops search`")
    return "\n".join(L)
