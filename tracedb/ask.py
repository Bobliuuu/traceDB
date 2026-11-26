"""tracedb ask: answer "where is / what is / how do we X" in one call, in seconds.

Runs the search runbook's first steps at once instead of one by one: the ontology (ops pages, memory, every node's
summary) and the full-text index are queried in parallel with the question's content words, the two rankings are
fused (reciprocal rank, curated ops/memory pages first), and the best page is printed inline, so most questions need
no second command. Every other hit carries its exact follow-up command.

    tracedb ask "where is the dashboard"        # text: best page inline + ranked hits
    tracedb ask "gpu boxes" --json --limit 5       # for scripts and bench/ask_eval.py

The ontology is optional (ontolagni importable and $ONTOLAGNI_DIR / $ONTOLOGY_DIR / ~/ontology present); without it
only the full-text index answers.
"""
from __future__ import annotations
import os, re, time
from concurrent.futures import ThreadPoolExecutor

# question words that only dilute an AND query ("where is the dashboard" -> "dashboard")
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


# "what is running now", "is X stuck": answered from snapshots pb-orch publishes (rsi-sync, stallwatch, the reapers),
# each shown with its age. Exact live state still needs pb-orch itself (ops/stallwatch, ops/pb-team).
LIVE_WORDS = {"running", "now", "currently", "live", "stuck", "status", "alive", "idle", "active", "stopped", "dead",
              "busy", "hung", "stalled", "down", "up"}
LIVE = {"lanes": "rsi/lanes/pb-orch.json", "stallwatch": "rsi/stallwatch/status.json", "ec2": "rsi/reaper/ec2/status.json"}


def _age(ts: str) -> str:
    import datetime as dt
    try:
        t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        m = (dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 60
        return f"{m:.0f} min old" if m < 120 else f"{m / 60:.1f} h old"
    except Exception:
        return "age unknown"


def live_summary(store) -> dict:
    import json as _j
    got = {}
    for k, path in LIVE.items():
        try:
            got[k] = _j.loads(store.read(path, max_bytes=-1))
        except Exception:
            pass
    out = {}
    L = got.get("lanes")
    if L:
        out["lanes"] = {"source": LIVE["lanes"], "age": _age(L.get("generated", "")),
                        "lanes": [f"{x['lane']}: {x.get('state')}, {len(x.get('live_seats') or [])}/{len(x.get('seats') or [])} seats live"
                                  + (f" ({', '.join(x['live_seats'][:8])})" if x.get("live_seats") else "") for x in L.get("lanes", [])]}
    W = got.get("stallwatch")
    if W:
        acts: dict = {}
        for x in W.get("seats", []):
            acts.setdefault(x.get("action", "?"), []).append(x.get("seat", "?"))
        bad = [f"{u['unit']}: {u.get('result')}" for u in W.get("units", []) if u.get("result") not in ("success", None)]
        out["stallwatch"] = {"source": LIVE["stallwatch"], "age": _age(W.get("at", "")),
                             "seats_by_action": {a: (len(v), v[:10]) for a, v in sorted(acts.items(), key=lambda kv: -len(kv[1]))},
                             "failed_units": bad}
    E = got.get("ec2")
    if E:
        out["ec2"] = {"source": LIVE["ec2"], "age": _age(E.get("at", "")),
                      "instances": [f"{x.get('name') or x.get('id')} {x.get('type')} {x.get('lifecycle')} {x.get('age_h')} h: {x.get('action')}"
                                    for x in E.get("instances", [])]}
    return out


SKIP_ROOTS = ("eval/",)               # search test sets quote the questions verbatim: never an answer
ANCHOR_DF = 12                       # a word in at most this many ontology nodes names a specific thing
ANCHOR_HIT, ANCHOR_MISS = 1.4, 0.6


def anchor_terms(question: str, words: list[str], onto) -> set[str]:
    """Words that pin the answer down, which a common word would otherwise outrank: a possessive name ("alice's
    report": alice), or the rare part of a compound token, the way ids and host names are written ("db-replicas":
    replicas in a handful of nodes). A plain English word is never an anchor: rare in the ontology ("codes", "synced") is not
    the same as specific. Every anchor must occur in at least one node."""
    inv = getattr(onto, "_inv", None) or {}
    df = lambda w: len(inv.get(w, ()))
    out = {w.lower() for w in re.findall(r"([A-Za-z][A-Za-z0-9_]*)'s\b", question)
           if w.lower() not in STOP and df(w.lower()) >= 1}
    for w in words:
        if re.search(r"[-.]|\d", w.strip(".")):
            for part in re.split(r"[-.]+", w.lower().strip(".")):
                if len(part) >= 4 and part not in STOP and 1 <= df(part) <= ANCHOR_DF:
                    out.add(part)
    return out


def _node_words(onto, c) -> str:
    n = (onto.nodes.get(c["id"]) if onto is not None else None) or {}
    return " ".join([c["id"], n.get("summary", ""), n.get("text", ""), c.get("snippet", ""), c.get("path", "")]).lower()


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

    found = [n for n in found if not n["id"].startswith(SKIP_ROOTS)]
    for r, n in enumerate(found):
        c = slot(n["id"])
        c["score"] += 1.0 / (RRF_K + r)
        c["via"].append("ontology")
        c.update({k: n[k] for k in ("kind", "summary", "readme") if n.get(k)})
    hits = [h for h in hits if not h["path"].startswith(SKIP_ROOTS)]
    for r, h in enumerate(hits):
        c = slot(_owner(onto, h["path"]))
        if "text" not in c["via"]:  # an owner counts its best text hit only: 500 matching files in one dir is one answer
            c["score"] += 1.0 / (RRF_K + r)
            c["via"].append("text")
            c.setdefault("snippet", h.get("snippet", ""))
            c.setdefault("path", h["path"])
            c.setdefault("cmd", h["cmd"])
    anchors = anchor_terms(question, words, onto)
    if anchors and onto is not None:
        for a in anchors:  # the nodes a distinctive word points at must be candidates
            for r, n in enumerate(x for x in onto.find(a, limit=3) if "id" in x):
                c = slot(n["id"])
                c["score"] += 0.5 / (RRF_K + r)
                if "ontology" not in c["via"]:
                    c["via"].append("ontology")
    for c in cand.values():
        if anchors:
            c["score"] *= ANCHOR_HIT if any(a in _node_words(onto, c) for a in anchors) else ANCHOR_MISS
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
    if {w.lower() for w in re.findall(r"[A-Za-z]+", question)} & LIVE_WORDS:
        t = time.time()
        out["live"] = live_summary(store)
        timing["live"] = round(time.time() - t, 2)
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
    lv = r.get("live")
    if lv:
        L += ["", "## live state (snapshots published from pb-orch; exact state: ssh pb-orch, see ops/stallwatch)"]
        if "lanes" in lv:
            L.append(f"lanes ({lv['lanes']['source']}, {lv['lanes']['age']}):")
            L += [f"  - {x}" for x in lv["lanes"]["lanes"]]
        if "stallwatch" in lv:
            w = lv["stallwatch"]
            L.append(f"seats by stallwatch action ({w['source']}, {w['age']}):")
            L += [f"  - {a}: {n}  {', '.join(v)}{' ...' if n > len(v) else ''}" for a, (n, v) in w["seats_by_action"].items()]
            if w["failed_units"]:
                L.append("  failed timer units: " + "; ".join(w["failed_units"]))
        if "ec2" in lv:
            L.append(f"EC2 instances ({lv['ec2']['source']}, {lv['ec2']['age']}):")
            L += [f"  - {x}" for x in lv["ec2"]["instances"]]
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
