"""Per-user daily AI-cost quota — fail-closed spend backstop.

phase-3-plan-tidy-widget.md §2. Calls the `consume_ai_quota` /
`release_ai_quota_grant` RPCs (20260914090000_invite_gate_and_ai_quota.sql)
with the service-role key and an explicit, JWT-verified `p_user_id` —
neither caller in this repo runs in a per-request client-JWT context, so
`auth.uid()` is not available to the RPC, matching that migration's own
comment.

This is a deliberate inversion of routers/pronunciation.py's `_consume_quota`
(shadowing coaching), which swallows every exception into "proceed without
charging" — correct for that feature (a coaching nicety) but wrong for a
spend-abuse backstop, where a Supabase outage must never become "call the
paid provider for free." `consume_ai_quota_or_503` raises on any failure
(RPC error, missing service-role client, `granted: false`) rather than
returning a degraded-but-still-usable dict — callers should not, and cannot,
accidentally treat a quota-infra failure as permission to proceed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import HTTPException

log = logging.getLogger("uvicorn.error")


class QuotaDenied(HTTPException):
    """Raised when consume_ai_quota returns granted: false. Distinguishes a
    quota/invite denial (4xx, caller's fault) from quota-infra failure
    (503, our fault) so route handlers don't need to inspect status codes to
    tell the two apart."""


async def consume_ai_quota_or_503(db, user_id: str, feature: str, idempotency_key: str) -> dict[str, Any]:
    """Consume one unit of quota for (user_id, feature, idempotency_key).

    Returns the RPC's data dict on success (granted: true). Raises
    QuotaDenied(403) if the invite has not been redeemed, QuotaDenied(429)
    if the daily cap is reached, or HTTPException(503) if quota state
    could not be consulted at all (db is None, RPC raises, or the response
    is malformed) — the caller must not invoke the paid provider in that
    case.
    """
    if db is None:
        raise HTTPException(status_code=503, detail={"error": "quota_service_unavailable"})
    try:
        res = await asyncio.to_thread(
            lambda: db.rpc(
                "consume_ai_quota",
                {"p_user_id": user_id, "p_feature": feature, "p_idempotency_key": idempotency_key},
            ).execute()
        )
        data = res.data or {}
        if "granted" not in data:
            raise ValueError(f"malformed consume_ai_quota response: {data!r}")
    except HTTPException:
        raise
    except Exception as e:
        log.warning("ai_quota: consume_ai_quota RPC failed for feature=%s: %s", feature, e)
        raise HTTPException(status_code=503, detail={"error": "quota_service_unavailable"}) from e

    if not data.get("granted"):
        reason = data.get("reason", "denied")
        status_code = 403 if reason == "invite_not_redeemed" else 429
        raise QuotaDenied(status_code=status_code, detail={"error": reason, **data})

    return data


async def release_ai_quota_grant(db, user_id: str, feature: str, idempotency_key: str) -> None:
    """Compensating delete for a grant that was consumed but whose provider
    call then failed (not a quota-infra failure — the grant was real, the
    work it paid for never completed). Logged at WARNING on failure, never
    raised: a failed refund must not fail the response to the caller, who
    already has their real result or real error from the provider call."""
    if db is None:
        return
    try:
        await asyncio.to_thread(
            lambda: db.rpc(
                "release_ai_quota_grant",
                {"p_user_id": user_id, "p_feature": feature, "p_idempotency_key": idempotency_key},
            ).execute()
        )
    except Exception as e:
        log.warning(
            "ai_quota: release_ai_quota_grant RPC failed for feature=%s (user stays charged): %s", feature, e
        )
