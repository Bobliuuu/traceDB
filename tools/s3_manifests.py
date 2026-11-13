#!/usr/bin/env python3
"""List big S3 prefixes (read-only) and write one gz manifest + summary per prefix into manifests-s3/<bucket>/ so the
tracedb store can reference them (pointer entries) without copying hundreds of GB. usage: s3_manifests.py OUTDIR"""
import boto3, gzip, json, os, sys
out = sys.argv[1]
S = [("inference-experiment-618283574049-us-east-1", "us-east-1", ["gemma-4-31b-g7e", "glm-gpqa-b300-20261013T1130Z", "models", "qwen-g7e-20261021", "qwen3.8-27b-gb10"]),
     ("inference-experiment-archive-618283574049-us-west-2", "us-west-2", ["blobs", "files", "shards"])]
sess = boto3.Session(profile_name=os.environ.get("AWS_PROFILE", "lab"))
for bucket, region, prefixes in S:
    c = sess.client("s3", region_name=region)
    for p in prefixes:
        rows, tot = [], 0
        for page in c.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=p + "/"):
            for o in page.get("Contents", []):
                rows.append({"key": o["Key"], "size": o["Size"], "etag": o["ETag"].strip('"'), "mtime": o["LastModified"].isoformat()}); tot += o["Size"]
        d = os.path.join(out, bucket); os.makedirs(d, exist_ok=True)
        with gzip.open(os.path.join(d, p + ".manifest.jsonl.gz"), "wt") as f:
            for r in rows: f.write(json.dumps(r) + "\n")
        json.dump({"format": "tracedb-s3ref/1", "bucket": bucket, "region": region, "prefix": p + "/", "objects": len(rows), "bytes": tot,
                   "note": "objects NOT copied; see <prefix>.manifest.jsonl.gz for per-object key/size/etag"},
                  open(os.path.join(d, p + ".SUMMARY.json"), "w"), indent=1)
        print(bucket, p, len(rows), tot, flush=True)
