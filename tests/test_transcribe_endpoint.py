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


def test_transcribe_rejects_a_bogus_bearer_token():
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
