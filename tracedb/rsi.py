"""Drop-in bridge for the RSI system (inference-experiment).

trace_store_put mirrors scripts/trace_store.py's contract used by rsi_state.upload():
returns a prefix string starting with a scheme, with manifest written LAST as the commit marker.
Key layout kept: <label>/<UTC stamp>-<host>-<6hex>/<relpath>   (so existing prefixes/readers still parse).
Exactly-once: the whole put is one segment + one HEAD CAS; a retry of identical content is a no-op.
"""
import hashlib, json, os, socket, time, secrets
from .store import Store


def trace_store_put(st: Store, paths: list, label: str, now=None) -> str:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now or time.time()))
    prefix = f"{label}/{stamp}-{socket.gethostname().split('.')[0]}-{secrets.token_hex(3)}/"
    items, files = [], []
    for root in paths:
        root = os.path.abspath(root)
        walk = [(os.path.dirname(root), [os.path.basename(root)])] if os.path.isfile(root) else \
               [(dp, fs) for dp, _, fs in os.walk(root)]
        base = os.path.dirname(root) if os.path.isfile(root) else root
        for dp, fs in walk:
            for f in fs:
                full = os.path.join(dp, f)
                with open(full, "rb") as fh:
                    data = fh.read()
                rel = os.path.relpath(full, base)
                items.append((prefix + rel, data, {"label": label}))
                files.append({"path": rel, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    man = json.dumps({"format": "trace-store/1", "prefix": prefix, "label": label, "files": files}).encode()
    items.append((prefix + "manifest.json", man, {"label": label}))  # same segment => atomic with the data
    st.put(items)
    return f"tdb://{st.name}/{prefix}"


def trace_store_get(st: Store, prefix: str, to: str, match=None, max_bytes=-1) -> list:
    import fnmatch
    st.sync()
    p = prefix.split("/", 3)[-1] if prefix.startswith("tdb://") else prefix
    out = []
    for e in st.ls(p, limit=10**6):
        rel = e["path"][len(p):]
        if match and not fnmatch.fnmatch(rel, match):
            continue
        dst = os.path.join(to, rel); os.makedirs(os.path.dirname(dst), exist_ok=True)
        open(dst, "wb").write(st.read(e["path"], max_bytes=max_bytes)); out.append(dst)
    return out
