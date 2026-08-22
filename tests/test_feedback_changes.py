"""Stage 3 (Learn-mode coach feedback plan): changes[] annotations. The diff
itself is computed client-side from transcript + improved_answer — the
backend's only job is to carry the LLM's {quote, quoteContext, category,
explanation} annotations through the same drop-only evidence gate
corrections[] already uses (_drop_unevidenced_items, generalized), and to
never ship changes[] without an improved_answer for the client to diff
against (invariant #11 — a diff implies an authoritative corrected answer).

Run: pytest backend/tests/test_feedback_changes.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.pop("AZURE_SPEECH_KEY", None)
os.environ.pop("AZURE_SPEECH_REGION", None)

import main


def _change(quote: str, evidenced: bool) -> dict:
    return {
        "quote": quote if evidenced else "",
        "explanation": "explains the change" if evidenced else "no evidence here",
        "category": "grammar",
    }


def test_changes_ship_when_improved_answer_present_and_evidenced():
    result = {
        "improved_answer": "Je vais au parc.",
        "changes": [_change("va", evidenced=True)],
    }
    gated = main._apply_coaching_quality_gate(result)
    assert gated["changes"] == [_change("va", evidenced=True)]


def test_unevidenced_change_is_dropped_but_evidenced_ones_survive():
    result = {
        "improved_answer": "Je vais au parc demain.",
        "changes": [_change("va", evidenced=True), _change("x", evidenced=False)],
    }
    gated = main._apply_coaching_quality_gate(result)
    assert [c["quote"] for c in gated["changes"]] == ["va"]


def test_changes_are_emptied_when_improved_answer_is_absent():
    """A diff without an authoritative corrected answer to diff against is
    meaningless (invariant #11) — changes[] must never ship in that case,
    even if the model attached annotations to it anyway."""
    result = {
        "improved_answer": "",
        "changes": [_change("va", evidenced=True)],
    }
    gated = main._apply_coaching_quality_gate(result)
    assert gated["changes"] == []


def test_enrich_feedback_defaults_changes_to_empty_list():
    req = main.FeedbackRequest(question="Q?", transcript="Je vais au parc.")
    fb = {"scores": {"comm": 7, "know": 7, "acc": 7}}
    enriched = main.enrich_feedback(fb, req)
    assert enriched["changes"] == []


def test_enrich_feedback_preserves_a_list_of_changes():
    req = main.FeedbackRequest(question="Q?", transcript="Je vais au parc.")
    fb = {
        "scores": {"comm": 7, "know": 7, "acc": 7},
        "improved_answer": "Je vais au parc.",
        "changes": [_change("va", evidenced=True)],
    }
    enriched = main.enrich_feedback(fb, req)
    assert enriched["changes"] == [_change("va", evidenced=True)]


if __name__ == "__main__":
    test_changes_ship_when_improved_answer_present_and_evidenced()
    test_unevidenced_change_is_dropped_but_evidenced_ones_survive()
    test_changes_are_emptied_when_improved_answer_is_absent()
    test_enrich_feedback_defaults_changes_to_empty_list()
    test_enrich_feedback_preserves_a_list_of_changes()
    print("All test_feedback_changes tests passed.")
