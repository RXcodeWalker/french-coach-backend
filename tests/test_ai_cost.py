"""AI cost metering (lib/ai_cost.py, migration 20261122090000).

No SDK is imported: the wrappers are exercised with fakes, so this runs without
groq / google-generativeai installed.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from lib import ai_cost
from lib.ai_quota import consume_ai_quota_or_503


@pytest.fixture
def rows(monkeypatch):
    """Capture emitted rows instead of queueing them for a DB writer."""
    captured: list[dict] = []
    monkeypatch.setattr(ai_cost, "_get_db", lambda: object())
    monkeypatch.setattr(ai_cost, "_ensure_worker", lambda: None)
    monkeypatch.setattr(ai_cost._q, "put_nowait", captured.append)
    monkeypatch.delenv("AI_COST_TRACKING", raising=False)
    ai_cost.bind_usage(None, ai_cost.UNATTRIBUTED)
    return captured


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── usage extraction ─────────────────────────────────────────────────────────

def test_groq_tokens_and_missing_usage():
    assert ai_cost.groq_chat_tokens(SimpleNamespace(prompt_tokens=100, completion_tokens=40)) == (100, 40)
    assert ai_cost.groq_chat_tokens({"prompt_tokens": 7, "completion_tokens": 3}) == (7, 3)
    assert ai_cost.groq_chat_tokens(None) is None
    assert ai_cost.groq_chat_tokens(SimpleNamespace()) is None


def test_gemini_thinking_tokens_are_billed_as_output():
    u = SimpleNamespace(prompt_token_count=200, candidates_token_count=50, thoughts_token_count=30)
    assert ai_cost.gemini_tokens(u) == (200, 80)
    assert ai_cost.gemini_tokens(SimpleNamespace(prompt_token_count=5, candidates_token_count=1)) == (5, 1)
    assert ai_cost.gemini_tokens(None) is None


def test_whisper_seconds_prefers_duration_then_last_segment():
    assert ai_cost.whisper_seconds(SimpleNamespace(duration=12.5)) == 12.5
    assert ai_cost.whisper_seconds(SimpleNamespace(duration=None, segments=[{"end": 3.0}, {"end": 9.25}])) == 9.25
    assert ai_cost.whisper_seconds(SimpleNamespace(duration=None, segments=[])) is None


# ── attribution ──────────────────────────────────────────────────────────────

def test_unbound_call_is_unattributed(rows):
    ai_cost._emit(provider="groq", model="m", kind="chat", input_tokens=1, output_tokens=1)
    assert rows[0]["user_id"] is None and rows[0]["is_guest"] is False
    assert rows[0]["feature"] == "unattributed"


def test_charging_quota_binds_the_user_and_feature(rows):
    # A guest never reaches Supabase, so no db is needed to reach the bind.
    monkey_guest = "guest:1.2.3.4"
    ai_cost.bind_usage(monkey_guest, "feedback")
    ai_cost._emit(provider="groq", model="m", kind="chat")
    assert rows[-1]["user_id"] is None and rows[-1]["is_guest"] is True and rows[-1]["feature"] == "feedback"

    ai_cost.bind_usage("11111111-1111-1111-1111-111111111111", "learn_answer")
    ai_cost._emit(provider="groq", model="m", kind="chat")
    assert rows[-1]["user_id"] == "11111111-1111-1111-1111-111111111111"
    assert rows[-1]["is_guest"] is False and rows[-1]["feature"] == "learn_answer"


def test_consume_ai_quota_sets_the_binding_even_when_denied(rows):
    class Db:
        def rpc(self, *_a, **_k):
            raise RuntimeError("down")

    async def go():
        with pytest.raises(Exception):
            await consume_ai_quota_or_503(Db(), "22222222-2222-2222-2222-222222222222", "score", "k")
        # Same task context as the handler, as in production.
        ai_cost._emit(provider="groq", model="m", kind="chat")

    run(go())
    assert rows[-1]["user_id"] == "22222222-2222-2222-2222-222222222222"
    assert rows[-1]["feature"] == "score"


def test_binding_survives_to_thread(rows):
    """Gemini calls run in asyncio.to_thread; the ContextVar must follow."""
    async def go():
        ai_cost.bind_usage("33333333-3333-3333-3333-333333333333", "drill")
        await asyncio.to_thread(lambda: ai_cost._emit(provider="gemini", model="g", kind="chat"))

    run(go())
    assert rows[-1]["user_id"] == "33333333-3333-3333-3333-333333333333"


# ── wrappers ─────────────────────────────────────────────────────────────────

def test_groq_chat_wrapper_records_usage_and_returns_the_response(rows):
    resp = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=900, completion_tokens=300), model="x")

    async def orig(self, **kw):
        return resp

    out = run(ai_cost._wrap_groq_chat(orig)(None, model="openai/gpt-oss-120b", messages=[]))
    assert out is resp
    assert rows == [{
        "user_id": None, "is_guest": False, "feature": "unattributed", "provider": "groq",
        "model": "openai/gpt-oss-120b", "kind": "chat", "input_tokens": 900, "output_tokens": 300,
        "audio_seconds": 0.0, "measured": True,
    }]


def test_groq_response_without_usage_is_recorded_unmeasured(rows):
    async def orig(self, **kw):
        return SimpleNamespace(usage=None)

    run(ai_cost._wrap_groq_chat(orig)(None, model="m"))
    assert rows[0]["measured"] is False and rows[0]["input_tokens"] == 0


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks
        self.closed = False

    def __aiter__(self):
        async def gen():
            for c in self._chunks:
                yield c
        return gen()

    async def close(self):
        self.closed = True


def test_groq_stream_is_passed_through_and_usage_read_from_the_last_chunk(rows):
    chunks = [
        SimpleNamespace(choices=[1], usage=None, x_groq=None),
        SimpleNamespace(choices=[], usage=None, x_groq=SimpleNamespace(usage=SimpleNamespace(prompt_tokens=50, completion_tokens=20))),
    ]

    async def orig(self, **kw):
        return _FakeStream(chunks)

    async def go():
        stream = await ai_cost._wrap_groq_chat(orig)(None, model="m", stream=True)
        got = [c async for c in stream]
        await stream.close()  # delegated to the real stream
        return got, stream

    got, stream = run(go())
    assert got == chunks and stream._inner.closed
    assert len(rows) == 1 and (rows[0]["input_tokens"], rows[0]["output_tokens"]) == (50, 20)


def test_stream_that_raises_still_records_once(rows):
    class Boom(_FakeStream):
        def __aiter__(self):
            async def gen():
                yield SimpleNamespace(choices=[], usage=None, x_groq=None)
                raise RuntimeError("provider dropped")
            return gen()

    async def orig(self, **kw):
        return Boom([])

    async def go():
        stream = await ai_cost._wrap_groq_chat(orig)(None, model="m", stream=True)
        with pytest.raises(RuntimeError):
            async for _ in stream:
                pass

    run(go())
    assert len(rows) == 1 and rows[0]["measured"] is False


def test_whisper_wrapper_records_audio_seconds(rows):
    async def orig(self, **kw):
        return SimpleNamespace(duration=7.4, text="x")

    run(ai_cost._wrap_groq_audio(orig)(None, model="whisper-large-v3"))
    assert rows[0]["kind"] == "audio" and rows[0]["audio_seconds"] == 7.4 and rows[0]["measured"] is True


def test_gemini_wrapper_strips_models_prefix(rows):
    resp = SimpleNamespace(usage_metadata=SimpleNamespace(prompt_token_count=10, candidates_token_count=4))
    fake_model = SimpleNamespace(model_name="models/gemini-3.6-flash")

    out = ai_cost._wrap_gemini(lambda self, *a, **k: resp)(fake_model, "hi")
    assert out is resp
    assert rows[0]["model"] == "gemini-3.6-flash" and rows[0]["provider"] == "gemini"
    assert (rows[0]["input_tokens"], rows[0]["output_tokens"]) == (10, 4)


def test_a_failing_provider_call_records_nothing_and_still_raises(rows):
    async def orig(self, **kw):
        raise RuntimeError("429")

    with pytest.raises(RuntimeError):
        run(ai_cost._wrap_groq_chat(orig)(None, model="m"))
    assert rows == []


def test_hooks_are_idempotent():
    class Owner:
        def create(self):
            return 1

    assert ai_cost._patch(Owner, "create", ai_cost._wrap_gemini) is True
    assert ai_cost._patch(Owner, "create", ai_cost._wrap_gemini) is False


def test_switch_off_records_nothing(rows, monkeypatch):
    monkeypatch.setenv("AI_COST_TRACKING", "0")
    ai_cost._emit(provider="groq", model="m", kind="chat")
    assert rows == [] and ai_cost.install_usage_hooks() == []


def test_a_full_queue_never_raises(monkeypatch):
    import queue

    def full(_row):
        raise queue.Full

    monkeypatch.setattr(ai_cost, "_get_db", lambda: object())
    monkeypatch.setattr(ai_cost, "_ensure_worker", lambda: None)
    monkeypatch.setattr(ai_cost._q, "put_nowait", full)
    ai_cost._emit(provider="groq", model="m", kind="chat")  # must not raise


# ── the migration agrees with the code ───────────────────────────────────────

MIGRATION = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "supabase", "migrations", "20261122090000_ai_cost_tracking.sql",
)


def test_migration_covers_every_provider_kind_and_feature_the_code_emits():
    sql = open(MIGRATION).read()
    assert "CHECK (provider IN ('groq', 'gemini'))" in sql
    assert "CHECK (kind IN ('chat', 'audio'))" in sql
    # The default Whisper and chat model IDs carry a (provisional) price.
    from lib.model_config import GROQ_MODEL, GROQ_WHISPER_MODEL

    assert re.search(rf"\('groq', '{re.escape(GROQ_MODEL)}',\s*'chat'", sql)
    assert re.search(rf"\('groq', '{re.escape(GROQ_WHISPER_MODEL)}',\s*'audio'", sql)
    # Seeded prices must not claim to be verified.
    assert "true," not in sql.split("INSERT INTO public.ai_model_pricing")[1].split("ON CONFLICT")[0]
