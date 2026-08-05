"""Tests for services/pronunciation/confidence.py (accent-analyzer plan §11).

The mandatory invariant (plan §17): monotonicity — worse SNR or lower
transcript agreement must never RAISE confidence. Weights are UNVALIDATED
placeholders; these tests check ordering/behavioural properties, not exact
calibrated values.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.pronunciation.confidence import compute_confidence, transcript_agreement


def _confidence(snr_db=20.0, azure_confidence=0.9, whisper_text="un bon vin", azure_text="un bon vin", duration_ms=2000):
    return compute_confidence(
        snr_db=snr_db,
        azure_confidence=azure_confidence,
        whisper_text=whisper_text,
        azure_text=azure_text,
        duration_ms=duration_ms,
    )


def test_overall_confidence_in_zero_to_one_range():
    result = _confidence()
    assert 0.0 <= result["overall"] <= 1.0


def test_identical_transcripts_yield_agreement_of_one():
    assert transcript_agreement("un bon vin", "un bon vin") == 1.0


def test_completely_different_transcripts_yield_low_agreement():
    agreement = transcript_agreement("bonjour tout le monde", "au revoir")
    assert agreement < 0.5


def test_empty_vs_nonempty_transcript_yields_zero_agreement():
    assert transcript_agreement("", "un bon vin") == 0.0


def test_both_empty_transcripts_yield_full_agreement():
    assert transcript_agreement("", "") == 1.0


# ── Monotonicity: worse SNR never raises confidence ─────────────────────────

def test_lower_snr_never_raises_confidence():
    high_snr = _confidence(snr_db=25.0)
    low_snr = _confidence(snr_db=5.0)
    assert low_snr["overall"] <= high_snr["overall"]


def test_snr_monotonic_across_a_range():
    snr_values = [0.0, 5.0, 10.0, 15.0, 20.0, 25.0, 30.0]
    results = [_confidence(snr_db=v)["overall"] for v in snr_values]
    for i in range(len(results) - 1):
        assert results[i] <= results[i + 1], (snr_values[i], snr_values[i + 1], results)


# ── Monotonicity: worse transcript agreement never raises confidence ────────

def test_lower_transcript_agreement_never_raises_confidence():
    high_agreement = _confidence(whisper_text="un bon vin blanc", azure_text="un bon vin blanc")
    low_agreement = _confidence(whisper_text="un bon vin blanc", azure_text="complètement différent")
    assert low_agreement["overall"] <= high_agreement["overall"]
    assert low_agreement["transcriptAgreement"] <= high_agreement["transcriptAgreement"]


# ── Monotonicity: shorter duration never raises confidence ──────────────────

def test_shorter_duration_never_raises_confidence():
    long_clip = _confidence(duration_ms=3000)
    short_clip = _confidence(duration_ms=300)
    assert short_clip["overall"] <= long_clip["overall"]


# ── Monotonicity: lower azure confidence never raises overall confidence ────

def test_lower_azure_confidence_never_raises_overall():
    high = _confidence(azure_confidence=0.95)
    low = _confidence(azure_confidence=0.2)
    assert low["overall"] <= high["overall"]


# ── Missing data handled without penalising a tier that structurally lacks it ─

def test_missing_snr_is_neutral_not_penalised_to_zero():
    result = _confidence(snr_db=None)
    assert result["overall"] > 0.0
    assert "snr" not in result["basis"]


def test_missing_azure_confidence_is_neutral_not_penalised_to_zero():
    result = _confidence(azure_confidence=None)
    assert result["overall"] > 0.0
    assert "azure_confidence" not in result["basis"]


def test_basis_always_includes_transcript_agreement_and_duration():
    result = _confidence()
    assert "transcript_agreement" in result["basis"]
    assert "duration_adequacy" in result["basis"]


if __name__ == "__main__":
    import inspect
    names = [n for n, f in list(globals().items()) if n.startswith("test_") and inspect.isfunction(f)]
    for name in names:
        globals()[name]()
    print(f"All {len(names)} test_confidence tests passed.")
