"""Slice 2c (Phase 2 "Close the loop"): _feedback_impl and the stream's own
`complete` tail validated nothing before this — validate_coaching_quality
existed but only _log_coaching_quality called it, which logs and discards
(§1.3 of the Phase 2 plan). Slice 2b added item-level dropping to the
streaming per-section path (on_section); this applies the same drop to the
assembled, final `result` dict via _apply_coaching_quality_gate, so a section
that would have been dropped mid-stream cannot reappear in the final
payload, and the non-streaming path (which never streamed sections at all)
gets the same guarantee.

Run: pytest backend/tests/test_non_streaming_quality_gate.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.pop("AZURE_SPEECH_KEY", None)
os.environ.pop("AZURE_SPEECH_REGION", None)


def _grammar_item(item_id: str, evidenced: bool) -> dict:
    return {
        "id": item_id,
        "msg": f"« student text {item_id} » is wrong" if evidenced else "This is wrong",
        "diagnostic": "",
        "quote": "",
        "severity": "major",
    }


def test_gate_clears_best_moment_lacking_a_quote():
    import main

    result = {
        "best_moment": "You communicated your ideas clearly.",
        "biggest_opportunity": "Try using the passé composé for one past event.",
        "grammar": {"critical": [], "polish": []},
    }
    gated = main._apply_coaching_quality_gate(result)
    assert gated["best_moment"] == ""
    assert gated["biggest_opportunity"] == "Try using the passé composé for one past event."


def test_gate_clears_biggest_opportunity_with_a_banned_phrase():
    import main

    result = {
        "best_moment": "Your use of « parce que j'aime » shows a clear link.",
        "biggest_opportunity": "Add more detail to your answer.",
        "grammar": {"critical": [], "polish": []},
    }
    gated = main._apply_coaching_quality_gate(result)
    assert gated["best_moment"] == "Your use of « parce que j'aime » shows a clear link."
    assert gated["biggest_opportunity"] == ""


def test_gate_drops_unevidenced_grammar_items_but_keeps_evidenced_ones():
    import main

    result = {
        "best_moment": "Your use of « parce que j'aime » shows a clear link.",
        "biggest_opportunity": "Try using the passé composé for one past event.",
        "grammar": {
            "critical": [
                _grammar_item("a", evidenced=True),
                _grammar_item("b", evidenced=False),
            ],
            "polish": [_grammar_item("c", evidenced=False)],
        },
    }
    gated = main._apply_coaching_quality_gate(result)
    assert [item["id"] for item in gated["grammar"]["critical"]] == ["a"]
    assert gated["grammar"]["polish"] == []


def test_gate_is_a_no_op_on_already_clean_content():
    import main

    result = {
        "best_moment": "Your use of « parce que j'aime » shows a clear link.",
        "biggest_opportunity": "Try using the passé composé for one past event.",
        "grammar": {"critical": [_grammar_item("a", evidenced=True)], "polish": []},
    }
    gated = main._apply_coaching_quality_gate(dict(result))
    assert gated == result


def test_feedback_impl_style_enriched_result_is_gated():
    """Exercises enrich_feedback -> _apply_coaching_quality_gate in the same
    order _feedback_impl now applies them, on a live-provider-shaped fb dict."""
    import main

    req = main.FeedbackRequest(
        question="Que fais-tu le week-end ?",
        transcript="Le week-end, je fais du sport avec mes amis.",
    )
    fb = {
        "providerStatus": "primary",
        "scores": {"comm": 6.0, "know": 7.0, "acc": 5.0},
        "best_moment": "You communicated your ideas clearly.",  # no quote
        "biggest_opportunity": "Good effort overall.",  # banned phrase
        "grammar": {
            "critical": [_grammar_item("a", evidenced=False)],
            "polish": [],
        },
    }

    result = main.enrich_feedback(fb, req)
    result = main._apply_coaching_quality_gate(result)

    assert result["best_moment"] == ""
    assert result["biggest_opportunity"] == ""
    assert result["grammar"]["critical"] == []


if __name__ == "__main__":
    test_gate_clears_best_moment_lacking_a_quote()
    test_gate_clears_biggest_opportunity_with_a_banned_phrase()
    test_gate_drops_unevidenced_grammar_items_but_keeps_evidenced_ones()
    test_gate_is_a_no_op_on_already_clean_content()
    test_feedback_impl_style_enriched_result_is_gated()
    print("All test_non_streaming_quality_gate tests passed.")
