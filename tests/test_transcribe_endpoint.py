"""Reliability plan §2.4 — POST /api/transcribe auth + upload size cap.

Confirms:
- the endpoint requires a valid Supabase JWT, same as the other authed routes
  (main.py:4431/4493/4510's verify_jwt convention);
- an upload exceeding the size cap is rejected with 413 before a temp file is
  ever written, rather than trusting the client-supplied Content-Length.

No live network call (no Groq/faster-whisper invocation reached in either
case — auth and the size check both short-circuit first). Follows the
TestClient pattern of test_api_surface_lockdown.py.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

import main


def test_transcribe_requires_auth():
    with TestClient(main.app) as client:
        resp = client.post(
            "/api/transcribe",
            files={"audio": ("recording.webm", b"fake-audio-bytes", "audio/webm")},
            data={"language": "fr"},
        )
    assert resp.status_code == 401


def test_transcribe_rejects_a_bogus_bearer_token(monkeypatch):
    # Without a configured secret (CI has no .env) the verifier answers 503
    # "Auth not configured" before it ever looks at the token, so the 401 path
    # under test is only reachable when a secret is set.
    monkeypatch.setattr(main, "SUPABASE_JWT_SECRET", "test-secret-not-used-to-sign-anything")
    with TestClient(main.app) as client:
        resp = client.post(
            "/api/transcribe",
            files={"audio": ("recording.webm", b"fake-audio-bytes", "audio/webm")},
            data={"language": "fr"},
            headers={"Authorization": "Bearer not-a-real-jwt"},
        )
    assert resp.status_code == 401


def test_transcribe_rejects_oversized_upload_before_writing_a_temp_file(monkeypatch):
    monkeypatch.setattr(main, "verify_jwt", lambda authorization: "user-1")

    oversized = b"x" * (main._TRANSCRIBE_MAX_BYTES + 1)

    written_paths: list[str] = []
    original_named_temp_file = main.tempfile.NamedTemporaryFile

    def _tracking_named_temp_file(*args, **kwargs):
        tmp = original_named_temp_file(*args, **kwargs)
        written_paths.append(tmp.name)
        return tmp

    monkeypatch.setattr(main.tempfile, "NamedTemporaryFile", _tracking_named_temp_file)

    with TestClient(main.app) as client:
        resp = client.post(
            "/api/transcribe",
            files={"audio": ("recording.webm", oversized, "audio/webm")},
            data={"language": "fr"},
            headers={"Authorization": "Bearer whatever"},
        )

    assert resp.status_code == 413
    assert written_paths == []


# ── Batch ASR plan, Phase 1: model from env, prompt, confidence, purpose ──────

import types

import pytest

from lib import model_config


class _FakeSeg:
    def __init__(self, text="bonjour", start=0.0, end=1.0, avg_logprob=-0.2, no_speech_prob=0.01, compression_ratio=1.1):
        self.text, self.start, self.end = text, start, end
        self.avg_logprob, self.no_speech_prob, self.compression_ratio = avg_logprob, no_speech_prob, compression_ratio


def _fake_groq(captured):
    async def _create(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(text=" bonjour ", segments=[_FakeSeg()])

    return types.SimpleNamespace(audio=types.SimpleNamespace(transcriptions=types.SimpleNamespace(create=_create)))


def _audio_file(tmp_path):
    p = tmp_path / "a.webm"
    p.write_bytes(b"audio")
    return str(p)


@pytest.mark.asyncio
async def test_groq_whisper_uses_the_env_model_and_keeps_confidence(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(main, "get_groq", lambda: _fake_groq(captured))
    monkeypatch.setattr(main, "GROQ_WHISPER_MODEL", "whisper-large-v3")
    out = await main._groq_whisper(_audio_file(tmp_path), "fr")
    assert captured["model"] == "whisper-large-v3"
    assert "prompt" not in captured
    assert out["model"] == "whisper-large-v3"
    assert out["text"] == "bonjour"
    seg = out["segments"][0]
    assert (seg["start"], seg["end"], seg["text"]) == (0.0, 1.0, "bonjour")
    assert seg["avgLogprob"] == -0.2 and seg["noSpeechProb"] == 0.01 and seg["compressionRatio"] == 1.1
    assert out["source"] == "groq-whisper"


@pytest.mark.asyncio
async def test_groq_whisper_passes_the_prompt_truncated(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(main, "get_groq", lambda: _fake_groq(captured))
    long_prompt = "Que fais-tu le week-end ?\n" + " ".join(f"mot{i}" for i in range(400))
    await main._groq_whisper(_audio_file(tmp_path), "fr", long_prompt)
    sent = captured["prompt"]
    assert len(sent) <= main._WHISPER_PROMPT_MAX_CHARS
    assert sent.startswith("Que fais-tu le week-end ?")
    assert sent.endswith("mot399")  # most recent context survives


def test_model_id_comes_from_env_not_a_literal():
    assert main.GROQ_WHISPER_MODEL == model_config.GROQ_WHISPER_MODEL
    src = open(main.__file__, encoding="utf-8").read()
    assert 'model="whisper-large-v3' not in src


def test_default_whisper_model_is_large_v3(monkeypatch):
    import importlib

    monkeypatch.delenv("GROQ_WHISPER_MODEL", raising=False)
    try:
        assert importlib.reload(model_config).GROQ_WHISPER_MODEL == "whisper-large-v3"
        monkeypatch.setenv("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo")
        assert importlib.reload(model_config).GROQ_WHISPER_MODEL == "whisper-large-v3-turbo"
    finally:
        monkeypatch.undo()
        importlib.reload(model_config)


def test_truncate_prompt_matrix():
    t = main.truncate_whisper_prompt
    assert t(None) is None and t("   ") is None
    assert t("Que fais-tu ?") == "Que fais-tu ?"
    over_q = "mot " * 400
    cut = t(over_q)
    assert len(cut) <= main._WHISPER_PROMPT_MAX_CHARS and cut == cut.strip()
    q = "Parle-moi de ta famille."
    ctx = "\n".join(["J'ai un frère."] * 80)
    out = t(f"{q}\n{ctx}")
    assert out.startswith(q) and len(out) <= main._WHISPER_PROMPT_MAX_CHARS
    assert t(f"{q}\n{ctx}") == out  # deterministic


@pytest.fixture
def _routed(monkeypatch):
    monkeypatch.setattr(main, "verify_jwt", lambda authorization: "user-1")
    monkeypatch.setattr(main, "GROQ_API_KEY", "k")
    charged, seen = [], {}

    async def _quota(db, user_id, feature, key):
        charged.append(feature)

    async def _consent(db, user_id):
        return None

    async def _fake(path, language, prompt=None):
        seen["prompt"] = prompt
        return {"text": "ok", "segments": [], "source": "groq-whisper", "model": "m"}

    monkeypatch.setattr(main, "consume_ai_quota_or_503", _quota)
    monkeypatch.setattr(main, "require_speaking_consent", _consent)
    monkeypatch.setattr(main, "_groq_whisper", _fake)
    monkeypatch.setattr(main, "get_supabase", lambda: None)
    return charged, seen


def _post_audio(**data):
    with TestClient(main.app) as client:
        return client.post(
            "/api/transcribe",
            files={"audio": ("a.webm", b"x", "audio/webm")},
            data={"language": "fr", **data},
            headers={"Authorization": "Bearer t"},
        )


@pytest.mark.parametrize(
    "purpose,feature",
    [(None, "transcribe"), ("learn", "learn_transcribe"), ("exam_turn", "exam_turn_transcribe")],
)
def test_purpose_selects_the_quota_feature(_routed, purpose, feature):
    charged, _ = _routed
    resp = _post_audio(**({"purpose": purpose} if purpose else {}))
    assert resp.status_code == 200
    assert charged == [feature]


def test_unknown_purpose_is_422_and_charges_nothing(_routed):
    charged, _ = _routed
    resp = _post_audio(purpose="bogus")
    assert resp.status_code == 422
    assert charged == []


def test_route_passes_the_prompt_through_and_stays_backward_compatible(_routed):
    _, seen = _routed
    resp = _post_audio(prompt="Que fais-tu le week-end ?")
    assert resp.status_code == 200
    assert seen["prompt"] == "Que fais-tu le week-end ?"
    body = resp.json()
    assert {"text", "segments", "source"} <= set(body) and body["model"] == "m"


def test_prompt_over_the_form_limit_is_422(_routed):
    assert _post_audio(prompt="x" * 1201).status_code == 422
