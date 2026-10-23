"""Learn marking ladder (Design D): the level checklist's prompt and provider wiring.

Frontend spec: docs/systems/learn-marking-levels.md (`learn-marking §N`, Design D amendment). The ladder
scheme makes two calls: Learn's unchanged v6 feedback call, then this checklist (SYSTEM_PROMPT_LADDER_CHECK
+ build_ladder_check_prompt) at temperature 0. Covers the checklist's prompt (level-blind, the rungs, the
error list it is given), which request each call sends, and the checklist's own Groq -> Gemini chain.
The v6 prompt's own pins live in tests/test_learn_prompt_v6.py (system prompt) and
tests/test_learn_demands.py (user prompt); they must not move, which is what proves Learn's feedback,
every other screen and every old app are unaffected.

Run: pytest tests/test_learn_ladder_prompt.py
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.pop("AZURE_SPEECH_KEY", None)
os.environ.pop("AZURE_SPEECH_REGION", None)

import main  # noqa: E402

# Pinned on purpose: bump LEARN_LADDER_PROMPT_VERSION and update these in the same commit as any
# change to SYSTEM_PROMPT_LADDER_CHECK or build_ladder_check_prompt.
LADDER_CHECK_SYSTEM_PROMPT_HASH = "2da545aad1e47deba2ceadfcdca95d587092120af43bbf107b98e1b2546c8c3f"
LADDER_CHECK_USER_PROMPT_HASH = "763d72f5a67474703cb6faed307374232ad55d48430885501238d862db0d7c9e"

TIERS = [None, *({"tier": t} for t in main._TIER_PROMPTS)]
_CEFR = re.compile(r"\b(?:A1|A2|B1|B2|C1|C2)\b")


def _req(question_id="hol_01", *, marking="ladder-v1", difficulty_context=None, version=None, **kw):
    return main.FeedbackRequest(
        question=kw.pop("question", "Où es-tu allé(e) pendant les dernières vacances ?"),
        transcript=kw.pop("transcript", "Je suis allé en France avec ma famille. J'ai allé à la plage."),
        question_id=question_id,
        demands_version=main.LEARN_DEMANDS_VERSION if version is None else version,
        difficulty_context=difficulty_context,
        marking=marking,
        **kw,
    )


def _fb(*quotes, polish=()):
    """A v6 feedback reply carrying grammar items with these quotes."""
    def item(q):
        return {"quote": q, "correction": f"[{q}]"}
    return {"grammar": {"critical": [item(q) for q in quotes], "polish": [item(q) for q in polish]}}


FB = _fb("J'ai allé")


# ── Versions and pins ───────────────────────────────────────────────────────


def test_the_checklist_has_its_own_version_and_v6_keeps_its_own():
    assert main.LEARN_LADDER_PROMPT_VERSION == "ladder-check-v2"
    assert main.LEARN_PROMPT_VERSION == "learn-prompt-v6"
    assert main.LEARN_LADDER_REPLY_VERSION == "learn-prompt-v6+ladder-check-v2"


def test_checklist_system_prompt_snapshot():
    actual = hashlib.sha256(main.SYSTEM_PROMPT_LADDER_CHECK.encode("utf-8")).hexdigest()
    assert actual == LADDER_CHECK_SYSTEM_PROMPT_HASH, (
        "SYSTEM_PROMPT_LADDER_CHECK changed — bump LEARN_LADDER_PROMPT_VERSION and update "
        "LADDER_CHECK_SYSTEM_PROMPT_HASH together in this commit"
    )


def test_checklist_user_prompt_snapshot():
    actual = hashlib.sha256(main.build_ladder_check_prompt(_req(), FB).encode("utf-8")).hexdigest()
    assert actual == LADDER_CHECK_USER_PROMPT_HASH, (
        "the checklist user prompt changed — bump LEARN_LADDER_PROMPT_VERSION and update "
        "LADDER_CHECK_USER_PROMPT_HASH together in this commit"
    )


# ── The first call is exactly v6 ────────────────────────────────────────────


@pytest.mark.parametrize("question_id", ["fam_01", "hol_01", "sch_03", "env_05", "fut_19"])
@pytest.mark.parametrize("ctx", TIERS)
def test_the_feedback_call_of_a_ladder_request_is_the_v6_prompt(question_id, ctx):
    ladder = main.build_user_prompt(_req(question_id, difficulty_context=ctx))
    v6 = main.build_user_prompt(_req(question_id, marking=None, difficulty_context=ctx))
    assert ladder == v6
    assert "LEVEL CHECK" not in ladder


def test_groq_feedback_request_is_v6_and_the_checklist_request_is_its_own():
    feedback = main._groq_feedback_kwargs("p", "deep")
    assert feedback["messages"][0] == {"role": "system", "content": main.SYSTEM_PROMPT}
    assert feedback["max_tokens"] == main._groq_token_budget(main.FEEDBACK_DEPTH_ANSWER_TOKENS["deep"])
    assert feedback["temperature"] == 0.4
    check = main._groq_ladder_check_kwargs("p")
    assert check["messages"] == [
        {"role": "system", "content": main.SYSTEM_PROMPT_LADDER_CHECK},
        {"role": "user", "content": "p"},
    ]
    assert check["temperature"] == 0
    assert check["max_tokens"] == main._groq_token_budget(main.LADDER_CHECK_ANSWER_TOKENS)
    assert check["response_format"] == {"type": "json_object"}


# ── SYSTEM_PROMPT_LADDER_CHECK (learn-marking §7) ───────────────────────────


def test_the_checklist_asks_for_verdicts_only():
    prompt = main.SYSTEM_PROMPT_LADDER_CHECK
    assert '"levelCheck"' in prompt and '"demandCheck"' in prompt
    assert "exactly these two keys" in prompt
    for absent in ('"scores"', '"fluency"', '"cefrLevel"', '"grammar"', '"corrections"', '"improved_answer"',
                   "TEACHER VOICE", "STRENGTHS"):
        assert absent not in prompt, absent
    assert 'JSON null (never the string "null")' in prompt
    assert "do not add errors of your own" in prompt
    # Only the top two Accuracy rungs count errors (ladder-check-v1 failed all four on five errors).
    assert "Rungs 1 and 2 do not count errors" in prompt
    assert "DATA BOUNDARY" in prompt


def test_the_checklist_system_prompt_names_no_cefr_level_and_no_target():
    prompt = main.SYSTEM_PROMPT_LADDER_CHECK
    assert not _CEFR.search(prompt)
    for banned in ("TARGET LEVEL", "target level", "marking level", "strict", "lenient", "Exam standard"):
        assert banned not in prompt, banned


# ── build_ladder_check_prompt ───────────────────────────────────────────────


@pytest.mark.parametrize("question_id", ["fam_01", "hol_01", "sch_03", "env_05", "fut_19"])
def test_the_checklist_prompt_is_level_and_tier_blind(question_id):
    prompts = {main.build_ladder_check_prompt(_req(question_id, difficulty_context=ctx), FB) for ctx in TIERS}
    assert len(prompts) == 1
    prompt = prompts.pop()
    assert not _CEFR.search(prompt), _CEFR.search(prompt)
    for banned in ("Demand level", "Learner's session target", "TARGET LEVEL", "Coaching tone", "DETERMINISTIC SIGNALS"):
        assert banned not in prompt, banned


def test_the_checklist_prompt_ignores_skill_context_metrics_and_signals():
    plain = main.build_ladder_check_prompt(_req(), FB)
    busy = main.build_ladder_check_prompt(_req(
        skill_context={"weaknesses": [{"name": "Gender", "recurrenceCount": 4}]},
        demand_signals=main.DemandSignals(cognitiveDemand="describe", wordCount=7, hasConnectors=False),
    ), FB)
    assert plain == busy


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
    prompt = main.build_ladder_check_prompt(_req(question_id), FB)
    assert f"LEVEL CHECK — ladders for this question: {ladders}. Return levelCheck with exactly these keys." in prompt


def test_unresolved_demands_check_only_the_four_always_ladders():
    prompt = main.build_ladder_check_prompt(_req("fut_19", version="stale-version"), FB)
    assert "QUESTION DEMANDS" not in prompt and "A complete answer must" not in prompt
    assert "LEVEL CHECK — ladders for this question: T, L, R, A." in prompt


def test_the_complete_answer_line_that_demand_check_judges_is_in_the_prompt():
    prompt = main.build_ladder_check_prompt(_req("hol_01"), FB)
    demands = main.resolve_learn_demands("hol_01", main.LEARN_DEMANDS_VERSION)
    assert f"\n\nA complete answer must: {demands['sufficientAnswer']}" in prompt


def test_every_rung_of_every_applicable_ladder_is_rendered_with_the_model_wording():
    demands = main.LEARN_DEMANDS_BY_QUESTION_ID["fut_19"]
    prompt = main.build_ladder_check_prompt(_req("fut_19"), FB)
    for ladder in main._learn_ladder.applicable_ladders(demands):
        for index in range(4):
            text = main._learn_ladder.rung_text(ladder, index, demands, "model")
            assert f"  {index + 1}. {text if text is not None else 'no requirement (always null)'}" in prompt


def test_each_ladder_states_its_verdict_shape():
    # The first gate run put null on rungs that have a requirement and once wrote the string "null".
    prompt = main.build_ladder_check_prompt(_req("fut_19"), FB)
    for ladder in ("T", "L", "R", "A"):
        assert '(verdicts: 4 × "y"/"n")' in prompt.split(f"\n{ladder} — ")[1].split("\n")[0]
    for ladder in ("O", "F"):
        header = prompt.split(f"\n{ladder} — ")[1].split("\n")[0]
        assert '(verdicts: null, then 3 × "y"/"n")' in header
        assert "  1. no requirement (always null)" in prompt.split(f"\n{ladder} — ")[1]


def test_the_error_list_is_the_feedback_calls_grounded_errors_each_once():
    transcript = "Je suis allé en France avec mon famille. J'ai allé à la plage. J'ai allé au musée."
    fb = _fb("J'ai allé", "mon famille", "j'ai allé", polish=("un erreur inventée",))
    errors = main.ladder_check_errors(fb, transcript)
    # A repeated quote counts once (the 2026-10-23 run listed one error twice); a quote that is not in
    # the transcript is dropped, as the coaching quality gate drops it.
    assert errors == [
        {"quote": "J'ai allé", "correction": "[J'ai allé]"},
        {"quote": "mon famille", "correction": "[mon famille]"},
    ]
    prompt = main.build_ladder_check_prompt(_req(transcript=transcript), fb)
    assert (
        "ERRORS ALREADY FOUND IN THIS ANSWER (for the Accuracy ladder): 2\n"
        "1. « J'ai allé » → « [J'ai allé] »\n"
        "2. « mon famille » → « [mon famille] »"
    ) in prompt


@pytest.mark.parametrize("fb", [{}, {"grammar": None}, {"grammar": {"critical": "x"}}, _fb()])
def test_no_errors_reads_none(fb):
    assert "ERRORS ALREADY FOUND IN THIS ANSWER (for the Accuracy ladder): 0\nnone" in main.build_ladder_check_prompt(_req(), fb)


def test_the_accuracy_rungs_point_at_the_error_list():
    prompt = main.build_ladder_check_prompt(_req(), FB)
    a_block = prompt.split("\nA — ")[1]
    assert "Count the errors under ERRORS ALREADY FOUND: 0-2" in a_block


def test_the_largest_checklist_request_fits_far_below_the_groq_per_request_limit():
    # Groq's on_demand tier refuses a request whose prompt + max_tokens is over 8,000 tokens (413). A
    # French/English token is at least ~2.5 characters, so this bound is conservative.
    longest = max(
        len(main.build_ladder_check_prompt(_req(q, transcript="Mot " * 120), _fb(*(f"mot{i}" for i in range(6)))))
        for q in main.LEARN_DEMANDS_BY_QUESTION_ID
    )
    estimate = (len(main.SYSTEM_PROMPT_LADDER_CHECK) + longest) / 2.5 + main._groq_token_budget(main.LADDER_CHECK_ANSWER_TOKENS)
    assert estimate < 6000, estimate


# ── The describe floor (the TS port; no prompt renders it) ──────────────────

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


def test_v6_keeps_its_unfloored_demand_level_line():
    assert "- Demand level: A1   |" in main.build_user_prompt(_req("hol_01", marking=None))


# ── The checklist's providers ───────────────────────────────────────────────


def test_the_ladder_gemini_model_is_created_with_the_checklist_prompt_at_temperature_0(monkeypatch):
    created = []

    class _Model:
        def __init__(self, name, system_instruction, generation_config=None):
            created.append((system_instruction, generation_config))

    fake_genai = SimpleNamespace(configure=lambda api_key: None, GenerativeModel=_Model)
    monkeypatch.setitem(sys.modules, "google.generativeai", fake_genai)
    import google

    monkeypatch.setattr(google, "generativeai", fake_genai, raising=False)
    monkeypatch.setattr(main, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(main, "_gemini_ladder_check_model", None)
    main.get_gemini_ladder_check()
    assert created == [(main.SYSTEM_PROMPT_LADDER_CHECK, {"temperature": 0, "response_mime_type": "application/json"})]


class _FakeGroq:
    def __init__(self, content):
        self.calls = []
        self._content = content
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self._content))])


def test_the_groq_checklist_call_sends_the_checklist_request(monkeypatch):
    fake = _FakeGroq('{"levelCheck": {}, "demandCheck": null}')
    monkeypatch.setattr(main, "get_groq", lambda: fake)
    result = asyncio.run(main._call_groq_ladder_check("check prompt"))
    assert fake.calls == [main._groq_ladder_check_kwargs("check prompt")]
    assert result["modelUsed"] == f"groq/{main.GROQ_MODEL}"


HOL_TRANSCRIPT = "Je suis allé en France avec ma famille. J'ai allé à la plage."


def _good_check():
    return {
        "levelCheck": {
            ladder: {"verdicts": [None if ladder == "F" else "y", "y", "n", "n"], "quote": "Je suis allé en France", "next": ""}
            for ladder in ("T", "L", "F", "R", "A")
        },
        "demandCheck": {"met": True, "quote": "Je suis allé en France"},
    }


def _stub_check(monkeypatch, *, groq, gemini):
    calls = []

    def make(name, reply):
        async def call(prompt):
            calls.append((name, prompt))
            if isinstance(reply, Exception):
                raise reply
            return dict(reply, modelUsed=f"{name}/test")
        return call

    monkeypatch.setattr(main, "_call_groq_ladder_check", make("groq", groq))
    monkeypatch.setattr(main, "_call_gemini_ladder_check", make("gemini", gemini))
    return calls


def test_the_checklist_runs_on_groq_first_with_the_built_prompt(monkeypatch):
    calls = _stub_check(monkeypatch, groq=_good_check(), gemini=RuntimeError("unused"))
    req = _req(transcript=HOL_TRANSCRIPT, model="groq")
    check = asyncio.run(main.call_ladder_check(req, FB))
    assert [c[0] for c in calls] == ["groq"]
    assert calls[0][1] == main.build_ladder_check_prompt(req, FB)
    assert check["levelCheck"] == _good_check()["levelCheck"]
    assert check["modelUsed"] == "groq/test" and "providerErrors" not in check


def test_an_unusable_checklist_falls_back_to_the_other_provider(monkeypatch):
    bad = _good_check()
    del bad["levelCheck"]["F"]  # an applicable ladder is missing
    calls = _stub_check(monkeypatch, groq=bad, gemini=_good_check())
    check = asyncio.run(main.call_ladder_check(_req(transcript=HOL_TRANSCRIPT, model="groq"), FB))
    assert [c[0] for c in calls] == ["groq", "gemini"]
    assert check["modelUsed"] == "gemini/test"
    assert check["providerErrors"][0]["type"] == "malformed_response"


@pytest.mark.parametrize("model", ["gemini", None])
def test_without_a_groq_preference_gemini_goes_first_as_in_call_ai_feedback(monkeypatch, model):
    calls = _stub_check(monkeypatch, groq=_good_check(), gemini=_good_check())
    asyncio.run(main.call_ladder_check(_req(transcript=HOL_TRANSCRIPT, model=model), FB))
    assert [c[0] for c in calls] == ["gemini"]


def test_no_usable_checklist_raises(monkeypatch):
    _stub_check(monkeypatch, groq={"levelCheck": "nope"}, gemini=RuntimeError("down"))
    with pytest.raises(main.LadderCheckUnavailable):
        asyncio.run(main.call_ladder_check(_req(transcript=HOL_TRANSCRIPT), FB))


def test_attach_keeps_the_feedback_and_marks_a_failed_checklist(monkeypatch):
    _stub_check(monkeypatch, groq=RuntimeError("down"), gemini=RuntimeError("down"))
    fb = asyncio.run(main.attach_ladder_check(_req(transcript=HOL_TRANSCRIPT), dict(FB, scores={"comm": 6})))
    assert fb["scores"] == {"comm": 6} and "levelCheck" not in fb
    assert fb["ladderCheckError"]
    assert not main._is_cacheable_result(dict(fb, providerStatus="primary"))


def test_call_ai_feedback_runs_the_checklist_only_for_a_ladder_request(monkeypatch):
    seen = []

    async def fake_groq(prompt, depth="standard"):
        seen.append(("feedback", prompt))
        return {"scores": {"comm": 6}, "grammar": FB["grammar"], "modelUsed": "groq/test"}

    async def fake_attach(req, fb):
        seen.append(("check", req.marking))
        return fb

    monkeypatch.setattr(main, "_call_groq", fake_groq)
    monkeypatch.setattr(main, "attach_ladder_check", fake_attach)
    monkeypatch.setattr(main, "_log_coaching_quality", lambda *a, **kw: None)
    asyncio.run(main.call_ai_feedback(_req(marking=None, model="groq")))
    assert [s[0] for s in seen] == ["feedback"]
    seen.clear()
    asyncio.run(main.call_ai_feedback(_req(model="groq")))
    assert seen[0] == ("feedback", main.build_user_prompt(_req(marking=None, model="groq")))
    assert seen[1] == ("check", "ladder-v1")
