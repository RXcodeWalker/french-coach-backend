"""Sentry beyond errors for the FastAPI service (lib/observability.py): the
trial's sampling, the extra scrubbing (transactions, logs, metrics), who a
request is attributed to, AI-provider spans and the metrics around them.

Same approach as test_sentry_observability.py: the real sentry-sdk with the
service's own options and a recording transport, so what is asserted is what
would have been sent.
"""
from __future__ import annotations

import asyncio
import logging
import time

import jwt as pyjwt
import pytest
import sentry_sdk
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sentry_sdk.transport import Transport

from lib import auth, observability
from lib.observability import (
    identify_user,
    record_request,
    scrub_telemetry_item,
    scrub_transaction,
    sentry_options,
    traced_provider,
)

SECRET = "eyJhbGciOi.secret-token.sig"
_JWT_SECRET = "unit-test-secret-that-is-long-enough-for-hs256"
DSN = "https://public@o0.ingest.sentry.io/0"


class AllItemsTransport(Transport):
    """Keeps every envelope item's JSON payload, by type."""

    def __init__(self, options=None):
        super().__init__(options)
        self.items: dict[str, list[dict]] = {}

    def capture_envelope(self, envelope):
        for item in envelope.items:
            try:
                payload = item.payload.json
            except Exception:  # binary items (profiles)
                continue
            self.items.setdefault(item.type, []).append(payload)

    def logs(self) -> list[dict]:
        return [entry for payload in self.items.get("log", []) for entry in payload.get("items", [])]

    def metrics(self) -> list[dict]:
        return [entry for payload in self.items.get("trace_metric", []) for entry in payload.get("items", [])]


@pytest.fixture()
def sentry():
    transport = AllItemsTransport()
    options = sentry_options({"SENTRY_DSN": DSN, "RENDER_GIT_COMMIT": "abc123", "RENDER": "true"})
    sentry_sdk.init(**options, transport=transport)
    yield transport
    sentry_sdk.flush()
    sentry_sdk.init()  # back to a disabled client for the rest of the suite


def _token(sub: str, **claims) -> str:
    return pyjwt.encode({"sub": sub, "exp": int(time.time()) + 3600, **claims}, _JWT_SECRET, algorithm="HS256")


# ── sampling ─────────────────────────────────────────────────────────────────


def test_everything_is_traced_by_default_except_health():
    sampler = sentry_options({"SENTRY_DSN": DSN})["traces_sampler"]
    assert sampler({"asgi_scope": {"path": "/api/feedback"}}) == 1.0
    assert sampler({"asgi_scope": {"path": "/health"}}) == 0.0
    assert sampler({}) == 1.0


def test_a_sampled_parent_decides():
    sampler = sentry_options({"SENTRY_DSN": DSN})["traces_sampler"]
    assert sampler({"asgi_scope": {"path": "/api/x"}, "parent_sampled": False}) == 0.0
    assert sampler({"asgi_scope": {"path": "/api/x"}, "parent_sampled": True}) == 1.0
    # ...but never a health poll, whoever asked.
    assert sampler({"asgi_scope": {"path": "/health"}, "parent_sampled": True}) == 0.0


def test_rates_come_from_the_environment_and_ignore_nonsense():
    opts = sentry_options({"SENTRY_DSN": DSN, "SENTRY_TRACES_SAMPLE_RATE": "0.2", "SENTRY_PROFILES_SAMPLE_RATE": "0"})
    assert opts["traces_sampler"]({"asgi_scope": {"path": "/x"}}) == 0.2
    assert opts["profile_session_sample_rate"] == 0.0
    bad = sentry_options({"SENTRY_DSN": DSN, "SENTRY_TRACES_SAMPLE_RATE": "lots", "SENTRY_PROFILES_SAMPLE_RATE": "7"})
    assert bad["traces_sampler"]({"asgi_scope": {"path": "/x"}}) == 1.0
    assert bad["profile_session_sample_rate"] == 0.25


def test_logs_metrics_and_profiling_are_on():
    opts = sentry_options({"SENTRY_DSN": DSN})
    assert opts["enable_logs"] is True and opts["enable_metrics"] is True
    assert opts["profile_lifecycle"] == "trace" and opts["profile_session_sample_rate"] == 0.25
    # Nothing is collected about the caller on its own: identity is explicit (identify_user).
    assert opts["send_default_pii"] is False and opts["max_request_body_size"] == "never"
    assert opts["include_local_variables"] is False


# ── scrubbing ────────────────────────────────────────────────────────────────


def test_a_transaction_is_scrubbed_down_to_its_spans():
    event = {
        "request": {"url": "https://api.example/x?token=abc", "headers": {"Authorization": f"Bearer {SECRET}"}, "data": {"t": "Je suis allé"}},
        "spans": [
            {"description": "GET https://api.example/jwks?access_token=abc", "data": {"http.query": "token=abc", "http.response.status_code": 200}},
            {"description": "groq feedback", "data": {"ai.provider": "groq"}},
        ],
    }
    out = scrub_transaction(event, {})
    assert out["request"]["headers"]["Authorization"] == "[Filtered]"
    assert "data" not in out["request"]
    assert out["spans"][0]["description"] == "GET https://api.example/jwks?access_token=[Filtered]"
    assert out["spans"][0]["data"] == {"http.query": "token=[Filtered]", "http.response.status_code": 200}
    assert out["spans"][1]["data"] == {"ai.provider": "groq"}


def test_a_log_is_scrubbed_and_a_health_poll_line_is_dropped():
    item = {"body": f"refresh failed token=abc123secret Bearer {SECRET}", "attributes": {"url": "https://x/?access_token=abc", "n": 3}}
    out = scrub_telemetry_item(item, {})
    assert "abc123secret" not in out["body"] and SECRET not in out["body"]
    assert out["attributes"] == {"url": "https://x/?access_token=[Filtered]", "n": 3}
    assert scrub_telemetry_item({"body": '{"endpoint": "/health", "status_code": 200}'}, {}) is None
    assert scrub_telemetry_item({"body": '{"endpoint": "/api/feedback", "status_code": 200}'}, {}) is not None


# ── who a request belongs to ─────────────────────────────────────────────────


def test_a_verified_learner_is_on_their_own_requests_events(sentry, monkeypatch):
    monkeypatch.setattr(auth, "SUPABASE_JWT_SECRET", _JWT_SECRET)
    app = FastAPI()

    @app.post("/who")
    async def who(authorization: str | None = None):
        # A real route verifies through verify_supabase_jwt, as main.verify_jwt does.
        payload = auth.verify_supabase_jwt(authorization, hs_secret=_JWT_SECRET)
        raise RuntimeError(f"boom for {payload['sub']}")

    @app.post("/anon")
    async def anon():
        raise RuntimeError("boom for nobody")

    client = TestClient(app, raise_server_exceptions=False)
    token = _token("11111111-1111-1111-1111-111111111111", email="ada@example.com", app_metadata={"role": "admin"})
    assert client.post("/who", params={"authorization": f"Bearer {token}"}).status_code == 500
    assert client.post("/anon").status_code == 500
    sentry_sdk.flush()

    events = {(e["exception"]["values"][0]["value"]): e for e in sentry.items["event"]}
    who_event = events["boom for 11111111-1111-1111-1111-111111111111"]
    assert who_event["user"]["id"] == "11111111-1111-1111-1111-111111111111"
    assert who_event["user"]["email"] == "ada@example.com"
    assert who_event["tags"]["user_role"] == "admin" and who_event["tags"]["is_guest"] == "false"
    anon_event = events["boom for nobody"]
    assert "id" not in (anon_event.get("user") or {})
    assert "user_role" not in anon_event.get("tags", {})


def test_a_guest_is_marked_as_one(sentry):
    app = FastAPI()

    @app.post("/guest")
    async def guest():
        identify_user("guest:203.0.113.9", is_guest=True)
        raise RuntimeError("boom for a guest")

    TestClient(app, raise_server_exceptions=False).post("/guest")
    sentry_sdk.flush()
    event = sentry.items["event"][0]
    assert event["user"] == {"id": "guest:203.0.113.9"}
    assert event["tags"]["is_guest"] == "true"


def test_identify_is_a_noop_without_sentry():
    sentry_sdk.init()  # disabled client
    identify_user("someone")  # must not raise


# ── AI provider spans and metrics ────────────────────────────────────────────


def test_a_provider_call_is_a_span_a_counter_and_a_latency(sentry):
    @traced_provider("groq", "feedback")
    async def call(prompt: str) -> dict:
        await asyncio.sleep(0.01)
        return {"ok": prompt}

    @traced_provider("gemini", "feedback")
    async def failing() -> dict:
        raise TimeoutError("gemini timed out")

    app = FastAPI()

    @app.post("/p")
    async def route():
        result = await call("hello")
        try:
            await failing()
        except TimeoutError:
            pass
        return result

    assert TestClient(app).post("/p").json() == {"ok": "hello"}
    sentry_sdk.flush()

    spans = [s for tx in sentry.items["transaction"] for s in tx["spans"] if s.get("op") == "ai.provider"]
    assert {s["description"] for s in spans} == {"groq feedback", "gemini feedback"}
    outcomes = {s["description"]: s["data"]["ai.outcome"] for s in spans}
    assert outcomes == {"groq feedback": "ok", "gemini feedback": "error"}

    metrics = sentry.metrics()
    calls = {(m["attributes"]["provider"]["value"], m["attributes"]["outcome"]["value"]) for m in metrics if m["name"] == "provider.calls"}
    assert calls == {("groq", "ok"), ("gemini", "error")}
    latency = next(m for m in metrics if m["name"] == "provider.latency_ms" and m["attributes"]["provider"]["value"] == "groq")
    assert latency["type"] == "distribution" and latency["unit"] == "millisecond" and latency["value"] >= 10


@pytest.mark.asyncio
async def test_a_traced_provider_changes_nothing_when_sentry_is_off():
    sentry_sdk.init()

    @traced_provider("groq", "feedback")
    async def call(x: int, *, y: int = 1) -> int:
        return x + y

    @traced_provider("groq", "feedback")
    async def failing() -> None:
        raise ValueError("as before")

    assert await call(2, y=3) == 5
    assert call.__name__ == "call"
    with pytest.raises(ValueError, match="as before"):
        await failing()


def test_a_traced_provider_only_wraps_async_functions():
    with pytest.raises(TypeError):
        traced_provider("groq", "x")(lambda: None)


# ── request metrics ──────────────────────────────────────────────────────────


def test_requests_are_counted_by_route_and_a_429_is_a_denial(sentry):
    record_request("/api/feedback", 200, 812.5, provider="groq", cached=True)
    record_request("/api/feedback", 429, 4.0)
    record_request("/health", 200, 1.0)  # polled all day: never recorded
    sentry_sdk.flush()
    metrics = sentry.metrics()
    requests = [m for m in metrics if m["name"] == "api.requests"]
    assert {m["attributes"]["status"]["value"] for m in requests} == {200, 429}
    assert all(m["attributes"]["route"]["value"] == "/api/feedback" for m in requests)
    assert any(m["attributes"].get("provider", {}).get("value") == "groq" for m in requests)
    assert [m["attributes"]["route"]["value"] for m in metrics if m["name"] == "api.quota_denied"] == ["/api/feedback"]
    assert not any("/health" in str(m["attributes"]) for m in metrics)


# ── logs ─────────────────────────────────────────────────────────────────────


def test_application_logs_become_sentry_logs_and_noisy_ones_do_not(sentry):
    # main.py runs logging.basicConfig(level=INFO); a bare test process has WARNING.
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("french-coach").info("native_voice stored %s (%d characters)", "tts/abc.mp3", 42)
    logging.getLogger("french-coach").error("provider said token=abc123secret")
    logging.getLogger("uvicorn.access").info('127.0.0.1 - "GET /api/x HTTP/1.1" 200')
    logging.getLogger("httpx").info('HTTP Request: POST https://api.groq.com/openai/v1/chat "HTTP/1.1 200 OK"')
    sentry_sdk.flush()

    bodies = [log["body"] for log in sentry.logs()]
    assert "native_voice stored tts/abc.mp3 (42 characters)" in bodies
    assert "provider said token=[Filtered]" in bodies
    assert not any("uvicorn" in b or "GET /api/x" in b or "api.groq.com" in b for b in bodies)
    # log.error stays a log and a breadcrumb, never an issue.
    assert sentry.items.get("event") is None
