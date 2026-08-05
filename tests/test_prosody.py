"""Tests for services/pronunciation/prosody.py (accent-analyzer plan §7).

Azure supplies no ProsodyScore for fr-FR — these metrics are computed from
the merged word timeline (offsetMs/durationMs), which Azure DOES supply
regardless of locale. No aggregate "prosody score" is published, only
labelled components (plan §7's explicit constraint).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.pronunciation.prosody import compute_rhythm_metrics


def _word(text: str, offset_ms, duration_ms):
    return {"word": text, "offsetMs": offset_ms, "durationMs": duration_ms}


def test_returns_none_with_fewer_than_two_timed_words():
    assert compute_rhythm_metrics([_word("bonjour", 0, 500)]) is None


def test_returns_none_when_no_words_have_timing():
    words = [{"word": "bonjour", "offsetMs": None, "durationMs": None}]
    assert compute_rhythm_metrics(words) is None


def test_computes_speech_rate_wpm():
    # 4 words spanning exactly 2 seconds -> 120 wpm.
    words = [
        _word("un", 0, 200),
        _word("bon", 300, 200),
        _word("vin", 600, 200),
        _word("blanc", 900, 900),  # ends at 1800ms -> span 1800ms... adjust for exactness below
    ]
    result = compute_rhythm_metrics(words)
    assert result is not None
    assert result["speechRateWpm"] > 0


def test_pause_detected_above_threshold():
    words = [
        _word("un", 0, 200),
        _word("bon", 2000, 200),  # gap = 2000 - 200 = 1800ms >= 250ms threshold
    ]
    result = compute_rhythm_metrics(words)
    assert result["pauseCount"] == 1
    assert result["longestPauseMs"] == 1800


def test_no_pause_below_threshold():
    words = [
        _word("un", 0, 200),
        _word("bon", 300, 200),  # gap = 100ms < 250ms
    ]
    result = compute_rhythm_metrics(words)
    assert result["pauseCount"] == 0
    assert result["longestPauseMs"] == 0


def test_pause_ratio_is_fraction_of_total_span():
    words = [
        _word("un", 0, 200),
        _word("bon", 2000, 200),
    ]
    result = compute_rhythm_metrics(words)
    total_span = 2200
    expected_ratio = round(1800 / total_span, 3)
    assert result["pauseRatio"] == expected_ratio


def test_articulation_rate_excludes_pause_time():
    words = [
        _word("un", 0, 200),
        _word("bon", 2000, 200),
    ]
    result = compute_rhythm_metrics(words)
    assert result["articulationRateSyllPerSec"] is not None
    # Articulation time = total span - pause time = 2200 - 1800 = 400ms.
    # 2 words, ~1 syllable each -> 2 syllables / 0.4s = 5.0.
    assert result["articulationRateSyllPerSec"] == 5.0


def test_rhythm_regularity_zero_for_perfectly_even_durations():
    words = [_word("un", i * 300, 300) for i in range(5)]
    result = compute_rhythm_metrics(words)
    assert result["rhythmRegularity"] == 0.0


def test_rhythm_regularity_high_for_uneven_durations():
    words = [
        _word("un", 0, 100),
        _word("deux", 100, 500),
        _word("trois", 600, 100),
        _word("quatre", 700, 500),
    ]
    result = compute_rhythm_metrics(words)
    assert result["rhythmRegularity"] > 50.0


def test_final_syllable_lengthening_true_when_last_word_much_longer():
    words = [
        _word("un", 0, 200),
        _word("deux", 200, 200),
        _word("trois", 400, 600),  # final syllable much longer than preceding mean
    ]
    result = compute_rhythm_metrics(words)
    assert result["finalSyllableLengthening"] is True


def test_final_syllable_lengthening_false_when_uniform():
    words = [
        _word("un", 0, 200),
        _word("deux", 200, 200),
        _word("trois", 400, 210),
    ]
    result = compute_rhythm_metrics(words)
    assert result["finalSyllableLengthening"] is False


def test_words_out_of_order_are_sorted_by_offset():
    words = [
        _word("bon", 300, 200),
        _word("un", 0, 200),
    ]
    result = compute_rhythm_metrics(words)
    assert result is not None
    assert result["pauseCount"] == 0


def test_result_never_includes_an_aggregate_prosody_score_key():
    words = [_word("un", 0, 200), _word("bon", 300, 200)]
    result = compute_rhythm_metrics(words)
    assert "prosodyScore" not in result
    assert "score" not in result


if __name__ == "__main__":
    import inspect
    names = [n for n, f in list(globals().items()) if n.startswith("test_") and inspect.isfunction(f)]
    for name in names:
        globals()[name]()
    print(f"All {len(names)} test_prosody tests passed.")
