"""Learn marking ladder, Batch 1b: the ladder scheme's prompt and provider wiring.

Frontend spec: docs/systems/learn-marking-levels.md (`learn-marking §N`). Covers SYSTEM_PROMPT_LADDER,
build_user_prompt's LEVEL CHECK rendering, the describe floor (ladder scheme only), and which system
prompt and token budget each provider call uses. The v6 prompt's own pins live in
tests/test_learn_prompt_v6.py (system prompt) and tests/test_learn_demands.py (user prompt); they
must not move, which is what proves every other screen and every old app is unaffected.

Run: pytest tests/test_learn_ladder_prompt.py
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.pop("AZURE_SPEECH_KEY", None)
os.environ.pop("AZURE_SPEECH_REGION", None)

import main  # noqa: E402

# Pinned on purpose: bump LEARN_LADDER_PROMPT_VERSION and update these in the same commit as any
# change to SYSTEM_PROMPT_LADDER or to the ladder branch of build_user_prompt.
LADDER_SYSTEM_PROMPT_HASH = "8805d5a5814b1bfcd912cb1e9c2f33cbb47be9be0933ab03fa0fd5f338e646a0"
LADDER_USER_PROMPT_HASH = "74d9bfb995554cf382e18b351c010ef2d5e09370dd839a5cab6ec99d73f737ed"

TIERS = [None, *({"tier": t} for t in main._TIER_PROMPTS)]


def _req(question_id="hol_01", *, marking="ladder-v1", difficulty_context=None, version=None, **kw):
    return main.FeedbackRequest(
        question=kw.pop("question", "Où es-tu allé(e) pendant les dernières vacances ?"),
        transcript=kw.pop("transcript", "Je suis allé en France avec ma famille."),
        question_id=question_id,
        demands_version=main.LEARN_DEMANDS_VERSION if version is None else version,
        difficulty_context=difficulty_context,
        marking=marking,
        **kw,
    )


# ── Versions and pins ───────────────────────────────────────────────────────


def test_the_ladder_prompt_has_its_own_version_and_v6_keeps_its_own():
    assert main.LEARN_LADDER_PROMPT_VERSION == "learn-prompt-v8"
    assert main.LEARN_PROMPT_VERSION == "learn-prompt-v6"


def test_ladder_system_prompt_snapshot():
    actual = hashlib.sha256(main.SYSTEM_PROMPT_LADDER.encode("utf-8")).hexdigest()
    assert actual == LADDER_SYSTEM_PROMPT_HASH, (
        "SYSTEM_PROMPT_LADDER changed — bump LEARN_LADDER_PROMPT_VERSION and update "
        "LADDER_SYSTEM_PROMPT_HASH together in this commit"
    )


def test_ladder_user_prompt_snapshot():
    req = _req(
        depth="standard",
        demand_signals=main.DemandSignals(cognitiveDemand="describe", wordCount=7, hasPastOrFuture=True),
    )
    actual = hashlib.sha256(main.build_user_prompt(req).encode("utf-8")).hexdigest()
    assert actual == LADDER_USER_PROMPT_HASH, (
        "the ladder user prompt changed — bump LEARN_LADDER_PROMPT_VERSION and update "
        "LADDER_USER_PROMPT_HASH together in this commit"
    )


# ── SYSTEM_PROMPT_LADDER (learn-marking §7) ─────────────────────────────────


def test_ladder_system_prompt_asks_for_verdicts_not_scores():
    prompt = main.SYSTEM_PROMPT_LADDER
    assert '"levelCheck"' in prompt and '"demandCheck"' in prompt
    for removed in ('"fluency":', '"scores":', '"cefrLevel":', '"answered_the_question":', '"demands_met":', '"demands_missed":'):
        assert removed not in prompt, removed
    assert "You give no score and no level" in prompt
    assert "levelCheck (one entry for every ladder listed under LEVEL CHECK) and demandCheck are REQUIRED" in prompt


def test_ladder_system_prompt_has_no_per_request_target_level():
    prompt = main.SYSTEM_PROMPT_LADDER
    assert "TARGET LEVEL" not in prompt
    assert "A2 with elements of B1 (the IGCSE target, Cambridge 0520 Teacher's Notes p.11), for every" in prompt


def test_ladder_system_prompt_keeps_v6_teaching_rules():
    # learn-marking §7: errors, strengths, improved_answer at exam standard, the teacher voice and the
    # filters stay as in v6.
    for shared in (
        "EVERY REAL ERROR — report every genuine error in this answer, most important first:",
        "STRENGTHS — strengths[] holds 2-4 real strengths of THIS answer",
        "TEACHER VOICE — you are the student's own French teacher",
        "COACHING QUALITY GATE — SELF-VALIDATE before returning.",
        "QUOTE PRECISION",
        "DATA BOUNDARY",
        '"improved_answer": "<\'Say it better\': take the student\'s exact answer and improve it at the target level (A2 with elements of B1)',
    ):
        assert shared in main.SYSTEM_PROMPT and shared in main.SYSTEM_PROMPT_LADDER, shared


# ── build_user_prompt, ladder branch ────────────────────────────────────────


@pytest.mark.parametrize("question_id", ["fam_01", "hol_01", "sch_03", "env_05", "fut_19"])
def test_the_ladder_prompt_is_the_same_whatever_tier_the_request_carries(question_id):
    # The level never reaches the backend (learn-marking §1.1); even a stray tier changes nothing.
    prompts = {main.build_user_prompt(_req(question_id, difficulty_context=ctx)) for ctx in TIERS}
    assert len(prompts) == 1


def test_the_ladder_prompt_contains_no_level_text():
    prompt = main.build_user_prompt(_req(difficulty_context={"tier": "expert"}))
    assert "TARGET LEVEL" not in prompt
    assert "Learner's session target" not in prompt
    assert "Coaching tone" not in prompt
    for tier in main._TIER_PROMPTS.values():
        assert tier["coachingRubric"] not in prompt
        assert tier["coachingTone"] not in prompt


def test_the_v6_prompt_still_carries_the_target_level():
    prompt = main.build_user_prompt(_req(marking=None, difficulty_context={"tier": "intermediate"}))
    assert "TARGET LEVEL: CEFR A2." in prompt
    assert "Learner's session target: A2" in prompt
    assert "LEVEL CHECK" not in prompt


@pytest.mark.parametrize(
    "question_id, ladders",
    [
        ("fam_01", "T, L, R, A"),
        ("hol_01", "T, L, F, R, A"),
        ("sch_03", "T, L, R, A"),
        ("env_05", "T, L, O, R, A"),
        ("fut_19", "T, L, O, F, R, A"),
    ],
)
def test_the_applicable_ladders_line_for_the_five_test_questions(question_id, ladders):
    prompt = main.build_user_prompt(_req(question_id))
    assert f"LEVEL CHECK — ladders for this question: {ladders}. Return levelCheck with exactly these keys." in prompt
    assert f"one entry for each of {ladders}," in prompt


def test_unresolved_demands_check_only_the_four_always_ladders():
    prompt = main.build_user_prompt(_req("fut_19", version="stale-version"))
    assert "QUESTION DEMANDS" not in prompt
    assert "LEVEL CHECK — ladders for this question: T, L, R, A." in prompt


def test_every_rung_of_every_applicable_ladder_is_rendered_with_the_model_wording():
    demands = main.LEARN_DEMANDS_BY_QUESTION_ID["fut_19"]
    prompt = main.build_user_prompt(_req("fut_19"))
    for ladder in main._learn_ladder.applicable_ladders(demands):
        for index in range(4):
            text = main._learn_ladder.rung_text(ladder, index, demands, "model")
            assert f"  {index + 1}. {text if text is not None else 'no requirement (always null)'}" in prompt


def test_o_and_f_have_no_a1_requirement_in_the_prompt():
    prompt = main.build_user_prompt(_req("fut_19"))
    o_block = prompt.split("\nO — ")[1].split("\nF — ")[0]
    f_block = prompt.split("\nF — ")[1].split("\nR — ")[0]
    assert "  1. no requirement (always null)" in o_block
    assert "  1. no requirement (always null)" in f_block


def test_the_reminder_asks_for_both_keys_and_nothing_is_optional():
    prompt = main.build_user_prompt(_req("env_05"))
    reminder = prompt.split("REMINDER — LEVEL CHECK AND COACHING QUALITY GATE:")[1]
    assert "levelCheck is REQUIRED" in reminder
    assert "demandCheck is REQUIRED" in reminder
    assert "OPTIONAL" not in prompt


def test_the_complete_answer_line_that_demand_check_judges_is_in_the_prompt():
    prompt = main.build_user_prompt(_req("hol_01"))
    assert "\n\nA complete answer must: " in prompt


# ── The describe floor (reused from v7, ladder scheme only) ─────────────────

# Shared with the TS port in Batch 2: (cognitiveDemand, timeFrames, responseLoad, lexicalReach,
# structures) -> (score, level), with the floor on.
DESCRIBE_FLOOR_CASES = [
    (("describe", ["past"], "developed", "everyday", []), (3.0, "A2")),
    (("describe", ["future"], "short", "everyday", []), (3.0, "A2")),
    (("describe", ["present", "past"], "extended", "everyday", []), (3.0, "A2")),
    (("describe", ["present", "past", "future"], "extended", "abstract", []), (3.5, "A2")),
    (("describe", ["present"], "extended", "everyday", []), (2.75, "A1")),
    (("describe", ["present"], "short", "everyday", []), (1.25, "A1")),
    (("explain", ["past"], "short", "everyday", []), (3.25, "A2")),
    (("hypothesize", ["conditional"], "extended", "abstract", ["conditional"]), (10.0, "B2")),
]


def _demands(case):
    demand, frames, load, reach, structures = case
    return {
        "cognitiveDemand": demand,
        "timeFrames": frames,
        "responseLoad": load,
        "lexicalReach": reach,
        "structures": structures,
    }


@pytest.mark.parametrize("case, expected", DESCRIBE_FLOOR_CASES)
def test_describe_floor_cases(case, expected):
    score = main.derive_demand_score(_demands(case), describe_floor=True)
    assert (score, main.demand_score_to_level(score)) == expected
    assert main._DESCRIBE_PAST_FUTURE_FLOOR == 3.0


def test_the_floor_is_off_by_default_so_v6_scores_do_not_move():
    assert main.derive_demand_score(_demands(DESCRIBE_FLOOR_CASES[0][0])) == 2.0
    assert main.derive_demand_score(_demands(DESCRIBE_FLOOR_CASES[1][0])) == 1.25


def test_the_floor_reaches_the_ladder_prompt_only():
    assert "- Demand level: A2\n" in main.build_user_prompt(_req("hol_01"))
    assert "- Demand level: A1   |" in main.build_user_prompt(_req("hol_01", marking=None))


# ── Which system prompt and budget each provider call uses ──────────────────


class _FakeGroq:
    def __init__(self, content='{"scores": {}}'):
        self.calls = []
        self._content = content
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self._content))])


@pytest.mark.parametrize("ladder, system_prompt, extra", [(False, "SYSTEM_PROMPT", 0), (True, "SYSTEM_PROMPT_LADDER", 1000)])
def test_groq_uses_the_schemes_system_prompt_and_budget(monkeypatch, ladder, system_prompt, extra):
    fake = _FakeGroq()
    monkeypatch.setattr(main, "get_groq", lambda: fake)
    kwargs = {"ladder": True} if ladder else {}
    asyncio.run(main._call_groq("user prompt", "deep", **kwargs))
    sent = fake.calls[0]
    assert sent["messages"][0] == {"role": "system", "content": getattr(main, system_prompt)}
    assert sent["max_tokens"] == main._groq_token_budget(main.FEEDBACK_DEPTH_ANSWER_TOKENS["deep"] + extra)


def test_gemini_uses_a_model_bound_to_the_ladder_system_prompt(monkeypatch):
    seen = []

    def model(name):
        return SimpleNamespace(generate_content=lambda prompt: seen.append(name) or SimpleNamespace(text='{"scores": {}}'))

    monkeypatch.setattr(main, "get_gemini", lambda: model("v6"))
    monkeypatch.setattr(main, "get_gemini_ladder", lambda: model("ladder"))
    asyncio.run(main._call_gemini("p"))
    asyncio.run(main._call_gemini("p", ladder=True))
    assert seen == ["v6", "ladder"]


def test_the_ladder_gemini_model_is_created_with_the_ladder_system_prompt(monkeypatch):
    created = []

    class _Model:
        def __init__(self, name, system_instruction):
            created.append(system_instruction)

    fake_genai = SimpleNamespace(configure=lambda api_key: None, GenerativeModel=_Model)
    monkeypatch.setitem(sys.modules, "google.generativeai", fake_genai)
    import google

    monkeypatch.setattr(google, "generativeai", fake_genai, raising=False)
    monkeypatch.setattr(main, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(main, "_gemini_ladder_model", None)
    main.get_gemini_ladder()
    assert created == [main.SYSTEM_PROMPT_LADDER]


def test_call_ai_feedback_sends_the_ladder_prompt_only_when_the_request_asks(monkeypatch):
    seen = []

    async def fake_groq(prompt, depth="standard", **kw):
        seen.append(kw)
        return {"scores": {"comm": 6}, "modelUsed": "groq/test"}

    monkeypatch.setattr(main, "_call_groq", fake_groq)
    monkeypatch.setattr(main, "_log_coaching_quality", lambda *a, **kw: None)
    asyncio.run(main.call_ai_feedback(_req(marking=None, model="groq")))
    assert seen == [{}]
