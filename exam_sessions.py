"""
In-memory exam session store with async-safe access.
Sessions live until exam_finish() cleans them up.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

_store: dict[str, dict[str, Any]] = {}
_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


async def create_session(card: dict[str, Any], candidate_id: str = "") -> dict[str, Any]:
    session: dict[str, Any] = {
        "session_id": str(uuid.uuid4()),
        "state": 0,
        "candidate_id": candidate_id,
        "roleplay_card": card,
        "current_task": 0,
        "repeat_used": False,
        "topic1_area": None,
        "topic2_area": None,
        "current_question": None,
        "transcript": {
            "roleplay": [],
            "topic1": [],
            "topic2": [],
        },
        "timestamps": {
            "state_0": time.time(),
        },
    }
    async with _get_lock():
        _store[session["session_id"]] = session
    return session


async def get_session(session_id: str) -> dict[str, Any]:
    async with _get_lock():
        session = _store.get(session_id)
    if session is None:
        raise KeyError(session_id)
    return session


async def update_session(session_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    async with _get_lock():
        session = _store.get(session_id)
        if session is None:
            raise KeyError(session_id)
        session.update(updates)
        return session


async def delete_session(session_id: str) -> None:
    async with _get_lock():
        _store.pop(session_id, None)
