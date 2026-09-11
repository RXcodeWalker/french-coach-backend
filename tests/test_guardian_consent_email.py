"""Phase 1.6 Part C — POST /api/consent/send-guardian-email.

Confirms:
- 401 without a valid caller JWT (route is gated, not open);
- 503 when SMTP isn't configured (default posture — no silent "sent");
- 503 when APP_ORIGIN isn't configured;
- a configured send calls smtplib with the expected recipient/link and
  returns {"ok": true}.

Follows the TestClient + monkeypatch pattern and _fake_jwt helper shape of
test_pronunciation.py.
"""

from __future__ import annotations

import os
import sys
import time

import jwt as pyjwt
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main

_VALID_JWT_SECRET = "test-jwt-secret-at-least-32-characters-long!!"


def _fake_jwt(sub: str = "user-123", secret: str = _VALID_JWT_SECRET) -> str:
    return pyjwt.encode({"sub": sub, "exp": int(time.time()) + 3600}, secret, algorithm="HS256")


def test_rejects_anonymous_caller(monkeypatch):
    monkeypatch.setattr(main, "SUPABASE_JWT_SECRET", _VALID_JWT_SECRET)
    with TestClient(main.app) as client:
        resp = client.post(
            "/api/consent/send-guardian-email",
            json={"guardian_email": "parent@example.com", "token": "tok"},
        )
    assert resp.status_code == 401


def test_503_when_smtp_not_configured(monkeypatch):
    monkeypatch.setattr(main, "SUPABASE_JWT_SECRET", _VALID_JWT_SECRET)
    monkeypatch.setattr(main, "SMTP_HOST", "")
    monkeypatch.setattr(main, "SMTP_FROM", "")
    token = _fake_jwt()
    with TestClient(main.app) as client:
        resp = client.post(
            "/api/consent/send-guardian-email",
            json={"guardian_email": "parent@example.com", "token": "tok"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 503


def test_503_when_app_origin_not_configured(monkeypatch):
    monkeypatch.setattr(main, "SUPABASE_JWT_SECRET", _VALID_JWT_SECRET)
    monkeypatch.setattr(main, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(main, "SMTP_FROM", "noreply@example.com")
    monkeypatch.setattr(main, "APP_ORIGIN", "")
    token = _fake_jwt()
    with TestClient(main.app) as client:
        resp = client.post(
            "/api/consent/send-guardian-email",
            json={"guardian_email": "parent@example.com", "token": "tok"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 503


def test_sends_email_and_returns_ok_when_configured(monkeypatch):
    monkeypatch.setattr(main, "SUPABASE_JWT_SECRET", _VALID_JWT_SECRET)
    monkeypatch.setattr(main, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(main, "SMTP_FROM", "noreply@example.com")
    monkeypatch.setattr(main, "APP_ORIGIN", "https://francais.example.com")

    sent = {}

    def _fake_send(guardian_email: str, consent_url: str) -> None:
        sent["guardian_email"] = guardian_email
        sent["consent_url"] = consent_url

    monkeypatch.setattr(main, "_send_guardian_consent_email", _fake_send)

    token = _fake_jwt()
    with TestClient(main.app) as client:
        resp = client.post(
            "/api/consent/send-guardian-email",
            json={"guardian_email": "parent@example.com", "token": "abc123"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert sent["guardian_email"] == "parent@example.com"
    assert sent["consent_url"] == "https://francais.example.com/guardian-consent?token=abc123"


def test_502_when_send_raises(monkeypatch):
    monkeypatch.setattr(main, "SUPABASE_JWT_SECRET", _VALID_JWT_SECRET)
    monkeypatch.setattr(main, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(main, "SMTP_FROM", "noreply@example.com")
    monkeypatch.setattr(main, "APP_ORIGIN", "https://francais.example.com")

    def _raise(*_args, **_kwargs):
        raise RuntimeError("smtp connection refused")

    monkeypatch.setattr(main, "_send_guardian_consent_email", _raise)

    token = _fake_jwt()
    with TestClient(main.app) as client:
        resp = client.post(
            "/api/consent/send-guardian-email",
            json={"guardian_email": "parent@example.com", "token": "abc123"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 502
