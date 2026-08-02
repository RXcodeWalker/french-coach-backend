"""Slice 2b (Phase 2 "Close the loop"): no unvalidated coaching text reaches
the screen — without a retry, which a stream cannot support. Slice 2a made
sections actually stream; this validates them before they're queued/yielded,
via feedback_stream's on_section -> _validate_and_filter_section.

validate_coaching_quality (the pre-existing whole-object check, still used by
_log_coaching_quality as an observe-only gate) is decomposed into per-field
predicates: _best_moment_issues, _generic_phrase_issues,
_grammar_item_issues (whole-object, reports only) and
_drop_unevidenced_grammar_items (item-level, actually filters). The
composition assertion (validate_coaching_quality == the sum of the parts)
guards against the two implementations drifting apart.

Run: pytest backend/tests/test_section_validation.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.pop("AZURE_SPEECH_KEY", None)
os.environ.pop("AZURE_SPEECH_REGION", None)


def test_banned_phrase_in_best_moment_drops_the_strongest_moment_event():
    import main

    data = {"best_moment": "« j'aime le sport » — good effort overall!"}
    result = main._validate_and_filter_section("strongest_moment", data)
    assert result is None


def test_best_moment_without_a_quote_drops_the_strongest_moment_event():
    import main

    data = {"best_moment": "You communicated your ideas about sport well."}
    result = main._validate_and_filter_section("strongest_moment", data)
    assert result is None


def test_clean_best_moment_passes_through_unchanged():
    import main

    data = {"best_moment": "Your use of « parce que j'aime » shows a clear cause-and-effect link."}
    result = main._validate_and_filter_section("strongest_moment", data)
    assert result == data


def test_banned_phrase_in_biggest_opportunity_drops_the_opportunity_event():
    import main

    data = {"biggest_opportunity": "You could expand on this a little more."}
    result = main._validate_and_filter_section("opportunity", data)
    assert result is None


def test_clean_biggest_opportunity_passes_through_unchanged():
    import main

    data = {"biggest_opportunity": "Try using the passé composé for one past event to show tense range."}
    result = main._validate_and_filter_section("opportunity", data)
    assert result == data


def _grammar_item(item_id: str, evidenced: bool) -> dict:
    return {
        "id": item_id,
        "msg": f"« student text {item_id} » is wrong" if evidenced else "This is wrong",
        "diagnostic": "",
        "quote": "" if evidenced else "",
        "severity": "major",
    }


def test_grammar_event_with_one_of_three_items_unevidenced_keeps_the_other_two():
    import main

    grammar = {
        "critical": [
            _grammar_item("a", evidenced=True),
            _grammar_item("b", evidenced=False),
            _grammar_item("c", evidenced=True),
        ],
        "polish": [],
    }
    data = {"grammar": grammar}
    result = main._validate_and_filter_section("grammar", data)

    assert result is not None
    kept_ids = [item["id"] for item in result["grammar"]["critical"]]
    assert kept_ids == ["a", "c"]


def test_grammar_event_with_all_items_dropped_yields_no_event():
    import main

    grammar = {
        "critical": [_grammar_item("a", evidenced=False)],
        "polish": [_grammar_item("b", evidenced=False)],
    }
    data = {"grammar": grammar}
    result = main._validate_and_filter_section("grammar", data)

    assert result is None


def test_grammar_item_with_quote_field_but_no_marker_in_text_is_kept():
    import main

    grammar = {
        "critical": [{
            "id": "x",
            "msg": "This is wrong",
            "diagnostic": "",
            "quote": "j'ai allé",
            "severity": "major",
        }],
        "polish": [],
    }
    data = {"grammar": grammar}
    result = main._validate_and_filter_section("grammar", data)

    assert result is not None
    assert len(result["grammar"]["critical"]) == 1


def test_non_coaching_section_types_pass_through_unvalidated():
    import main

    data = {"scores": {"communication": 6}, "fluency": 7, "cefrLevel": "B1", "wordCount": 42}
    result = main._validate_and_filter_section("snapshot", data)
    assert result == data

    data2 = {"vocabulary": [{"word": "sport"}]}
    assert main._validate_and_filter_section("vocabulary", data2) == data2


def test_validate_coaching_quality_is_the_composition_of_the_per_field_predicates():
    import main

    fb = {
        "best_moment": "You communicated clearly.",  # no quote AND a banned phrase
        "biggest_opportunity": "Add more detail.",
        "grammar": {"critical": [_grammar_item("a", evidenced=False)], "polish": []},
    }
    whole_object_issues = main.validate_coaching_quality(fb, transcript="whatever")

    composed = (
        main._best_moment_issues(fb["best_moment"])
        + main._generic_phrase_issues(fb["best_moment"], fb["biggest_opportunity"], "")
        + main._grammar_item_issues(fb["grammar"])
    )

    assert whole_object_issues == composed
    assert len(whole_object_issues) == 3


if __name__ == "__main__":
    test_banned_phrase_in_best_moment_drops_the_strongest_moment_event()
    test_best_moment_without_a_quote_drops_the_strongest_moment_event()
    test_clean_best_moment_passes_through_unchanged()
    test_banned_phrase_in_biggest_opportunity_drops_the_opportunity_event()
    test_clean_biggest_opportunity_passes_through_unchanged()
    test_grammar_event_with_one_of_three_items_unevidenced_keeps_the_other_two()
    test_grammar_event_with_all_items_dropped_yields_no_event()
    test_grammar_item_with_quote_field_but_no_marker_in_text_is_kept()
    test_non_coaching_section_types_pass_through_unvalidated()
    test_validate_coaching_quality_is_the_composition_of_the_per_field_predicates()
    print("All test_section_validation tests passed.")
