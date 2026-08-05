"""Tests for the rewritten /api/repair (accent-analyzer plan, Phase 3
roadmap item: "rewrite /api/repair as a /api/pronunciation caller — it is
currently a second, unaudited pronunciation scorer"). Verifies the endpoint
now goes through assess_with_fallback + the grounded coaching narrator
instead of asking an LLM to invent a 0-10 score from a bare transcript.

Offline: assess_with_fallback (and Whisper/coaching LLM calls) are
monkeypatched directly on the `main` module — not gated via env vars, since
main.py's Azure config is read once at import time by azure_client.py and
this repo's local .env has real AZURE_SPEECH_KEY/AZURE_SPEECH_REGION values,
so unsetting os.environ after import would not stop a live call. No live
network call is made by these tests.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

import main


def _post_repair(client, **form_overrides):
    data = {"word": "vin", "context": "un bon vin blanc", "original_problem": "nasal vowel"}
    data.update(form_overrides)
    return client.post(
        "/api/repair",
        data=data,
        files={"audio": ("clip.webm", b"fake-audio-bytes", "audio/webm")},
    )


def test_repair_uses_the_shared_pipeline_not_a_bespoke_llm_score(monkeypatch):
    async def _fake_faster_whisper(tmp_path: str, language: str):
        return {"text": "vin", "words": [{"word": "vin", "probability": 0.9}]}

    async def _fake_assess_with_fallback(**kwargs):
        assert kwargs["target_text"] == "vin"  # word IS the reference text, not a transcript diff
        assert kwargs["mode"] == "scripted"
        return {
            "score": 62,
            "transcript": "vin",
            "issues": [],
            "words": [
                {
                    "word": "vin", "accuracyScore": 35.0, "errorType": "mispronounced",
                    "confidence": None,
                    "phonemes": [{"phoneme": "v", "accuracyScore": 92.0}, {"phoneme": "ɛ̃", "accuracyScore": 20.0}],
                    "offsetMs": 0, "durationMs": 300, "nearChunkBoundary": False,
                },
            ],
            "provider": "azure",
            "subScores": {"accuracy": 62.0, "fluency": 80.0, "completeness": 100.0, "prosody": None},
            "couldNotAssess": False,
            "couldNotAssessReason": None,
            "snrDb": 20.0,
            "azureConfidence": 0.85,
        }

    async def _fake_coach_groq(prompt: str):
        return {
            "summary": "Watch the nasal vowel in «vin».",
            "topPriority": "Fix the nasal vowel in «vin».",
            "tips": ["Practise «vin» slowly."],
        }

    monkeypatch.setattr(main, "_faster_whisper", _fake_faster_whisper)
    monkeypatch.setattr(main, "assess_with_fallback", _fake_assess_with_fallback)
    monkeypatch.setattr(main, "_call_groq_coach", _fake_coach_groq)
    monkeypatch.setattr(main, "_call_gemini_coach", None)
    monkeypatch.setattr(main, "GROQ_API_KEY", "")  # skip Groq Whisper branch, use faster-whisper fake

    client = TestClient(main.app)
    response = _post_repair(client)

    assert response.status_code == 200
    body = response.json()
    assert body["score"] == 62
    assert body["source"] == "azure"
    assert body["improved"] is False  # 62 < PRACTICE_PASS_SCORE (70)
    assert "vin" in body["feedback"]
    assert "vin" in body["phonetics_guide"]


def test_repair_never_fabricates_a_score_on_couldnotassess(monkeypatch):
    async def _fake_faster_whisper(tmp_path: str, language: str):
        return {"text": "", "words": []}

    async def _fake_assess_with_fallback(**kwargs):
        return {
            "score": None, "transcript": "", "issues": [], "words": [],
            "provider": "whisper-heuristic", "subScores": None,
            "couldNotAssess": True, "couldNotAssessReason": "no_speech_recognized",
        }

    async def _fake_coach_groq(prompt: str):
        raise RuntimeError("unreachable in test")

    monkeypatch.setattr(main, "_faster_whisper", _fake_faster_whisper)
    monkeypatch.setattr(main, "assess_with_fallback", _fake_assess_with_fallback)
    monkeypatch.setattr(main, "_call_groq_coach", _fake_coach_groq)
    monkeypatch.setattr(main, "_call_gemini_coach", None)
    monkeypatch.setattr(main, "GROQ_API_KEY", "")

    client = TestClient(main.app)
    response = _post_repair(client)

    assert response.status_code == 200
    body = response.json()
    assert body["score"] is None
    assert body["improved"] is None
    # Template fallback (no LLM reachable) — still non-empty, still grounded
    # in nothing-but-findings (there are none here, since couldNotAssess).
    assert body["feedback"]


def test_repair_degrades_gracefully_on_pipeline_exception(monkeypatch):
    async def _raise_faster_whisper(tmp_path: str, language: str):
        raise RuntimeError("whisper unreachable")

    async def _raise_assess_with_fallback(**kwargs):
        raise RuntimeError("pipeline unreachable")

    monkeypatch.setattr(main, "_faster_whisper", _raise_faster_whisper)
    monkeypatch.setattr(main, "assess_with_fallback", _raise_assess_with_fallback)
    monkeypatch.setattr(main, "GROQ_API_KEY", "")

    client = TestClient(main.app)
    response = _post_repair(client)

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "offline_fallback"
    assert body["score"] is None


if __name__ == "__main__":
    import inspect
    names = [n for n, f in list(globals().items()) if n.startswith("test_") and inspect.isfunction(f)]
    for name in names:
        print(f"Run {name} via pytest (needs monkeypatch fixture).")
