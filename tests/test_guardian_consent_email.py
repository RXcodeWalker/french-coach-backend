"""Phase 1.6 Part C — POST /api/consent/send-guardian-email.

Confirms:
- 401 without a valid caller JWT (route is gated, not open);
- 503 when SMTP isn't configured (default posture — no silent "sent");
- 503 when APP_ORIGIN isn't configured;
- a configured send calls smtplib with the expected recipient/link and
  returns {"ok": true};
- security review F7: the token must look like one, must be the caller's own
  open consent request (the email goes to that request's address, never the
  body's), and an account gets five sends a day.

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

import pytest

import main

TOKEN = "ab" * 32  # the shape request_guardian_consent mints: 64 hex
_REAL_LOOKUP = main._open_guardian_request_email  # before the autouse stub
_VALID_JWT_SECRET = "test-jwt-secret-at-least-32-characters-long!!"


def _fake_jwt(sub: str = "user-123", secret: str = _VALID_JWT_SECRET) -> str:
    return pyjwt.encode({"sub": sub, "exp": int(time.time()) + 3600}, secret, algorithm="HS256")


@pytest.fixture(autouse=True)
def open_request(monkeypatch):
    """user-123 has one open consent request, for parent@example.com, whose
    token is TOKEN. Lookups are recorded."""
    lookups: list[tuple[str, str]] = []

    async def _lookup(db, user_id, token):
        lookups.append((user_id, token))
        return "parent@example.com" if (user_id, token) == ("user-123", TOKEN) else None

    monkeypatch.setattr(main, "_open_guardian_request_email", _lookup)
    monkeypatch.setattr(main, "get_supabase", lambda: object())
    main._guardian_email_sends.clear()
    return lookups


def test_rejects_anonymous_caller(monkeypatch):
    monkeypatch.setattr(main, "SUPABASE_JWT_SECRET", _VALID_JWT_SECRET)
    with TestClient(main.app) as client:
        resp = client.post(
            "/api/consent/send-guardian-email",
            json={"guardian_email": "parent@example.com", "token": TOKEN},
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
            json={"guardian_email": "parent@example.com", "token": TOKEN},
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
            json={"guardian_email": "parent@example.com", "token": TOKEN},
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
            json={"guardian_email": "parent@example.com", "token": TOKEN},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert sent["guardian_email"] == "parent@example.com"
    assert sent["consent_url"] == f"https://francais.example.com/guardian-consent?token={TOKEN}"


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
            json={"guardian_email": "parent@example.com", "token": TOKEN},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 502


def _configured(monkeypatch):
    monkeypatch.setattr(main, "SUPABASE_JWT_SECRET", _VALID_JWT_SECRET)
    monkeypatch.setattr(main, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(main, "SMTP_FROM", "noreply@example.com")
    monkeypatch.setattr(main, "APP_ORIGIN", "https://francais.example.com")
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(main, "_send_guardian_consent_email", lambda to, url: sent.append((to, url)))
    return sent


def _send(client, body, sub="user-123"):
    return client.post(
        "/api/consent/send-guardian-email", json=body, headers={"Authorization": f"Bearer {_fake_jwt(sub=sub)}"}
    )


@pytest.mark.parametrize("token", [
    "x" + "\n" * 2 + "Your account is locked. Sign in at https://evil.example to keep it.",  # the audit's proof
    "AB" * 32,  # upper-case hex is not what the RPC mints
    "ab" * 31,
    "",
])
def test_a_token_that_is_not_a_token_is_422_and_sends_nothing(monkeypatch, open_request, token):
    sent = _configured(monkeypatch)
    with TestClient(main.app) as client:
        resp = _send(client, {"guardian_email": "stranger@example.org", "token": token})
    assert resp.status_code == 422
    assert sent == [] and open_request == []


def test_someone_elses_or_a_closed_request_is_404(monkeypatch):
    sent = _configured(monkeypatch)
    with TestClient(main.app) as client:
        other_user = _send(client, {"guardian_email": "parent@example.com", "token": TOKEN}, sub="user-999")
        unknown = _send(client, {"guardian_email": "parent@example.com", "token": "cd" * 32})
    assert other_user.status_code == 404 and unknown.status_code == 404
    assert sent == []


def test_the_email_goes_to_the_requests_own_guardian_not_the_body(monkeypatch):
    sent = _configured(monkeypatch)
    with TestClient(main.app) as client:
        resp = _send(client, {"guardian_email": "stranger@example.org", "token": TOKEN})
    assert resp.status_code == 200
    assert [to for to, _ in sent] == ["parent@example.com"]


def test_an_account_gets_five_sends_a_day(monkeypatch):
    sent = _configured(monkeypatch)
    with TestClient(main.app) as client:
        statuses = [_send(client, {"token": TOKEN}).status_code for _ in range(6)]
    assert statuses == [200] * 5 + [429]
    assert len(sent) == 5


def test_the_daily_sends_come_back_after_24_hours():
    main._guardian_email_sends.clear()
    for _ in range(5):
        assert main._take_guardian_email_slot("u", now=1000.0)
    assert not main._take_guardian_email_slot("u", now=1000.0 + 3600)
    assert main._take_guardian_email_slot("u", now=1000.0 + 86401)


class _Query:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def __getattr__(self, name):
        def _chain(*args):
            self.calls.append((name, args))
            return self
        return _chain

    def execute(self):
        return type("R", (), {"data": self.rows})()


def test_the_lookup_matches_the_token_hash_the_rpc_stores_and_the_callers_open_row():
    import asyncio
    import hashlib

    query = _Query([{"guardian_email": "parent@example.com"}])
    db = type("Db", (), {"table": lambda self, name: (query.calls.append(("table", (name,))), query)[1]})()
    real = _REAL_LOOKUP
    assert asyncio.run(real(db, "user-123", TOKEN)) == "parent@example.com"
    assert ("table", ("guardian_consents",)) in query.calls
    assert ("eq", ("token_hash", hashlib.sha256(TOKEN.encode()).hexdigest())) in query.calls
    assert ("eq", ("child_user_id", "user-123")) in query.calls
    assert ("is_", ("granted_at", "null")) in query.calls and ("is_", ("revoked_at", "null")) in query.calls
