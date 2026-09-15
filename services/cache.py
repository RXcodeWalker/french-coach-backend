"""Bounded, TTL-evicting in-memory cache.

Extracted from main.py's `_feedback_cache` (an `OrderedDict`-based LRU with
TTL that was already correct) rather than reusing `_cache_get`/`_cache_set`
(an unbounded plain dict with no eviction — keying that by audio hash would
be a slow memory leak). One class, two independent instances: feedback
keeps its own cache object, and pronunciation gets its own with its own
size/TTL — sharing an instance would let one endpoint's traffic evict the
other's entries.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Generic, TypeVar

V = TypeVar("V")


class BoundedTTLCache(Generic[V]):
    def __init__(self, max_size: int, ttl_sec: float) -> None:
        self._max_size = max_size
        self._ttl_sec = ttl_sec
        self._store: OrderedDict[str, tuple[V, float]] = OrderedDict()
        self._lock = asyncio.Lock()
        self.hits = 0

    async def get(self, key: str) -> V | None:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if time.monotonic() > expires_at:
                del self._store[key]
                return None
            self._store.move_to_end(key)
            self.hits += 1
            return value

    async def set(self, key: str, value: V, ttl_sec: float | None = None) -> None:
        """ttl_sec overrides the instance default for this one entry — needed
        by main.py's general-purpose cache (health-probe TTL is much shorter
        than a cached news/vocab response), unlike the feedback/pronunciation
        caches, which only ever use one TTL per instance."""
        async with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = (value, time.monotonic() + (ttl_sec if ttl_sec is not None else self._ttl_sec))
            while len(self._store) > self._max_size:
                self._store.popitem(last=False)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._store.pop(key, None)

    def pop_prefix_sync(self, prefix: str) -> None:
        """Synchronous, lock-free by design: main.py's admin-write cache
        invalidation hook (_invalidate_content_cache) is itself a plain sync
        function called from request handlers already holding no lock on
        this cache, and content-cache invalidation racing a concurrent
        read/write here is a stale-cache-for-one-request risk, not a
        correctness one — matching the plain-dict version this replaced,
        which had no locking either."""
        for k in [k for k in self._store if k.startswith(prefix)]:
            self._store.pop(k, None)
