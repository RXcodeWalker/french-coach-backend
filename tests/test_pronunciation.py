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


def test_coaching_full_populates_grounded_coaching(monkeypatch):
    """Phase 3 (§8, R5) integration proof: coaching=full triggers the
    narrator, which is grounded in the same phonologicalFindings the
    response already carries — not a separate, ungrounded LLM guess."""
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
            ],
            "provider": "azure",
            "subScores": {"accuracy": 82.0, "fluency": 90.0, "completeness": 100.0, "prosody": None},
            "couldNotAssess": False,
            "couldNotAssessReason": None,
            "snrDb": 22.0,
            "azureConfidence": 0.9,
        }

    async def _fake_call_groq(prompt: str):
        return {
            "summary": "Watch the nasal vowel in «vin».",
            "topPriority": "Fix the nasal vowel in «vin».",
            "tips": ["Practise «vin» slowly."],
        }

    monkeypatch.setattr(pronunciation_router, "assess_with_fallback", _fake_assess_with_fallback)
    monkeypatch.setattr(pronunciation_router, "_coach_call_groq", _fake_call_groq)
    monkeypatch.setattr(pronunciation_router, "_coach_call_gemini", None)
    client = TestClient(_build_app())

    response = client.post(
        "/api/pronunciation",
        data={"target_text": "Un bon vin blanc.", "mode": "scripted", "coaching": "full"},
        files={"audio": ("clip.wav", b"fake-audio-bytes", "audio/wav")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["coaching"] is not None
    assert body["coaching"]["grounded"] is True
    assert "vin" in body["coaching"]["summary"]
    PronunciationAssessmentResponse(**body)


def test_coaching_none_skips_llm_entirely(monkeypatch):
    """Default (drill mode) — coaching stays null, and the narrator is never
    invoked (plan §8: "coaching=none skips the LLM entirely")."""
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)

    import routers.pronunciation as pronunciation_router

    async def _fake_assess_with_fallback(**kwargs):
        return {
            "score": 85, "transcript": "Un bon vin blanc.", "issues": [],
            "words": [], "provider": "azure",
            "subScores": {"accuracy": 82.0, "fluency": 90.0, "completeness": 100.0, "prosody": None},
            "couldNotAssess": False, "couldNotAssessReason": None,
            "snrDb": 22.0, "azureConfidence": 0.9,
        }

    async def _fail_if_called(prompt: str):
        raise AssertionError("coaching LLM must not be called when coaching=none")

    monkeypatch.setattr(pronunciation_router, "assess_with_fallback", _fake_assess_with_fallback)
    monkeypatch.setattr(pronunciation_router, "_coach_call_groq", _fail_if_called)
    client = TestClient(_build_app())

    response = client.post(
        "/api/pronunciation",
        data={"target_text": "Un bon vin blanc.", "mode": "scripted"},
        files={"audio": ("clip.wav", b"fake-audio-bytes", "audio/wav")},
    )

    assert response.status_code == 200
    assert response.json()["coaching"] is None


def test_invalid_coaching_value_is_rejected(monkeypatch):
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
    client = TestClient(_build_app())

    response = client.post(
        "/api/pronunciation",
        data={"target_text": "Un bon vin blanc.", "coaching": "bogus"},
        files={"audio": ("clip.webm", b"fake-audio-bytes", "audio/webm")},
    )
    assert response.status_code == 422


@pytest.fixture(autouse=True)
def _clear_pronunciation_caches():
    """The router's audio/coaching caches are module-level singletons, so a
    result cached by one test leaks into any later test that posts the same
    audio bytes + reference text. Reset them around every test."""
    import routers.pronunciation as pronunciation_router

    pronunciation_router._audio_cache._store.clear()
    pronunciation_router._coaching_cache._store.clear()
    yield
    pronunciation_router._audio_cache._store.clear()
    pronunciation_router._coaching_cache._store.clear()


def _build_app_with(groq_fn, faster_fn, align_fn=_fake_align) -> FastAPI:
    """Same fresh-router construction as _build_app, with the injected
    transcription/alignment seams overridable per test."""
    configure(groq_fn, faster_fn, align_fn, lambda: True, None)
    fresh_router = APIRouter(prefix="/api", tags=["pronunciation"])
    fresh_router.post("/pronunciation", response_model=PronunciationAssessmentResponse)(pronunciation_evaluate)
    app = FastAPI()
    app.include_router(fresh_router)
    return app


def test_transcription_failure_degrades_instead_of_crashing_the_worker(monkeypatch):
    """Regression: an unguarded `await _faster_whisper_fn(...)` propagated out
    of the handler. In production that call loads a local model in-process and
    took the worker down on a memory-constrained host — the client saw a bare
    502 with an empty body, not a handled error. Transcription is a
    best-effort input, never a reason to fail the request."""
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)

    async def _boom_groq(tmp_path: str, language: str):
        raise RuntimeError("groq rejected the audio")

    async def _boom_faster(tmp_path: str, language: str):
        raise MemoryError("model load exhausted available memory")

    client = TestClient(_build_app_with(_boom_groq, _boom_faster))
    response = client.post(
        "/api/pronunciation",
        data={"target_text": "Un bon vin blanc.", "mode": "scripted"},
        files={"audio": ("clip.webm", b"fake-audio-bytes", "audio/webm")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["couldNotAssess"] is True
    assert body["score"] is None


def test_absent_local_whisper_fallback_degrades_instead_of_503(monkeypatch):
    """main.py passes None for the faster-whisper seam when
    PRONUNCIATION_LOCAL_WHISPER is off, so that a Groq failure cannot trigger
    an in-process model load (an OOM-kill there is uncatchable and reaches the
    browser as an empty-bodied 502). A missing local fallback must degrade the
    result, not refuse the request."""
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)

    async def _boom_groq(tmp_path: str, language: str):
        raise RuntimeError("groq rejected the audio")

    client = TestClient(_build_app_with(_boom_groq, None))
    response = client.post(
        "/api/pronunciation",
        data={"target_text": "Un bon vin blanc.", "mode": "scripted"},
        files={"audio": ("clip.webm", b"fake-audio-bytes", "audio/webm")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["couldNotAssess"] is True
    assert body["score"] is None


def test_whisper_silence_hallucination_is_not_scored_as_a_real_attempt(monkeypatch):
    """Regression: Whisper emits a subtitle-credit artefact when fed silence.
    Aligning the target against it produced a confident score 0 plus fabricated
    per-word issues ("Said 'sous-titrage' instead of 'un'") — a verdict on
    audio that was never assessable."""
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)

    async def _hallucinating_groq(tmp_path: str, language: str):
        return {"text": "Sous-titrage Société Radio-Canada", "words": []}

    client = TestClient(_build_app_with(_hallucinating_groq, _fake_faster_whisper))
    response = client.post(
        "/api/pronunciation",
        data={"target_text": "Un bon vin blanc.", "mode": "scripted"},
        files={"audio": ("clip.wav", b"fake-audio-bytes", "audio/wav")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["score"] is None, "silence must never render as a score of 0"
    assert body["couldNotAssess"] is True
    assert body["couldNotAssessReason"] == "no_speech_recognized"
    assert body["issues"] == [], "no fabricated per-word issues from hallucinated text"


def test_empty_transcript_reports_no_verdict_rather_than_zero(monkeypatch):
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)

    async def _silent_groq(tmp_path: str, language: str):
        return {"text": "  .  ", "words": []}

    client = TestClient(_build_app_with(_silent_groq, _fake_faster_whisper))
    response = client.post(
        "/api/pronunciation",
        data={"target_text": "Un bon vin blanc.", "mode": "scripted"},
        files={"audio": ("clip.wav", b"fake-audio-bytes", "audio/wav")},
    )

    body = response.json()
    assert body["score"] is None
    assert body["couldNotAssess"] is True


def test_zero_alignment_reports_no_verdict_rather_than_a_confident_zero(monkeypatch):
    """The heuristic tier has no acoustic signal — only a word diff — so a
    zero-match result cannot be distinguished from unusable audio."""
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)

    async def _mismatched_groq(tmp_path: str, language: str):
        return {"text": "bonjour tout le monde", "words": []}

    def _zero_align(target_text: str, heard_text: str, whisper_words):
        return {"score": 0, "issues": [{"word": "un", "problem": "x", "severity": "medium", "drill": {}}]}

    client = TestClient(_build_app_with(_mismatched_groq, _fake_faster_whisper, _zero_align))
    response = client.post(
        "/api/pronunciation",
        data={"target_text": "Un bon vin blanc.", "mode": "scripted"},
        files={"audio": ("clip.wav", b"fake-audio-bytes", "audio/wav")},
    )

    body = response.json()
    assert body["score"] is None
    assert body["couldNotAssess"] is True
    assert body["issues"] == []


def test_freeform_without_a_transcript_reports_no_verdict(monkeypatch):
    """Freeform mode derives its reference text from the transcript; with no
    transcript there is no reference to grade against at all."""
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)

    async def _boom_groq(tmp_path: str, language: str):
        raise RuntimeError("nope")

    async def _boom_faster(tmp_path: str, language: str):
        raise RuntimeError("nope")

    client = TestClient(_build_app_with(_boom_groq, _boom_faster))
    response = client.post(
        "/api/pronunciation",
        data={"target_text": "ignored in freeform", "mode": "freeform"},
        files={"audio": ("clip.wav", b"fake-audio-bytes", "audio/wav")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["score"] is None
    assert body["couldNotAssess"] is True
    assert body["couldNotAssessReason"] == "no_speech_recognized"


def test_genuine_partial_attempt_still_receives_a_score(monkeypatch):
    """Guard against the no-verdict rules swallowing real, scorable attempts."""
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)

    async def _partial_groq(tmp_path: str, language: str):
        return {"text": "Un bon vin blanc.", "words": []}

    client = TestClient(_build_app_with(_partial_groq, _fake_faster_whisper))
    response = client.post(
        "/api/pronunciation",
        data={"target_text": "Un bon vin blanc.", "mode": "scripted"},
        files={"audio": ("clip.wav", b"fake-audio-bytes", "audio/wav")},
    )

    body = response.json()
    assert body["couldNotAssess"] is False
    assert body["score"] == 70  # _fake_align's 0-10 score of 7, rescaled


if __name__ == "__main__":
    with pytest.MonkeyPatch.context() as mp:
        test_pronunciation_degrades_to_whisper_heuristic_without_azure_key(mp)
        test_invalid_mode_is_rejected(mp)
        test_freeform_mode_without_azure_reports_no_verdict_not_a_fabricated_score(mp)
        test_scripted_mode_without_azure_still_uses_align_fn_diff(mp)
        test_azure_scripted_response_populates_phase2_guardrails(mp)
    print("All test_pronunciation tests passed.")
