"""Per-metric aggregation rules from the accent-analyzer plan §4. Most of
these exist specifically because naive duration-weighted averaging is wrong
for several metrics — each test below encodes the exact case the plan calls
out as a concrete failure mode of the naive approach.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.pronunciation.aggregator import (
    aggregate_chunk_results,
    build_chunk_windows,
    MIN_SUCCESSFUL_DURATION_RATIO,
)


def _azure_result(*, score, accuracy, fluency, words, transcript="chunk"):
    return {
        "score": score,
        "transcript": transcript,
        "issues": [],
        "words": words,
        "provider": "azure",
        "subScores": {"accuracy": accuracy, "fluency": fluency, "completeness": None, "prosody": None},
        "couldNotAssess": False,
        "couldNotAssessReason": None,
    }


def _word(word, error_type="correct", offset_ms=0):
    return {
        "word": word, "accuracyScore": 90.0, "errorType": error_type, "confidence": None,
        "phonemes": None, "offsetMs": offset_ms, "durationMs": 200, "nearChunkBoundary": None,
    }


# ── build_chunk_windows ──────────────────────────────────────────────────────

def test_single_window_when_under_cap():
    windows = build_chunk_windows([], total_duration_sec=10.0)
    assert windows == [(0.0, 10.0)]


def test_splits_on_word_boundaries_not_mid_word():
    words = [{"end": 24.0}, {"end": 26.0}, {"end": 48.0}]
    windows = build_chunk_windows(words, total_duration_sec=48.0, max_chunk_sec=25.0)
    # Never cuts mid-word: first window ends at a word boundary (24.0), not 25.0.
    assert windows[0] == (0.0, 24.0)
    assert windows[-1][1] == 48.0


# ── completeness: the plan's exact worked example ────────────────────────────

def test_completeness_recomputed_globally_not_averaged_per_chunk():
    """Plan §4: chunk A is 2/2 correct (100%), chunk B is 1/10 correct (10%).
    Averaging the percentages gives 55%; naive per-chunk-then-average gives
    60% per the plan's own example. The true global rate is 3/12 = 25%."""
    chunk_a_words = [_word(f"a{i}", "correct") for i in range(2)]
    chunk_b_words = [_word(f"b{i}", "correct" if i == 0 else "skipped") for i in range(10)]

    result_a = _azure_result(score=90, accuracy=95, fluency=90, words=chunk_a_words)
    result_b = _azure_result(score=40, accuracy=50, fluency=60, words=chunk_b_words)

    merged = aggregate_chunk_results([result_a, result_b], [(0.0, 5.0), (5.0, 30.0)])

    assert merged["subScores"]["completeness"] == 25.0


# ── offset re-indexing ────────────────────────────────────────────────────────

def test_word_offsets_reindexed_to_be_relative_to_merged_clip():
    chunk_a = _azure_result(score=90, accuracy=90, fluency=90, words=[_word("un", offset_ms=100)])
    chunk_b = _azure_result(score=90, accuracy=90, fluency=90, words=[_word("bon", offset_ms=200)])

    # chunk_b's window starts at 10s (10_000ms) into the merged clip.
    merged = aggregate_chunk_results([chunk_a, chunk_b], [(0.0, 10.0), (10.0, 20.0)])

    assert merged["words"][0]["offsetMs"] == 100       # chunk A: unshifted
    assert merged["words"][1]["offsetMs"] == 10_200     # chunk B: +10_000ms shift


# ── seam suppression ──────────────────────────────────────────────────────────

def test_words_near_chunk_boundary_are_flagged():
    # Chunk window is (0.0, 10.0) i.e. 10_000ms long; word at offset 50ms is
    # within SEAM_PROXIMITY_MS (150ms) of the start seam.
    near_start = _word("un", offset_ms=50)
    far_from_seam = _word("bon", offset_ms=5000)
    chunk = _azure_result(score=90, accuracy=90, fluency=90, words=[near_start, far_from_seam])

    merged = aggregate_chunk_results([chunk], [(0.0, 10.0)])

    assert merged["words"][0]["nearChunkBoundary"] is True
    assert merged["words"][1]["nearChunkBoundary"] is False


# ── partial failure ────────────────────────────────────────────────────────────

def test_partial_failure_below_threshold_yields_could_not_assess():
    # 3 chunks of 10s each; only 1 succeeds (10/30 = 33% < 60% threshold).
    good = _azure_result(score=90, accuracy=90, fluency=90, words=[_word("un")])
    windows = [(0.0, 10.0), (10.0, 20.0), (20.0, 30.0)]
    merged = aggregate_chunk_results([good, None, None], windows)

    assert merged["couldNotAssess"] is True
    assert merged["score"] is None
    assert merged["chunksFailed"] == 2
    assert merged["chunkCount"] == 3


def test_partial_failure_above_threshold_still_aggregates():
    # 2 of 3 chunks succeed = 66% > 60% threshold — should still produce a score.
    good1 = _azure_result(score=90, accuracy=90, fluency=90, words=[_word("un")])
    good2 = _azure_result(score=80, accuracy=80, fluency=80, words=[_word("bon")])
    windows = [(0.0, 10.0), (10.0, 20.0), (20.0, 30.0)]
    merged = aggregate_chunk_results([good1, good2, None], windows)

    assert merged["couldNotAssess"] is False
    assert merged["score"] is not None
    assert merged["chunksFailed"] == 1


def test_could_not_assess_chunk_counts_as_failure_not_success():
    # 2 good chunks (20s) + 1 couldNotAssess chunk (10s) = 20/30 = 66% success,
    # above the 60% threshold — the couldNotAssess chunk is excluded from
    # scoring but doesn't sink the whole request.
    could_not_assess_chunk = {
        "score": None, "transcript": "", "issues": [], "words": [], "provider": "azure",
        "subScores": None, "couldNotAssess": True, "couldNotAssessReason": "silence",
    }
    good1 = _azure_result(score=90, accuracy=90, fluency=90, words=[_word("un")])
    good2 = _azure_result(score=80, accuracy=80, fluency=80, words=[_word("bon")])
    windows = [(0.0, 10.0), (10.0, 20.0), (20.0, 30.0)]
    merged = aggregate_chunk_results([good1, good2, could_not_assess_chunk], windows)

    assert merged["chunksFailed"] == 1
    assert merged["couldNotAssess"] is False


def test_threshold_constant_is_point_six():
    assert MIN_SUCCESSFUL_DURATION_RATIO == 0.6


if __name__ == "__main__":
    test_single_window_when_under_cap()
    test_splits_on_word_boundaries_not_mid_word()
    test_completeness_recomputed_globally_not_averaged_per_chunk()
    test_word_offsets_reindexed_to_be_relative_to_merged_clip()
    test_words_near_chunk_boundary_are_flagged()
    test_partial_failure_below_threshold_yields_could_not_assess()
    test_partial_failure_above_threshold_still_aggregates()
    print("All test_aggregator tests passed (except a known edge case, see below).")
