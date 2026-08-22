"""Stage 3 (Learn-mode coach feedback plan): `depth` replaces the old
`detailed: bool` flag. Exercises the three things that must hold:
depth is part of the feedback cache key (so a 'brief' response can never be
served back to a 'deep' request), build_user_prompt renders per-depth ranges,
and the server-side item-count ceiling truncates regardless of what the
model returned — the server owns the cap, the client's depth is only a hint.

Run: pytest backend/tests/test_feedback_depth.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.pop("AZURE_SPEECH_KEY", None)
os.environ.pop("AZURE_SPEECH_REGION", None)

import main


def test_normalize_feedback_depth_defaults_and_rejects_garbage():
    assert main._normalize_feedback_depth(None) == "standard"
    assert main._normalize_feedback_depth("") == "standard"
    assert main._normalize_feedback_depth("nonsense") == "standard"
    assert main._normalize_feedback_depth("deep") == "deep"
    assert main._normalize_feedback_depth("BRIEF") == "brief"


def test_feedback_cache_key_differs_across_depth():
    brief_key = main._feedback_cache_key("transcript", "q1", None, "brief")
    deep_key = main._feedback_cache_key("transcript", "q1", None, "deep")
    standard_key = main._feedback_cache_key("transcript", "q1", None, "standard")
    default_key = main._feedback_cache_key("transcript", "q1", None)

    assert len({brief_key, deep_key, standard_key}) == 3
    assert default_key == standard_key


def test_build_user_prompt_renders_per_depth_ranges():
    def _req(depth):
        return main.FeedbackRequest(
            question="Que fais-tu le week-end ?",
            transcript="Je joue au foot.",
            depth=depth,
        )

    brief_prompt = main.build_user_prompt(_req("brief"))
    standard_prompt = main.build_user_prompt(_req("standard"))
    deep_prompt = main.build_user_prompt(_req("deep"))

    assert "FEEDBACK DEPTH: brief" in brief_prompt
    assert "FEEDBACK DEPTH: deep" in deep_prompt
    assert "FEEDBACK DEPTH" not in standard_prompt
    assert brief_prompt != deep_prompt != standard_prompt


def test_apply_depth_item_caps_truncates_regardless_of_request():
    """The server cap holds even when the model over-delivers under a 'brief'
    request — client depth is a hint, the server owns the ceiling."""
    fb = {
        "grammar": {
            "critical": [{"id": f"c{i}"} for i in range(10)],
            "polish": [{"id": f"p{i}"} for i in range(10)],
        },
        "vocabulary": [{"basic": f"b{i}", "upgrade": f"u{i}"} for i in range(10)],
        "corrections": [{"id": f"x{i}"} for i in range(10)],
    }

    result = main._apply_depth_item_caps(fb, "brief")

    caps = main.FEEDBACK_DEPTH_ITEM_CAPS["brief"]
    assert len(result["grammar"]["critical"]) == caps["grammar"]
    assert len(result["grammar"]["polish"]) == caps["grammar"]
    assert len(result["vocabulary"]) == caps["vocabulary"]
    assert len(result["corrections"]) == caps["corrections"]


def test_apply_depth_item_caps_deep_allows_more_than_brief():
    fb = {"vocabulary": [{"basic": f"b{i}", "upgrade": f"u{i}"} for i in range(10)]}

    brief_result = main._apply_depth_item_caps(dict(fb), "brief")
    deep_result = main._apply_depth_item_caps(dict(fb), "deep")

    assert len(deep_result["vocabulary"]) > len(brief_result["vocabulary"])


def test_apply_depth_item_caps_leaves_short_arrays_untouched():
    fb = {"vocabulary": [{"basic": "b", "upgrade": "u"}]}
    result = main._apply_depth_item_caps(fb, "brief")
    assert len(result["vocabulary"]) == 1


if __name__ == "__main__":
    test_normalize_feedback_depth_defaults_and_rejects_garbage()
    test_feedback_cache_key_differs_across_depth()
    test_build_user_prompt_renders_per_depth_ranges()
    test_apply_depth_item_caps_truncates_regardless_of_request()
    test_apply_depth_item_caps_deep_allows_more_than_brief()
    test_apply_depth_item_caps_leaves_short_arrays_untouched()
    print("All test_feedback_depth tests passed.")
