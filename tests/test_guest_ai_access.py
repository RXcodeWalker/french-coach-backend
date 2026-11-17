"""Guest (header-less) access to the AI routes — lib/guest.py."""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import main
from lib import guest
from lib.ai_quota import QuotaDenied, consume_ai_quota_or_503


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setenv("GUEST_AI_ENABLED", "1")
    guest._grants.clear()


def test_interpret_open_to_guests_when_enabled():
    with TestClient(main.app) as c:
        r = c.post("/api/exam/interpret", json={"transcript": "", "part": "topic1"})
    assert r.status_code == 200


def test_kill_switch_restores_401(monkeypatch):
    monkeypatch.setenv("GUEST_AI_ENABLED", "0")
    with TestClient(main.app) as c:
        r = c.post("/api/exam/interpret", json={"transcript": "", "part": "topic1"})
    assert r.status_code == 401


def test_bad_token_is_never_downgraded_to_guest():
    with TestClient(main.app) as c:
        r = c.post(
            "/api/exam/interpret",
            json={"transcript": "", "part": "topic1"},
            headers={"Authorization": "Bearer nope"},
        )
    assert r.status_code in (401, 503)


def test_guest_quota_caps_per_feature_and_replays_are_free(monkeypatch):
    monkeypatch.setenv("GUEST_AI_LIMIT", "2")
    g = "guest:1.2.3.4"
    run = lambda k, f="feedback": asyncio.run(consume_ai_quota_or_503(None, g, f, k))
    assert run("a")["granted"] and run("b")["granted"]
    assert run("a")["replayed"] is True
    with pytest.raises(QuotaDenied) as e:
        run("c")
    assert e.value.status_code == 429
    assert run("c", "transcribe")["granted"]  # separate feature bucket
    monkeypatch.setenv("GUEST_AI_AUDIO_LIMIT", "1")
    assert run("t1", "pronunciation")["granted"]
    with pytest.raises(QuotaDenied):
        run("t2", "pronunciation")


def test_every_transcribe_purpose_gets_the_guest_audio_backstop():
    # A guest's Whisper call (Learn, exam turn, Junior) is capped by
    # GUEST_AI_AUDIO_LIMIT, not the looser text limit.
    assert set(main._TRANSCRIBE_QUOTA_FEATURE.values()) <= guest._AUDIO_FEATURES


def test_guest_never_touches_supabase():
    # db=None would 503 for a real user; a guest must not care.
    assert asyncio.run(consume_ai_quota_or_503(None, "guest:9.9.9.9", "feedback", "k"))["granted"]


# ── Security review 2026-11 F6: the guest is the trusted proxy's hop ─────────


class _Req:
    def __init__(self, xff: str | None = None, peer: str = "10.0.0.9", auth: str | None = None):
        self.headers = {k: v for k, v in (("x-forwarded-for", xff), ("authorization", auth)) if v is not None}
        self.client = type("C", (), {"host": peer})()


@pytest.mark.parametrize(
    ("hops", "xff", "expected"),
    [
        ("1", "6.6.6.6, 203.0.113.7", "203.0.113.7"),          # the caller wrote 6.6.6.6
        ("2", "6.6.6.6, 203.0.113.7, 10.1.1.1", "203.0.113.7"),
        ("2", "203.0.113.7", "10.0.0.9"),                       # fewer entries than proxies: the peer
        ("0", "6.6.6.6, 203.0.113.7", "10.0.0.9"),              # header ignored
        ("1", None, "10.0.0.9"),
        ("nonsense", "6.6.6.6, 203.0.113.7", "203.0.113.7"),    # unreadable: the default, 1
    ],
)
def test_client_ip_uses_the_hop_the_trusted_proxy_appended(monkeypatch, hops, xff, expected):
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", hops)
    assert guest.client_ip(_Req(xff)) == expected


def test_a_new_forwarded_for_value_is_not_a_new_guest(monkeypatch):
    """The audit's proof: three requests whose first X-Forwarded-For hop differs
    were three guests. Here the trusted proxy (one, the default) appends the
    address it saw after whatever the caller wrote, as it does in production;
    the audit's own version sent the header with no proxy in between, which
    only an app that ignores the header entirely could pass."""
    monkeypatch.setenv("GUEST_AI_LIMIT", "1")
    monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)
    seen: list[str] = []

    async def _quota(db, user_id, feature, key):
        seen.append(user_id)
        return guest.consume_guest_quota(user_id, feature, key)

    async def _feedback(req, audio_path=None, audio_mime="audio/webm"):
        return {"scores": {}, "modelUsed": "stub"}

    monkeypatch.setattr(main, "consume_ai_quota_or_503", _quota)
    monkeypatch.setattr(main, "call_ai_feedback", _feedback)
    statuses = []
    with TestClient(main.app) as client:
        for i in range(3):
            r = client.post(
                "/api/feedback",
                json={"question": "Q", "transcript": f"reponse numero {i} de ma famille"},
                headers={"X-Forwarded-For": f"203.0.113.{i}, 198.51.100.1"},
            )
            statuses.append(r.status_code)
    assert len(set(seen)) == 1, seen
    assert statuses.count(200) <= 1, statuses


def test_the_rate_limit_bucket_is_the_verified_user(monkeypatch):
    monkeypatch.setattr(main, "verify_supabase_jwt", lambda authorization: {"sub": "user-9"})
    assert main._rate_limit_key(_Req("6.6.6.6, 203.0.113.7", auth="Bearer good")) == "user:user-9"


def test_an_unverified_token_gets_the_address_bucket(monkeypatch):
    monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)

    def _bad(authorization):
        raise HTTPException(status_code=401, detail="Invalid token")

    monkeypatch.setattr(main, "verify_supabase_jwt", _bad)
    assert main._rate_limit_key(_Req("6.6.6.6, 203.0.113.7", auth="Bearer forged")) == "ip:203.0.113.7"
    assert main._rate_limit_key(_Req("6.6.6.6, 203.0.113.7")) == "ip:203.0.113.7"
