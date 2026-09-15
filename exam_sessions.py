"""
In-memory exam session store with async-safe access.
Sessions live until exam_finish() cleans them up, or the TTL below expires
them — whichever comes first. A plain, hand-rolled dict here had no bound at
all: a session that's created via /api/exam/start and never finished (an
abandoned exam attempt, a crashed client) lived forever, an unbounded-memory
growth vector (phase-3-plan-tidy-widget.md §3, "Bound exam_sessions._store").
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from services.cache import BoundedTTLCache

# A real exam attempt runs well under an hour end-to-end (10-min prep +
# roleplay + two topic discussions); 2 hours is generous headroom for a
# slow/interrupted-but-still-live candidate without keeping abandoned
# sessions around indefinitely. 1000 concurrent in-flight sessions is far
# above any realistic simultaneous-candidate count for this app.
_SESSION_TTL_SEC = 2 * 60 * 60
_SESSION_MAX = 1000

_store: BoundedTTLCache[dict[str, Any]] = BoundedTTLCache(_SESSION_MAX, _SESSION_TTL_SEC)


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
    await _store.set(session["session_id"], session)
    return session


async def get_session(session_id: str) -> dict[str, Any]:
    session = await _store.get(session_id)
    if session is None:
        raise KeyError(session_id)
    return session


async def update_session(session_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    # get() returns a live reference (BoundedTTLCache does not copy on read),
    # so this in-place .update() already changes the stored dict — but set()
    # is still required: it's the only thing that renews the TTL clock and
    # performs the LRU touch, so skipping it would silently stop renewing
    # session expiry (confirmed correct per phase-3-plan-tidy-widget.md §3
    # item 10). This does not fix the pre-existing unprotected
    # read-modify-write race across the two awaits below (no lock spans the
    # whole get-then-set sequence, only each call's own body) — that race
    # predates this migration and is out of scope here.
    session = await _store.get(session_id)
    if session is None:
        raise KeyError(session_id)
    session.update(updates)
    await _store.set(session_id, session)
    return session


async def delete_session(session_id: str) -> None:
    await _store.delete(session_id)
