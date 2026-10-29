"""GET /api/pronunciation/allowance — the Pronunciation Lab's read-only counter."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
from lib import guest


class _Res:
    def __init__(self, data=None, count=None):
        self.data, self.count = data, count


class _Query:
    def __init__(self, table, calls):
        self.table, self.calls, self.filters = table, calls, {}

    def select(self, *a, **kw):
        return self

    def eq(self, key, value):
        self.filters[key] = value
        return self

    def execute(self):
        self.calls.append((self.table, dict(self.filters)))
        if self.table == "ai_quota_limits":
            return _Res(data=[{"daily_limit": 30}])
        return _Res(count=12)


class _Db:
    def __init__(self):
        self.calls = []

    def table(self, name):
        return _Query(name, self.calls)


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(main, "verify_jwt", lambda authorization: "user-1")
    if main._limiter is not None:
        monkeypatch.setattr(main._limiter, "enabled", False)
    from fastapi.testclient import TestClient

    with TestClient(main.app) as c:
        yield c


def test_the_allowance_reads_today_without_charging(client, monkeypatch):
    db = _Db()
    monkeypatch.setattr(main, "get_supabase", lambda: db)
    data = client.get("/api/pronunciation/allowance", headers={"Authorization": "Bearer x"}).json()
    assert {k: data[k] for k in ("limit", "used", "remaining")} == {"limit": 30, "used": 12, "remaining": 18}
    assert data["resetsAt"].endswith("+00:00")
    grants = [f for t, f in db.calls if t == "ai_usage_grants"][0]
    assert grants["user_id"] == "user-1" and grants["feature"] == "pronunciation_lab"
    assert grants["grant_date"] == main.datetime.now(main.timezone.utc).date().isoformat()


def test_the_allowance_is_503_without_quota_state(client, monkeypatch):
    monkeypatch.setattr(main, "get_supabase", lambda: None)
    assert client.get("/api/pronunciation/allowance", headers={"Authorization": "Bearer x"}).status_code == 503


def test_a_guest_reads_the_in_process_backstop(client, monkeypatch):
    monkeypatch.setenv("GUEST_AI_ENABLED", "1")
    data = client.get("/api/pronunciation/allowance").json()
    assert data["limit"] == guest._limit_for("pronunciation_lab") and data["used"] == 0
