"""Learn overhaul Batch 4 (learn-prompt-v5): the coach prompt asks for at most 2
fixes (quote -> full French correction at A2 with elements of B1, TN p.11 -> one
sentence why), one quoted strength and a "say it better" answer at the same
level, and no longer asks for advanced_answer. The wire contract does not move:
every key still ships (advanced_answer as "" via setdefault) and cefrLevel is
still requested, because the frontend's zod schema requires it
(src/services/api/feedbackSchema.ts) and FEEDBACK_CONTRACT_VERSION is unchanged.

Run: pytest backend/tests/test_learn_prompt_v5.py
"""

from __future__ import annotations

import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.pop("AZURE_SPEECH_KEY", None)
os.environ.pop("AZURE_SPEECH_REGION", None)

import main

# SYSTEM_PROMPT is half of what LEARN_PROMPT_VERSION versions; the user-prompt
# half is pinned in test_learn_demands.py. Bump LEARN_PROMPT_VERSION and this
# hash together.
SYSTEM_PROMPT_HASH = "fc5f935514cdc46a15e47e203a02154a9f4afb0f21f2d53b89cd85e85a026e22"


def test_prompt_version_is_v5():
    assert main.LEARN_PROMPT_VERSION == "learn-prompt-v5"


def test_system_prompt_snapshot():
    actual = hashlib.sha256(main.SYSTEM_PROMPT.encode("utf-8")).hexdigest()
    assert actual == SYSTEM_PROMPT_HASH, (
        f'SYSTEM_PROMPT changed — bump LEARN_PROMPT_VERSION (currently "{main.LEARN_PROMPT_VERSION}") '
        "and update SYSTEM_PROMPT_HASH together in this commit"
    )


def test_feedback_contract_version_unchanged():
    assert main.FEEDBACK_CONTRACT_VERSION == 2


def test_system_prompt_asks_for_at_most_two_fixes_at_the_igcse_target():
    p = main.SYSTEM_PROMPT
    assert "AT MOST 2 FIXES" in p
    assert "A2 with elements of B1" in p
    assert "Teacher's Notes p.11" in p
    assert "Never above B1" in p
    assert "ONE sentence" in p


def test_system_prompt_still_requests_cefr_level():
    # Required by the frontend schema: a response without it falls back to the next engine.
    assert '"cefrLevel": "<Exactly one of: A1 | A2 | B1 | B2>"' in main.SYSTEM_PROMPT


def test_no_prompt_asks_for_advanced_answer():
    assert '"advanced_answer"' not in main.SYSTEM_PROMPT
    assert '"advanced_answer"' not in main.MULTIMODAL_SYSTEM_PROMPT


def test_depth_ranges_no_longer_ask_for_more_grammar_items():
    for text in main.FEEDBACK_DEPTH_PROMPT_RANGES.values():
        assert "grammar items" not in text
        assert "mark band" not in text


def test_user_prompt_reminds_two_fixes():
    req = main.FeedbackRequest(question="Q", transcript="Je suis allé au cinéma hier.")
    assert "At most 2 fixes" in main.build_user_prompt(req)


def test_advanced_answer_key_still_ships_empty():
    req = main.FeedbackRequest(question="Q", transcript="Une reponse quelconque avec assez de mots.")
    fb = main.enrich_feedback({"scores": {"comm": 5, "know": 5, "acc": 5}, "cefrLevel": "A2"}, req)
    assert fb["advanced_answer"] == ""
    assert fb["cefrLevel"] == "A2"


def test_new_generic_phrases_are_rejected():
    for phrase in ("Great job!", "Keep it up.", "keep practising", "Good job on this"):
        assert main._generic_phrase_issues(phrase), phrase


def test_specific_feedback_is_not_rejected():
    specific = "Your use of « parce que j'aime » links your opinion to a reason."
    assert main._generic_phrase_issues(specific) == []


def test_generic_best_moment_section_is_dropped():
    data = {"best_moment": "Great job — « je suis allé » is right."}
    assert main._validate_and_filter_section("strongest_moment", data) is None
