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
    monkeypatch.setenv("GUEST_AI_DAILY_LIMIT", "2")
    g = "guest:1.2.3.4"
    run = lambda k, f="feedback": asyncio.run(consume_ai_quota_or_503(None, g, f, k))
    assert run("a")["granted"] and run("b")["granted"]
    assert run("a")["replayed"] is True
    with pytest.raises(QuotaDenied) as e:
        run("c")
    assert e.value.status_code == 429
    assert run("c", "transcribe")["granted"]  # separate feature bucket


def test_guest_never_touches_supabase():
    # db=None would 503 for a real user; a guest must not care.
    assert asyncio.run(consume_ai_quota_or_503(None, "guest:9.9.9.9", "feedback", "k"))["granted"]
