"""POST /api/native-voice (Pronunciation Lab Stage 4b).

Offline: Azure TTS and Supabase Storage are an httpx.MockTransport, the quota
and the character ledger are in-memory fakes. Covers the voice-key parity
vectors, the cached path (no charge, no Azure), the charged path (quota key =
object path, characters reserved, file uploaded where the app plays it), every
failure's releases, the monthly cap, Azure's own rate limit, text validation,
guests, and auth.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import jwt as pyjwt
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

import lib.auth as lib_auth
import routers.native_voice as nv
from lib.ai_quota import QuotaDenied
from lib.voice_key import normalize_spoken_text, voice_object_path

_SECRET = "test-jwt-secret-at-least-32-characters-long!!"
USER_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SUPABASE = "https://proj.supabase.co"
SENTENCE = "Le week-end dernier, je suis allé au cinéma avec mes amis."
MP3 = b"\xff\xf3" + b"\x00" * 4000
VECTORS = Path(__file__).resolve().parents[1] / "data" / "tts" / "voice_key_vectors.json"


def _jwt(sub: str) -> str:
    return pyjwt.encode({"sub": sub, "exp": int(time.time()) + 3600}, _SECRET, algorithm="HS256")


# ── Voice key parity ─────────────────────────────────────────────────────────

def test_voice_key_matches_the_shared_vectors():
    data = json.loads(VECTORS.read_text(encoding="utf-8"))
    assert data["version"] == "v2" and data["voice"] == "henri"
    for v in data["vectors"]:
        assert normalize_spoken_text(v["text"]) == v["normalized"]
        assert voice_object_path(v["text"]) == v["path"]


def test_voice_key_pins_the_exam_greeting():
    # The same pinned vector as the main repo's voiceKey.test.ts.
    assert voice_object_path("Bonjour ! Comment ça va ? Es-tu prêt ? On va commencer.") == (
        "v2/henri/2363d401f53d9816d3b069c74953fd5a.mp3"
    )


def test_voice_key_whitespace_is_javascripts():
    assert normalize_spoken_text("﻿Bonjour ! ") == "Bonjour !"
    # \x1f is whitespace to Python's \s but not to JavaScript's: it must be kept.
    assert normalize_spoken_text("a\x1fb") == "a\x1fb"


def test_ssml_matches_the_recording_script():
    assert nv.build_ssml("Tu es là & moi <ici>") == (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xmlns:mstts="http://www.w3.org/2001/mstts" xml:lang="fr-FR">'
        '<voice name="fr-FR-HenriNeural"><mstts:silence type="Sentenceboundary" value="250ms"/>'
        "Tu es là &amp; moi &lt;ici&gt;</voice></speak>"
    )


# ── Fakes ────────────────────────────────────────────────────────────────────

class _Res:
    def __init__(self, data):
        self.data = data


class _Rpc:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return _Res(self.fn())


class FakeDb:
    def __init__(self, *, granted: bool = True, fail: bool = False):
        self.granted, self.fail = granted, fail
        self.calls: list[tuple[str, dict]] = []

    def rpc(self, name, args):
        self.calls.append((name, args))

        def run():
            if self.fail:
                raise RuntimeError("supabase down")
            if name == "reserve_azure_tts_characters":
                if not self.granted:
                    return {"granted": False, "reason": "budget_exhausted"}
                return {"granted": True, "reservation_id": "r-1"}
            return {"ok": True}

        return _Rpc(run)

    def names(self):
        return [n for n, _ in self.calls]


class Quota:
    def __init__(self, deny: int | None = None):
        self.deny = deny
        self.calls: list[tuple[str, str, str]] = []
        self.released: list[tuple[str, str, str]] = []

    async def consume(self, db, user_id, feature, key):
        self.calls.append((user_id, feature, key))
        if self.deny:
            raise QuotaDenied(status_code=self.deny, detail={"error": "daily_limit_reached"})
        return {"granted": True}

    async def release(self, db, user_id, feature, key):
        self.released.append((user_id, feature, key))


class Env:
    def __init__(self, monkeypatch, *, stored: bool = False, azure_status: int = 200, upload_status: int = 200,
                 azure_type: str = "audio/mpeg", azure_body: bytes = MP3, db: FakeDb | None = None,
                 quota: Quota | None = None, azure: bool = True):
        monkeypatch.setattr(lib_auth, "SUPABASE_JWT_SECRET", _SECRET)
        monkeypatch.setenv("SUPABASE_URL", SUPABASE + "/")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "service-key")
        if azure:
            monkeypatch.setenv("AZURE_SPEECH_KEY", "azure-key")
            monkeypatch.setenv("AZURE_SPEECH_REGION", "westeurope")
        else:
            monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
        self.db = db or FakeDb()
        monkeypatch.setattr(nv, "_db", lambda: self.db)
        self.quota = quota or Quota()
        monkeypatch.setattr(nv, "consume_ai_quota_or_503", self.quota.consume)
        monkeypatch.setattr(nv, "release_ai_quota_grant", self.quota.release)
        self.requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            url = str(request.url)
            if request.method == "HEAD":
                return httpx.Response(200 if stored else 400)
            if "tts.speech.microsoft.com" in url:
                return httpx.Response(azure_status, content=azure_body if azure_status == 200 else b"nope",
                                      headers={"content-type": azure_type if azure_status == 200 else "text/plain"})
            if "/storage/v1/object/" in url:
                return httpx.Response(upload_status, json={"Key": "ok"} if upload_status < 300 else {"message": "boom"})
            raise AssertionError(f"unexpected request {request.method} {url}")

        original = httpx.AsyncClient

        def patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", patched)
        router = APIRouter(prefix="/api")
        router.post("/native-voice", response_model=nv.NativeVoiceResponse)(nv.native_voice)
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def post(self, text=SENTENCE, *, sub: str | None = USER_A, headers=None):
        h = dict(headers or {})
        if sub:
            h["Authorization"] = f"Bearer {_jwt(sub)}"
        return self.client.post("/api/native-voice", json={"text": text}, headers=h)

    def kinds(self):
        out = []
        for r in self.requests:
            url = str(r.url)
            out.append("head" if r.method == "HEAD" else "azure" if "microsoft" in url else "upload")
        return out


# ── Paths ────────────────────────────────────────────────────────────────────

def test_a_stored_sentence_is_served_with_no_charge_and_no_azure(monkeypatch):
    env = Env(monkeypatch, stored=True)
    resp = env.post()
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready", "path": voice_object_path(SENTENCE), "cached": True}
    assert env.kinds() == ["head"]
    assert str(env.requests[0].url) == f"{SUPABASE}/storage/v1/object/public/tts-audio/{voice_object_path(SENTENCE)}"
    assert env.quota.calls == [] and env.db.calls == []


def test_a_new_sentence_is_synthesised_once_and_stored_where_the_app_plays_it(monkeypatch):
    env = Env(monkeypatch)
    resp = env.post("  Le week-end dernier, je suis allé au cinéma avec mes amis.  ")
    path = voice_object_path(SENTENCE)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready", "path": path, "cached": False}
    assert env.kinds() == ["head", "azure", "upload"]
    assert env.quota.calls == [(USER_A, "native_voice", f"native_voice:{path}")]
    assert env.db.names() == ["reserve_azure_tts_characters"]
    assert env.db.calls[0][1] == {"p_user_id": USER_A, "p_object_path": path, "p_characters": len(SENTENCE)}
    azure, upload = env.requests[1], env.requests[2]
    assert azure.headers["X-Microsoft-OutputFormat"] == "audio-24khz-48kbitrate-mono-mp3"
    assert azure.headers["Ocp-Apim-Subscription-Key"] == "azure-key"
    assert azure.content.decode() == nv.build_ssml(SENTENCE)
    assert str(upload.url) == f"{SUPABASE}/storage/v1/object/tts-audio/{path}"
    assert upload.headers["x-upsert"] == "false" and upload.headers["content-type"] == "audio/mpeg"
    assert upload.headers["authorization"] == "Bearer service-key"
    assert upload.content == MP3
    assert env.quota.released == []


def test_an_upload_that_finds_the_file_already_there_is_a_success(monkeypatch):
    env = Env(monkeypatch, upload_status=409)
    resp = env.post()
    assert resp.status_code == 200 and resp.json()["cached"] is False
    assert env.quota.released == []


# ── Failures release what they charged ───────────────────────────────────────

@pytest.mark.parametrize("kwargs", [
    {"azure_status": 500},
    {"azure_type": "application/json"},
    {"azure_body": b"\xff" * 10},
    {"upload_status": 500},
])
def test_a_provider_failure_releases_quota_and_characters(monkeypatch, kwargs):
    env = Env(monkeypatch, **kwargs)
    resp = env.post()
    assert resp.status_code == 502
    assert resp.json()["detail"] == {"error": "native_voice_failed"}
    path = voice_object_path(SENTENCE)
    assert env.quota.released == [(USER_A, "native_voice", f"native_voice:{path}")]
    assert env.db.names() == ["reserve_azure_tts_characters", "release_azure_tts_characters"]
    assert env.db.calls[1][1] == {"p_reservation_id": "r-1"}


def test_azures_own_rate_limit_is_a_429_and_releases(monkeypatch):
    env = Env(monkeypatch, azure_status=429)
    resp = env.post()
    assert resp.status_code == 429
    assert resp.json()["detail"] == {"error": "native_voice_busy"}
    assert resp.headers["retry-after"] == "60"
    assert len(env.quota.released) == 1
    assert env.db.names()[-1] == "release_azure_tts_characters"


def test_the_monthly_cap_refuses_before_azure_and_releases_the_quota(monkeypatch):
    env = Env(monkeypatch, db=FakeDb(granted=False))
    resp = env.post()
    assert resp.status_code == 429
    assert resp.json()["detail"] == {"error": "native_voice_budget_exhausted"}
    assert "azure" not in env.kinds()
    assert len(env.quota.released) == 1


def test_an_unreadable_ledger_fails_closed(monkeypatch):
    env = Env(monkeypatch, db=FakeDb(fail=True))
    resp = env.post()
    assert resp.status_code == 503
    assert resp.json()["detail"] == {"error": "native_voice_unavailable"}
    assert "azure" not in env.kinds()
    assert len(env.quota.released) == 1


def test_the_daily_quota_refuses_before_the_ledger_and_azure(monkeypatch):
    env = Env(monkeypatch, quota=Quota(deny=429))
    resp = env.post()
    assert resp.status_code == 429
    assert env.db.calls == [] and "azure" not in env.kinds()


def test_no_azure_configured_is_unavailable(monkeypatch):
    env = Env(monkeypatch, azure=False)
    assert env.post().status_code == 503
    assert env.quota.calls == []


def test_no_azure_configured_still_serves_a_stored_file(monkeypatch):
    env = Env(monkeypatch, azure=False, stored=True)
    assert env.post().status_code == 200


def test_no_storage_configured_is_unavailable(monkeypatch):
    env = Env(monkeypatch)
    monkeypatch.delenv("SUPABASE_URL")
    resp = env.post()
    assert resp.status_code == 503 and env.requests == []


# ── Validation and auth ──────────────────────────────────────────────────────

@pytest.mark.parametrize("text,error", [
    ("   ", "invalid_text"),
    ("123 456 !", "invalid_text"),
    ("Bonjour\x00 toi", "invalid_text"),
    ("a" * 601, "text_too_long"),
])
def test_invalid_text_is_refused_before_anything_is_charged(monkeypatch, text, error):
    env = Env(monkeypatch)
    resp = env.post(text)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == error
    assert env.requests == [] and env.quota.calls == []


def test_signed_out_is_401_when_guest_ai_is_off(monkeypatch):
    env = Env(monkeypatch)
    resp = env.post(sub=None)
    assert resp.status_code == 401
    assert env.requests == []


def test_a_bad_token_is_401_even_with_guests_on(monkeypatch):
    monkeypatch.setenv("GUEST_AI_ENABLED", "1")
    env = Env(monkeypatch)
    resp = env.post(sub=None, headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_guests_are_charged_as_guests_and_metered_without_a_user(monkeypatch):
    monkeypatch.setenv("GUEST_AI_ENABLED", "1")
    env = Env(monkeypatch)
    resp = env.post(sub=None, headers={"X-Forwarded-For": "203.0.113.9"})
    assert resp.status_code == 200
    assert env.quota.calls[0][0] == "guest:203.0.113.9"
    assert env.db.calls[0][1]["p_user_id"] is None


def test_the_route_is_registered_on_the_app():
    import main

    paths = {getattr(r, "path", None) for r in main.app.routes}
    assert "/api/native-voice" in paths
