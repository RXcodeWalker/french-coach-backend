"""Guest (signed-out) access to the AI endpoints — a temporary, switchable opening.

Phase 3 put every AI route behind a verified Supabase JWT, which made guest mode
(a supported way into the app) useless for anything AI. This re-opens those
routes to callers that send *no* Authorization header at all, while keeping a
spend backstop:

- A guest has no `auth.users` row, so the per-user Supabase quota RPC
  (`consume_ai_quota`, FK + invite gate) cannot apply. Guests are instead
  identified as ``guest:<client ip>`` and capped by a small in-process daily
  counter per (guest, feature). It resets on restart and is per-instance — a
  backstop against casual abuse, not a ledger. The IP comes from the first
  X-Forwarded-For hop, so it is spoofable; the per-route slowapi limits still
  apply on top.
- A token that IS sent must still verify: an expired/invalid session is a 401
  (the client's "sign in again" path), never silently downgraded to a guest.

Kill switch: set ``GUEST_AI_ENABLED=0`` and every route is back to JWT-only.
Caps (lifetime of the process, not daily — the app itself shows guests 3 free
attempts then asks them to sign in; these are the looser server backstop):
``GUEST_AI_LIMIT`` (default 5: feedback / roleplay / examiner calls) and
``GUEST_AI_AUDIO_LIMIT`` (default 8: transcribe / pronunciation, which a single
attempt can use more than once). Both reset when the server restarts.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Callable

from fastapi import Request

from lib.auth import verify_supabase_jwt

GUEST_PREFIX = "guest:"
_DEFAULT_LIMIT = 5
_DEFAULT_AUDIO_LIMIT = 8
_AUDIO_FEATURES = {"transcribe", "pronunciation"}
_MAX_TRACKED = 50_000  # hard bound on memory if someone sprays IPs

_lock = threading.Lock()
# (guest_id, feature) -> idempotency keys already charged
_grants: dict[tuple[str, str], set[str]] = {}


def guest_ai_enabled() -> bool:
    return os.getenv("GUEST_AI_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")


def _limit_for(feature: str) -> int:
    name, default = (
        ("GUEST_AI_AUDIO_LIMIT", _DEFAULT_AUDIO_LIMIT)
        if feature in _AUDIO_FEATURES
        else ("GUEST_AI_LIMIT", _DEFAULT_LIMIT)
    )
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def is_guest(user_id: str | None) -> bool:
    return bool(user_id) and str(user_id).startswith(GUEST_PREFIX)


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip() or "unknown"
    return request.client.host if request.client else "unknown"


def verify_user_or_guest(
    authorization: str | None,
    request: Request,
    verifier: Callable[[str | None], str] | None = None,
) -> str:
    """User id for a signed-in caller, ``guest:<ip>`` for a header-less one (when
    enabled), else whatever the verifier raises (401). A present-but-bad token
    always goes to the verifier. ``verifier`` returns the user id; callers pass
    their module's own verify function so it stays the one patchable seam."""
    if not authorization and guest_ai_enabled():
        return f"{GUEST_PREFIX}{_client_ip(request)}"
    if verifier is not None:
        return verifier(authorization)
    return str(verify_supabase_jwt(authorization)["sub"])


def consume_guest_quota(guest_id: str, feature: str, idempotency_key: str) -> dict[str, Any]:
    """Same contract as ai_quota.consume_ai_quota_or_503 for a guest: returns a
    granted dict (``replayed`` true for an already-charged key) or raises 429."""
    from lib.ai_quota import QuotaDenied  # local: ai_quota imports this module

    limit = _limit_for(feature)
    with _lock:
        bucket_key = (guest_id, feature)
        if bucket_key not in _grants and len(_grants) >= _MAX_TRACKED:
            raise QuotaDenied(status_code=429, detail={"error": "guest_limit_reached", "limit": limit})
        keys = _grants.setdefault(bucket_key, set())
        if idempotency_key in keys:
            return {"granted": True, "replayed": True, "used": len(keys), "limit": limit}
        if len(keys) >= limit:
            raise QuotaDenied(
                status_code=429,
                detail={"error": "guest_limit_reached", "used": len(keys), "limit": limit, "granted": False},
            )
        keys.add(idempotency_key)
        return {"granted": True, "replayed": False, "used": len(keys), "limit": limit}


def release_guest_grant(guest_id: str, feature: str, idempotency_key: str) -> None:
    with _lock:
        _grants.get((guest_id, feature), set()).discard(idempotency_key)
