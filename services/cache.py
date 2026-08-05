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

    async def set(self, key: str, value: V) -> None:
        async with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = (value, time.monotonic() + self._ttl_sec)
            while len(self._store) > self._max_size:
                self._store.popitem(last=False)
