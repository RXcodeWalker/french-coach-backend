"""Auth helpers for the admin content layer.

Extracted JWT verification (returns full payload, not just the user id) plus a
FastAPI dependency that gates admin-only routes on JWT `app_metadata.role`.

`app_metadata` is writable only with the service-role key, so it is the correct
place to store trust-level roles — anon/user tokens cannot forge it.
"""

from __future__ import annotations

import os

import jwt as pyjwt
from fastapi import Header, HTTPException

SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "").strip()


def verify_supabase_jwt(authorization: str | None) -> dict:
    """Verify a Supabase JWT and return the full decoded payload.

    Raises HTTP 401 on a missing/invalid/expired token, 503 if the server has
    no JWT secret configured.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.split(" ", 1)[1]
    if not SUPABASE_JWT_SECRET:
        raise HTTPException(status_code=503, detail="Auth not configured on server")
    try:
        return pyjwt.decode(
            token,
            SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")


def _role_from_payload(payload: dict) -> str | None:
    meta = payload.get("app_metadata") or {}
    return meta.get("role")


async def require_admin(authorization: str | None = Header(None)) -> dict:
    """FastAPI dependency — 403 unless the caller's JWT carries an admin role."""
    payload = verify_supabase_jwt(authorization)
    if _role_from_payload(payload) != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return payload


def grant_admin(supabase_admin, user_id: str) -> None:
    """Promote a user to admin by writing app_metadata via the service role.

    `supabase_admin` must be a client created with the service-role key.
    """
    supabase_admin.auth.admin.update_user_by_id(
        user_id, {"app_metadata": {"role": "admin"}}
    )
