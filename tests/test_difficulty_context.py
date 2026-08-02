"""Slice 1 (Phase 2 "Close the loop"): difficultyContext was always sent by
the frontend (src/services/api/apiClient.ts) but silently dropped on the
backend — FeedbackRequest had no field for it, _parse_feedback_request never
extracted it, and build_user_prompt never rendered it. This exercises the
three things that changed: build_user_prompt is byte-identical when
difficulty_context is None (no regression for existing callers), the
rendered prompt differs across tiers, and multipart requests that include a
`question` form field still extract skillContext/difficultyContext from the
bundled `data` JSON (previously gated behind `if not question:`, which never
fired since the client always sets `question`).

Run: pytest backend/tests/test_difficulty_context.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.pop("AZURE_SPEECH_KEY", None)
os.environ.pop("AZURE_SPEECH_REGION", None)


def _build_req(difficulty_context=None):
    import main

    return main.FeedbackRequest(
        question="Que fais-tu le week-end ?",
        transcript="Le week-end, je fais du sport avec mes amis.",
        skill_context=None,
        difficulty_context=difficulty_context,
    )


def test_build_user_prompt_byte_identical_when_difficulty_context_is_none():
    import main

    req_without_field = main.FeedbackRequest(
        question="Que fais-tu le week-end ?",
        transcript="Le week-end, je fais du sport avec mes amis.",
    )
    req_with_none = _build_req(difficulty_context=None)

    assert main.build_user_prompt(req_without_field) == main.build_user_prompt(req_with_none)


def test_build_user_prompt_differs_beginner_vs_expert():
    import main

    beginner = _build_req(difficulty_context={
        "tier": "beginner",
        "label": "Beginner",
        "cefrTarget": "A2",
        "coachingTone": "warm and encouraging, celebrate small wins",
        "coachingRubric": "Focus only on the most impactful single error.",
    })
    expert = _build_req(difficulty_context={
        "tier": "expert",
        "label": "Expert",
        "cefrTarget": "C1",
        "coachingTone": "rigorous and exacting, examiner-style",
        "coachingRubric": "Hold the student to native-like precision.",
    })

    beginner_prompt = main.build_user_prompt(beginner)
    expert_prompt = main.build_user_prompt(expert)

    assert beginner_prompt != expert_prompt
    assert "A2" in beginner_prompt
    assert "C1" in expert_prompt
    assert "warm and encouraging" in beginner_prompt
    assert "rigorous and exacting" in expert_prompt


def test_multipart_with_question_field_still_extracts_skill_and_difficulty_context():
    import main
    from fastapi.testclient import TestClient
    import asyncio
    import json as jsonlib

    async def _run():
        from starlette.requests import Request as StarletteRequest
        from starlette.datastructures import FormData, UploadFile as StarletteUploadFile
        import io

        data_payload = jsonlib.dumps({
            "skillContext": {"weaknesses": [{"name": "subjunctive", "recurrenceCount": 3}]},
            "difficultyContext": {"tier": "expert", "cefrTarget": "C1"},
        })

        # Exercise _parse_feedback_request directly against a hand-built
        # multipart form: the `question` field is always set by the real
        # client (apiClient.ts:237) alongside `data`, which is exactly the
        # combination that used to silently drop skillContext/difficultyContext.
        form = FormData([
            ("question", "Que fais-tu le week-end ?"),
            ("transcript", "Le week-end, je fais du sport."),
            ("data", data_payload),
        ])

        class _FakeRequest:
            headers = {"content-type": "multipart/form-data; boundary=x"}

            async def form(self):
                return form

        result = await main._parse_feedback_request(_FakeRequest())
        return result

    (question, transcript, model, detailed, metrics_json,
     skill_context, difficulty_context, audio_bytes, audio_mime) = asyncio.run(_run())

    assert question == "Que fais-tu le week-end ?"
    assert skill_context == {"weaknesses": [{"name": "subjunctive", "recurrenceCount": 3}]}
    assert difficulty_context == {"tier": "expert", "cefrTarget": "C1"}


def test_feedback_cache_key_differs_across_difficulty_tiers():
    import main

    beginner_key = main._feedback_cache_key("transcript", "q1", {"tier": "beginner"})
    expert_key = main._feedback_cache_key("transcript", "q1", {"tier": "expert"})
    no_tier_key = main._feedback_cache_key("transcript", "q1", None)

    assert beginner_key != expert_key
    assert beginner_key != no_tier_key


if __name__ == "__main__":
    test_build_user_prompt_byte_identical_when_difficulty_context_is_none()
    test_build_user_prompt_differs_beginner_vs_expert()
    test_multipart_with_question_field_still_extracts_skill_and_difficulty_context()
    test_feedback_cache_key_differs_across_difficulty_tiers()
    print("All test_difficulty_context tests passed.")
