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
