"""Sentry error tracking for the FastAPI service.

A no-op unless SENTRY_DSN is set (local dev, tests, CI). Release is
RENDER_GIT_COMMIT, the commit Render deployed.

What becomes a Sentry issue:
  * any 5xx response: unhandled exceptions and HTTPException(5xx), via the
    FastAPI/Starlette integrations (failed_request_status_codes = 500-599);
  * the catch-all 500 handler in main.py (explicit capture_exception);
  * the *final* failure of an upstream provider (Groq, Gemini, Whisper,
    Azure) through capture_provider_failure, tagged provider/failure/route —
    retries in between stay breadcrumbs;
  * an exception inside the streaming feedback generator (the response is
    already 200 by then, so nothing else would see it).

What never does: expected 4xx (401, 403 consent_required, 409, 422, 429
quota) — the integrations only report 5xx and before_send drops any 4xx
HTTPException that slips through. log.error lines are breadcrumbs, not
issues (event_level=None): main.py logs every provider retry at error level,
and those would otherwise flood the free tier as one badly-grouped issue.

Request bodies (audio uploads, transcripts) and stack-frame local variables
(which hold raw headers and provider keys) are never attached, and
Authorization/cookie headers and tokens in query strings are filtered — size
and security.
"""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Any

try:
    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration
except ImportError:  # pragma: no cover - sentry-sdk is in requirements.txt
    sentry_sdk = None  # type: ignore[assignment]

_FAILED_STATUS_CODES = set(range(500, 600))
_SECRET_HEADERS = {"authorization", "cookie", "set-cookie", "x-api-key", "apikey", "x-supabase-auth"}
_SECRET_QUERY = re.compile(r"((?:^|[?&#])(?:access_token|refresh_token|token|apikey|api_key|code|key)=)[^&#\s]*", re.I)
_BEARER = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+")

# At most this many reports per (provider, failure) per window, per process:
# a provider outage or a spent Gemini daily quota hits every request, and one
# issue with a handful of events says as much as thousands.
_THROTTLE_MAX = 5
_THROTTLE_WINDOW_SEC = 600.0
_throttle: dict[str, list[float]] = {}
_throttle_lock = threading.Lock()


def _redact(value: str) -> str:
    return _BEARER.sub("Bearer [Filtered]", _SECRET_QUERY.sub(r"\1[Filtered]", value))


def scrub_event(event: dict[str, Any], hint: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """before_send: drop expected 4xx, strip bodies, auth headers, cookies and tokens."""
    exc_info = (hint or {}).get("exc_info")
    if exc_info:
        exc = exc_info[1]
        status = getattr(exc, "status_code", None)
        if isinstance(status, int) and status < 500:
            return None

    request = event.get("request")
    if isinstance(request, dict):
        request.pop("data", None)
        request.pop("cookies", None)
        headers = request.get("headers")
        if isinstance(headers, dict):
            for key in list(headers):
                if key.lower() in _SECRET_HEADERS:
                    headers[key] = "[Filtered]"
        if isinstance(request.get("query_string"), str):
            request["query_string"] = _redact(request["query_string"])
        if isinstance(request.get("url"), str):
            request["url"] = _redact(request["url"])

    for value in (event.get("exception") or {}).get("values") or []:
        if isinstance(value.get("value"), str):
            value["value"] = _redact(value["value"])
    for crumb in (event.get("breadcrumbs") or {}).get("values") or []:
        if isinstance(crumb.get("message"), str):
            crumb["message"] = _redact(crumb["message"])
    return event


def sentry_options(env: dict[str, str] | os._Environ[str] = os.environ) -> dict[str, Any]:
    return {
        "dsn": env.get("SENTRY_DSN", "").strip(),
        "release": (env.get("RENDER_GIT_COMMIT") or env.get("SENTRY_RELEASE") or "").strip() or None,
        "environment": (env.get("SENTRY_ENVIRONMENT") or ("production" if env.get("RENDER") else "development")).strip(),
        "send_default_pii": False,
        "max_request_body_size": "never",
        # Frame locals would carry the raw ASGI scope (Authorization header)
        # and provider credentials (e.g. azure_client's subscription-key headers).
        "include_local_variables": False,
        # Free tier: every error, 5% of traces, no profiling.
        "sample_rate": 1.0,
        "traces_sample_rate": 0.05,
        "integrations": [
            StarletteIntegration(transaction_style="url", failed_request_status_codes=_FAILED_STATUS_CODES),
            FastApiIntegration(transaction_style="url", failed_request_status_codes=_FAILED_STATUS_CODES),
            LoggingIntegration(event_level=None),
        ],
        "before_send": scrub_event,
    }


def init_sentry(env: dict[str, str] | os._Environ[str] = os.environ) -> bool:
    """Initialise Sentry from SENTRY_DSN. Returns whether it is on."""
    if sentry_sdk is None or not env.get("SENTRY_DSN", "").strip():
        return False
    sentry_sdk.init(**sentry_options(env))
    return True


def _enabled() -> bool:
    return sentry_sdk is not None and sentry_sdk.is_initialized()


def _allow(key: str) -> bool:
    now = time.monotonic()
    with _throttle_lock:
        recent = [t for t in _throttle.get(key, []) if now - t < _THROTTLE_WINDOW_SEC]
        if len(recent) >= _THROTTLE_MAX:
            _throttle[key] = recent
            return False
        recent.append(now)
        _throttle[key] = recent
        return True


def _provider_tags(provider: str) -> tuple[str, str]:
    """'gemini/gemini-3.6-flash quota exhausted' -> ('gemini', 'quota exhausted')."""
    head, _, failure = provider.partition(" ")
    name = head.split("/")[0] or "unknown"
    return name, failure or "error"


def capture_provider_failure(provider: str, exc: BaseException, *, route: str | None = None) -> None:
    """Report an upstream provider's final failure, tagged provider/failure/route."""
    if not _enabled():
        return
    name, failure = _provider_tags(provider)
    if not _allow(f"{name}|{failure}|{type(exc).__name__}"):
        return
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("provider", name)
        scope.set_tag("failure", failure)
        if route:
            scope.set_tag("route", route)
        scope.set_extra("provider_detail", provider)
        sentry_sdk.capture_exception(exc)


def capture_route_error(exc: BaseException, *, route: str, stage: str | None = None) -> None:
    """Report a failure a route handled without answering 5xx (e.g. inside a stream)."""
    if not _enabled():
        return
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("route", route)
        if stage:
            scope.set_tag("stage", stage)
        sentry_sdk.capture_exception(exc)


def tag_request_route(path: str) -> None:
    """Tag every event from this request with its path (Sentry scopes are per request)."""
    if _enabled():
        sentry_sdk.set_tag("route", path)
