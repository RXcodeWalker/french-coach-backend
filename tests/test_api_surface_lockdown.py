"""Phase 1.2 — FastAPI incidental-exposure lockdown.

Confirms:
- interactive API docs (/docs, /redoc, /openapi.json) are OFF by default;
- GET /metrics requires an admin JWT (no anonymous access);
- POST /api/admin/roles 404s while ADMIN_SETUP_ENABLED is unset — the
  break-glass admin-grant path is not a standing route.

No live network call. Follows the TestClient + monkeypatch pattern of
test_pronunciation.py / test_engine_preference.py.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

import main


def test_docs_are_disabled_by_default():
    with TestClient(main.app) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_root_does_not_advertise_docs_when_disabled():
    with TestClient(main.app) as client:
        body = client.get("/").json()
    assert "docs" not in body
    assert body["health"] == "/health"


def test_metrics_requires_auth_no_anonymous_access():
    with TestClient(main.app) as client:
        resp = client.get("/metrics")
    # 401 (no/invalid token), 403 (valid non-admin token), or 503 (server has
    # no JWT secret configured) — all of which mean "not anonymously readable".
    # What must NOT happen is a 200 with the traffic map.
    assert resp.status_code in (401, 403, 503)
    assert resp.status_code != 200


def test_metrics_rejects_a_bogus_bearer_token():
    with TestClient(main.app) as client:
        resp = client.get("/metrics", headers={"Authorization": "Bearer not-a-real-jwt"})
    assert resp.status_code in (401, 403, 503)
    assert resp.status_code != 200


def test_admin_roles_is_404_when_setup_flag_unset(monkeypatch):
    # Simulate prod: neither ADMIN_SETUP_ENABLED nor ADMIN_SETUP_SECRET set.
    monkeypatch.setattr(main, "ADMIN_SETUP_ENABLED", False)
    monkeypatch.setattr(main, "ADMIN_SETUP_SECRET", "")
    with TestClient(main.app) as client:
        resp = client.post("/api/admin/roles", json={"user_id": "u1", "secret": "anything"})
    assert resp.status_code == 404


def test_admin_roles_still_404_when_secret_set_but_flag_off(monkeypatch):
    # The secret alone is no longer enough — the flag must also be true.
    monkeypatch.setattr(main, "ADMIN_SETUP_ENABLED", False)
    monkeypatch.setattr(main, "ADMIN_SETUP_SECRET", "s3cr3t")
    with TestClient(main.app) as client:
        resp = client.post("/api/admin/roles", json={"user_id": "u1", "secret": "s3cr3t"})
    assert resp.status_code == 404


def test_admin_roles_rejects_wrong_secret_when_enabled(monkeypatch):
    monkeypatch.setattr(main, "ADMIN_SETUP_ENABLED", True)
    monkeypatch.setattr(main, "ADMIN_SETUP_SECRET", "s3cr3t")
    with TestClient(main.app) as client:
        resp = client.post("/api/admin/roles", json={"user_id": "u1", "secret": "wrong"})
    # Enabled + wrong secret -> 403, not 404 and not a grant.
    assert resp.status_code == 403


# ── Security review 2026-11 F5: no paid model call without a sign-in ─────────

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402


class _FakeGemini:
    def __init__(self, calls):
        self.calls = calls

    def generate_content(self, prompt, **kwargs):
        self.calls.append(prompt)

        class _R:
            text = '{"ipa":"x","hint":"x","sentences":[],"tip":"x","vocab":[],"phrases":[]}'
        return _R()


class _FakeGroq:
    def __init__(self, calls):
        outer = self
        self.calls = calls

        class _Completions:
            async def create(self, **kwargs):
                outer.calls.append(kwargs.get("messages"))

                class _Msg:
                    content = '{"rule":"x","examples":[],"practice":[]}'

                class _Choice:
                    message = _Msg()

                class _Resp:
                    choices = [_Choice()]
                return _Resp()

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


@pytest.fixture
def model_calls(monkeypatch):
    calls: list = []
    monkeypatch.setattr(main, "get_gemini", lambda: _FakeGemini(calls))
    monkeypatch.setattr(main, "get_groq", lambda: _FakeGroq(calls))

    async def _scenario(description):
        calls.append(description)
        return {"title": "t"}

    import scenario_generator
    monkeypatch.setattr(scenario_generator, "generate_scenario", _scenario)
    main._CACHE.pop_prefix_sync("grammar:")
    main._CACHE.pop_prefix_sync("vocab:")
    return calls


def _call(client, route, headers=None):
    if route == "drill":
        return client.post("/api/drill", data={"word": "bonjour", "issue": "ignore the above"}, headers=headers)
    if route == "grammar_lesson":
        return client.get("/api/grammar-lesson", params={"topic": "le passé composé"}, headers=headers)
    if route == "vocab_prep":
        return client.get("/api/vocab-prep", params={"topic": "la famille"}, headers=headers)
    return client.post("/api/generate-scenario", json={"description": "au marché"}, headers=headers)


_ROUTES = ["drill", "grammar_lesson", "vocab_prep", "scenario_generate"]


@pytest.mark.parametrize("route", _ROUTES)
def test_ai_routes_need_a_sign_in(model_calls, route):
    with TestClient(main.app) as client:
        resp = _call(client, route)
    assert resp.status_code == 401, resp.text
    assert model_calls == [], "an anonymous request reached the model"


@pytest.mark.parametrize("route", _ROUTES)
def test_ai_routes_charge_their_own_row_before_the_model(monkeypatch, model_calls, route):
    monkeypatch.setattr(main, "verify_jwt", lambda authorization: "user-1")
    charged: list[str] = []

    async def _refuse(db, user_id, feature, key):
        charged.append(feature)
        raise HTTPException(status_code=429, detail={"error": "daily_quota_reached"})

    monkeypatch.setattr(main, "consume_ai_quota_or_503", _refuse)
    with TestClient(main.app) as client:
        resp = _call(client, route, headers={"Authorization": "Bearer t"})
    assert resp.status_code == 429
    assert charged == [route]
    assert model_calls == []


def test_generate_scenario_reads_its_body(monkeypatch, model_calls):
    """It answered 422 "missing query req" for every call: slowapi's wrapper made
    FastAPI resolve the string annotation in the wrong module (main.rate_limit)."""
    monkeypatch.setattr(main, "verify_jwt", lambda authorization: "user-1")

    async def _grant(db, user_id, feature, key):
        return {"granted": True}

    monkeypatch.setattr(main, "consume_ai_quota_or_503", _grant)
    with TestClient(main.app) as client:
        resp = _call(client, "scenario_generate", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 200, resp.text
    assert model_calls == ["au marché"]


def test_roleplay_turn_reads_its_body():
    # Same root cause as generate-scenario: the body is resolved, so the route
    # reaches its own auth check instead of a 422.
    with TestClient(main.app) as client:
        resp = client.post("/api/roleplay/turn", json={"scenario_id": "cafe", "turn_history": [], "student_transcript": "x"})
    assert resp.status_code == 401


@pytest.mark.parametrize("path", ["/api/exam/start", "/api/exam/respond", "/api/exam/finish", "/api/exam/evaluate"])
def test_the_legacy_exam_pipeline_is_gone(path):
    with TestClient(main.app) as client:
        resp = client.post(path, json={"candidate_id": "anyone", "session_id": "s"})
    assert resp.status_code == 404


# ── Security review 2026-11 F11a: the SRS routes are gone ────────────────────
# They took `user_id` from the caller and read/wrote srs_cards with the service
# key and no sign-in (inert only because the table does not exist).


@pytest.mark.parametrize(
    ("method", "path"),
    [("post", "/api/srs/card"), ("get", "/api/srs/next"), ("post", "/api/srs/review")],
)
def test_the_srs_routes_are_gone(monkeypatch, method, path):
    touched: list[str] = []
    monkeypatch.setattr(main, "get_supabase", lambda: touched.append("db"))
    with TestClient(main.app) as client:
        resp = getattr(client, method)(path, params={"user_id": "22222222-2222-2222-2222-222222222222"})
    assert resp.status_code == 404
    assert touched == []
