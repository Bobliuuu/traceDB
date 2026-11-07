"""Object-store backends. Same tiny surface for local dir and S3.

All writes are create-only (put_if_absent) except HEAD, which is a compare-and-swap.
"""
from __future__ import annotations
import os, tempfile, threading, hashlib
from typing import Iterable, Optional


class CASConflict(Exception):
    pass


class Backend:
    def put_if_absent(self, key: str, data: bytes) -> bool: ...
    def get(self, key: str, start: Optional[int] = None, end: Optional[int] = None) -> bytes: ...
    def size(self, key: str) -> Optional[int]: ...
    def get_head(self) -> tuple[Optional[bytes], Optional[str]]: ...
    def cas_head(self, data: bytes, etag: Optional[str]) -> str: ...
    def list(self, prefix: str) -> Iterable[tuple[str, int]]: ...
    def warm(self, n: int) -> None:
        """Open n connections ahead of time (no-op for local)."""


class LocalBackend(Backend):
    """Directory-as-bucket. Used for tests, benchmarks and as a local mirror."""
    HEAD = "HEAD"

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        self._lock = threading.Lock()

    def _p(self, key):
        return os.path.join(self.root, key)

    def put_if_absent(self, key, data):
        p = self._p(key)
        if os.path.exists(p):
            return False
        os.makedirs(os.path.dirname(p), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix=".tmp-")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, p)  # atomic create-only
            created = True
        except FileExistsError:
            created = False
        os.unlink(tmp)
        return created

    def get(self, key, start=None, end=None):
        with open(self._p(key), "rb") as f:
            if start is None:
                return f.read()
            f.seek(start)
            return f.read(None if end is None else end - start)

    def size(self, key):
        try:
            return os.path.getsize(self._p(key))
        except FileNotFoundError:
            return None

    def get_head(self):
        try:
            d = self.get(self.HEAD)
        except FileNotFoundError:
            return None, None
        return d, hashlib.sha256(d).hexdigest()

    def cas_head(self, data, etag):
        with self._lock:
            cur, cur_etag = self.get_head()
            if cur_etag != etag:
                raise CASConflict()
            fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".tmp-head-")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._p(self.HEAD))
            return hashlib.sha256(data).hexdigest()

    def list(self, prefix):
        base = self._p(prefix)
        for dp, _, fs in os.walk(base if os.path.isdir(base) else os.path.dirname(base)):
            for f in fs:
                full = os.path.join(dp, f)
                key = os.path.relpath(full, self.root)
                if key.startswith(prefix) and not f.startswith(".tmp-"):
                    yield key, os.path.getsize(full)


class S3Backend(Backend):
    """boto3 backend. Create-only PUT via If-None-Match, HEAD via If-Match CAS."""

    def __init__(self, bucket: str, prefix: str = "", profile: Optional[str] = None,
                 region: Optional[str] = None, pool: int = 32):
        import boto3
        from botocore.config import Config
        sess = boto3.Session(profile_name=profile, region_name=region)
        self.c = sess.client("s3", config=Config(max_pool_connections=pool,
                                                 retries={"max_attempts": 5, "mode": "adaptive"},
                                                 tcp_keepalive=True))
        self.bucket = bucket
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""

    def _k(self, key):
        return self.prefix + key

    def put_if_absent(self, key, data):
        from botocore.exceptions import ClientError
        try:
            self.c.put_object(Bucket=self.bucket, Key=self._k(key), Body=data, IfNoneMatch="*")
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] in ("PreconditionFailed", "412", "ConditionalRequestConflict"):
                return False
            raise

    def get(self, key, start=None, end=None):
        kw = {}
        if start is not None:
            kw["Range"] = f"bytes={start}-{'' if end is None else end - 1}"
        return self.c.get_object(Bucket=self.bucket, Key=self._k(key), **kw)["Body"].read()

    def size(self, key):
        from botocore.exceptions import ClientError
        try:
            return self.c.head_object(Bucket=self.bucket, Key=self._k(key))["ContentLength"]
        except ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

    def get_head(self):
        from botocore.exceptions import ClientError
        try:
            r = self.c.get_object(Bucket=self.bucket, Key=self._k("HEAD"))
            return r["Body"].read(), r["ETag"]
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None, None
            raise

    def cas_head(self, data, etag):
        from botocore.exceptions import ClientError
        kw = {"IfMatch": etag} if etag else {"IfNoneMatch": "*"}
        try:
            r = self.c.put_object(Bucket=self.bucket, Key=self._k("HEAD"), Body=data, **kw)
            return r["ETag"]
        except ClientError as e:
            if e.response["Error"]["Code"] in ("PreconditionFailed", "412", "ConditionalRequestConflict"):
                raise CASConflict()
            raise

    def warm(self, n):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(n) as ex:
            list(ex.map(lambda _: self.c.head_bucket(Bucket=self.bucket), range(n)))

    def list(self, prefix):
        pg = self.c.get_paginator("list_objects_v2")
        for page in pg.paginate(Bucket=self.bucket, Prefix=self._k(prefix)):
            for o in page.get("Contents", []):
                yield o["Key"][len(self.prefix):], o["Size"]
