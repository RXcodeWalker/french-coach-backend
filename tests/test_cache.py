"""BoundedTTLCache — extracted from main.py's feedback cache (R1). Two
things must hold for it to be a safe replacement for the unbounded
_cache_get/_cache_set dict: it evicts on size, and it expires on time.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.cache import BoundedTTLCache


def test_get_returns_none_for_missing_key():
    async def run():
        cache: BoundedTTLCache[str] = BoundedTTLCache(max_size=10, ttl_sec=60.0)
        assert await cache.get("missing") is None
    asyncio.run(run())


def test_set_then_get_roundtrips():
    async def run():
        cache: BoundedTTLCache[str] = BoundedTTLCache(max_size=10, ttl_sec=60.0)
        await cache.set("k", "v")
        assert await cache.get("k") == "v"
    asyncio.run(run())


def test_evicts_oldest_when_over_max_size():
    async def run():
        cache: BoundedTTLCache[int] = BoundedTTLCache(max_size=2, ttl_sec=60.0)
        await cache.set("a", 1)
        await cache.set("b", 2)
        await cache.set("c", 3)  # evicts "a" (oldest)
        assert await cache.get("a") is None
        assert await cache.get("b") == 2
        assert await cache.get("c") == 3
    asyncio.run(run())


def test_expires_after_ttl():
    async def run():
        cache: BoundedTTLCache[str] = BoundedTTLCache(max_size=10, ttl_sec=0.05)
        await cache.set("k", "v")
        await asyncio.sleep(0.1)
        assert await cache.get("k") is None
    asyncio.run(run())


def test_get_moves_key_to_end_protecting_it_from_eviction():
    async def run():
        cache: BoundedTTLCache[int] = BoundedTTLCache(max_size=2, ttl_sec=60.0)
        await cache.set("a", 1)
        await cache.set("b", 2)
        await cache.get("a")       # touch "a" -> now most-recently-used
        await cache.set("c", 3)    # should evict "b", not "a"
        assert await cache.get("a") == 1
        assert await cache.get("b") is None
        assert await cache.get("c") == 3
    asyncio.run(run())


def test_hits_counter_increments_on_hit_only():
    async def run():
        cache: BoundedTTLCache[str] = BoundedTTLCache(max_size=10, ttl_sec=60.0)
        await cache.set("k", "v")
        await cache.get("missing")
        await cache.get("k")
        await cache.get("k")
        assert cache.hits == 2
    asyncio.run(run())


if __name__ == "__main__":
    test_get_returns_none_for_missing_key()
    test_set_then_get_roundtrips()
    test_evicts_oldest_when_over_max_size()
    test_expires_after_ttl()
    test_get_moves_key_to_end_protecting_it_from_eviction()
    test_hits_counter_increments_on_hit_only()
    print("All test_cache tests passed.")
