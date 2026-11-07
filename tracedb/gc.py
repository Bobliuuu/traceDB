"""Orphan GC for packs/ and segments/.

Safety argument: a writer creates pack -> segment -> HEAD CAS, so an in-flight writer's objects are
unreferenced but *fresh*. We only delete unreferenced objects whose mtime is older than grace_seconds
(pick grace >> longest write, default 1h). Live set = every segment in HEAD plus every pack named by
any of those segments (read from the segments themselves, not just the catalog, because the catalog
keeps only the latest entry per path while older segments in HEAD still reference older packs) plus
the catalog's packs. HEAD is never listed under packs/ or segments/ and the backends refuse to delete it.

Order: HEAD is read AFTER the listing, so anything committed while we listed is seen as live.
Known caveat: put_if_absent of a pack that already exists as an old orphan does not refresh its mtime,
so a writer re-uploading byte-identical content in the instant between our listing and its HEAD CAS
could lose that pack. Run gc rarely / off the hot path.
"""
from __future__ import annotations
import json, time, zlib


def gc(store, grace_seconds: float = 3600, dry_run: bool = True) -> dict:
    b = store.b
    now = time.time()
    listed = []
    for pfx in ("packs/", "segments/"):
        listed.extend(b.list_mtime(pfx))
    raw, _ = b.get_head()  # after listing, see docstring
    head_segs = list(json.loads(raw)["segments"]) if raw else []
    live = {f"segments/{s}.seg" for s in head_segs}
    for s in head_segs:
        blob = b.get(f"segments/{s}.seg")
        for line in zlib.decompress(blob).splitlines():
            live.add(f"packs/{json.loads(line)['k']}.pack")
    with store._lock:
        store.sync()
        live.update(f"packs/{r[0]}.pack" for r in store.db.execute("SELECT DISTINCT pack FROM entries"))
    orphans, young, live_n, freed = [], 0, 0, 0
    for key, size, mtime in listed:
        if key in live:
            live_n += 1
        elif now - mtime < grace_seconds:
            young += 1
        else:
            orphans.append((key, size))
    for key, size in orphans:
        if not dry_run:
            b.delete(key)
        freed += size
    return {"dry_run": dry_run, "grace_seconds": grace_seconds, "live": live_n, "skipped_young": young,
            "orphans": len(orphans), "orphan_bytes": freed, "deleted": 0 if dry_run else len(orphans),
            "orphan_keys": [k for k, _ in orphans]}
