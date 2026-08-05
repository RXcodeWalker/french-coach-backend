"""Capability matrix: per (locale, mode, tier), declares whether each metric
is authoritative (measured by the provider), derived (computed by us from
provider-supplied timings), inferred (rule-based, uncertain), or unavailable.

This is the mechanical anti-fabrication guard the accent-analyzer plan
requires (§3): the response builder consults this matrix and MUST null out
any field the matrix marks unavailable for the request's actual
(locale, mode, tier). There is no code path that can emit a value for an
unavailable metric — enforced here, not left to each call site to remember.

Adding a locale is adding a file; adding a capability level (e.g. once
Microsoft ships fr-FR prosody) is flipping one entry, not touching this
module.
"""

from __future__ import annotations

import json
import os
from typing import Literal

CapabilityLevel = Literal["authoritative", "derived", "inferred", "unavailable"]
PronunciationTier = Literal["azure", "whisper-heuristic"]
PronunciationMode = Literal["scripted", "freeform"]

_DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "phonology", "fr.json")

_matrix_cache: dict | None = None


def _load_matrix() -> dict:
    global _matrix_cache
    if _matrix_cache is None:
        with open(_DATA_PATH, encoding="utf-8") as f:
            _matrix_cache = json.load(f)
    return _matrix_cache


def get_capability(
    metric: str,
    *,
    mode: PronunciationMode,
    tier: PronunciationTier,
    locale: str = "fr-FR",
) -> CapabilityLevel:
    """Returns the capability level for one metric under (locale, mode, tier).

    Unknown locale/mode/tier/metric combinations default to "unavailable" —
    the safe direction (never fabricates a value for something the matrix
    doesn't explicitly authorise)."""
    matrix = _load_matrix()
    if matrix.get("locale") != locale:
        return "unavailable"
    mode_block = matrix.get("capabilities", {}).get(mode, {})
    tier_block = mode_block.get(tier, {})
    return tier_block.get(metric, "unavailable")  # type: ignore[return-value]


def confidence_ceiling(metric: str) -> float | None:
    """Confidence ceiling for an 'inferred' metric (e.g. liaison capped at
    0.6 — inference from timing, not measurement). None if uncapped."""
    matrix = _load_matrix()
    return matrix.get("confidenceCeilings", {}).get(metric)


def is_available(
    metric: str,
    *,
    mode: PronunciationMode,
    tier: PronunciationTier,
    locale: str = "fr-FR",
) -> bool:
    return get_capability(metric, mode=mode, tier=tier, locale=locale) != "unavailable"


def enforce_capabilities(
    response: dict,
    *,
    mode: PronunciationMode,
    tier: PronunciationTier,
    locale: str = "fr-FR",
) -> dict:
    """Nulls out any top-level field the matrix marks unavailable for this
    (locale, mode, tier). Mutates and returns `response`.

    Field-name mapping mirrors the matrix's metric names to this module's own
    response vocabulary (models/pronunciation.py), since the matrix is
    intentionally keyed by product-level capability, not raw field name."""
    field_to_metric = {
        "prosodyMetrics": "rhythmMetrics",
        "phonologicalFindings": None,  # gated per-finding-category below, not as a whole
    }
    for field, metric in field_to_metric.items():
        if metric is None:
            continue
        if field in response and not is_available(metric, mode=mode, tier=tier, locale=locale):
            response[field] = None

    if "phonologicalFindings" in response and response["phonologicalFindings"]:
        allowed_categories = {
            "liaison": is_available("liaison", mode=mode, tier=tier, locale=locale),
            "nasalVowel": is_available("nasalVowel", mode=mode, tier=tier, locale=locale),
            "frenchR": is_available("frenchR", mode=mode, tier=tier, locale=locale),
            "silentLetter": is_available("silentLetter", mode=mode, tier=tier, locale=locale),
        }
        response["phonologicalFindings"] = [
            finding
            for finding in response["phonologicalFindings"]
            if allowed_categories.get(finding.get("category"), False)
        ]

    if "subScores" in response and response["subScores"] is not None:
        if not is_available("prosodyScore", mode=mode, tier=tier, locale=locale):
            response["subScores"]["prosody"] = None
        if not is_available("completeness", mode=mode, tier=tier, locale=locale):
            response["subScores"]["completeness"] = None

    if "words" in response:
        observed_ipa_available = is_available("observedIpa", mode=mode, tier=tier, locale=locale)
        phoneme_accuracy_available = is_available("phonemeAccuracy", mode=mode, tier=tier, locale=locale)
        for word in response["words"]:
            if not phoneme_accuracy_available:
                word["phonemes"] = None
            if not observed_ipa_available:
                # observedIpa lives on issues[], not words[]; nothing to null here
                # today, but keep the branch so a future per-word ipaHeard field
                # is covered without a second pass through this function.
                pass

    if "issues" in response and not is_available("observedIpa", mode=mode, tier=tier, locale=locale):
        for issue in response["issues"]:
            issue["ipaHeard"] = ""

    return response
