"""Mechanical anti-fabrication guard (accent-analyzer plan §3, §17): for
every (mode, tier) combination in the capability matrix, and every metric
the matrix marks 'unavailable', enforce_capabilities must null it out of a
response that (incorrectly) tried to populate it. This is what makes the
capability matrix load-bearing rather than documentation.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.pronunciation.capabilities import (
    enforce_capabilities,
    get_capability,
    is_available,
    _load_matrix,
)

MODES = ["scripted", "freeform"]
TIERS = ["azure", "whisper-heuristic"]


def _fully_populated_response() -> dict:
    """A response that (wrongly) has every optional field populated —
    enforcement must strip whatever the matrix says this (mode, tier)
    cannot produce."""
    return {
        "subScores": {"accuracy": 90.0, "fluency": 88.0, "completeness": 95.0, "prosody": 77.0},
        "prosodyMetrics": {"speechRateWpm": 120.0},
        "phonologicalFindings": [
            {"category": "liaison", "word": "les amis", "explanation": "x", "confidence": 0.6},
            {"category": "nasalVowel", "word": "bon", "explanation": "x", "confidence": 0.6},
            {"category": "frenchR", "word": "rouge", "explanation": "x", "confidence": 0.6},
            {"category": "silentLetter", "word": "petit", "explanation": "x", "confidence": 0.6},
        ],
        "words": [{"word": "bon", "phonemes": [{"phoneme": "b", "accuracyScore": 90.0}]}],
        "issues": [{"word": "bon", "ipaHeard": "b o~"}],
    }


def test_matrix_loads_and_has_both_modes_and_tiers():
    matrix = _load_matrix()
    assert matrix["locale"] == "fr-FR"
    for mode in MODES:
        for tier in TIERS:
            assert matrix["capabilities"][mode][tier]


def test_every_unavailable_metric_is_nulled_by_enforcement():
    matrix = _load_matrix()
    for mode in MODES:
        for tier in TIERS:
            metrics = matrix["capabilities"][mode][tier]
            response = _fully_populated_response()
            enforced = enforce_capabilities(response, mode=mode, tier=tier)

            for metric, level in metrics.items():
                if level != "unavailable":
                    continue
                if metric == "rhythmMetrics":
                    assert enforced["prosodyMetrics"] is None, (mode, tier, metric)
                elif metric == "prosodyScore":
                    assert enforced["subScores"]["prosody"] is None, (mode, tier, metric)
                elif metric == "completeness":
                    assert enforced["subScores"]["completeness"] is None, (mode, tier, metric)
                elif metric == "phonemeAccuracy":
                    assert all(w["phonemes"] is None for w in enforced["words"]), (mode, tier, metric)
                elif metric == "observedIpa":
                    assert all(i["ipaHeard"] == "" for i in enforced["issues"]), (mode, tier, metric)
                elif metric in ("liaison", "nasalVowel", "frenchR", "silentLetter"):
                    categories = {f["category"] for f in enforced["phonologicalFindings"]}
                    assert metric not in categories, (mode, tier, metric)


def test_azure_scripted_authoritative_fields_survive_enforcement():
    response = _fully_populated_response()
    enforced = enforce_capabilities(response, mode="scripted", tier="azure")
    assert enforced["subScores"]["accuracy"] == 90.0
    assert enforced["subScores"]["completeness"] == 95.0
    assert enforced["words"][0]["phonemes"] is not None


def test_unknown_locale_defaults_to_unavailable():
    assert get_capability("accuracy", mode="scripted", tier="azure", locale="es-ES") == "unavailable"
    assert is_available("accuracy", mode="scripted", tier="azure", locale="es-ES") is False


def test_unknown_metric_defaults_to_unavailable():
    assert get_capability("madeUpMetric", mode="scripted", tier="azure") == "unavailable"


if __name__ == "__main__":
    test_matrix_loads_and_has_both_modes_and_tiers()
    test_every_unavailable_metric_is_nulled_by_enforcement()
    test_azure_scripted_authoritative_fields_survive_enforcement()
    test_unknown_locale_defaults_to_unavailable()
    test_unknown_metric_defaults_to_unavailable()
    print("All test_capabilities tests passed.")
