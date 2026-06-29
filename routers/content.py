"""Public content reads from Supabase — published rows only.

These complement the existing /api/questions and /api/exam-sets handlers in
main.py (which filter on is_active). They expose the new status-aware content
(questions + scenarios) that the frontend contentService consumes, and are
served behind the shared TTL cache injected from main.py.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/content", tags=["content"])

_supabase = None


def _db():
    global _supabase
    if _supabase is None:
        url = os.getenv("SUPABASE_URL", "").strip()
        key = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
        if not (url and key):
            raise HTTPException(status_code=503, detail="Database not configured")
        from supabase import create_client
        _supabase = create_client(url, key)
    return _supabase


# Cache hooks injected by main.py (reuse its TTL cache).
_cache_get: Callable[[str], Awaitable[Any]] | None = None
_cache_set: Callable[..., Awaitable[None]] | None = None
_CACHE_TTL = 300.0  # 5 min


def set_cache(get_fn, set_fn) -> None:
    global _cache_get, _cache_set
    _cache_get, _cache_set = get_fn, set_fn


async def _cached(key: str, builder: Callable[[], Awaitable[Any]]):
    if _cache_get:
        hit = await _cache_get(key)
        if hit is not None:
            return hit
    value = await builder()
    if _cache_set:
        await _cache_set(key, value, _CACHE_TTL)
    return value


async def _run(query):
    return await asyncio.to_thread(query.execute)


@router.get("/questions")
async def list_published_questions(topic_key: str | None = None):
    async def build():
        db = _db()
        q = db.table("questions").select("*").eq("status", "published")
        if topic_key:
            q = q.eq("topic_key", topic_key)
        return (await _run(q)).data

    return await _cached(f"content:questions:{topic_key or 'all'}", build)


@router.get("/scenarios")
async def list_published_scenarios():
    async def build():
        db = _db()
        return (await _run(
            db.table("scenarios").select("*").eq("status", "published")
        )).data

    return await _cached("content:scenarios:all", build)
