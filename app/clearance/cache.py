"""A tiny persistent cache for ClickHouse lookups.

The public demo cluster enforces a per-IP hourly quota: 60 queries per hour, and
only 20 queries of the same *normalised shape* per hour. One uncached scan costs
around four queries, so without caching a handful of demo runs exhausts the budget
and every later request fails with QUOTA_EXCEEDED (ClickHouse error 201).

Judging happens unattended over two weeks, so a cold cache during evaluation would
mean a broken demo. Results are therefore cached on disk, keyed by the lookup, and
the repository ships a warm cache for the sample scene.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

CACHE_PATH = Path(os.getenv("CLEARANCE_CACHE", Path(__file__).parent.parent.parent / "cache.json"))
TTL_NOTE = "Cached lookups are refreshed whenever the file is deleted."

_lock = threading.Lock()
_data: dict[str, object] | None = None


def _load() -> dict:
    global _data
    if _data is None:
        try:
            _data = json.loads(CACHE_PATH.read_text())
        except Exception:
            _data = {}
    return _data


def get(key: str):
    with _lock:
        return _load().get(key)


def put(key: str, value) -> None:
    with _lock:
        d = _load()
        d[key] = value
        try:
            CACHE_PATH.write_text(json.dumps(d, default=str))
        except OSError:
            pass  # read-only filesystem: in-memory cache still works for this instance


def stats() -> dict:
    with _lock:
        return {"entries": len(_load()), "path": str(CACHE_PATH)}
