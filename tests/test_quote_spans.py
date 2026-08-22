"""Stage 2 (docs/architecture, learn-feedback-contract): corrections[]/
quoteSpans[] span resolution. The server enumerates candidate occurrences of
a correction's `quote` in the canonical transcript (accent/case-tolerant),
narrows by `quoteContext` when ambiguous, and emits a span only when exactly
one candidate remains — ambiguity is always resolved by dropping the span,
never by guessing (invariant #10). Also covers _drop_unevidenced_items, the
generalization of _drop_unevidenced_grammar_items (finding H) over a flat
corrections[] list.

Run: pytest backend/tests/test_quote_spans.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.pop("AZURE_SPEECH_KEY", None)
os.environ.pop("AZURE_SPEECH_REGION", None)


def test_unique_quote_resolves_to_correct_offset():
    import main

    transcript = "Je suis allé à Paris hier."
    start, end = main._resolve_quote_span(transcript, "allé", None)
    assert transcript[start:end] == "allé"


def test_repeated_identical_quote_with_no_context_resolves_to_no_span():
    import main

    transcript = "Paris est belle. Jaime Paris."
    assert main._resolve_quote_span(transcript, "Paris", None) is None


def test_repeated_quote_with_distinguishing_context_resolves_the_occurrence():
    import main

    transcript = "Jaime Paris et ma soeur aime Paris aussi."
    start, end = main._resolve_quote_span(transcript, "Paris", "ma soeur aime Paris aussi")
    assert transcript[start:end] == "Paris"
    # the SECOND occurrence, not the first
    assert start > transcript.index("Paris")


def test_ambiguous_repeated_quote_with_non_discriminating_context_resolves_to_no_span():
    import main

    transcript = "Paris est belle. Jaime Paris."
    # context doesn't actually contain the quote adjacent to a unique occurrence
    assert main._resolve_quote_span(transcript, "Paris", "quelque chose sans rapport") is None


def test_quote_not_found_resolves_to_no_span():
    import main

    transcript = "Je vais au marché."
    assert main._resolve_quote_span(transcript, "piscine", None) is None


def test_accent_and_case_tolerant_matching():
    import main

    transcript = "Je suis allé à Paris."
    start, end = main._resolve_quote_span(transcript, "ALLE", None)
    assert transcript[start:end] == "allé"


def test_build_quote_spans_emits_non_overlapping_spans_only():
    import main

    transcript = "Je suis allé à Paris hier avec mes amis."
    corrections = [
        {"id": "c1", "quote": "allé"},
        {"id": "c2", "quote": "Paris"},
        {"id": "c3", "quote": "introuvable"},  # not in transcript — no span
    ]
    spans = main._build_quote_spans(transcript, corrections)
    ids = {s["correctionId"] for s in spans}
    assert ids == {"c1", "c2"}
    for span in spans:
        assert transcript[span["start"]:span["end"]].lower() in ("allé", "paris")


def test_drop_unevidenced_items_keeps_only_quoted_or_evidenced_corrections():
    import main

    corrections = [
        {"id": "a", "description": "« j'ai allé » uses the wrong auxiliary", "quote": ""},
        {"id": "b", "description": "This is wrong", "quote": ""},  # no evidence
        {"id": "c", "description": "Missing article", "quote": "un chien"},  # quote field alone is evidence
    ]
    kept, dropped = main._drop_unevidenced_items(corrections)
    assert [item["id"] for item in kept] == ["a", "c"]
    assert dropped == 1


def test_apply_coaching_quality_gate_drops_unevidenced_corrections_and_their_spans():
    import main

    transcript = "Je suis allé à Paris."
    result = {
        "best_moment": "Your use of « parce que j'aime » shows a clear link.",
        "biggest_opportunity": "Try using the passé composé for one past event.",
        "grammar": {"critical": [], "polish": []},
        "corrections": [
            {"id": "c1", "description": "« allé » needs être", "quote": "allé"},
            {"id": "c2", "description": "wrong", "quote": ""},  # unevidenced
        ],
    }
    gated = main._apply_coaching_quality_gate(result, transcript)
    assert [c["id"] for c in gated["corrections"]] == ["c1"]
    span_ids = {s["correctionId"] for s in gated["quoteSpans"]}
    assert span_ids == {"c1"}


def test_validate_and_filter_section_drops_unevidenced_corrections_but_keeps_evidenced():
    import main

    data = {
        "corrections": [
            {"id": "a", "description": "« j'ai allé » is wrong", "quote": ""},
            {"id": "b", "description": "This is wrong", "quote": ""},
        ]
    }
    result = main._validate_and_filter_section("corrections", data)
    assert result is not None
    assert [item["id"] for item in result["corrections"]] == ["a"]


def test_validate_and_filter_section_drops_the_whole_corrections_event_when_all_unevidenced():
    import main

    data = {"corrections": [{"id": "a", "description": "wrong", "quote": ""}]}
    result = main._validate_and_filter_section("corrections", data)
    assert result is None


def test_enrich_feedback_computes_quote_spans_for_corrections():
    import main

    req = main.FeedbackRequest(question="Q", transcript="Je suis allé à Paris hier.")
    fb: dict = {
        "providerStatus": "primary",
        "scores": {"comm": 6.0, "know": 6.0, "acc": 6.0},
        "corrections": [{"id": "c1", "quote": "allé"}],
    }

    result = main.enrich_feedback(fb, req)

    assert result["corrections"] == [{"id": "c1", "quote": "allé"}]
    assert result["quoteSpans"] == [{"correctionId": "c1", "start": 8, "end": 12}]


def test_enrich_feedback_defaults_corrections_and_quote_spans_to_empty_lists():
    import main

    req = main.FeedbackRequest(question="Q", transcript="Une reponse quelconque.")
    fb: dict = {"providerStatus": "primary", "scores": {"comm": 6.0, "know": 6.0, "acc": 6.0}}

    result = main.enrich_feedback(fb, req)

    assert result["corrections"] == []
    assert result["quoteSpans"] == []


if __name__ == "__main__":
    test_unique_quote_resolves_to_correct_offset()
    test_repeated_identical_quote_with_no_context_resolves_to_no_span()
    test_repeated_quote_with_distinguishing_context_resolves_the_occurrence()
    test_ambiguous_repeated_quote_with_non_discriminating_context_resolves_to_no_span()
    test_quote_not_found_resolves_to_no_span()
    test_accent_and_case_tolerant_matching()
    test_build_quote_spans_emits_non_overlapping_spans_only()
    test_drop_unevidenced_items_keeps_only_quoted_or_evidenced_corrections()
    test_apply_coaching_quality_gate_drops_unevidenced_corrections_and_their_spans()
    test_validate_and_filter_section_drops_unevidenced_corrections_but_keeps_evidenced()
    test_validate_and_filter_section_drops_the_whole_corrections_event_when_all_unevidenced()
    test_enrich_feedback_computes_quote_spans_for_corrections()
    test_enrich_feedback_defaults_corrections_and_quote_spans_to_empty_lists()
    print("All test_quote_spans tests passed.")
