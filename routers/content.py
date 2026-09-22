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

from fastapi import APIRouter, HTTPException, Request

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
async def list_published_questions(request: Request, topic_key: str | None = None):
    async def build():
        db = _db()
        q = db.table("questions").select("*").eq("status", "published")
        if topic_key:
            q = q.eq("topic_key", topic_key)
        return (await _run(q)).data

    return await _cached(f"content:questions:{topic_key or 'all'}", build)


@router.get("/scenarios")
async def list_published_scenarios(request: Request):
    async def build():
        db = _db()
        return (await _run(
            db.table("scenarios").select("*").eq("status", "published")
        )).data

    return await _cached("content:scenarios:all", build)


@router.get("/igcse-sets")
async def list_published_igcse_sets(request: Request):
    """S11 §8: usability-only listing of published question-set ids, so the
    frontend can pick a set without a hardcoded id. No adaptive/weighted/
    history-aware selection -- that's explicitly out of scope."""
    async def build():
        db = _db()
        res = await _run(
            db.table("igcse_question_sets").select("id").eq("status", "published")
        )
        return [row["id"] for row in res.data]

    return await _cached("content:igcse-sets:all", build)


@router.get("/igcse-sets/{question_set_id}")
async def get_published_igcse_set(request: Request, question_set_id: str):
    """S11: one published AuthoredQuestionSet payload, by id. The frontend
    loader (data/exam/bank/loader.ts) validates the payload again on receipt
    (parseAuthoredQuestionSet) -- this endpoint returns the raw stored payload,
    not a re-derived shape."""
    async def build():
        db = _db()
        res = await _run(
            db.table("igcse_question_sets")
            .select("payload")
            .eq("id", question_set_id)
            .eq("status", "published")
        )
        if not res.data:
            raise HTTPException(status_code=404, detail="Question set not found or not published")
        return res.data[0]["payload"]

    return await _cached(f"content:igcse-sets:{question_set_id}", build)


# ── Rate limiting (W7 reliability) ────────────────────────────────────────────
# This router was unauthenticated AND unrate-limited. It stays unauthenticated
# on purpose (see verification-log.md): every row here is a `status =
# 'published'` read, already RLS-scoped so auth would add nothing the DB isn't
# already enforcing, and igcse-sets specifically is on loader.ts's guest path
# (falls back to the 1-set offline fixture on any non-2xx, including a 401,
# so gating it would silently starve every guest exam attempt down to that
# one fixture). Per-IP rate limiting is the real fix for the actual gap (open
# to scraping/hammering), sized so ExamSelect's normal flow — one catalog
# listing call plus a fetch per set (up to 10 today) — is nowhere near it.
# Applied post-hoc, mirroring exam_controller.py/pronunciation.py's
# set_rate_limiter: main.py's slowapi `_limiter` doesn't exist until main.py
# has started executing, and this module is imported by main.py.
_RATE_LIMITED_ENDPOINTS = {
    "/api/content/questions": list_published_questions,
    "/api/content/scenarios": list_published_scenarios,
    "/api/content/igcse-sets": list_published_igcse_sets,
    "/api/content/igcse-sets/{question_set_id}": get_published_igcse_set,
}


def set_rate_limiter(rate_limit_decorator, target_router) -> None:
    """Must run AFTER `app.include_router(router)`, and must mutate the route
    objects living on `target_router` (the app's router), not on this
    module's own `router` — see exam_controller.py's set_rate_limiter for why
    (rebuilding the dependant from the wrapped function rather than the
    original breaks `from __future__ import annotations` string-annotation
    resolution for parameters typed on modules the rate-limit wrapper can't see)."""
    limited = {path: rate_limit_decorator("30/minute")(fn) for path, fn in _RATE_LIMITED_ENDPOINTS.items()}
    for route in target_router.routes:
        path = getattr(route, "path", None)
        if path in limited:
            route.endpoint = limited[path]
            route.dependant.call = limited[path]
