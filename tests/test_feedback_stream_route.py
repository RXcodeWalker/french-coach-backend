"""/api/feedback/stream end to end, with the providers stubbed.

Regression: `question = question[:…]` inside the route's nested generate() made `question` local to
it, so every stream raised UnboundLocalError right after the `transcript` event and Learn fell back
to /api/feedback/v3 on every answer (since 68d3e8f). These tests drive the real route.

Run: pytest tests/test_feedback_stream_route.py
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.pop("AZURE_SPEECH_KEY", None)
os.environ.pop("AZURE_SPEECH_REGION", None)

import main  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "verify_jwt", lambda authorization: "user-1")

    async def _granted(*a, **kw):
        return {"granted": True}

    monkeypatch.setattr(main, "consume_ai_quota_or_503", _granted)
    if main._limiter is not None:
        monkeypatch.setattr(main._limiter, "enabled", False)
    monkeypatch.setattr(main, "_feedback_cache", main.BoundedTTLCache(50, 300.0))
    from fastapi.testclient import TestClient

    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture
def groq_stream(monkeypatch):
    prompts: list[str] = []

    async def fake_stream(prompt, depth, on_section, **kw):
        prompts.append(prompt)
        return {
            "fluency": 6.0,
            "scores": {"comm": 6, "know": 6, "acc": 6},
            "cefrLevel": "A2",
            "modelUsed": "groq/test",
        }

    monkeypatch.setattr(main, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(main, "get_groq", lambda: object())
    monkeypatch.setattr(main, "_stream_groq", fake_stream)
    return prompts


def _events(resp):
    return [json.loads(line) for line in resp.text.splitlines() if line.strip()]


def test_the_stream_reaches_complete(client, groq_stream):
    events = _events(client.post(
        "/api/feedback/stream",
        json={"question": "Décris ta famille.", "transcript": "Ma famille est grande."},
        headers={"Authorization": "Bearer x"},
    ))
    assert [e["type"] for e in events] == ["status", "transcript", "status", "complete"]
    assert events[-1]["data"]["scores"] == {"comm": 6, "know": 6, "acc": 6}
    assert len(groq_stream) == 1


def test_the_stream_still_caps_the_question(client, groq_stream):
    long_question = "Q" * (main._FEEDBACK_QUESTION_MAX_CHARS + 500)
    events = _events(client.post(
        "/api/feedback/stream",
        json={"question": long_question, "transcript": "Bonjour."},
        headers={"Authorization": "Bearer x"},
    ))
    assert events[-1]["type"] == "complete"
    assert "Q" * main._FEEDBACK_QUESTION_MAX_CHARS in groq_stream[0]
    assert "Q" * (main._FEEDBACK_QUESTION_MAX_CHARS + 1) not in groq_stream[0]


def test_an_unscored_streamed_reply_is_not_cached(client, monkeypatch):
    # A reply without scores is shown as malformed_response; caching it served
    # that same unscored reply to every retry instead of asking the model again.
    calls: list[int] = []

    async def unscored(prompt, depth, on_section, **kw):
        calls.append(1)
        return {"best_moment": "x", "modelUsed": "groq/test"}

    monkeypatch.setattr(main, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(main, "get_groq", lambda: object())
    monkeypatch.setattr(main, "_stream_groq", unscored)
    body = {"question": "Décris ta famille.", "transcript": "Ma famille est grande."}
    for _ in range(2):
        events = _events(client.post("/api/feedback/stream", json=body, headers={"Authorization": "Bearer x"}))
        assert events[-1]["data"]["providerStatus"] == "malformed_response"
    assert len(calls) == 2


def test_a_scored_streamed_reply_is_cached(client, groq_stream):
    body = {"question": "Décris ta famille.", "transcript": "Ma famille est grande."}
    for _ in range(2):
        client.post("/api/feedback/stream", json=body, headers={"Authorization": "Bearer x"})
    assert len(groq_stream) == 1


def test_stream_and_v3_charge_the_same_key_without_a_question(client, groq_stream, monkeypatch):
    # The stream falls back to /v3 on failure; both must use one quota key, or
    # the fallback is charged twice.
    keys: list[str] = []

    async def record(db, user_id, feature, key):
        keys.append(key)
        return {"granted": True}

    async def groq(prompt, depth="standard", **kw):
        return {"scores": {"comm": 6, "know": 6, "acc": 6}, "modelUsed": "groq/test"}

    monkeypatch.setattr(main, "consume_ai_quota_or_503", record)
    monkeypatch.setattr(main, "_feedback_cache", main.BoundedTTLCache(50, 300.0))
    monkeypatch.setattr(main, "_call_groq", groq)
    body = {"question": "", "transcript": "Ma famille est grande.", "enginePreference": "groq"}
    client.post("/api/feedback/stream", json=body, headers={"Authorization": "Bearer x"})
    monkeypatch.setattr(main, "_feedback_cache", main.BoundedTTLCache(50, 300.0))
    assert client.post("/api/feedback/v3", json=body, headers={"Authorization": "Bearer x"}).status_code == 200
    assert len(keys) == 2 and keys[0] == keys[1]
