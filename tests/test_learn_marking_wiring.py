"""Learn marking ladder, Batch 1b: the backend wiring around lib/learn_ladder.py.

Frontend spec: docs/systems/learn-marking-levels.md (cited `learn-marking §N`). Covers the `marking`
request field (only "ladder-v1", anything else 422), the LEARN_LADDER_ENABLED kill switch, the cache
key, and the stream path writing the cache.

Run: pytest tests/test_learn_marking_wiring.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.pop("AZURE_SPEECH_KEY", None)
os.environ.pop("AZURE_SPEECH_REGION", None)

import main  # noqa: E402
from fastapi import HTTPException  # noqa: E402


class _JsonRequest:
    def __init__(self, payload):
        self.headers = {"content-type": "application/json"}
        self._payload = payload

    async def json(self):
        return self._payload


class _MultipartRequest:
    def __init__(self, data):
        self.headers = {"content-type": "multipart/form-data; boundary=x"}
        self._form = {"question": "Décris ta famille.", "data": json.dumps(data)}

    async def form(self):
        return self._form


def _parse(request):
    return asyncio.run(main._parse_feedback_request(request))


@pytest.fixture
def e2e(monkeypatch):
    """A signed-in caller with the quota and the rate limit out of the way, and an empty cache."""
    monkeypatch.setattr(main, "verify_jwt", lambda authorization: "user-1")

    async def _granted(*a, **kw):
        return {"granted": True}

    monkeypatch.setattr(main, "consume_ai_quota_or_503", _granted)
    if main._limiter is not None:
        monkeypatch.setattr(main._limiter, "enabled", False)
    monkeypatch.setattr(main, "_feedback_cache", main.BoundedTTLCache(50, 300.0))
    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        yield client


# ── The `marking` request field (learn-marking §8.1) ────────────────────────


def test_marking_absent_is_v6_and_ladder_v1_is_accepted():
    assert main._parse_marking(None) is None
    assert main._parse_marking("ladder-v1") == "ladder-v1"


@pytest.mark.parametrize("value", ["ladder-v2", "", "LADDER-V1", "v6", 1, True, {"scheme": "ladder-v1"}, ["ladder-v1"]])
def test_any_other_marking_is_a_422(value):
    with pytest.raises(HTTPException) as exc:
        main._parse_marking(value)
    assert exc.value.status_code == 422


def test_parse_feedback_request_reads_marking_from_json_and_from_multipart_data():
    assert _parse(_JsonRequest({"question": "Q", "transcript": "T", "marking": "ladder-v1"}))[-1] == "ladder-v1"
    assert _parse(_JsonRequest({"question": "Q", "transcript": "T"}))[-1] is None
    assert _parse(_MultipartRequest({"transcript": "T", "marking": "ladder-v1"}))[-1] == "ladder-v1"
    assert _parse(_MultipartRequest({"transcript": "T"}))[-1] is None


def test_parse_feedback_request_rejects_an_unknown_marking_in_json_and_in_multipart_data():
    for request in (_JsonRequest({"marking": "strict"}), _MultipartRequest({"marking": "strict"})):
        with pytest.raises(HTTPException) as exc:
            _parse(request)
        assert exc.value.status_code == 422


@pytest.mark.parametrize("path", ["/api/feedback/v3", "/api/feedback/stream"])
def test_both_endpoints_reject_an_unknown_marking_even_with_the_kill_switch_off(e2e, monkeypatch, path):
    monkeypatch.setenv("LEARN_LADDER_ENABLED", "0")
    resp = e2e.post(
        path,
        json={"question": "Q?", "transcript": "Bonjour.", "marking": "ladder-v2"},
        headers={"Authorization": "Bearer whatever"},
    )
    assert resp.status_code == 422


def test_the_level_the_learner_picked_has_no_field_on_the_request():
    # learn-marking §1.1: the level never reaches the backend. The only marking input is the scheme.
    fields = set(main.FeedbackRequest.model_fields)
    assert "marking" in fields
    assert not fields & {"markingLevel", "marking_level", "strictness", "level"}


# ── The kill switch and which requests use the ladder ───────────────────────


def test_the_ladder_is_used_only_when_asked_for_enabled_and_without_audio(monkeypatch):
    monkeypatch.delenv("LEARN_LADDER_ENABLED", raising=False)
    assert main._effective_marking("ladder-v1", has_audio=False) == "ladder-v1"
    assert main._effective_marking(None, has_audio=False) is None
    # A request with audio goes to the multimodal prompt, which the ladder leaves untouched.
    assert main._effective_marking("ladder-v1", has_audio=True) is None


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF", " 0 "])
def test_the_kill_switch_ignores_the_field(monkeypatch, value):
    monkeypatch.setenv("LEARN_LADDER_ENABLED", value)
    assert main._effective_marking("ladder-v1", has_audio=False) is None


@pytest.mark.parametrize("value", ["1", "true", "on", ""])
def test_the_ladder_is_on_unless_switched_off(monkeypatch, value):
    monkeypatch.setenv("LEARN_LADDER_ENABLED", value)
    assert main._effective_marking("ladder-v1", has_audio=False) == "ladder-v1"


# ── The cache key (learn-marking §9.2) ──────────────────────────────────────


def test_cache_key_separates_schemes_question_ids_and_demand_versions():
    base = main._feedback_cache_key("t", "Décris ta famille.", None, "standard")
    assert base == main._feedback_cache_key("t", "Décris ta famille.", None, "standard", marking=None)
    variants = {
        main._feedback_cache_key("t", "Décris ta famille.", None, "standard", marking="ladder-v1"),
        main._feedback_cache_key("t", "Décris ta famille.", None, "standard", learn_question_id="fam_01"),
        main._feedback_cache_key("t", "Décris ta famille.", None, "standard", learn_question_id="fam_02"),
        main._feedback_cache_key("t", "Décris ta famille.", None, "standard", learn_question_id="fam_01", demands_version="a"),
        main._feedback_cache_key("t", "Décris ta famille.", None, "standard", learn_question_id="fam_01", demands_version="b"),
    }
    assert base not in variants
    assert len(variants) == 5


def test_cache_key_carries_the_prompt_version_of_its_scheme(monkeypatch):
    v6 = main._feedback_cache_key("t", "q")
    ladder = main._feedback_cache_key("t", "q", marking="ladder-v1")
    monkeypatch.setattr(main, "LEARN_PROMPT_VERSION", "learn-prompt-v6-test")
    assert main._feedback_cache_key("t", "q") != v6
    assert main._feedback_cache_key("t", "q", marking="ladder-v1") == ladder
    monkeypatch.setattr(main, "LEARN_LADDER_PROMPT_VERSION", "learn-prompt-v8-test")
    assert main._feedback_cache_key("t", "q", marking="ladder-v1") != ladder


def test_request_cache_key_reads_the_request(monkeypatch):
    monkeypatch.delenv("LEARN_LADDER_ENABLED", raising=False)
    req = main.FeedbackRequest(
        question="Q", transcript="T", depth="deep", question_id="fam_01", demands_version="v", marking="ladder-v1",
    )
    assert main._request_cache_key(req, "Q") == main._feedback_cache_key(
        "T", "Q", None, "deep", marking="ladder-v1", learn_question_id="fam_01", demands_version="v",
    )


# ── The stream path writes the cache ────────────────────────────────────────


def _stream_events(resp):
    return [json.loads(line) for line in resp.text.splitlines() if line.strip()]


def test_a_streamed_reply_is_cached_and_served_to_the_next_identical_request(e2e, monkeypatch):
    calls = []

    async def fake_stream(prompt, depth, on_section, **kw):
        calls.append(prompt)
        return {
            "fluency": 6.0,
            "scores": {"comm": 6, "know": 6, "acc": 6},
            "cefrLevel": "A2",
            "best_moment": "",
            "modelUsed": "groq/test",
        }

    monkeypatch.setattr(main, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(main, "get_groq", lambda: object())
    monkeypatch.setattr(main, "_stream_groq", fake_stream)
    body = {"question": "Décris ta famille.", "transcript": "Ma famille est grande."}

    first = _stream_events(e2e.post("/api/feedback/stream", json=body, headers={"Authorization": "Bearer x"}))
    second = _stream_events(e2e.post("/api/feedback/stream", json=body, headers={"Authorization": "Bearer x"}))

    assert len(calls) == 1, "the second identical request must be served from the cache"
    assert first[-1]["type"] == "complete" and second[-1]["type"] == "complete"
    assert first[-1]["data"]["providerStatus"] == "primary"
    assert second[-1]["data"]["scores"] == first[-1]["data"]["scores"]


# ── Slice 3: the ladder reply, end to end (learn-marking §6-§8) ─────────────

HOL_01_QUESTION = "Où es-tu allé(e) pendant les dernières vacances ?"
HOL_01_TRANSCRIPT = (
    "Pendant les vacances, je suis allé en France avec ma famille et nous avons visité Paris. C'était génial."
)


def _raw_ladder_reply(**overrides):
    """A ladder-scheme model reply for hol_01 (T L F R A): verdicts only, plus v6's teaching fields."""
    reply = {
        "levelCheck": {
            "T": {"verdicts": ["y", "y", "n", "n"], "quote": "je suis allé en France … nous avons visité Paris",
                  "next": "Nous avons visité Paris parce que mon père adore les musées."},
            "L": {"verdicts": ["y", "n", "n", "n"], "quote": "avec ma famille et nous avons visité", "next": ""},
            "F": {"verdicts": [None, "y", "n", "n"], "quote": "je suis allé en France",
                  "next": "J'étais très content parce que c'était mon premier voyage."},
            "R": {"verdicts": ["y", "y", "n", "n"], "quote": "nous avons visité Paris", "next": ""},
            "A": {"verdicts": ["y", "y", "y", "y"], "quote": "je suis allé en France", "next": ""},
        },
        "demandCheck": {"met": False, "quote": ""},
        "best_moment": "Your « je suis allé en France » uses the right auxiliary for aller.",
        "grammar": {"critical": [], "polish": []},
        "modelUsed": "groq/test",
    }
    reply.update(overrides)
    return reply


def _ladder_body(**overrides):
    body = {
        "question": HOL_01_QUESTION,
        "transcript": HOL_01_TRANSCRIPT,
        "questionId": "hol_01",
        "demandsVersion": main.LEARN_DEMANDS_VERSION,
        "enginePreference": "groq",
        "marking": "ladder-v1",
    }
    body.update(overrides)
    return body


def _expected_ladder():
    demands = main.resolve_learn_demands("hol_01", main.LEARN_DEMANDS_VERSION)
    return main._learn_ladder.score_ladders(_raw_ladder_reply()["levelCheck"], demands, HOL_01_TRANSCRIPT)


def _assert_scored_ladder_reply(data):
    expected = _expected_ladder()
    exam = expected.level_check["subScoresByLevel"]["exam"]
    assert data["levelCheck"] == expected.level_check
    assert data["levelCheck"]["applies"] == ["T", "L", "F", "R", "A"]
    assert data["scores"] == {"comm": exam["comm"], "know": exam["know"], "acc": exam["acc"]}
    assert data["fluency"] == exam["fluency"]
    assert data["cefrLevel"] == expected.cefr_level
    assert data["answered_the_question"] is True
    assert data["promptVersion"] == "learn-prompt-v8"
    assert data["demandCheck"] == {"met": False, "quote": ""}
    assert data.get("providerStatus") != "malformed_response"


@pytest.fixture
def ladder_on(monkeypatch):
    monkeypatch.delenv("LEARN_LADDER_ENABLED", raising=False)
    monkeypatch.setattr(main, "_log_coaching_quality", lambda *a, **kw: None)


def _stub_providers(monkeypatch, *, groq_reply, gemini_reply=None):
    calls = []

    async def fake_groq(prompt, depth="standard", **kw):
        calls.append(("groq", kw))
        return groq_reply() if callable(groq_reply) else groq_reply

    async def fake_gemini(prompt, **kw):
        calls.append(("gemini", kw))
        if gemini_reply is None:
            raise RuntimeError("gemini down")
        return gemini_reply

    monkeypatch.setattr(main, "_call_groq", fake_groq)
    monkeypatch.setattr(main, "_call_gemini", fake_gemini)
    return calls


def test_v3_returns_the_computed_scores_and_the_level_check(e2e, ladder_on, monkeypatch):
    # The model's own scores (if it wrote any) never survive: code computes them.
    calls = _stub_providers(monkeypatch, groq_reply=lambda: _raw_ladder_reply(
        scores={"comm": 1, "know": 1, "acc": 1}, fluency=1.0, cefrLevel="B2", demands_met=["x"],
    ))
    resp = e2e.post("/api/feedback/v3", json=_ladder_body(), headers={"Authorization": "Bearer x"})
    assert resp.status_code == 200
    data = resp.json()
    _assert_scored_ladder_reply(data)
    assert "demands_met" not in data
    assert calls == [("groq", {"ladder": True})]


def test_a_bad_level_check_falls_back_to_the_next_provider(e2e, ladder_on, monkeypatch):
    bad = _raw_ladder_reply()
    del bad["levelCheck"]["F"]  # an applicable ladder is missing
    calls = _stub_providers(monkeypatch, groq_reply=bad, gemini_reply=_raw_ladder_reply(modelUsed="gemini/test"))
    data = e2e.post("/api/feedback/v3", json=_ladder_body(), headers={"Authorization": "Bearer x"}).json()
    assert calls == [("groq", {"ladder": True}), ("gemini", {"ladder": True})]
    assert data["providerStatus"] == "fallback"
    assert data["providerErrors"][0]["type"] == "malformed_response"
    _assert_scored_ladder_reply(data)


def test_no_usable_level_check_from_any_provider_is_a_502(e2e, ladder_on, monkeypatch):
    _stub_providers(monkeypatch, groq_reply={"scores": {"comm": 7}}, gemini_reply={"levelCheck": "nope"})
    resp = e2e.post("/api/feedback/v3", json=_ladder_body(), headers={"Authorization": "Bearer x"})
    assert resp.status_code == 502


def test_with_the_kill_switch_off_the_same_request_is_marked_by_v6(e2e, monkeypatch):
    monkeypatch.setenv("LEARN_LADDER_ENABLED", "0")
    monkeypatch.setattr(main, "_log_coaching_quality", lambda *a, **kw: None)
    v6_reply = {"scores": {"comm": 6, "know": 6, "acc": 6}, "fluency": 6.0, "cefrLevel": "A2", "modelUsed": "groq/test"}
    calls = _stub_providers(monkeypatch, groq_reply=v6_reply)
    data = e2e.post("/api/feedback/v3", json=_ladder_body(), headers={"Authorization": "Bearer x"}).json()
    assert calls == [("groq", {})]
    assert data["scores"] == {"comm": 6, "know": 6, "acc": 6}
    assert "levelCheck" not in data and "promptVersion" not in data


def test_the_reply_does_not_depend_on_a_tier_the_request_carries(e2e, ladder_on, monkeypatch):
    _stub_providers(monkeypatch, groq_reply=_raw_ladder_reply)
    replies = []
    for tier in (None, "beginner", "expert"):
        body = _ladder_body(**({"difficultyContext": {"tier": tier}} if tier else {}))
        data = e2e.post("/api/feedback/v3", json=body, headers={"Authorization": "Bearer x"}).json()
        replies.append((data["scores"], data["fluency"], data["cefrLevel"], data["levelCheck"]))
    assert replies[0] == replies[1] == replies[2]


def test_the_stream_scores_the_ladder_before_complete_and_hides_the_models_snapshot(e2e, ladder_on, monkeypatch):
    calls = []

    async def fake_stream(prompt, depth, on_section, **kw):
        calls.append(kw)
        await on_section("snapshot", {"scores": {"comm": 1, "know": 1, "acc": 1}, "fluency": 1})
        await on_section("strongest_moment", {"best_moment": _raw_ladder_reply()["best_moment"]})
        return _raw_ladder_reply()

    monkeypatch.setattr(main, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(main, "get_groq", lambda: object())
    monkeypatch.setattr(main, "_stream_groq", fake_stream)

    first = _stream_events(e2e.post("/api/feedback/stream", json=_ladder_body(), headers={"Authorization": "Bearer x"}))
    second = _stream_events(e2e.post("/api/feedback/stream", json=_ladder_body(), headers={"Authorization": "Bearer x"}))

    assert calls == [{"ladder": True}], "the second identical request must be a cache hit"
    assert [e["type"] for e in first] == ["status", "transcript", "status", "strongest_moment", "complete"]
    _assert_scored_ladder_reply(first[-1]["data"])
    # A cache hit holds the raw verdicts and scores them again, to the same result.
    assert second[-1]["type"] == "complete"
    _assert_scored_ladder_reply(second[-1]["data"])


def test_a_bad_level_check_ends_the_stream_with_an_error_and_is_not_cached(e2e, ladder_on, monkeypatch):
    calls = []

    async def fake_stream(prompt, depth, on_section, **kw):
        calls.append(kw)
        return {"scores": {"comm": 7, "know": 7, "acc": 7}, "fluency": 7.0}

    monkeypatch.setattr(main, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(main, "get_groq", lambda: object())
    monkeypatch.setattr(main, "_stream_groq", fake_stream)

    for _ in range(2):
        events = _stream_events(e2e.post("/api/feedback/stream", json=_ladder_body(), headers={"Authorization": "Bearer x"}))
        assert events[-1]["type"] == "error"
        assert "complete" not in [e["type"] for e in events]
    assert len(calls) == 2


def test_apply_learn_ladder_leaves_a_v6_reply_untouched():
    req = main.FeedbackRequest(question="Q", transcript="T")
    fb = {"scores": {"comm": 6}, "levelCheck": {"T": "anything"}}
    assert main.apply_learn_ladder(dict(fb), req) == fb


def test_apply_learn_ladder_scores_unresolved_demands_on_four_ladders(ladder_on):
    req = main.FeedbackRequest(
        question=HOL_01_QUESTION, transcript=HOL_01_TRANSCRIPT, question_id="hol_01",
        demands_version="stale", marking="ladder-v1",
    )
    raw = _raw_ladder_reply()
    del raw["levelCheck"]["F"]
    fb = main.apply_learn_ladder(raw, req)
    assert fb["levelCheck"]["applies"] == ["T", "L", "R", "A"]


@pytest.mark.parametrize(
    "raw, expected",
    [
        ({"met": True, "quote": " mon dernier voyage "}, {"met": True, "quote": "mon dernier voyage"}),
        ({"met": False}, {"met": False, "quote": ""}),
        ({"met": None, "quote": ""}, {"met": None, "quote": ""}),
        ({"met": 1, "quote": "x"}, None),
        ({"met": "yes"}, None),
        ({"quote": "x"}, None),
        ("met", None),
        (None, None),
    ],
)
def test_demand_check_is_kept_only_in_its_documented_shape(raw, expected):
    assert main._clean_demand_check(raw) == expected
