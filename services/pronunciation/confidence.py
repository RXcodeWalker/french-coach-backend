"""Confidence scoring (accent-analyzer plan §11).

overall = w1*snr_factor + w2*azure_confidence + w3*transcript_agreement
          + w4*duration_adequacy

Weights are UNVALIDATED placeholders until real calibration data exists —
same convention as src/domain/pronunciation/practiceThresholds.ts and
azure_client.py's _LOW_ACCURACY_THRESHOLD. Per-finding confidence
(phonology findings) is separate and ceilinged by the capability matrix;
this module computes only the response-level `confidence.overall`.

Required invariant (plan §17, test_confidence.py): monotonicity — worse SNR
or lower transcript agreement must never RAISE confidence. This is checked
directly by the tests below, not just asserted in prose.
"""

from __future__ import annotations

import difflib

# UNVALIDATED weights (plan §11) — sum to 1.0 so `overall` stays in [0, 1]
# when every term is already normalised to [0, 1].
_W_SNR = 0.25
_W_AZURE_CONFIDENCE = 0.35
_W_TRANSCRIPT_AGREEMENT = 0.30
_W_DURATION_ADEQUACY = 0.10

# SNR floor/ceiling for normalising to [0, 1] — below the floor, audio is
# unusably noisy; above the ceiling, additional SNR stops mattering.
_SNR_FLOOR_DB = 0.0
_SNR_CEILING_DB = 30.0

# Below this, a clip is too short to assess reliably (plan §9: reject <0.4s
# clips client-side; this is the server-side confidence-side echo of that).
_MIN_ADEQUATE_DURATION_MS = 400
_FULL_ADEQUACY_DURATION_MS = 2000


def _snr_factor(snr_db: float | None) -> float:
    if snr_db is None:
        return 0.5  # unknown SNR: neutral, neither penalised nor rewarded
    clamped = max(_SNR_FLOOR_DB, min(_SNR_CEILING_DB, snr_db))
    return (clamped - _SNR_FLOOR_DB) / (_SNR_CEILING_DB - _SNR_FLOOR_DB)


def _duration_adequacy(duration_ms: float | None) -> float:
    if duration_ms is None:
        return 0.5
    if duration_ms <= _MIN_ADEQUATE_DURATION_MS:
        return 0.0
    if duration_ms >= _FULL_ADEQUACY_DURATION_MS:
        return 1.0
    span = _FULL_ADEQUACY_DURATION_MS - _MIN_ADEQUATE_DURATION_MS
    return (duration_ms - _MIN_ADEQUATE_DURATION_MS) / span


def transcript_agreement(whisper_text: str, azure_text: str) -> float:
    """Normalised Levenshtein-style similarity ratio between the two
    engines' transcripts (plan §11: "the most informative term"). Uses
    difflib.SequenceMatcher, already the codebase's chosen diff mechanism
    for pronunciation alignment (fallback.py's whisper-heuristic tier uses
    the same library) — not a new dependency."""
    a = (whisper_text or "").strip().lower()
    b = (azure_text or "").strip().lower()
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def compute_confidence(
    *,
    snr_db: float | None,
    azure_confidence: float | None,
    whisper_text: str,
    azure_text: str,
    duration_ms: float | None,
) -> dict[str, float | list[str] | None]:
    """Returns a PronunciationConfidence-shaped dict. `azure_confidence` is
    NBest[0].Confidence (0-1) — None when unavailable (e.g. whisper-heuristic
    tier, which never calls Azure), treated as neutral (0.5) rather than
    penalising a tier for a metric it cannot structurally provide."""
    agreement = transcript_agreement(whisper_text, azure_text)
    azure_conf_term = azure_confidence if azure_confidence is not None else 0.5

    overall = (
        _W_SNR * _snr_factor(snr_db)
        + _W_AZURE_CONFIDENCE * azure_conf_term
        + _W_TRANSCRIPT_AGREEMENT * agreement
        + _W_DURATION_ADEQUACY * _duration_adequacy(duration_ms)
    )
    overall = max(0.0, min(1.0, round(overall, 3)))

    basis = ["transcript_agreement", "duration_adequacy"]
    if snr_db is not None:
        basis.append("snr")
    if azure_confidence is not None:
        basis.append("azure_confidence")

    return {
        "overall": overall,
        "basis": basis,
        "transcriptAgreement": round(agreement, 3),
    }
