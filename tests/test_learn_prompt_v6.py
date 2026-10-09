"""Learn feedback Batch 6a (learn-prompt-v6): the coach prompt asks for every real
error, most important first (quote -> full French correction at A2 with elements
of B1, TN p.11 -> one sentence why -> tip), 2-4 quoted strengths in a new optional
strengths[] (best_moment stays = the strongest), and a teacher voice: second
person, with encouragement as an opening line that quotes the student. The server
ceiling (FEEDBACK_DEPTH_ITEM_CAPS) still bounds every list, and strengths[] is
capped at 4. FEEDBACK_CONTRACT_VERSION is 3 for the additive strengths[] key.
advanced_answer still ships as "" and cefrLevel is still requested, because the
frontend's zod schema requires it (src/services/api/feedbackSchema.ts).

Run: pytest backend/tests/test_learn_prompt_v6.py
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
SYSTEM_PROMPT_HASH = "5f73d750bd22cb2098151dcc551df1194e123fe52caee18b9cce3510b709a243"


def test_prompt_version_is_v6():
    assert main.LEARN_PROMPT_VERSION == "learn-prompt-v6"


def test_system_prompt_snapshot():
    actual = hashlib.sha256(main.SYSTEM_PROMPT.encode("utf-8")).hexdigest()
    assert actual == SYSTEM_PROMPT_HASH, (
        f'SYSTEM_PROMPT changed — bump LEARN_PROMPT_VERSION (currently "{main.LEARN_PROMPT_VERSION}") '
        "and update SYSTEM_PROMPT_HASH together in this commit"
    )


def test_feedback_contract_version_is_3_for_strengths():
    assert main.FEEDBACK_CONTRACT_VERSION == 3


def test_system_prompt_asks_for_every_real_error_at_the_igcse_target():
    p = main.SYSTEM_PROMPT
    assert "EVERY REAL ERROR" in p
    assert "most important first" in p
    assert "Never invent an error" in p
    assert "A2 with elements of B1" in p
    assert "Teacher's Notes p.11" in p
    assert "Never above B1" in p
    assert "ONE sentence" in p


def test_no_prompt_caps_fixes_at_two():
    req = main.FeedbackRequest(question="Q", transcript="Je suis allé au cinéma hier.", depth="deep")
    for text in (main.SYSTEM_PROMPT, main.build_user_prompt(req), *main.FEEDBACK_DEPTH_PROMPT_RANGES.values()):
        assert "at most 2" not in text.lower()
        assert "ONE STRENGTH" not in text


def test_system_prompt_requests_quoted_strengths_and_keeps_best_moment():
    p = main.SYSTEM_PROMPT
    assert '"strengths": [' in p
    assert "2-4 real strengths" in p
    assert '"quote":' in p and '"why":' in p
    assert "Never praise words you also report as an error" in p
    assert '"best_moment":' in p


def test_system_prompt_asks_for_a_quoting_opening_line_in_the_teacher_voice():
    p = main.SYSTEM_PROMPT
    assert "TEACHER VOICE" in p
    assert "second person" in p
    assert "OPENING LINE" in p
    assert "Do not name yourself or the student" in p


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


def test_user_prompt_reminds_every_error_and_strengths():
    req = main.FeedbackRequest(question="Q", transcript="Je suis allé au cinéma hier.")
    prompt = main.build_user_prompt(req)
    assert "Every real error, most important first" in prompt
    assert "strengths: 2-4" in prompt


def test_advanced_answer_key_still_ships_empty_and_strengths_defaults_to_a_list():
    req = main.FeedbackRequest(question="Q", transcript="Une reponse quelconque avec assez de mots.")
    fb = main.enrich_feedback({"scores": {"comm": 5, "know": 5, "acc": 5}, "cefrLevel": "A2"}, req)
    assert fb["advanced_answer"] == ""
    assert fb["cefrLevel"] == "A2"
    assert fb["strengths"] == []
    assert fb["schemaVersion"] == 3


def test_new_generic_phrases_are_rejected():
    for phrase in ("Great job!", "Keep it up.", "keep practising", "Good job on this"):
        assert main._generic_phrase_issues(phrase), phrase


def test_specific_feedback_is_not_rejected():
    specific = "Your use of « parce que j'aime » links your opinion to a reason."
    assert main._generic_phrase_issues(specific) == []


def test_generic_best_moment_section_is_dropped():
    data = {"best_moment": "Great job — « je suis allé » is right."}
    assert main._validate_and_filter_section("strongest_moment", data) is None


# ── Quality gate over strengths[] and the opening line ───────────────────────

def _gate(result: dict) -> dict:
    return main._apply_coaching_quality_gate(result, "Je suis allé au parc avec mes amis parce que c'est drôle.")


def test_quality_gate_drops_generic_and_unquoted_strengths_and_keeps_real_ones():
    kept = {"quote": "parce que c'est drôle", "why": "You gave a reason for your opinion."}
    result = _gate({
        "strengths": [
            kept,
            {"quote": "avec mes amis", "why": "Great job adding who you were with."},
            {"quote": "", "why": "You used a connective."},
            {"why": "You described the park."},
            "not an object",
        ],
    })
    assert result["strengths"] == [kept]


def test_quality_gate_clears_a_generic_opening_line_and_keeps_a_specific_one():
    generic = _gate({"encouragement": "Great job, keep it up!"})
    assert generic["encouragement"] == ""
    specific = "You did really well with « parce que c'est drôle » — here's where you could improve."
    assert _gate({"encouragement": specific})["encouragement"] == specific


def test_quality_gate_replaces_a_malformed_strengths_value_with_an_empty_list():
    assert _gate({"strengths": "« avec mes amis »"})["strengths"] == []


def test_depth_caps_bound_strengths_at_four():
    strengths = [{"quote": f"q{i}", "why": f"w{i}"} for i in range(6)]
    for depth in ("brief", "standard", "deep"):
        fb = main._apply_depth_item_caps({"strengths": list(strengths)}, depth)
        assert fb["strengths"] == strengths[:4]


def test_depth_caps_still_bound_fixes_at_the_server_ceiling():
    items = [{"quote": f"q{i}"} for i in range(10)]
    fb = main._apply_depth_item_caps(
        {"grammar": {"critical": list(items), "polish": []}, "corrections": list(items)}, "standard"
    )
    assert len(fb["grammar"]["critical"]) == 5
    assert len(fb["corrections"]) == 5
