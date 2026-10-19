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
