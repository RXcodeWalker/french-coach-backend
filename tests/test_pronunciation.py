"""Offline test: /api/pronunciation degrades to the whisper-heuristic tier
when AZURE_SPEECH_KEY/AZURE_SPEECH_REGION are unset. No live Azure/Groq/
network call — the router is exercised directly with fake injected functions,
following the DI seam it was built with (configure()).

Run: pytest backend/tests/test_pronunciation.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from routers.pronunciation import configure, pronunciation_evaluate
from models.pronunciation import PronunciationAssessmentResponse
from services.pronunciation.azure_client import _is_configured


def _fake_align(target_text: str, heard_text: str, whisper_words):
    return {"score": 7, "issues": []}


async def _fake_groq_whisper(tmp_path: str, language: str):
    raise RuntimeError("groq not reachable in test")


async def _fake_faster_whisper(tmp_path: str, language: str):
    return {"text": "Un bon vin blanc.", "words": [{"word": "Un", "probability": 0.9}]}


def _build_app() -> FastAPI:
    # Build the route on a fresh APIRouter rather than importing and reusing
    # routers.pronunciation's module-level `router` singleton: when the test
    # suite also imports main.py (which already ran app.include_router() on
    # that same singleton at import time), a second include_router() call on
    # the same route objects makes FastAPI mis-resolve
    # `audio: Annotated[UploadFile, File(...)]` as a query param instead of
    # a body/File param (reproduced directly against fastapi 0.115 — the
    # route's rebuilt `dependant` moves `audio` into query_params on the
    # second registration). Re-registering the same endpoint function on a
    # brand-new router avoids the double-registration entirely.
    configure(_fake_groq_whisper, _fake_faster_whisper, _fake_align, lambda: False, None)
    fresh_router = APIRouter(prefix="/api", tags=["pronunciation"])
    fresh_router.post("/pronunciation", response_model=PronunciationAssessmentResponse)(pronunciation_evaluate)
    app = FastAPI()
    app.include_router(fresh_router)
    return app


def test_pronunciation_degrades_to_whisper_heuristic_without_azure_key(monkeypatch):
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
    assert _is_configured() is None
    client = TestClient(_build_app())

    response = client.post(
        "/api/pronunciation",
        data={"target_text": "Un bon vin blanc."},
        files={"audio": ("clip.webm", b"fake-audio-bytes", "audio/webm")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "whisper-heuristic"
    assert body["transcript"] == "Un bon vin blanc."
    assert body["subScores"] is None
    # Validates against the Pydantic model field-for-field.
    PronunciationAssessmentResponse(**body)


def test_invalid_mode_is_rejected(monkeypatch):
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
    client = TestClient(_build_app())

    response = client.post(
        "/api/pronunciation",
        data={"target_text": "Un bon vin blanc.", "mode": "bogus"},
        files={"audio": ("clip.webm", b"fake-audio-bytes", "audio/webm")},
    )
    assert response.status_code == 422


def test_freeform_mode_without_azure_reports_no_verdict_not_a_fabricated_score(monkeypatch):
    """Defect #5 regression, whisper-heuristic-tier edge case: in freeform
    mode there is no independent reference to diff the transcript against
    (target_text IS heard_text), so the fallback tier must not produce a
    diff-based score — that would always be a trivial perfect match."""
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
    client = TestClient(_build_app())

    response = client.post(
        "/api/pronunciation",
        # target_text deliberately does NOT match what _fake_faster_whisper
        # returns — proving the backend ignores it in freeform mode rather
        # than diffing against it.
        data={"target_text": "Ceci ne sera jamais utilisé.", "mode": "freeform"},
        files={"audio": ("clip.webm", b"fake-audio-bytes", "audio/webm")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "whisper-heuristic"
    assert body["score"] is None
    assert body["couldNotAssess"] is True
    assert body["transcript"] == "Un bon vin blanc."  # from _fake_faster_whisper
    PronunciationAssessmentResponse(**body)


def test_scripted_mode_without_azure_still_uses_align_fn_diff(monkeypatch):
    """Unchanged behaviour: scripted mode's whisper-heuristic fallback still
    diffs the caller's real target_text against the transcript."""
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
    client = TestClient(_build_app())

    response = client.post(
        "/api/pronunciation",
        data={"target_text": "Un bon vin blanc.", "mode": "scripted"},
        files={"audio": ("clip.webm", b"fake-audio-bytes", "audio/webm")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "whisper-heuristic"
    assert body["couldNotAssess"] is False
    assert body["score"] == 70  # _fake_align returns score=7, rescaled *10


def test_azure_scripted_response_populates_phase2_guardrails(monkeypatch):
    """Integration proof for Phase 2 (§6, §7, §11): an azure-scripted
    response gets prosodyMetrics/phonologicalFindings/confidence populated
    by the router, not just unit-tested against the modules in isolation.
    Mocks assess_with_fallback directly (already-normalized shape) since a
    real Azure call is out of scope for an offline test."""
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)

    import routers.pronunciation as pronunciation_router

    async def _fake_assess_with_fallback(**kwargs):
        return {
            "score": 85,
            "transcript": "Un bon vin blanc.",
            "issues": [],
            "words": [
                {
                    "word": "vin", "accuracyScore": 35.0, "errorType": "mispronounced",
                    "confidence": None,
                    "phonemes": [{"phoneme": "v", "accuracyScore": 92.0}, {"phoneme": "ɛ̃", "accuracyScore": 20.0}],
                    "offsetMs": 500, "durationMs": 300, "nearChunkBoundary": False,
                },
                {
                    "word": "blanc", "accuracyScore": 90.0, "errorType": "correct",
                    "confidence": None, "phonemes": [], "offsetMs": 900, "durationMs": 400,
                    "nearChunkBoundary": False,
                },
            ],
            "provider": "azure",
            "subScores": {"accuracy": 82.0, "fluency": 90.0, "completeness": 100.0, "prosody": None},
            "couldNotAssess": False,
            "couldNotAssessReason": None,
            "snrDb": 22.0,
            "azureConfidence": 0.9,
        }

    monkeypatch.setattr(pronunciation_router, "assess_with_fallback", _fake_assess_with_fallback)
    client = TestClient(_build_app())

    response = client.post(
        "/api/pronunciation",
        data={"target_text": "Un bon vin blanc.", "mode": "scripted"},
        files={"audio": ("clip.wav", b"fake-audio-bytes", "audio/wav")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "azure"
    assert body["prosodyMetrics"] is not None
    assert body["prosodyMetrics"]["speechRateWpm"] is not None
    assert any(f["category"] == "nasalVowel" for f in body["phonologicalFindings"])
    assert body["confidence"] is not None
    assert body["confidence"]["overall"] > 0.0
    assert body["audioQuality"]["snrDb"] == 22.0
    PronunciationAssessmentResponse(**body)


if __name__ == "__main__":
    with pytest.MonkeyPatch.context() as mp:
        test_pronunciation_degrades_to_whisper_heuristic_without_azure_key(mp)
        test_invalid_mode_is_rejected(mp)
        test_freeform_mode_without_azure_reports_no_verdict_not_a_fabricated_score(mp)
        test_scripted_mode_without_azure_still_uses_align_fn_diff(mp)
        test_azure_scripted_response_populates_phase2_guardrails(mp)
    print("All test_pronunciation tests passed.")
