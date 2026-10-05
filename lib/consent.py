"""Server-side speaking-consent gate for every route that receives audio.

Under-13 accounts need guardian confirmation before any audio capture or
provider call (main repo docs/systems/child-safety-consent.md, ADR 0006). The
client already gates recording (SpeakingConsentGate / useRecording(blocked)),
but some audio screens have no client gate and a client gate is not a control.
This is the first server-side check: it reads `profiles.consent_status`
(service-role write only, never client-writable) with the service key.

Contract:
- a guest (``guest:<ip>``, lib/guest.py) has no account and no profile row;
  guest access is a separate, switchable opening and is not consent-gated
  here, matching the client gate (which only blocks a signed-in `pending`
  account);
- `pending` -> 403 {"error": "consent_required"};
- no profile row at all -> 403 as well (deny): a deleted account, or a
  request racing a guardian revocation, must not reach a provider;
- consent state that can't be read (no service client, RPC error) -> 503:
  fail closed, same posture as consume_ai_quota_or_503.

Call it right after authentication and before the upload is processed.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import HTTPException

from lib.guest import is_guest

log = logging.getLogger("uvicorn.error")

CONSENT_REQUIRED = "consent_required"
_MISSING = object()


async def _fetch_consent_status(db, user_id: str):
    """`profiles.consent_status` for user_id, `_MISSING` for no row. Raises
    HTTPException(503) when the state can't be read. The single patchable
    seam for tests (tests/conftest.py stubs it suite-wide)."""
    if db is None:
        raise HTTPException(status_code=503, detail={"error": "consent_check_unavailable"})
    try:
        res = await asyncio.to_thread(
            lambda: db.table("profiles").select("consent_status").eq("id", user_id).limit(1).execute()
        )
    except Exception as e:
        log.warning("consent: profiles.consent_status read failed: %s", e)
        raise HTTPException(status_code=503, detail={"error": "consent_check_unavailable"}) from e
    rows = res.data or []
    if not rows:
        return _MISSING
    return rows[0].get("consent_status")


async def require_speaking_consent(db, user_id: str) -> None:
    if is_guest(user_id):
        return
    status = await _fetch_consent_status(db, user_id)
    if status is _MISSING or status == "pending":
        raise HTTPException(status_code=403, detail={"error": CONSENT_REQUIRED})
