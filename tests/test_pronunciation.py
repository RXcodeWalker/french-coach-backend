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

import lib.auth as lib_auth
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


_VALID_JWT_SECRET = "test-jwt-secret-at-least-32-characters-long!!"


def _fake_jwt(sub: str = "user-123", secret: str = _VALID_JWT_SECRET, expired: bool = False) -> str:
    import time
    import jwt as pyjwt

    exp = int(time.time()) - 3600 if expired else int(time.time()) + 3600
    return pyjwt.encode({"sub": sub, "exp": exp}, secret, algorithm="HS256")


async def _fake_assess_with_fallback_azure(**kwargs):
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


class _FakeRpcCall:
    """Stand-in for the object `db.rpc(name, args)` returns, which the
    router then calls `.execute()` on (matching the real Supabase client's
    call shape used in routers/pronunciation.py's `_consume_quota`/`_release_quota`)."""

    def __init__(self, data=None, raise_on_execute=False):
        self._data = data
        self._raise = raise_on_execute

    def execute(self):
        if self._raise:
            raise RuntimeError("rpc unreachable")
        return _FakeRpcResult(self._data)


class _FakeRpcResult:
    def __init__(self, data):
        self.data = data


class _FakeQuotaDb:
    """Stand-in for the Supabase service-role client's .rpc(name, args).execute()
    chain, used to drive the quota gate without a real Supabase instance."""

    def __init__(self, consume_response=None, release_response=None, raise_on_consume=False, raise_on_release=False):
        self.consume_response = consume_response or {"used": 1, "limit": 3, "granted": True, "replayed": False}
        self.release_response = release_response or {"used": 0, "limit": 3, "released": True}
        self.raise_on_consume = raise_on_consume
        self.raise_on_release = raise_on_release
        self.consume_calls: list[dict] = []
        self.release_calls: list[dict] = []

    def rpc(self, name: str, args: dict):
        if name == "consume_shadowing_coaching_quota":
            self.consume_calls.append(args)
            return _FakeRpcCall(self.consume_response, raise_on_execute=self.raise_on_consume)
        if name == "release_shadowing_coaching_grant":
            self.release_calls.append(args)
            return _FakeRpcCall(self.release_response, raise_on_execute=self.raise_on_release)
        raise AssertionError(f"unexpected rpc: {name}")


def test_coaching_full_grounded_groq_no_refund(monkeypatch):
    """Case 1 (plan §6): coaching='full', quota granted, Groq returns valid
    grounded JSON -> coaching populated, grounded True, coachingQuota.granted
    True, and the refund RPC is never called."""
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "fake-service-key")
    monkeypatch.setattr(lib_auth, "SUPABASE_JWT_SECRET", _VALID_JWT_SECRET)

    import routers.pronunciation as pronunciation_router

    fake_db = _FakeQuotaDb(consume_response={"used": 1, "limit": 3, "granted": True, "replayed": False})

    async def _fake_groq(prompt: str):
        return {
            "summary": "Overall solid, watch «vin».",
            "strengths": [{"word": "blanc", "note": "Clean vowel."}],
            "problems": [{"word": "vin", "note": "Nasalize the vowel more."}],
            "rhythmNote": None,
            "nextRepetition": "Slow down slightly on «vin».",
        }

    monkeypatch.setattr(pronunciation_router, "assess_with_fallback", lambda **k: _fake_assess_with_fallback_azure(**k))
    monkeypatch.setattr(pronunciation_router, "_coach_call_groq", _fake_groq)
    monkeypatch.setattr(pronunciation_router, "_db", lambda: fake_db)
    client = TestClient(_build_app())

    response = client.post(
        "/api/pronunciation",
        data={"target_text": "Un bon vin blanc.", "mode": "scripted", "coaching": "full", "coaching_request_id": "req-1"},
        headers={"Authorization": f"Bearer {_fake_jwt()}"},
        files={"audio": ("clip.wav", b"fake-audio-bytes", "audio/wav")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["coaching"] is not None
    assert body["coaching"]["grounded"] is True
    assert body["coachingQuota"]["granted"] is True
    assert len(fake_db.consume_calls) == 1
    assert len(fake_db.release_calls) == 0
    PronunciationAssessmentResponse(**body)


def test_coaching_full_quota_denied_groq_never_invoked(monkeypatch):
    """Case 2: quota stubbed granted:false -> coaching is None, reason is
    daily_limit_reached, Groq is never invoked."""
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "fake-service-key")
    monkeypatch.setattr(lib_auth, "SUPABASE_JWT_SECRET", _VALID_JWT_SECRET)

    import routers.pronunciation as pronunciation_router

    fake_db = _FakeQuotaDb(consume_response={"used": 3, "limit": 3, "granted": False, "reason": "daily_limit_reached"})

    async def _fail_if_called(prompt: str):
        raise AssertionError("Groq must not be called when quota is denied")

    monkeypatch.setattr(pronunciation_router, "assess_with_fallback", lambda **k: _fake_assess_with_fallback_azure(**k))
    monkeypatch.setattr(pronunciation_router, "_coach_call_groq", _fail_if_called)
    monkeypatch.setattr(pronunciation_router, "_db", lambda: fake_db)
    client = TestClient(_build_app())

    response = client.post(
        "/api/pronunciation",
        data={"target_text": "Un bon vin blanc.", "mode": "scripted", "coaching": "full", "coaching_request_id": "req-2"},
        headers={"Authorization": f"Bearer {_fake_jwt()}"},
        files={"audio": ("clip.wav", b"fake-audio-bytes", "audio/wav")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["coaching"] is None
    assert body["coachingQuota"]["reason"] == "daily_limit_reached"


def test_coaching_full_no_auth_header_degrades(monkeypatch):
    """Case 3: no Authorization header + coaching='full' -> 200, coaching is
    None, reason unauthenticated, Groq never invoked, assessment intact."""
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
    monkeypatch.setattr(lib_auth, "SUPABASE_JWT_SECRET", _VALID_JWT_SECRET)

    import routers.pronunciation as pronunciation_router

    async def _fail_if_called(prompt: str):
        raise AssertionError("Groq must not be called without auth")

    monkeypatch.setattr(pronunciation_router, "assess_with_fallback", lambda **k: _fake_assess_with_fallback_azure(**k))
    monkeypatch.setattr(pronunciation_router, "_coach_call_groq", _fail_if_called)
    client = TestClient(_build_app())

    response = client.post(
        "/api/pronunciation",
        data={"target_text": "Un bon vin blanc.", "mode": "scripted", "coaching": "full"},
        files={"audio": ("clip.wav", b"fake-audio-bytes", "audio/wav")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["coaching"] is None
    assert body["coachingQuota"]["reason"] == "unauthenticated"
    assert body["score"] == 85


def test_coaching_full_expired_token_degrades(monkeypatch):
    """Case 4: expired/forged token behaves identically to no header — fails
    closed."""
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
    monkeypatch.setattr(lib_auth, "SUPABASE_JWT_SECRET", _VALID_JWT_SECRET)

    import routers.pronunciation as pronunciation_router

    async def _fail_if_called(prompt: str):
        raise AssertionError("Groq must not be called with an expired token")

    monkeypatch.setattr(pronunciation_router, "assess_with_fallback", lambda **k: _fake_assess_with_fallback_azure(**k))
    monkeypatch.setattr(pronunciation_router, "_coach_call_groq", _fail_if_called)
    client = TestClient(_build_app())

    response = client.post(
        "/api/pronunciation",
        data={"target_text": "Un bon vin blanc.", "mode": "scripted", "coaching": "full"},
        headers={"Authorization": f"Bearer {_fake_jwt(expired=True)}"},
        files={"audio": ("clip.wav", b"fake-audio-bytes", "audio/wav")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["coaching"] is None
    assert body["coachingQuota"]["reason"] == "unauthenticated"


def test_coaching_full_quota_rpc_raises_degrades(monkeypatch):
    """Case 5: quota RPC raises -> 200, quota_unavailable, Groq never
    invoked."""
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "fake-service-key")
    monkeypatch.setattr(lib_auth, "SUPABASE_JWT_SECRET", _VALID_JWT_SECRET)

    import routers.pronunciation as pronunciation_router

    fake_db = _FakeQuotaDb(raise_on_consume=True)

    async def _fail_if_called(prompt: str):
        raise AssertionError("Groq must not be called when quota RPC raises")

    monkeypatch.setattr(pronunciation_router, "assess_with_fallback", lambda **k: _fake_assess_with_fallback_azure(**k))
    monkeypatch.setattr(pronunciation_router, "_coach_call_groq", _fail_if_called)
    monkeypatch.setattr(pronunciation_router, "_db", lambda: fake_db)
    client = TestClient(_build_app())

    response = client.post(
        "/api/pronunciation",
        data={"target_text": "Un bon vin blanc.", "mode": "scripted", "coaching": "full", "coaching_request_id": "req-5"},
        headers={"Authorization": f"Bearer {_fake_jwt()}"},
        files={"audio": ("clip.wav", b"fake-audio-bytes", "audio/wav")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["coaching"] is None
    assert body["coachingQuota"]["reason"] == "quota_unavailable"


def test_coaching_full_groq_raises_fallback_and_refund(monkeypatch):
    """Case 6: Groq raises -> template fallback (grounded False) AND the
    refund RPC is called once."""
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "fake-service-key")
    monkeypatch.setattr(lib_auth, "SUPABASE_JWT_SECRET", _VALID_JWT_SECRET)

    import routers.pronunciation as pronunciation_router

    fake_db = _FakeQuotaDb(
        consume_response={"used": 1, "limit": 3, "granted": True, "replayed": False},
        release_response={"used": 0, "limit": 3, "released": True},
    )

    async def _boom_groq(prompt: str):
        raise RuntimeError("groq unreachable")

    monkeypatch.setattr(pronunciation_router, "assess_with_fallback", lambda **k: _fake_assess_with_fallback_azure(**k))
    monkeypatch.setattr(pronunciation_router, "_coach_call_groq", _boom_groq)
    monkeypatch.setattr(pronunciation_router, "_db", lambda: fake_db)
    client = TestClient(_build_app())

    response = client.post(
        "/api/pronunciation",
        data={"target_text": "Un bon vin blanc.", "mode": "scripted", "coaching": "full", "coaching_request_id": "req-6"},
        headers={"Authorization": f"Bearer {_fake_jwt()}"},
        files={"audio": ("clip.wav", b"fake-audio-bytes", "audio/wav")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["coaching"] is not None
    assert body["coaching"]["grounded"] is False
    assert len(fake_db.release_calls) == 1
    assert fake_db.release_calls[0]["p_idempotency_key"] == "req-6"


def test_coaching_full_malformed_json_fallback_and_refund(monkeypatch):
    """Case 7: Groq returns malformed JSON -> same as case 6 (fallback +
    refund)."""
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "fake-service-key")
    monkeypatch.setattr(lib_auth, "SUPABASE_JWT_SECRET", _VALID_JWT_SECRET)

    import routers.pronunciation as pronunciation_router

    fake_db = _FakeQuotaDb()

    async def _malformed_groq(prompt: str):
        raise ValueError("No JSON object found in model response")

    monkeypatch.setattr(pronunciation_router, "assess_with_fallback", lambda **k: _fake_assess_with_fallback_azure(**k))
    monkeypatch.setattr(pronunciation_router, "_coach_call_groq", _malformed_groq)
    monkeypatch.setattr(pronunciation_router, "_db", lambda: fake_db)
    client = TestClient(_build_app())

    response = client.post(
        "/api/pronunciation",
        data={"target_text": "Un bon vin blanc.", "mode": "scripted", "coaching": "full", "coaching_request_id": "req-7"},
        headers={"Authorization": f"Bearer {_fake_jwt()}"},
        files={"audio": ("clip.wav", b"fake-audio-bytes", "audio/wav")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["coaching"]["grounded"] is False
    assert len(fake_db.release_calls) == 1


def test_coaching_full_invented_problem_word_fallback_and_refund(monkeypatch):
    """Case 8: Groq invents a problem word absent from problem_words -> the
    per-claim gate drops it -> fallback + refund."""
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "fake-service-key")
    monkeypatch.setattr(lib_auth, "SUPABASE_JWT_SECRET", _VALID_JWT_SECRET)

    import routers.pronunciation as pronunciation_router

    fake_db = _FakeQuotaDb()

    async def _inventing_groq(prompt: str):
        return {
            "summary": "Watch out.",
            "strengths": [],
            "problems": [{"word": "fromage", "note": "Not even in the sentence."}],
            "rhythmNote": None,
            "nextRepetition": "Try again.",
        }

    monkeypatch.setattr(pronunciation_router, "assess_with_fallback", lambda **k: _fake_assess_with_fallback_azure(**k))
    monkeypatch.setattr(pronunciation_router, "_coach_call_groq", _inventing_groq)
    monkeypatch.setattr(pronunciation_router, "_db", lambda: fake_db)
    client = TestClient(_build_app())

    response = client.post(
        "/api/pronunciation",
        data={"target_text": "Un bon vin blanc.", "mode": "scripted", "coaching": "full", "coaching_request_id": "req-8"},
        headers={"Authorization": f"Bearer {_fake_jwt()}"},
        files={"audio": ("clip.wav", b"fake-audio-bytes", "audio/wav")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["coaching"]["grounded"] is False
    assert len(fake_db.release_calls) == 1


def test_coaching_full_praises_mispronounced_word_fallback_and_refund(monkeypatch):
    """Case 9: Groq praises a word Azure marked mispronounced -> gate drops
    it -> fallback + refund."""
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "fake-service-key")
    monkeypatch.setattr(lib_auth, "SUPABASE_JWT_SECRET", _VALID_JWT_SECRET)

    import routers.pronunciation as pronunciation_router

    fake_db = _FakeQuotaDb()

    async def _wrongly_praising_groq(prompt: str):
        return {
            "summary": "Nice work overall.",
            # "vin" was mispronounced per the fixture, not correct — this
            # must fail the per-claim gate.
            "strengths": [{"word": "vin", "note": "Great nasal vowel."}],
            "problems": [],
            "rhythmNote": None,
            "nextRepetition": "Keep it up.",
        }

    monkeypatch.setattr(pronunciation_router, "assess_with_fallback", lambda **k: _fake_assess_with_fallback_azure(**k))
    monkeypatch.setattr(pronunciation_router, "_coach_call_groq", _wrongly_praising_groq)
    monkeypatch.setattr(pronunciation_router, "_db", lambda: fake_db)
    client = TestClient(_build_app())

    response = client.post(
        "/api/pronunciation",
        data={"target_text": "Un bon vin blanc.", "mode": "scripted", "coaching": "full", "coaching_request_id": "req-9"},
        headers={"Authorization": f"Bearer {_fake_jwt()}"},
        files={"audio": ("clip.wav", b"fake-audio-bytes", "audio/wav")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["coaching"]["grounded"] is False
    assert len(fake_db.release_calls) == 1


def test_coaching_full_no_prosody_rhythm_note_fails_gate(monkeypatch):
    """Case 10: prosodyMetrics is None -> the prompt tells the model not to
    comment on rhythm, and a non-null rhythmNote in the response fails the
    gate (fallback + refund). The capability matrix
    (data/phonology/fr.json) marks rhythmMetrics 'unavailable' for the
    whisper-heuristic tier in both modes, so enforce_capabilities nulls
    prosodyMetrics whenever the provider is whisper-heuristic — used here
    instead of freeform mode, since freeform+azure keeps rhythmMetrics
    'derived' (only completeness/wordErrorType go unavailable there)."""
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "fake-service-key")
    monkeypatch.setattr(lib_auth, "SUPABASE_JWT_SECRET", _VALID_JWT_SECRET)

    import routers.pronunciation as pronunciation_router

    fake_db = _FakeQuotaDb()

    async def _rhythm_commenting_groq(prompt: str):
        return {
            "summary": "Good attempt.",
            "strengths": [{"word": "blanc", "note": "Good."}],
            "problems": [{"word": "vin", "note": "Fix the nasal vowel."}],
            "rhythmNote": "Your rhythm was a bit choppy.",  # forbidden: no prosody available
            "nextRepetition": "Try again slower.",
        }

    monkeypatch.setattr(pronunciation_router, "_coach_call_groq", _rhythm_commenting_groq)
    monkeypatch.setattr(pronunciation_router, "_db", lambda: fake_db)
    # Whisper-heuristic tier, real align_fn path (no AZURE_SPEECH_KEY set),
    # scripted mode: rhythmMetrics is 'unavailable' for whisper-heuristic in
    # both modes per the capability matrix.
    client = TestClient(_build_app_with(_fake_groq_whisper, _fake_faster_whisper))

    response = client.post(
        "/api/pronunciation",
        data={"target_text": "Un bon vin blanc.", "mode": "scripted", "coaching": "full", "coaching_request_id": "req-10"},
        headers={"Authorization": f"Bearer {_fake_jwt()}"},
        files={"audio": ("clip.wav", b"fake-audio-bytes", "audio/wav")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "whisper-heuristic"
    assert body["prosodyMetrics"] is None
    assert body["coaching"]["grounded"] is False
    assert len(fake_db.release_calls) == 1


def test_coaching_full_could_not_assess_skips_quota(monkeypatch):
    """Case 11: couldNotAssess + coaching='full' -> quota RPC not called,
    reason 'could_not_assess'."""
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "fake-service-key")
    monkeypatch.setattr(lib_auth, "SUPABASE_JWT_SECRET", _VALID_JWT_SECRET)

    import routers.pronunciation as pronunciation_router

    fake_db = _FakeQuotaDb()

    async def _silent_groq(tmp_path: str, language: str):
        return {"text": "  .  ", "words": []}

    async def _fail_if_called(prompt: str):
        raise AssertionError("Groq must not be called on couldNotAssess")

    monkeypatch.setattr(pronunciation_router, "_coach_call_groq", _fail_if_called)
    monkeypatch.setattr(pronunciation_router, "_db", lambda: fake_db)
    client = TestClient(_build_app_with(_silent_groq, _fake_faster_whisper))

    response = client.post(
        "/api/pronunciation",
        data={"target_text": "Un bon vin blanc.", "mode": "scripted", "coaching": "full", "coaching_request_id": "req-11"},
        headers={"Authorization": f"Bearer {_fake_jwt()}"},
        files={"audio": ("clip.wav", b"fake-audio-bytes", "audio/wav")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["couldNotAssess"] is True
    assert body["coachingQuota"]["reason"] == "could_not_assess"
    assert len(fake_db.consume_calls) == 0


def test_coaching_full_refund_rpc_raises_still_charged_but_200(monkeypatch):
    """Case 12: refund RPC itself raises -> still 200, coaching present as
    the template, user stays charged, warning logged (not asserted here,
    just that the request doesn't fail)."""
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "fake-service-key")
    monkeypatch.setattr(lib_auth, "SUPABASE_JWT_SECRET", _VALID_JWT_SECRET)

    import routers.pronunciation as pronunciation_router

    fake_db = _FakeQuotaDb(raise_on_release=True)

    async def _boom_groq(prompt: str):
        raise RuntimeError("groq unreachable")

    monkeypatch.setattr(pronunciation_router, "assess_with_fallback", lambda **k: _fake_assess_with_fallback_azure(**k))
    monkeypatch.setattr(pronunciation_router, "_coach_call_groq", _boom_groq)
    monkeypatch.setattr(pronunciation_router, "_db", lambda: fake_db)
    client = TestClient(_build_app())

    response = client.post(
        "/api/pronunciation",
        data={"target_text": "Un bon vin blanc.", "mode": "scripted", "coaching": "full", "coaching_request_id": "req-12"},
        headers={"Authorization": f"Bearer {_fake_jwt()}"},
        files={"audio": ("clip.wav", b"fake-audio-bytes", "audio/wav")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["coaching"] is not None
    assert body["coaching"]["grounded"] is False


def test_coaching_cache_differs_by_target_text_shares_by_identical_context(monkeypatch):
    """Case 13: two requests with identical findings but different
    target_text do NOT share cached coaching; two byte-identical contexts DO
    (one Groq call, both charged)."""
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "fake-service-key")
    monkeypatch.setattr(lib_auth, "SUPABASE_JWT_SECRET", _VALID_JWT_SECRET)

    import routers.pronunciation as pronunciation_router

    fake_db = _FakeQuotaDb()
    call_count = {"n": 0}

    async def _counting_groq(prompt: str):
        call_count["n"] += 1
        return {
            "summary": "Watch «vin».",
            "strengths": [{"word": "blanc", "note": "Good."}],
            "problems": [{"word": "vin", "note": "Fix it."}],
            "rhythmNote": None,
            "nextRepetition": "Again.",
        }

    monkeypatch.setattr(pronunciation_router, "assess_with_fallback", lambda **k: _fake_assess_with_fallback_azure(**k))
    monkeypatch.setattr(pronunciation_router, "_coach_call_groq", _counting_groq)
    monkeypatch.setattr(pronunciation_router, "_db", lambda: fake_db)

    client = TestClient(_build_app())
    headers = {"Authorization": f"Bearer {_fake_jwt()}"}

    r1 = client.post(
        "/api/pronunciation",
        data={"target_text": "Un bon vin blanc.", "mode": "scripted", "coaching": "full", "coaching_request_id": "req-a1"},
        headers=headers, files={"audio": ("clip1.wav", b"audio-bytes-1", "audio/wav")},
    )
    r2 = client.post(
        "/api/pronunciation",
        data={"target_text": "Un different target text.", "mode": "scripted", "coaching": "full", "coaching_request_id": "req-a2"},
        headers=headers, files={"audio": ("clip2.wav", b"audio-bytes-2", "audio/wav")},
    )
    assert r1.status_code == 200 and r2.status_code == 200
    assert call_count["n"] == 2, "different target_text must not share a cache entry"

    r3 = client.post(
        "/api/pronunciation",
        data={"target_text": "Un bon vin blanc.", "mode": "scripted", "coaching": "full", "coaching_request_id": "req-a3"},
        headers=headers, files={"audio": ("clip1.wav", b"audio-bytes-1", "audio/wav")},
    )
    assert r3.status_code == 200
    assert call_count["n"] == 2, "byte-identical context must share the cached coaching (no 3rd Groq call)"
    assert len(fake_db.consume_calls) == 3, "each request still consumes its own quota slot"


def test_coaching_cache_never_stores_fallback(monkeypatch):
    """Case 14: only grounded output is cached — a fallback result is never
    written to the shadowing coaching cache."""
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "fake-service-key")
    monkeypatch.setattr(lib_auth, "SUPABASE_JWT_SECRET", _VALID_JWT_SECRET)

    import routers.pronunciation as pronunciation_router

    fake_db = _FakeQuotaDb()

    async def _boom_groq(prompt: str):
        raise RuntimeError("groq unreachable")

    monkeypatch.setattr(pronunciation_router, "assess_with_fallback", lambda **k: _fake_assess_with_fallback_azure(**k))
    monkeypatch.setattr(pronunciation_router, "_coach_call_groq", _boom_groq)
    monkeypatch.setattr(pronunciation_router, "_db", lambda: fake_db)

    client = TestClient(_build_app())
    client.post(
        "/api/pronunciation",
        data={"target_text": "Un bon vin blanc.", "mode": "scripted", "coaching": "full", "coaching_request_id": "req-cache-1"},
        headers={"Authorization": f"Bearer {_fake_jwt()}"}, files={"audio": ("clip.wav", b"fake-audio-bytes", "audio/wav")},
    )

    assert len(pronunciation_router._shadowing_coaching_cache._store) == 0


def test_coaching_full_never_reaches_gemini(monkeypatch):
    """Case 15: Gemini is never reachable from the shadowing path — patch
    _coach_call_gemini to raise and make Groq fail; assert the template
    fallback, not a Gemini result."""
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "fake-service-key")
    monkeypatch.setattr(lib_auth, "SUPABASE_JWT_SECRET", _VALID_JWT_SECRET)

    import routers.pronunciation as pronunciation_router

    fake_db = _FakeQuotaDb()

    async def _boom_groq(prompt: str):
        raise RuntimeError("groq unreachable")

    async def _gemini_should_never_be_called(prompt: str):
        raise AssertionError("Gemini must never be reachable from the shadowing coaching path")

    monkeypatch.setattr(pronunciation_router, "assess_with_fallback", lambda **k: _fake_assess_with_fallback_azure(**k))
    monkeypatch.setattr(pronunciation_router, "_coach_call_groq", _boom_groq)
    monkeypatch.setattr(pronunciation_router, "_coach_call_gemini", _gemini_should_never_be_called)
    monkeypatch.setattr(pronunciation_router, "_db", lambda: fake_db)
    client = TestClient(_build_app())

    response = client.post(
        "/api/pronunciation",
        data={"target_text": "Un bon vin blanc.", "mode": "scripted", "coaching": "full", "coaching_request_id": "req-15"},
        headers={"Authorization": f"Bearer {_fake_jwt()}"},
        files={"audio": ("clip.wav", b"fake-audio-bytes", "audio/wav")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["coaching"]["grounded"] is False


def test_coaching_none_skips_llm_and_quota_entirely(monkeypatch):
    """Case 16 (extends the old test_coaching_none_skips_llm_entirely):
    default (drill mode) — coaching stays null, the narrator is never
    invoked, and the quota RPC is not called either."""
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "fake-service-key")

    import routers.pronunciation as pronunciation_router

    fake_db = _FakeQuotaDb()

    async def _fail_if_called(prompt: str):
        raise AssertionError("coaching LLM must not be called when coaching=none")

    monkeypatch.setattr(pronunciation_router, "assess_with_fallback", lambda **k: _fake_assess_with_fallback_azure(**k))
    monkeypatch.setattr(pronunciation_router, "_coach_call_groq", _fail_if_called)
    monkeypatch.setattr(pronunciation_router, "_db", lambda: fake_db)
    client = TestClient(_build_app())

    response = client.post(
        "/api/pronunciation",
        data={"target_text": "Un bon vin blanc.", "mode": "scripted"},
        files={"audio": ("clip.wav", b"fake-audio-bytes", "audio/wav")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["coaching"] is None
    assert body.get("coachingQuota") is None
    assert len(fake_db.consume_calls) == 0


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
    pronunciation_router._shadowing_coaching_cache._store.clear()
    yield
    pronunciation_router._audio_cache._store.clear()
    pronunciation_router._shadowing_coaching_cache._store.clear()


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
    LOCAL_WHISPER_ENABLED is off, so that a Groq failure cannot trigger
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
