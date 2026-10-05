"""Server-side speaking-consent gate (lib/consent.py) on every audio route
(exam-pronunciation plan §5, Batch 1).

A signed-in account whose profiles.consent_status is `pending` (under-13, no
guardian confirmation yet) gets 403 {"error": "consent_required"} from
/api/pronunciation and /api/transcribe before the upload is read or any
provider/quota call is made. A missing profile is denied the same way. A
consent state that can't be read fails closed (503). Guests have no account
and are not gated here.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import lib.consent as consent
import main
import routers.pronunciation as pronunciation_router
from lib import guest

# Captured at import (collection) time, before tests/conftest.py's autouse
# fixture swaps in its consenting stub.
_REAL_FETCH = consent._fetch_consent_status

_AUDIO = {"audio": ("recording.wav", b"RIFF-not-really", "audio/wav")}


def _status(value):
    async def _fetch(db, user_id):
        return value
    return _fetch


@pytest.fixture
def signed_in(monkeypatch):
    monkeypatch.setattr(main, "verify_jwt", lambda authorization: "11111111-1111-1111-1111-111111111111")
    monkeypatch.setattr(
        pronunciation_router, "verify_supabase_jwt", lambda authorization: {"sub": "11111111-1111-1111-1111-111111111111"}
    )


@pytest.fixture
def downstream_calls(monkeypatch):
    """Records any quota charge — the first downstream step of both routes —
    and stops the request there with a sentinel status."""
    calls: list[str] = []

    async def _quota(db, user_id, feature, key):
        calls.append(feature)
        raise HTTPException(status_code=418, detail="reached downstream")

    monkeypatch.setattr(main, "consume_ai_quota_or_503", _quota)
    monkeypatch.setattr(pronunciation_router, "consume_ai_quota_or_503", _quota)
    # Keep /api/pronunciation's best-effort transcription offline.
    async def _no_whisper(path, lang):
        return {"text": "bonjour", "words": [], "segments": []}
    monkeypatch.setattr(pronunciation_router, "_groq_key_check_fn", lambda: False)
    monkeypatch.setattr(pronunciation_router, "_faster_whisper_fn", _no_whisper)
    return calls


def _post(client, path):
    data = {"language": "fr"} if path == "/api/transcribe" else {"target_text": "bonjour", "mode": "scripted"}
    return client.post(path, files=_AUDIO, data=data, headers={"Authorization": "Bearer t"})


@pytest.mark.parametrize("path", ["/api/transcribe", "/api/pronunciation"])
@pytest.mark.parametrize("status", ["pending", consent._MISSING])
def test_pending_or_missing_profile_is_403_before_any_downstream_call(monkeypatch, signed_in, downstream_calls, path, status):
    monkeypatch.setattr(consent, "_fetch_consent_status", _status(status))
    with TestClient(main.app) as client:
        resp = _post(client, path)
    assert resp.status_code == 403
    assert resp.json()["detail"] == {"error": "consent_required"}
    assert downstream_calls == []


@pytest.mark.parametrize("path", ["/api/transcribe", "/api/pronunciation"])
@pytest.mark.parametrize("status", ["13_plus_not_required", "granted"])
def test_consenting_accounts_pass_the_gate(monkeypatch, signed_in, downstream_calls, path, status):
    monkeypatch.setattr(consent, "_fetch_consent_status", _status(status))
    with TestClient(main.app) as client:
        resp = _post(client, path)
    assert resp.status_code == 418
    assert downstream_calls == ["transcribe" if path == "/api/transcribe" else "pronunciation"]


@pytest.mark.parametrize("path", ["/api/transcribe", "/api/pronunciation"])
def test_guests_are_not_consent_gated(monkeypatch, downstream_calls, path):
    monkeypatch.setenv("GUEST_AI_ENABLED", "1")
    guest._grants.clear()

    async def _must_not_read(db, user_id):
        raise AssertionError("a guest has no profile to read")

    monkeypatch.setattr(consent, "_fetch_consent_status", _must_not_read)
    data = {"language": "fr"} if path == "/api/transcribe" else {"target_text": "bonjour"}
    with TestClient(main.app) as client:
        resp = client.post(path, files=_AUDIO, data=data)
    assert resp.status_code == 418


@pytest.mark.parametrize("path", ["/api/transcribe", "/api/pronunciation"])
def test_unreadable_consent_state_fails_closed(monkeypatch, signed_in, downstream_calls, path):
    monkeypatch.setattr(consent, "_fetch_consent_status", _REAL_FETCH)
    monkeypatch.setattr(main, "get_supabase", lambda: None)
    monkeypatch.setattr(pronunciation_router, "_db", lambda: None)
    with TestClient(main.app) as client:
        resp = _post(client, path)
    assert resp.status_code == 503
    assert resp.json()["detail"] == {"error": "consent_check_unavailable"}
    assert downstream_calls == []


def test_repair_endpoint_is_gone():
    with TestClient(main.app) as client:
        resp = client.post("/api/repair", files=_AUDIO, data={"word": "vin"})
    assert resp.status_code == 404


# ── _fetch_consent_status against a fake service-role client ────────────────

class _Query:
    def __init__(self, rows=None, exc=None):
        self.rows, self.exc, self.filters = rows, exc, []

    def select(self, cols):
        self.cols = cols
        return self

    def eq(self, col, val):
        self.filters.append((col, val))
        return self

    def limit(self, n):
        return self

    def execute(self):
        if self.exc:
            raise self.exc
        return type("R", (), {"data": self.rows})()


class _Db:
    def __init__(self, query):
        self.query = query
        self.tables: list[str] = []

    def table(self, name):
        self.tables.append(name)
        return self.query


def test_fetch_reads_the_users_own_profile_row():
    db = _Db(_Query(rows=[{"consent_status": "pending"}]))
    assert asyncio.run(_REAL_FETCH(db, "u-1")) == "pending"
    assert db.tables == ["profiles"]
    assert db.query.cols == "consent_status"
    assert db.query.filters == [("id", "u-1")]


def test_fetch_reports_a_missing_row():
    assert asyncio.run(_REAL_FETCH(_Db(_Query(rows=[])), "u-1")) is consent._MISSING


@pytest.mark.parametrize("db", [None, _Db(_Query(exc=RuntimeError("supabase down")))])
def test_fetch_fails_closed(db):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_REAL_FETCH(db, "u-1"))
    assert exc.value.status_code == 503
