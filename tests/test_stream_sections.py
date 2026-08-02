"""Slice 2a (Phase 2 "Close the loop"): _emit_ready_sections's depth-1 key
scanner cleared key_buf unconditionally before the '{'/'[' branches could
consume it, so object/array-valued top-level keys were never registered, and
a depth-1 string VALUE was mistaken for a key start (resetting the scanner's
key-collection state). Only scalar-valued keys ever registered — and in the
documented schema, those are `fluency`/`wordCount`, which both map to `None`
in _SECTION_MAP. Emitting a real section (scores/best_moment/
biggest_opportunity/grammar/vocabulary/pronunciation, all non-scalar) was
structurally impossible. Verified by direct execution before this fix: zero
events for every key order tried.

The fix tracks position at depth 1 explicitly (pending_key / expect_key)
instead of inferring it from key_buf's clear/non-clear state.

Feeds each payload into _emit_ready_sections one character at a time (the
worst case — a real Groq delta arrives in larger chunks) and asserts:
  - the correct section set is eventually emitted
  - each emitted value equals the value in the final, fully-parsed payload
    (for keys that have actually arrived in the buffer by emission time —
    the last top-level key is a deliberate exception, see below)
  - no section is re-emitted with a changed value
  - the LAST top-level key in the payload is never emitted (nothing proves
    it's closed — the whole point of the "next key proves prior key closed"
    design)
  - _repair_partial_json never raises on any partial prefix

Run: pytest backend/tests/test_stream_sections.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.pop("AZURE_SPEECH_KEY", None)
os.environ.pop("AZURE_SPEECH_REGION", None)


def _feed_char_by_char(payload: dict):
    """Returns (events, final_parsed). events is a list of (type, data) in
    emission order, exactly as a real streaming call would receive them."""
    import main

    buf_full = main.json.dumps(payload, ensure_ascii=False)
    already_emitted: set[str] = set()
    events: list[tuple[str, dict]] = []

    for i in range(1, len(buf_full) + 1):
        chunk = buf_full[:i]
        # _repair_partial_json must never raise on any partial prefix.
        main._repair_partial_json(chunk)
        for event_type, data in main._emit_ready_sections(chunk, already_emitted):
            events.append((event_type, data))

    final_parsed = main.json.loads(buf_full)
    return events, final_parsed


def _assert_no_mismatch_or_reemission(events, final_parsed, unproven_keys: set[str]):
    """Shared assertions across all three payload orders below.

    unproven_keys: keys whose value may legitimately still be None/absent at
    emission time — the payload's last top-level key (nothing proves it
    closed), and, for the grouped `snapshot` event specifically, any of its
    four constituent fields (fluency/scores/cefrLevel/wordCount) that sits
    later in the payload than `scores` itself: the snapshot fires as soon as
    `scores` is provably closed, which can be before a later sibling field's
    own value is provably closed. This is documented, correct behaviour
    (§1.11 of the Phase 2 plan), not a scanner defect — SnapshotCard.tsx
    tolerates the resulting undefined wordCount/cefrLevel with a fallback.
    """
    seen_types: set[str] = set()
    for event_type, data in events:
        assert event_type not in seen_types, f"{event_type} was re-emitted"
        seen_types.add(event_type)
        for key, value in data.items():
            if key in unproven_keys:
                continue
            if key not in final_parsed:
                continue
            assert value == final_parsed[key], (
                f"emitted {event_type}.{key} = {value!r}, "
                f"final parse has {final_parsed[key]!r}"
            )


_DOCUMENTED_ORDER_PAYLOAD = {
    "fluency": 7,
    "scores": {"communication": 6, "knowledge": 7, "accuracy": 5},
    "cefrLevel": "B1",
    "wordCount": 42,
    "best_moment": "Le week-end, je fais du sport.",
    "biggest_opportunity": "Try using more connectives.",
    "grammar": [{"issue": "gender agreement"}],
    "vocabulary": [{"word": "sport"}],
    "pronunciation": {"issues": []},
}

_COACHING_FIRST_PAYLOAD = {
    "best_moment": "Le week-end, je fais du sport «vraiment» bien.",
    "biggest_opportunity": 'Try using more connectives, e.g. "donc", "alors".',
    "grammar": [{"issue": "gender agreement", "note": 'a {curly} and [bracket] test, with "quotes"'}],
    "vocabulary": [{"word": "sport"}],
    "pronunciation": {"issues": []},
    "fluency": 7,
    "scores": {"communication": 6, "knowledge": 7, "accuracy": 5},
    "cefrLevel": "B1",
    "wordCount": 42,
}

_ADVERSARIAL_PAYLOAD = {
    "best_moment": 'A tricky value: {not an object}, [not an array], key: value, a,b,c and an escaped \\"quote\\" inside.',
    "biggest_opportunity": 'Another: {"fake":"json"} embedded, plus a comma, and a colon: like this.',
    "scores": {"communication": 6.5, "knowledge": 7.0, "accuracy": 5.5},
    "fluency": 8,
    "cefrLevel": "B2",
    "wordCount": 55,
    "grammar": [{"issue": "x", "quote": 'literal \\"escaped\\" text with a } brace and ] bracket'}],
    "vocabulary": [{"word": "sport", "note": "v,w,x,y,z"}],
    "pronunciation": {"issues": [{"word": "bonjour", "note": "contains: colon, {brace}, [bracket], comma,here"}]},
}


def test_documented_key_order_emits_all_real_sections():
    events, final_parsed = _feed_char_by_char(_DOCUMENTED_ORDER_PAYLOAD)
    emitted_types = [e[0] for e in events]

    assert emitted_types == ["snapshot", "strongest_moment", "opportunity", "grammar", "vocabulary"]
    # wordCount sits after `scores` in this payload, so the snapshot's
    # scores-triggered emit can fire before wordCount's own value is provably
    # closed (see _assert_no_mismatch_or_reemission's docstring).
    _assert_no_mismatch_or_reemission(events, final_parsed, unproven_keys={"pronunciation", "wordCount"})

    snapshot_data = dict(events[0][1])
    assert snapshot_data["scores"] == final_parsed["scores"]


def test_coaching_first_key_order_emits_all_real_sections():
    events, final_parsed = _feed_char_by_char(_COACHING_FIRST_PAYLOAD)
    emitted_types = [e[0] for e in events]

    assert set(emitted_types) == {"strongest_moment", "opportunity", "grammar", "vocabulary", "pronunciation", "snapshot"}
    _assert_no_mismatch_or_reemission(events, final_parsed, unproven_keys={"wordCount"})

    snapshot_data = dict(next(data for etype, data in events if etype == "snapshot"))
    assert snapshot_data["wordCount"] is None  # last key overall — never provably closed


def test_adversarial_string_values_do_not_break_the_scanner():
    """String values containing '{', '[', ':', ',' and escaped quotes must not
    be mistaken for structural JSON tokens or key boundaries."""
    events, final_parsed = _feed_char_by_char(_ADVERSARIAL_PAYLOAD)
    emitted_types = [e[0] for e in events]

    assert "strongest_moment" in emitted_types
    assert "opportunity" in emitted_types
    assert "grammar" in emitted_types
    assert "vocabulary" in emitted_types
    # wordCount sits after `scores`, so the same early-snapshot-emit caveat
    # as the documented-order test applies here.
    _assert_no_mismatch_or_reemission(events, final_parsed, unproven_keys={"pronunciation", "wordCount"})

    for event_type, data in events:
        if event_type == "strongest_moment":
            assert data["best_moment"] == final_parsed["best_moment"]
        if event_type == "opportunity":
            assert data["biggest_opportunity"] == final_parsed["biggest_opportunity"]


if __name__ == "__main__":
    test_documented_key_order_emits_all_real_sections()
    test_coaching_first_key_order_emits_all_real_sections()
    test_adversarial_string_values_do_not_break_the_scanner()
    print("All test_stream_sections tests passed.")
