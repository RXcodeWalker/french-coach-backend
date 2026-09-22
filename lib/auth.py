"""Auth helpers: Supabase JWT verification plus the admin-role dependency.

**Why this is not a one-line `pyjwt.decode(..., algorithms=["HS256"])`.**
Supabase projects have moved from a single shared HS256 secret to asymmetric
*JWT signing keys*: access tokens are now signed ES256 (P-256) and carry a
`kid` naming the key, published at `{SUPABASE_URL}/auth/v1/.well-known/jwks.json`.
An HS256-only verifier rejects every one of those tokens with
`InvalidAlgorithmError`, which surfaced as a blanket 401 `{"detail":"Invalid
token"}` on every `verify_jwt`-gated endpoint (feedback, pronunciation, …) for
users who were, in fact, correctly signed in.

So: verify asymmetric tokens against the project JWKS (cached, refreshed on an
unknown `kid`), and keep HS256-with-the-shared-secret as the fallback for
legacy tokens issued before the migration — and for the tests, which mint their
own HS256 tokens. Requires PyJWT's `crypto` extra for the EC/RSA algorithms.

`app_metadata` is writable only with the service-role key, so it is the correct
place to store trust-level roles — anon/user tokens cannot forge it.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import httpx
import jwt as pyjwt
from fastapi import Header, HTTPException

# Kept as module globals (not read fresh from os.getenv on every use) because
# the test-suite monkeypatches these names directly. They are re-read from the
# environment at call time when blank, because main.py imports this module
# *before* it calls load_dotenv() — at import time the .env has not been read.
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()

# Asymmetric algorithms Supabase can issue. HS256 is handled separately: it is
# verified with the shared secret, never with a JWKS key.
_ASYMMETRIC_ALGS = ["ES256", "RS256"]

# Signing keys rotate rarely; a long TTL keeps the (blocking) fetch off the hot
# path. An unrecognised `kid` forces an out-of-band refresh, rate-limited by
# _JWKS_MIN_REFRESH_SEC so a stream of junk tokens cannot turn into a stream of
# outbound requests.
_JWKS_TTL_SEC = 600.0
_JWKS_MIN_REFRESH_SEC = 30.0
_JWKS_TIMEOUT_SEC = 5.0

_jwks_lock = threading.Lock()
_jwks_keys: dict[str, Any] = {}
_jwks_fetched_at = 0.0


def _env(name: str, current: str) -> str:
    return (current or os.getenv(name, "")).strip()


def _jwks_url() -> str:
    base = _env("SUPABASE_URL", SUPABASE_URL)
    return f"{base.rstrip('/')}/auth/v1/.well-known/jwks.json" if base else ""


def _refresh_jwks(force: bool = False) -> dict[str, Any]:
    """Fetch and cache the project's public signing keys, keyed by `kid`."""
    global _jwks_keys, _jwks_fetched_at
    url = _jwks_url()
    if not url:
        return {}
    with _jwks_lock:
        age = time.monotonic() - _jwks_fetched_at
        min_age = _JWKS_MIN_REFRESH_SEC if force else _JWKS_TTL_SEC
        if _jwks_keys and age < min_age:
            return _jwks_keys
        try:
            res = httpx.get(url, timeout=_JWKS_TIMEOUT_SEC)
            res.raise_for_status()
            keys = {}
            for jwk in res.json().get("keys", []):
                kid = jwk.get("kid")
                if not kid:
                    continue
                try:
                    keys[kid] = pyjwt.PyJWK(jwk)
                except Exception:
                    continue  # an algorithm this PyJWT build can't load
            # Only replace a populated cache with a populated result — an empty
            # or malformed response must not blind us to keys we already hold.
            if keys or not _jwks_keys:
                _jwks_keys = keys
            _jwks_fetched_at = time.monotonic()
        except Exception:
            # Network/HTTP failure: keep serving the cached keys, and let the
            # clock advance so we don't hammer a down endpoint per request.
            _jwks_fetched_at = time.monotonic()
        return _jwks_keys


def _signing_key(kid: str) -> Any | None:
    key = _refresh_jwks().get(kid)
    if key is None:
        # Unknown kid — most likely a rotation since the last fetch.
        key = _refresh_jwks(force=True).get(kid)
    return key


def decode_supabase_jwt(token: str, *, hs_secret: str | None = None) -> dict:
    """Decode+verify a Supabase access token. Raises HTTPException on failure."""
    secret = (hs_secret if hs_secret is not None else SUPABASE_JWT_SECRET) or ""
    secret = _env("SUPABASE_JWT_SECRET", secret)
    has_jwks = bool(_jwks_url())
    if not secret and not has_jwks:
        raise HTTPException(status_code=503, detail="Auth not configured on server")

    try:
        header = pyjwt.get_unverified_header(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

    alg = header.get("alg")
    kid = header.get("kid")

    try:
        if alg in _ASYMMETRIC_ALGS:
            if not has_jwks:
                raise HTTPException(status_code=503, detail="Auth not configured on server")
            key = _signing_key(kid) if kid else None
            if key is None:
                raise HTTPException(status_code=401, detail="Invalid token")
            payload = pyjwt.decode(
                token,
                key.key,
                algorithms=[alg],
                options={"verify_aud": False},
            )
        else:
            if not secret:
                raise HTTPException(status_code=503, detail="Auth not configured on server")
            payload = pyjwt.decode(
                token,
                secret,
                algorithms=["HS256"],
                options={"verify_aud": False},
            )
    except HTTPException:
        raise
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

    if not isinstance(payload, dict) or not payload.get("sub"):
        raise HTTPException(status_code=401, detail="Invalid token")
    return payload


def verify_supabase_jwt(authorization: str | None, *, hs_secret: str | None = None) -> dict:
    """Verify a Supabase JWT and return the full decoded payload.

    Raises HTTP 401 on a missing/invalid/expired token, 503 if the server has
    neither a JWT secret nor a JWKS endpoint configured.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.split(" ", 1)[1]
    return decode_supabase_jwt(token, hs_secret=hs_secret)


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
