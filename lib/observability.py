"""Sentry for the FastAPI service: errors, traces, profiles, logs and metrics.

A no-op unless SENTRY_DSN is set (local dev, tests, CI). Release is
RENDER_GIT_COMMIT, the commit Render deployed.

Beyond errors (below), everything is on for the school trial:
  * traces for every request (SENTRY_TRACES_SAMPLE_RATE, default 1.0) — the
    FastAPI/Starlette/httpx integrations give request, middleware and outgoing
    HTTP spans, and `traced_provider` adds one per AI provider call. /health is
    never traced (Render and uptime monitors poll it all day);
  * profiles on a quarter of traced requests (SENTRY_PROFILES_SAMPLE_RATE,
    default 0.25: the sampler runs on a small shared CPU);
  * Python logging as Sentry logs (INFO and up; uvicorn's access log and httpx's
    request lines are left out — the traces already say that);
  * metrics: a counter and latency distribution per route, per AI provider call,
    and quota denials (`record_request`, `traced_provider`);
  * the signed-in learner's id/email and role on every event from their request
    (`identify_user`).

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

import functools
import inspect
import logging
import os
import re
import threading
import time
from typing import Any, Callable

try:
    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration, ignore_logger, ignore_logger_for_sentry_logs
    from sentry_sdk.integrations.starlette import StarletteIntegration
except ImportError:  # pragma: no cover - sentry-sdk is in requirements.txt
    sentry_sdk = None  # type: ignore[assignment]

_FAILED_STATUS_CODES = set(range(500, 600))
_SECRET_HEADERS = {"authorization", "cookie", "set-cookie", "x-api-key", "apikey", "x-supabase-auth"}
_SECRET_QUERY = re.compile(r"((?:^|[?&#])(?:access_token|refresh_token|token|apikey|api_key|code|key)=)[^&#\s]*", re.I)
_BEARER = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+")
# `token=…` in free text (a log line), not only in a URL.
_BARE_SECRET_PAIR = re.compile(r"\b((?:access_token|refresh_token|id_token|api_key|apikey|token|password|secret)=)[^&\s\"'<>]+", re.I)

# Polled all day by Render's health check and any uptime monitor: never traced.
_QUIET_PATHS = {"/health", "/healthz"}
# main.py's per-request JSON log line for those same polls.
_QUIET_LOG_LINE = re.compile(r'"endpoint":\s*"/healthz?"')
# Loggers whose lines the traces already say better (or that carry URLs).
_NOISY_LOGGERS = ("uvicorn.access", "httpx", "httpcore")

# At most this many reports per (provider, failure) per window, per process:
# a provider outage or a spent Gemini daily quota hits every request, and one
# issue with a handful of events says as much as thousands.
_THROTTLE_MAX = 5
_THROTTLE_WINDOW_SEC = 600.0
_throttle: dict[str, list[float]] = {}
_throttle_lock = threading.Lock()


def _redact(value: str) -> str:
    return _BARE_SECRET_PAIR.sub(r"\1[Filtered]", _BEARER.sub("Bearer [Filtered]", _SECRET_QUERY.sub(r"\1[Filtered]", value)))


def _redact_headers(headers: Any) -> None:
    if isinstance(headers, dict):
        for key in list(headers):
            if key.lower() in _SECRET_HEADERS:
                headers[key] = "[Filtered]"


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
        _redact_headers(request.get("headers"))
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
        data = crumb.get("data")
        if isinstance(data, dict):
            for key in ("url", "http.query"):
                if isinstance(data.get(key), str):
                    data[key] = _redact(data[key])
    return event


def scrub_transaction(event: dict[str, Any], hint: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """before_send_transaction: the event scrub, plus every span's description and URL data."""
    scrub_event(event, hint)
    for span in event.get("spans") or []:
        if isinstance(span.get("description"), str):
            span["description"] = _redact(span["description"])
        data = span.get("data")
        if isinstance(data, dict):
            for key, value in list(data.items()):
                if isinstance(value, str):
                    data[key] = _redact(value)
    return event


def scrub_telemetry_item(item: dict[str, Any], hint: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """before_send_log / before_send_metric: a body and string attributes, scrubbed like an event."""
    if isinstance(item.get("body"), str):
        if _QUIET_LOG_LINE.search(item["body"]):
            return None
        item["body"] = _redact(item["body"])
    attributes = item.get("attributes")
    if isinstance(attributes, dict):
        for key, value in list(attributes.items()):
            if isinstance(value, str):
                attributes[key] = _redact(value)
    return item


def _rate(env: Any, name: str, default: float) -> float:
    """A sample rate from the environment: a number in 0..1, else the default."""
    raw = str(env.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if 0.0 <= value <= 1.0 else default


def _traces_sampler_for(rate: float) -> Callable[[dict[str, Any]], float]:
    def sampler(context: dict[str, Any]) -> float:
        path = (context.get("asgi_scope") or {}).get("path")
        if path in _QUIET_PATHS:
            return 0.0
        parent = context.get("parent_sampled")
        if parent is not None:
            return 1.0 if parent else 0.0
        return rate

    return sampler


def sentry_options(env: dict[str, str] | os._Environ[str] = os.environ) -> dict[str, Any]:
    # Registered with the logging integration whichever way init is reached
    # (idempotent): their lines would only repeat what the traces say.
    for noisy in _NOISY_LOGGERS:
        ignore_logger(noisy)  # breadcrumbs and events
        ignore_logger_for_sentry_logs(noisy)  # Sentry Logs
    return {
        "dsn": env.get("SENTRY_DSN", "").strip(),
        "release": (env.get("RENDER_GIT_COMMIT") or env.get("SENTRY_RELEASE") or "").strip() or None,
        "environment": (env.get("SENTRY_ENVIRONMENT") or ("production" if env.get("RENDER") else "development")).strip(),
        # The learner is identified explicitly from their verified token
        # (identify_user); nothing is collected automatically.
        "send_default_pii": False,
        "max_request_body_size": "never",
        # Frame locals would carry the raw ASGI scope (Authorization header)
        # and provider credentials (e.g. azure_client's subscription-key headers).
        "include_local_variables": False,
        "attach_stacktrace": True,
        "max_breadcrumbs": 200,
        "in_app_include": ["main", "lib", "routers", "services", "models"],
        "sample_rate": 1.0,
        # Every request is traced for the school trial; /health never is.
        "traces_sampler": _traces_sampler_for(_rate(env, "SENTRY_TRACES_SAMPLE_RATE", 1.0)),
        # Profiling runs on a small shared CPU, so it is a sample of traced requests.
        "profile_session_sample_rate": _rate(env, "SENTRY_PROFILES_SAMPLE_RATE", 0.25),
        "profile_lifecycle": "trace",
        "enable_logs": True,
        "enable_metrics": True,
        "integrations": [
            StarletteIntegration(transaction_style="url", failed_request_status_codes=_FAILED_STATUS_CODES),
            FastApiIntegration(transaction_style="url", failed_request_status_codes=_FAILED_STATUS_CODES),
            # log.error lines are breadcrumbs and Sentry logs, never issues (event_level=None).
            LoggingIntegration(level=logging.INFO, event_level=None, sentry_logs_level=logging.INFO),
        ],
        "before_send": scrub_event,
        "before_send_transaction": scrub_transaction,
        "before_send_log": scrub_telemetry_item,
        "before_send_metric": scrub_telemetry_item,
    }


def init_sentry(env: dict[str, str] | os._Environ[str] = os.environ) -> bool:
    """Initialise Sentry from SENTRY_DSN. Returns whether it is on."""
    if sentry_sdk is None or not env.get("SENTRY_DSN", "").strip():
        return False
    sentry_sdk.init(**sentry_options(env))
    # Set SENTRY_TEST_EVENT=1 on Render to confirm the wiring (one event per
    # boot), then remove it.
    if env.get("SENTRY_TEST_EVENT", "").strip() == "1":
        sentry_sdk.capture_message("Sentry test event from the FastAPI backend (SENTRY_TEST_EVENT=1)", level="error")
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


def identify_user(user_id: str, *, email: str | None = None, role: str | None = None, is_guest: bool = False) -> None:
    """Attribute this request's events, traces and logs to a learner.

    Called with a *verified* identity only (a Supabase JWT that passed
    verification, or the guest id the service itself derived), so a browser can
    never name someone else. The request's scope is its own (Starlette's
    middleware forks one per request), so this cannot reach another request.
    """
    if not _enabled() or not user_id:
        return
    user: dict[str, Any] = {"id": user_id}
    if email:
        user["email"] = email
    sentry_sdk.set_user(user)
    sentry_sdk.set_tag("is_guest", "true" if is_guest else "false")
    if role:
        sentry_sdk.set_tag("user_role", role)


def identify_from_jwt_payload(payload: dict[str, Any]) -> None:
    """identify_user for a verified Supabase token's claims (id, email, app_metadata.role)."""
    meta = payload.get("app_metadata") or {}
    identify_user(
        str(payload.get("sub") or ""),
        email=(str(payload["email"]).strip() or None) if payload.get("email") else None,
        role=str(meta["role"]) if isinstance(meta, dict) and meta.get("role") else None,
    )


def record_request(route: str, status: int, latency_ms: float, *, provider: str | None = None, cached: bool = False) -> None:
    """One finished request as metrics: a counter and a latency distribution per route, and quota denials.

    `route` is the route template (bounded), never a raw path.
    """
    if not _enabled() or route in _QUIET_PATHS:
        return
    attributes: dict[str, Any] = {"route": route, "status": status, "status_class": f"{status // 100}xx"}
    if provider:
        attributes["provider"] = provider
    if cached:
        attributes["cached"] = True
    sentry_sdk.metrics.count("api.requests", 1, attributes=attributes)
    sentry_sdk.metrics.distribution("api.latency_ms", latency_ms, unit="millisecond", attributes=attributes)
    if status == 429:
        sentry_sdk.metrics.count("api.quota_denied", 1, attributes={"route": route})


def _record_provider_call(provider: str, operation: str, outcome: str, started: float) -> None:
    attributes = {"provider": provider, "operation": operation, "outcome": outcome}
    sentry_sdk.metrics.count("provider.calls", 1, attributes=attributes)
    sentry_sdk.metrics.distribution(
        "provider.latency_ms", (time.monotonic() - started) * 1000.0, unit="millisecond", attributes=attributes
    )


def traced_provider(provider: str, operation: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator for an async function that calls an upstream AI provider (Groq,
    Gemini, Whisper): a span in the request's trace plus a call counter and
    latency distribution by provider/operation/outcome. Changes nothing the
    function returns or raises; costs one `is_initialized` check when Sentry is off.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        if not inspect.iscoroutinefunction(fn):  # pragma: no cover - guards a future misuse
            raise TypeError(f"traced_provider wraps async functions; {fn.__name__} is not one")

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not _enabled():
                return await fn(*args, **kwargs)
            started = time.monotonic()
            outcome = "error"
            with sentry_sdk.start_span(op="ai.provider", name=f"{provider} {operation}") as span:
                span.set_data("ai.provider", provider)
                span.set_data("ai.operation", operation)
                try:
                    result = await fn(*args, **kwargs)
                    outcome = "ok"
                    return result
                except Exception:
                    span.set_status("internal_error")
                    raise
                finally:
                    span.set_data("ai.outcome", outcome)
                    _record_provider_call(provider, operation, outcome, started)

        return wrapper

    return decorator
