"""Stage 8 (docs §9.1/§9.2): the client sends only questionId + demandsVersion
— never the demand fields themselves. This exercises resolve_learn_demands's
trust boundary (unknown id, version mismatch, missing args all degrade to
None/demandsResolved: false, never a silent substitution), the Python port of
deriveDemandScore/demandScoreToLevel against known TS fixture values, and a
prompt-version snapshot pin for build_user_prompt's QUESTION DEMANDS /
DETERMINISTIC SIGNALS rendering (mirrors
src/domain/igcse/judgement/__tests__/version-pin.test.ts's discipline).

Run: pytest backend/tests/test_learn_demands.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.pop("AZURE_SPEECH_KEY", None)
os.environ.pop("AZURE_SPEECH_REGION", None)

LEARN_PROMPT_FIXTURE_HASH = "6147c532c8965d83f9bb7dfa570115b9f19f6d1e01a70228389d72ed05a8f967"


def test_learn_demands_corpus_loaded():
    import main

    assert main.LEARN_DEMANDS_VERSION != ""
    assert len(main.LEARN_DEMANDS_BY_QUESTION_ID) == 428
    assert "fam_01" in main.LEARN_DEMANDS_BY_QUESTION_ID


def test_resolve_learn_demands_known_id_correct_version():
    import main

    result = main.resolve_learn_demands("fam_01", main.LEARN_DEMANDS_VERSION)
    assert result is not None
    assert result["cognitiveDemand"] == "describe"


def test_resolve_learn_demands_unknown_id_returns_none():
    import main

    result = main.resolve_learn_demands("does_not_exist_99", main.LEARN_DEMANDS_VERSION)
    assert result is None


def test_resolve_learn_demands_version_mismatch_returns_none():
    import main

    result = main.resolve_learn_demands("fam_01", "stale-or-forged-version")
    assert result is None


def test_resolve_learn_demands_missing_args_returns_none():
    import main

    assert main.resolve_learn_demands(None, None) is None
    assert main.resolve_learn_demands("fam_01", None) is None
    assert main.resolve_learn_demands(None, main.LEARN_DEMANDS_VERSION) is None


def test_derive_demand_score_matches_ts_fixture():
    """fam_01: describe, timeFrames=[present], responseLoad=extended,
    lexicalReach=everyday -> 2.0 (base) + 0.75 (extended) = 2.75 -> A1.
    Cross-checked against deriveDemandScore/demandScoreToLevel in
    src/domain/learn/demand/deriveDemandLevel.ts for the same input."""
    import main

    demands = main.resolve_learn_demands("fam_01", main.LEARN_DEMANDS_VERSION)
    score = main.derive_demand_score(demands)
    assert score == 2.75
    assert main.demand_score_to_level(score) == "A1"


def test_build_user_prompt_omits_demands_section_when_unresolved():
    import main

    req = main.FeedbackRequest(
        question="Que fais-tu le week-end ?",
        transcript="Le week-end, je fais du sport.",
        question_id="does_not_exist_99",
        demands_version=main.LEARN_DEMANDS_VERSION,
    )
    prompt = main.build_user_prompt(req)
    assert "QUESTION DEMANDS" not in prompt
    assert "DETERMINISTIC SIGNALS" not in prompt


def test_build_user_prompt_omits_demands_section_when_no_question_id():
    import main

    req = main.FeedbackRequest(
        question="Que fais-tu le week-end ?",
        transcript="Le week-end, je fais du sport.",
    )
    prompt = main.build_user_prompt(req)
    assert "QUESTION DEMANDS" not in prompt


def test_build_user_prompt_byte_identical_when_demand_fields_absent():
    """No regression for existing callers (mirrors
    test_build_user_prompt_byte_identical_when_difficulty_context_is_none in
    test_difficulty_context.py): a request with no question_id/demands_version
    at all renders identically to one with them explicitly set to None."""
    import main

    req_without_fields = main.FeedbackRequest(question="Q", transcript="T")
    req_with_none = main.FeedbackRequest(
        question="Q", transcript="T",
        question_id=None, demands_version=None, demand_signals=None,
    )
    assert main.build_user_prompt(req_without_fields) == main.build_user_prompt(req_with_none)


def test_build_user_prompt_renders_demands_section_when_resolved():
    import main

    req = main.FeedbackRequest(
        question="Decris ta famille.",
        transcript="Ma famille est grande.",
        question_id="fam_01",
        demands_version=main.LEARN_DEMANDS_VERSION,
        difficulty_context={"cefrTarget": "A2"},
        demand_signals=main.DemandSignals(
            cognitiveDemand="describe",
            wordCount=4,
            hasJustification=False,
            hasConnectors=False,
            hasSubjunctive=False,
            hasConditional=False,
            hasPastOrFuture=False,
        ),
    )
    prompt = main.build_user_prompt(req)
    assert "QUESTION DEMANDS" in prompt
    assert "What the learner must do: describe" in prompt
    assert "Demand level: A1" in prompt
    assert "Learner's session target: A2" in prompt
    assert "DETERMINISTIC SIGNALS" in prompt
    assert "word count: 4" in prompt
    assert "justification markers: absent" in prompt


def test_learn_prompt_version_snapshot():
    """Version-drift guard: if build_user_prompt's demands rendering changes,
    this fails loudly — bump LEARN_PROMPT_VERSION and update
    LEARN_PROMPT_FIXTURE_HASH together in the same commit."""
    import hashlib
    import main

    req = main.FeedbackRequest(
        question="Decris ta famille.",
        transcript="Ma famille est grande.",
        question_id="fam_01",
        demands_version=main.LEARN_DEMANDS_VERSION,
        difficulty_context={"cefrTarget": "A2"},
        demand_signals=main.DemandSignals(
            cognitiveDemand="describe",
            wordCount=4,
            hasJustification=False,
            hasConnectors=False,
            hasSubjunctive=False,
            hasConditional=False,
            hasPastOrFuture=False,
        ),
    )
    prompt = main.build_user_prompt(req)
    actual = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    assert actual == LEARN_PROMPT_FIXTURE_HASH, (
        f'learn prompt output changed — bump LEARN_PROMPT_VERSION '
        f'(currently "{main.LEARN_PROMPT_VERSION}") and update '
        f"LEARN_PROMPT_FIXTURE_HASH together in this commit"
    )


def test_parse_feedback_request_json_extracts_demand_fields():
    """§9.1: the client sends questionId + demandsVersion (+ optional
    demandSignals) as top-level JSON fields; _parse_feedback_request (used by
    /api/feedback/stream) must extract all three alongside the existing
    skillContext/difficultyContext extraction."""
    import asyncio
    import main

    async def _run():
        class _FakeRequest:
            headers = {"content-type": "application/json"}

            async def json(self):
                return {
                    "question": "Decris ta famille.",
                    "transcript": "Ma famille est grande.",
                    "enginePreference": "gemini",
                    "questionId": "fam_01",
                    "demandsVersion": main.LEARN_DEMANDS_VERSION,
                    "demandSignals": {"cognitiveDemand": "describe", "wordCount": 5},
                }

        return await main._parse_feedback_request(_FakeRequest())

    (question, transcript, model, depth, metrics_json,
     skill_context, difficulty_context, audio_bytes, audio_mime,
     question_id, demands_version, demand_signals) = asyncio.run(_run())

    assert question_id == "fam_01"
    assert demands_version == main.LEARN_DEMANDS_VERSION
    assert demand_signals == {"cognitiveDemand": "describe", "wordCount": 5}


def test_parse_feedback_request_multipart_extracts_demand_fields_and_engine():
    """Same as test_parse_feedback_request_json_extracts_demand_fields but for
    the multipart branch (used when an audio recording accompanies the
    request) — the client bundles questionId/demandsVersion/demandSignals/
    enginePreference inside the `data` JSON field, not as top-level form
    fields, mirroring how skillContext/difficultyContext already work there."""
    import asyncio
    import json as jsonlib
    import main

    async def _run():
        from starlette.datastructures import FormData

        data_payload = jsonlib.dumps({
            "questionId": "fam_01",
            "demandsVersion": main.LEARN_DEMANDS_VERSION,
            "demandSignals": {"cognitiveDemand": "describe", "wordCount": 12},
            "enginePreference": "gemini",
        })
        form = FormData([
            ("question", "Q"),
            ("transcript", "T"),
            ("data", data_payload),
        ])

        class _FakeRequest:
            headers = {"content-type": "multipart/form-data; boundary=x"}

            async def form(self):
                return form

        return await main._parse_feedback_request(_FakeRequest())

    (question, transcript, model, depth, metrics_json,
     skill_context, difficulty_context, audio_bytes, audio_mime,
     question_id, demands_version, demand_signals) = asyncio.run(_run())

    assert model == "gemini"
    assert question_id == "fam_01"
    assert demands_version == main.LEARN_DEMANDS_VERSION
    assert demand_signals == {"cognitiveDemand": "describe", "wordCount": 12}


def test_parse_feedback_request_json_reads_engine_preference():
    """Bug fix (docs §3.12/§9.2): apiClient.ts's streamFeedback sends
    enginePreference, never a top-level `model` field. Before this fix,
    _parse_feedback_request read only payload.get("model"), so the client's
    engine choice was silently dropped on every /api/feedback/stream request
    and it always ran Groq-first regardless of what the user selected —
    mirrors the fix already applied to the non-streaming /api/feedback
    endpoint (see the `model = str(payload.get("enginePreference") or ...)`
    line in feedback())."""
    import asyncio
    import main

    async def _run():
        class _FakeRequest:
            headers = {"content-type": "application/json"}

            async def json(self):
                return {
                    "question": "Q",
                    "transcript": "T",
                    "enginePreference": "gemini",
                }

        return await main._parse_feedback_request(_FakeRequest())

    result = asyncio.run(_run())
    model = result[2]
    assert model == "gemini"


if __name__ == "__main__":
    test_learn_demands_corpus_loaded()
    test_resolve_learn_demands_known_id_correct_version()
    test_resolve_learn_demands_unknown_id_returns_none()
    test_resolve_learn_demands_version_mismatch_returns_none()
    test_resolve_learn_demands_missing_args_returns_none()
    test_derive_demand_score_matches_ts_fixture()
    test_build_user_prompt_omits_demands_section_when_unresolved()
    test_build_user_prompt_omits_demands_section_when_no_question_id()
    test_build_user_prompt_byte_identical_when_demand_fields_absent()
    test_build_user_prompt_renders_demands_section_when_resolved()
    test_learn_prompt_version_snapshot()
    test_parse_feedback_request_json_extracts_demand_fields()
    test_parse_feedback_request_multipart_extracts_demand_fields_and_engine()
    test_parse_feedback_request_json_reads_engine_preference()
    print("All test_learn_demands tests passed.")
