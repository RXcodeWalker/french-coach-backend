"""Regression tests for client engine-preference routing in call_ai_feedback.

The bug: the /api/feedback* endpoints read only `model` from the request body,
but the web client sends its choice as `enginePreference` (top-level JSON, or
inside the multipart `data` field). The preference was therefore always dropped,
and call_ai_feedback ran a hardcoded Gemini-first chain. A client that asked for
Groq — and budgeted a short Groq timeout accordingly — still paid the full Gemini
latency before Groq was called, which surfaced in the browser as "groq timed out".

No live network call is made: both provider callables are monkeypatched.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main


def _stub_providers(monkeypatch, calls: list[str], *, gemini_ok=True, groq_ok=True):
    async def fake_gemini(prompt, *a, **kw):
        calls.append("gemini")
        if not gemini_ok:
            raise RuntimeError("gemini down")
        return {"scores": {"comm": 7}, "modelUsed": f"gemini/{main.GEMINI_MODEL}"}

    async def fake_groq(prompt, depth="standard"):
        calls.append("groq")
        if not groq_ok:
            raise RuntimeError("groq down")
        return {"scores": {"comm": 6}, "modelUsed": "groq/llama-3.3-70b-versatile"}

    monkeypatch.setattr(main, "_call_gemini", fake_gemini)
    monkeypatch.setattr(main, "_call_gemini_multimodal", fake_gemini)
    monkeypatch.setattr(main, "_call_groq", fake_groq)
    monkeypatch.setattr(main, "_log_coaching_quality", lambda *a, **kw: None)


def _request(model: str | None) -> main.FeedbackRequest:
    return main.FeedbackRequest(
        question="Parle-moi de tes loisirs.",
        transcript="J'aime le football parce que c'est amusant.",
        model=model,
    )


def test_groq_preference_calls_groq_first(monkeypatch):
    calls: list[str] = []
    _stub_providers(monkeypatch, calls)

    result = asyncio.run(main.call_ai_feedback(_request("groq")))

    assert calls == ["groq"], "Groq preference must not pay Gemini latency first"
    assert result["engineMeta"]["actualEngine"] == "groq"
    assert result["engineMeta"]["fallbackUsed"] is False


def test_groq_preference_falls_back_to_gemini(monkeypatch):
    calls: list[str] = []
    _stub_providers(monkeypatch, calls, groq_ok=False)

    result = asyncio.run(main.call_ai_feedback(_request("groq")))

    assert calls == ["groq", "gemini"]
    assert result["engineMeta"]["actualEngine"] == "gemini"
    assert result["engineMeta"]["fallbackUsed"] is True
    assert result["providerErrors"][0]["provider"].startswith("groq/")


def test_default_and_gemini_preference_stay_gemini_first(monkeypatch):
    for requested in (None, "gemini"):
        calls: list[str] = []
        _stub_providers(monkeypatch, calls)

        result = asyncio.run(main.call_ai_feedback(_request(requested)))

        assert calls == ["gemini"], f"preference {requested!r} should try Gemini first"
        assert result["engineMeta"]["actualEngine"] == "gemini"


def test_engine_preference_field_is_read_from_json_body(monkeypatch):
    """The web client's field name is `enginePreference`, not `model`."""
    seen: dict[str, str] = {}

    async def fake_impl(**kwargs):
        seen.update({"model": kwargs["model"]})
        return {"scores": {}, "provider": "groq"}

    monkeypatch.setattr(main, "_feedback_impl", fake_impl)

    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        client.post(
            "/api/feedback/v3",
            json={"transcript": "Bonjour.", "question": {"text": "Q?"}, "enginePreference": "groq"},
        )

    assert seen["model"] == "groq"
