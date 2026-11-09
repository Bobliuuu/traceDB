"""What a store refuses to hold. The rules are data, kept in the store itself as POLICY.json (beside HEAD), so every
writer of that store enforces them and this code names nothing:

    {"v": 1, "exclude": ["^repos/some-project(/|$)", ...]}      regexes, matched against entry paths

A store without POLICY.json excludes nothing. Tools that read local files before any store exists (ingest --dry,
build_index, purge_index) take the same file through TRACEDB_POLICY=path, else the policy of $TRACEDB_STORE."""
from __future__ import annotations
import json, os, re

POLICY_KEY = "POLICY.json"


class Excluded(ValueError):
    """A put of a path the store's policy refuses."""


class Policy:
    def __init__(self, patterns=()):
        self.patterns = list(patterns)
        self._re = re.compile("|".join(f"(?:{p})" for p in self.patterns)) if self.patterns else None

    def excluded(self, path: str) -> bool:
        return bool(self._re and self._re.search(path))

    @classmethod
    def parse(cls, data: bytes) -> "Policy":
        doc = json.loads(data)
        if not isinstance(doc, dict) or not isinstance(doc.get("exclude", []), list):
            raise ValueError(f"{POLICY_KEY}: expected {{\"exclude\": [regex, ...]}}")
        return cls(doc.get("exclude", []))

    def dumps(self) -> bytes:
        return json.dumps({"v": 1, "exclude": self.patterns}, indent=1).encode()


def of_backend(backend) -> Policy:
    if backend.size(POLICY_KEY) is None:
        return Policy()
    return Policy.parse(backend.get(POLICY_KEY))


def from_env() -> Policy:
    """For tools: TRACEDB_POLICY (a local POLICY.json), else the policy of $TRACEDB_STORE, else none."""
    if os.environ.get("TRACEDB_POLICY"):
        with open(os.environ["TRACEDB_POLICY"], "rb") as f:
            return Policy.parse(f.read())
    if os.environ.get("TRACEDB_STORE"):
        from .store import open_store
        return open_store(os.environ["TRACEDB_STORE"]).policy
    return Policy()
