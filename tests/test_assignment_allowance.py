"""Homework allowance for Learn answers (lib/assignment_allowance.py, teacher dashboard Stage 3).

A Learn answer for homework the RPC accepts charges assignment_learn_answer with an
'asg:<id>:' key; everything else — no header, guest, bad id, RPC error, a "no" —
falls back to learn_answer. A failed lookup never makes an answer free.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
from lib import assignment_allowance as aa

ASG = "6f1c1d0e-3b0a-4c39-9d43-6a6f3a0e2b11"


class _Rpc:
    def __init__(self, data=None, exc=None):
        self.data, self.exc = data, exc

    def execute(self):
        if self.exc:
            raise self.exc
        return type("R", (), {"data": self.data})()


class _Db:
    def __init__(self, data=None, exc=None):
        self.data, self.exc, self.calls = data, exc, []

    def rpc(self, name, params):
        self.calls.append((name, params))
        return _Rpc(self.data, self.exc)


def _run(coro):
    return asyncio.run(coro)


def test_parse_assignment_id_accepts_only_uuids():
    assert aa.parse_assignment_id(ASG.upper()) == ASG
    assert aa.parse_assignment_id(" " + ASG + " ") == ASG
    for bad in (None, "", "abc", "1; drop table", 42):
        assert aa.parse_assignment_id(bad) is None


def test_accepted_homework_gets_a_prefixed_key_and_asks_the_rpc_with_it():
    db = _Db({"ok": True, "feature": "assignment_learn_answer", "used": 0, "cap": 5})
    key = _run(aa.homework_learn_key(db, "user-1", ASG, "k1"))
    assert key == f"asg:{ASG}:k1"
    assert db.calls == [("assignment_allowance", {"p_user_id": "user-1", "p_assignment_id": ASG, "p_exclude_key": key})]


def test_anything_but_a_clear_yes_falls_back_to_the_normal_limit():
    assert _run(aa.homework_learn_key(_Db({"ok": False, "reason": "used_up"}), "user-1", ASG, "k")) is None
    assert _run(aa.homework_learn_key(_Db({"ok": True, "feature": "assignment_exam"}), "user-1", ASG, "k")) is None
    assert _run(aa.homework_learn_key(_Db(exc=RuntimeError("down")), "user-1", ASG, "k")) is None
    assert _run(aa.homework_learn_key(_Db({"ok": True, "feature": "assignment_learn_answer"}), "user-1", None, "k")) is None
    assert _run(aa.homework_learn_key(None, "user-1", ASG, "k")) is None
    guest_db = _Db({"ok": True, "feature": "assignment_learn_answer"})
    assert _run(aa.homework_learn_key(guest_db, "guest:1.2.3.4", ASG, "k")) is None
    assert guest_db.calls == []


def _capture_charges(monkeypatch, db):
    charges = []

    async def fake_consume(_db, user_id, feature, key):
        charges.append((feature, key))
        return {"granted": True}

    monkeypatch.setattr(main, "consume_ai_quota_or_503", fake_consume)
    monkeypatch.setattr(main, "get_supabase", lambda: db)
    return charges


def test_learn_homework_charges_assignment_learn_answer(monkeypatch):
    charges = _capture_charges(monkeypatch, _Db({"ok": True, "feature": "assignment_learn_answer"}))
    charged = _run(main._consume_feedback_quota("user-1", "k1", learn=True, assignment_id=ASG))
    assert charged == ("assignment_learn_answer", f"asg:{ASG}:k1")
    assert charges == [charged]


def test_learn_without_homework_still_charges_learn_answer(monkeypatch):
    charges = _capture_charges(monkeypatch, _Db({"ok": False, "reason": "not_assigned"}))
    assert _run(main._consume_feedback_quota("user-1", "k1", learn=True, assignment_id=ASG)) == ("learn_answer", "k1")
    assert _run(main._consume_feedback_quota("user-1", "k2", learn=True)) == ("learn_answer", "k2")
    assert charges == [("learn_answer", "k1"), ("learn_answer", "k2")]


def test_non_learn_feedback_ignores_the_header(monkeypatch):
    db = _Db({"ok": True, "feature": "assignment_learn_answer"})
    charges = _capture_charges(monkeypatch, db)
    assert _run(main._consume_feedback_quota("user-1", "k", learn=False, assignment_id=ASG)) == ("feedback", "k")
    assert charges == [("feedback", "k")]
    assert db.calls == []
