"""Sentry for the FastAPI service (lib/observability.py).

End-to-end: the real sentry-sdk with the service's own options and a recording
transport in place of Sentry, against a small FastAPI app that uses the same
FastAPI/Starlette integrations. 5xx and provider failures arrive tagged, with
no request body or auth header; expected 4xx never arrive. Plus: main's retry
loop reports a provider failure once, not once per attempt.
"""
from __future__ import annotations

import json

import pytest
import sentry_sdk
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient
from sentry_sdk.transport import Transport

from lib import observability
from lib.observability import capture_provider_failure, capture_route_error, scrub_event, sentry_options

SECRET = "eyJhbGciOi.secret-token.sig"
BODY = {"transcript": "Je suis allé au cinéma avec mes amis"}


class RecordingTransport(Transport):
    def __init__(self, options=None):
        super().__init__(options)
        self.events: list[dict] = []

    def capture_envelope(self, envelope):
        for item in envelope.items:
            if item.type == "event":
                self.events.append(item.payload.json)


@pytest.fixture()
def sentry():
    transport = RecordingTransport()
    options = sentry_options({"SENTRY_DSN": "https://public@o0.ingest.sentry.io/0", "RENDER_GIT_COMMIT": "abc123", "RENDER": "true"})
    options["traces_sample_rate"] = 0  # only the error path is under test
    sentry_sdk.init(**options, transport=transport)
    observability._throttle.clear()
    yield transport
    sentry_sdk.flush()
    sentry_sdk.init()  # back to a disabled client for the rest of the suite


def _app() -> FastAPI:
    app = FastAPI()

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        capture_route_error(exc, route=request.url.path, stage="unhandled")
        return JSONResponse(status_code=500, content={"error": "internal_server_error"})

    @app.post("/boom")
    async def boom():
        raise RuntimeError("unexpected failure in handler")

    @app.post("/db-down")
    async def db_down():
        raise HTTPException(status_code=503, detail="Database not configured")

    @app.post("/provider")
    async def provider():
        try:
            raise TimeoutError("groq timed out after 25s")
        except TimeoutError as exc:
            capture_provider_failure("groq/openai/gpt-oss-120b timeout", exc)
        return {"ok": True, "provider": "offline"}

    @app.post("/stream")
    async def stream():
        async def gen():
            try:
                yield json.dumps({"type": "status"}) + "\n"
                raise ValueError("model returned malformed JSON")
            except ValueError as exc:
                capture_route_error(exc, route="/api/feedback/stream", stage="stream")
                yield json.dumps({"type": "error"}) + "\n"
        return StreamingResponse(gen(), media_type="application/x-ndjson")

    for path, status, detail in [
        ("/unauthorized", 401, "unauthorized"),
        ("/consent", 403, {"error": "consent_required"}),
        ("/already", 409, {"error": "already_generated"}),
        ("/invalid", 422, "invalid"),
        ("/quota", 429, {"error": "quota_exceeded"}),
    ]:
        async def handler(status=status, detail=detail):
            raise HTTPException(status_code=status, detail=detail)
        app.post(path)(handler)
    return app


def _post(client: TestClient, path: str):
    return client.post(path, json=BODY, headers={"Authorization": f"Bearer {SECRET}", "Cookie": "sb=abc"})


def _event_for(transport: RecordingTransport, message: str) -> dict | None:
    for event in transport.events:
        values = (event.get("exception") or {}).get("values") or []
        if any(v.get("value") == message for v in values):
            return event
    return None


def test_unhandled_error_is_captured_without_body_or_auth(sentry):
    client = TestClient(_app(), raise_server_exceptions=False)
    assert _post(client, "/boom").status_code == 500
    sentry_sdk.flush()
    event = _event_for(sentry, "unexpected failure in handler")
    assert event is not None
    print("[sentry fastapi event]", json.dumps({k: event.get(k) for k in ("release", "environment", "tags", "request", "transaction")}, indent=2, default=str))
    assert event["release"] == "abc123"
    assert event["environment"] == "production"
    assert event["tags"]["route"] == "/boom"
    request = event.get("request") or {}
    assert "data" not in request
    raw = json.dumps(event, default=str)
    assert SECRET not in raw
    assert "cinéma" not in raw
    assert "sb=abc" not in raw


def test_http_5xx_is_captured(sentry):
    client = TestClient(_app(), raise_server_exceptions=False)
    assert _post(client, "/db-down").status_code == 503
    sentry_sdk.flush()
    assert any("Database not configured" in json.dumps(e, default=str) for e in sentry.events)


def test_provider_failure_is_tagged(sentry):
    client = TestClient(_app(), raise_server_exceptions=False)
    assert _post(client, "/provider").status_code == 200
    sentry_sdk.flush()
    event = _event_for(sentry, "groq timed out after 25s")
    assert event is not None
    assert event["tags"]["provider"] == "groq"
    assert event["tags"]["failure"] == "timeout"
    assert event["extra"]["provider_detail"] == "groq/openai/gpt-oss-120b timeout"


def test_stream_error_is_captured(sentry):
    client = TestClient(_app(), raise_server_exceptions=False)
    assert _post(client, "/stream").status_code == 200
    sentry_sdk.flush()
    event = _event_for(sentry, "model returned malformed JSON")
    assert event is not None
    assert event["tags"] == {**event["tags"], "route": "/api/feedback/stream", "stage": "stream"}


@pytest.mark.parametrize("path,status", [("/unauthorized", 401), ("/consent", 403), ("/already", 409), ("/invalid", 422), ("/quota", 429)])
def test_expected_4xx_is_not_captured(sentry, path, status):
    client = TestClient(_app(), raise_server_exceptions=False)
    assert _post(client, path).status_code == status
    sentry_sdk.flush()
    assert sentry.events == []


def test_provider_outage_is_throttled(sentry):
    for _ in range(20):
        try:
            raise ConnectionError("gemini unreachable")
        except ConnectionError as exc:
            capture_provider_failure("gemini/gemini-3.6-flash", exc)
    sentry_sdk.flush()
    assert len(sentry.events) == observability._THROTTLE_MAX


def test_scrub_event_strips_secrets_and_drops_4xx():
    event = {
        "request": {
            "url": "https://api.example/api/transcribe?token=abc",
            "headers": {"Authorization": f"Bearer {SECRET}", "Cookie": "sb=abc", "User-Agent": "x"},
            "data": BODY,
            "cookies": {"sb": "abc"},
            "query_string": "access_token=abc&purpose=learn",
        },
        "exception": {"values": [{"value": f"401 with Bearer {SECRET}"}]},
    }
    out = scrub_event(event, {})
    assert out is not None
    assert "data" not in out["request"] and "cookies" not in out["request"]
    assert out["request"]["headers"] == {"Authorization": "[Filtered]", "Cookie": "[Filtered]", "User-Agent": "x"}
    assert out["request"]["url"] == "https://api.example/api/transcribe?token=[Filtered]"
    assert out["request"]["query_string"] == "access_token=[Filtered]&purpose=learn"
    assert out["exception"]["values"][0]["value"] == "401 with Bearer [Filtered]"

    quota = HTTPException(status_code=429, detail="quota")
    assert scrub_event({}, {"exc_info": (type(quota), quota, None)}) is None


def test_init_is_a_noop_without_dsn():
    assert observability.init_sentry({}) is False


@pytest.mark.asyncio
async def test_retry_loop_reports_a_provider_failure_once(monkeypatch):
    import main

    reported: list[str] = []
    monkeypatch.setattr(main, "capture_provider_failure", lambda provider, exc, **_: reported.append(provider))
    monkeypatch.setattr(main, "_RETRY_DELAYS", (0.0, 0.0))

    async def always_fails():
        raise ConnectionError("groq unreachable")

    with pytest.raises(ConnectionError):
        await main._run_with_retries("groq/test-model", always_fails, attempts=3, timeout_sec=1)
    assert reported == ["groq/test-model"]
