"""`tracedb health`: read the sync box heartbeat (s3://<bucket>/health/heartbeat.json) and report its age.
Exit 0 = fresh (<= --max-age-h, default 3 h) and status ok/running, 2 = stale/alert, 3 = unreadable."""
import json, time, datetime


def _bucket(store: str) -> str:
    assert store.startswith("s3://"), "health needs an s3:// store"
    return store[5:].split("/", 1)[0]


def check(store: str, max_age_h: float = 3.0) -> tuple:
    import boto3
    b = _bucket(store)
    try:
        raw = boto3.client("s3").get_object(Bucket=b, Key="health/heartbeat.json")["Body"].read()
        hb = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        return 3, {"error": f"cannot read health/heartbeat.json: {type(e).__name__}: {str(e)[:120]}"}
    def age(k):
        try:
            return round((time.time() - datetime.datetime.fromisoformat(hb[k].replace("Z", "+00:00")).timestamp()) / 3600, 2)
        except Exception:  # noqa: BLE001
            return None
    out = dict(hb, heartbeat_age_h=age("time"), last_ok_age_h=age("last_ok"))
    stale = out["last_ok_age_h"] is None or out["last_ok_age_h"] > max_age_h or out["heartbeat_age_h"] is None or out["heartbeat_age_h"] > max_age_h
    out["verdict"] = "STALE" if stale else "ok"
    return (2 if stale else 0), out
