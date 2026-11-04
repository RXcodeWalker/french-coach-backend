"""Homework allowance for Learn answers (main repo docs/plans/teacher-dashboard.md §5 Stage 3, D3).

A Learn answer the client sends with an ``X-Assignment-Id`` header is homework a
teacher set. If the learner is an active member of that assignment's class and
the assignment still has answers left, it charges ``assignment_learn_answer``
(its own daily row + a per-assignment cap) instead of the learner's own
``learn_answer`` (3/day) — so homework never uses up, or is blocked by, the
learner's daily answers.

The check is the service-role RPC ``assignment_allowance`` (migration
20261103091000), which owns every rule (membership, targeting, closed after
due + 14 days, the cap). This module only asks it. Anything unexpected — no
header, a guest, a malformed id, an RPC error, a "no" — falls back to the
normal ``learn_answer`` charge: homework allowance is never granted by
accident, and a failed lookup never makes an answer free.

Grants carry the key prefix ``asg:<assignment_id>:`` so the RPC can count an
assignment's answers from ``ai_usage_grants`` (id = user:feature:key).
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from lib.guest import is_guest

log = logging.getLogger("uvicorn.error")

ASSIGNMENT_HEADER = "x-assignment-id"
ASSIGNMENT_LEARN_FEATURE = "assignment_learn_answer"


def parse_assignment_id(raw: object) -> str | None:
    """A canonical UUID string, or None for anything else."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return str(uuid.UUID(raw.strip()))
    except ValueError:
        return None


def assignment_key(assignment_id: str, idempotency_key: str) -> str:
    return f"asg:{assignment_id}:{idempotency_key}"


async def homework_learn_key(db, user_id: str, assignment_id: str | None, idempotency_key: str) -> str | None:
    """The idempotency key to charge ``assignment_learn_answer`` with, or None
    when this answer must be charged to the learner's own ``learn_answer``."""
    if assignment_id is None or db is None or is_guest(user_id):
        return None
    key = assignment_key(assignment_id, idempotency_key)
    try:
        res = await asyncio.to_thread(
            lambda: db.rpc(
                "assignment_allowance",
                {"p_user_id": user_id, "p_assignment_id": assignment_id, "p_exclude_key": key},
            ).execute()
        )
        data = res.data or {}
    except Exception as e:  # noqa: BLE001 — any failure means "use the normal limit"
        log.warning("assignment_allowance RPC failed: %s", e)
        return None
    if isinstance(data, dict) and data.get("ok") is True and data.get("feature") == ASSIGNMENT_LEARN_FEATURE:
        return key
    return None
