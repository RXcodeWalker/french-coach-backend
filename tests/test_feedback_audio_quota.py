"""Security review 2026-11 F2: feedback with audio is charged before Whisper.

/api/feedback (and /v3) and /api/feedback/stream used to transcribe a recording
first and charge the daily allowance afterwards, so a caller over their limit
still got a Whisper call per request (and the stream sent the transcript back
before the refusal), and the upload was read whole into memory. Now: the
allowance is charged before the first provider call, keyed on the recording's
digest so an identical retry is not charged twice, released when nothing is
marked, and the upload is read with /api/transcribe's cap. /api/pronunciation
also charges before its Whisper call.

No network: providers, quota and the consent read are stubbed.
"""

from __future__ import annotations

import json
import os
import sys

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
import routers.pronunciation as pronunciation_router  # noqa: E402

USER = "11111111-1111-1111-1111-111111111111"
AUTH = {"Authorization": "Bearer t"}
AUDIO = {"audio": ("answer.webm", b"not-really-audio" * 10, "audio/webm")}
QUESTION = {"question": "Parle de ta famille."}


@pytest.fixture(autouse=True)
def signed_in(monkeypatch):
    monkeypatch.setattr(main, "verify_jwt", lambda authorization: USER)
    monkeypatch.setattr(pronunciation_router, "verify_supabase_jwt", lambda authorization: {"sub": USER})
    if main._limiter is not None:
        monkeypatch.setattr(main._limiter, "enabled", False)


@pytest.fixture
def whisper_calls(monkeypatch):
    calls: list[str] = []

    async def _whisper(path, lang, prompt=None):
        calls.append(path)
        return {"text": "bonjour je m'appelle Leo", "words": [], "segments": []}

    monkeypatch.setattr(main, "GROQ_API_KEY", "stub")
    monkeypatch.setattr(main, "_groq_whisper", _whisper)
    monkeypatch.setattr(pronunciation_router, "_groq_key_check_fn", lambda: True)
    monkeypatch.setattr(pronunciation_router, "_groq_whisper_fn", _whisper)
    return calls


@pytest.fixture
def refusing_quota(monkeypatch):
    calls: list[str] = []

    async def _quota(db, user_id, feature, key):
        calls.append(feature)
        raise HTTPException(status_code=429, detail={"error": "limit_reached"})

    monkeypatch.setattr(main, "consume_ai_quota_or_503", _quota)
    monkeypatch.setattr(pronunciation_router, "consume_ai_quota_or_503", _quota)
    return calls


@pytest.fixture
def ledger(monkeypatch):
    """A granting quota that records every charge and release."""
    events: list[tuple[str, str, str]] = []

    async def _quota(db, user_id, feature, key):
        events.append(("charge", feature, key))
        return {"granted": True, "replayed": False}

    async def _release(db, user_id, feature, key):
        events.append(("release", feature, key))

    monkeypatch.setattr(main, "consume_ai_quota_or_503", _quota)
    monkeypatch.setattr(main, "release_ai_quota_grant", _release)
    return events


@pytest.mark.parametrize("path", ["/api/feedback", "/api/feedback/v3"])
def test_feedback_charges_before_transcribing(path, whisper_calls, refusing_quota):
    with TestClient(main.app) as client:
        resp = client.post(path, files=AUDIO, data=QUESTION, headers=AUTH)
    assert resp.status_code == 429
    assert refusing_quota, "the request was never charged"
    assert whisper_calls == [], "Whisper ran for a request the allowance refused"


def test_stream_charges_before_transcribing_and_sends_no_transcript(whisper_calls, refusing_quota):
    with TestClient(main.app) as client:
        resp = client.post("/api/feedback/stream", files=AUDIO, data=QUESTION, headers=AUTH)
    events = [json.loads(line) for line in resp.text.splitlines() if line.strip()]
    assert whisper_calls == []
    assert not any(e["type"] == "transcript" for e in events), events
    assert events[-1]["type"] == "error"


def test_an_identical_retry_uses_the_same_key(whisper_calls, ledger, monkeypatch):
    async def _feedback(req, audio_path=None, audio_mime="audio/webm"):
        return {"scores": {"comm": 6}, "modelUsed": "groq/stub"}

    monkeypatch.setattr(main, "call_ai_feedback", _feedback)
    with TestClient(main.app) as client:
        for _ in range(2):
            assert client.post("/api/feedback", files=AUDIO, data=QUESTION, headers=AUTH).status_code == 200
    keys = {key for kind, _, key in ledger if kind == "charge"}
    assert len(keys) == 1, ledger


def test_the_stream_and_its_v3_fallback_share_the_key(whisper_calls, ledger, monkeypatch):
    async def _feedback(req, audio_path=None, audio_mime="audio/webm"):
        return {"scores": {"comm": 6}, "modelUsed": "groq/stub"}

    monkeypatch.setattr(main, "call_ai_feedback", _feedback)
    monkeypatch.setattr(main, "get_groq", lambda: None)
    with TestClient(main.app) as client:
        client.post("/api/feedback/stream", files=AUDIO, data=QUESTION, headers=AUTH)
        client.post("/api/feedback/v3", files=AUDIO, data=QUESTION, headers=AUTH)
    keys = {key for kind, _, key in ledger if kind == "charge"}
    assert len(keys) == 1, ledger


def test_a_failed_marking_gives_the_answer_back(whisper_calls, ledger, monkeypatch):
    async def _fails(req, audio_path=None, audio_mime="audio/webm"):
        raise HTTPException(status_code=502, detail="every provider failed")

    monkeypatch.setattr(main, "call_ai_feedback", _fails)
    with TestClient(main.app) as client:
        resp = client.post("/api/feedback", files=AUDIO, data=QUESTION, headers=AUTH)
    assert resp.status_code == 502
    assert [kind for kind, _, _ in ledger] == ["charge", "release"]


def test_an_empty_transcription_gives_the_answer_back(ledger, monkeypatch):
    async def _silence(path, lang, prompt=None):
        return {"text": "", "words": [], "segments": []}

    async def _no_local(path, lang):
        return {}

    monkeypatch.setattr(main, "GROQ_API_KEY", "stub")
    monkeypatch.setattr(main, "_groq_whisper", _silence)
    monkeypatch.setattr(main, "_faster_whisper", _no_local)
    with TestClient(main.app) as client:
        resp = client.post("/api/feedback", files=AUDIO, data=QUESTION, headers=AUTH)
        stream = client.post("/api/feedback/stream", files=AUDIO, data=QUESTION, headers=AUTH)
    assert resp.status_code == 400
    assert '"error"' in stream.text
    assert [kind for kind, _, _ in ledger] == ["charge", "release", "charge", "release"]


@pytest.mark.parametrize("path", ["/api/feedback", "/api/feedback/stream"])
def test_an_oversized_upload_is_413_before_any_provider(path, whisper_calls, ledger, monkeypatch):
    monkeypatch.setattr(main, "_TRANSCRIBE_MAX_BYTES", 64)
    with TestClient(main.app) as client:
        resp = client.post(path, files=AUDIO, data=QUESTION, headers=AUTH)
    assert resp.status_code == 413
    assert whisper_calls == []
    assert ledger == []


def test_pronunciation_charges_before_transcribing(whisper_calls, refusing_quota):
    with TestClient(main.app) as client:
        resp = client.post(
            "/api/pronunciation", files=AUDIO, data={"target_text": "bonjour", "mode": "freeform"}, headers=AUTH
        )
    assert resp.status_code == 429
    assert refusing_quota == ["pronunciation"]
    assert whisper_calls == []
