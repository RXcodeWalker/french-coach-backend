"""W7 reliability — POST /api/exam/interpret auth.

/api/exam/interpret was documented (exam_controller.py, "Rate limiting" section)
as a known-unauthenticated gap: every candidate turn during a live exam calls
it, unauthenticated and unquota'd. This closes the auth hole with verify_jwt,
deliberately WITHOUT consume_ai_quota_or_503 — see the route's own docstring
and verification-log.md for why (max_tokens=60, fixed prompt, and the session
is already metered where the real cost is).

Confirms:
- the endpoint requires a valid Supabase JWT, same convention as
  test_transcribe_endpoint.py / main.py's other authed routes;
- a signed-in caller reaches the route's normal behavior (no AI-quota check
  in front of it) — an empty transcript short-circuits to the deterministic
  "silence" observation before any Groq call, so this needs no provider stub.
- GET /api/exam/interpret/health stays unauthenticated — it's the frontend's
  pre-exam warm-up probe (pingInterpretServiceHealth), fired before any auth
  context is guaranteed, and makes no model call.

No live network call. Follows the TestClient + monkeypatch pattern of
test_api_surface_lockdown.py / test_transcribe_endpoint.py.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

import exam_controller
import main


def test_interpret_requires_auth():
    with TestClient(main.app) as client:
        resp = client.post("/api/exam/interpret", json={"transcript": "Oui.", "part": "topic1"})
    assert resp.status_code == 401


def test_interpret_rejects_a_bogus_bearer_token():
    with TestClient(main.app) as client:
        resp = client.post(
            "/api/exam/interpret",
            json={"transcript": "Oui.", "part": "topic1"},
            headers={"Authorization": "Bearer not-a-real-jwt"},
        )
    # 401 (bad signature) or 503 (this environment has no JWT secret/JWKS
    # configured at all — see test_api_surface_lockdown.py's identical
    # tolerance for /metrics) — either way, never processed as authenticated.
    assert resp.status_code in (401, 503)
    assert resp.status_code != 200


def test_interpret_processes_normally_once_authenticated(monkeypatch):
    monkeypatch.setattr(exam_controller, "verify_supabase_jwt", lambda authorization: {"sub": "user-1"})

    with TestClient(main.app) as client:
        resp = client.post(
            "/api/exam/interpret",
            json={"transcript": "", "part": "topic1"},
            headers={"Authorization": "Bearer whatever-verify_supabase_jwt-accepts"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"speechAct": "silence", "hesitation": False, "confidence": 1.0}


def test_interpret_health_stays_unauthenticated():
    with TestClient(main.app) as client:
        resp = client.get("/api/exam/interpret/health")
    assert resp.status_code == 200
