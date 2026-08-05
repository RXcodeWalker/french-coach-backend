"""Language-agnostic phonology evaluator (accent-analyzer plan §6, R4).

Reads pure data from backend/data/phonology/<locale>.json — adding a
language means adding a data file, not touching this module. Operates on
the EXPECTED IPA sequence Azure returns per word/phoneme, not orthography
alone: the previous main.py heuristics (`is_nasal`: substring match on
"on"/"an"/"en"/"in"/"un"; `is_vibrant`: "r" in word) flagged *bonne*,
*personne*, *monotone* as nasal and every word containing "r" as a French-R
error — both unsound, both deleted. Every rule here triggers off IPA
phonemes and per-phoneme/per-word AccuracyScore, which is what Azure
actually measured.

All findings from this module carry provenance="inferred" and are ceilinged
by capabilities.confidence_ceiling() — timing/IPA-pattern inference, not a
direct measurement. Enforcement (capabilities.enforce_capabilities) is what
actually nulls categories the matrix marks unavailable for a given
(mode, tier); this module does not need to know about tiers at all, since
whisper-heuristic-tier words carry no `phonemes`/IPA and simply produce no
findings.
"""

from __future__ import annotations

import json
import os
from typing import Any

from services.pronunciation.capabilities import confidence_ceiling

_DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "phonology", "fr.json")

_rules_cache: dict | None = None


def _load_rules(locale: str = "fr-FR") -> dict:
    global _rules_cache
    if _rules_cache is None:
        with open(_DATA_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("locale") != locale:
            return {}
        _rules_cache = data.get("phonologyRules", {})
    return _rules_cache


def _finding(category: str, word: str, explanation: str) -> dict[str, Any]:
    return {
        "category": category,
        "word": word,
        "explanation": explanation,
        "confidence": confidence_ceiling(category) or 0.6,
        "provenance": "inferred",
    }


def _detect_nasal_vowels(words: list[dict[str, Any]], rules: dict) -> list[dict[str, Any]]:
    cfg = rules.get("nasalVowels")
    if not cfg:
        return []
    triggers = set(cfg.get("ipaTriggers", []))
    threshold = cfg.get("lowAccuracyThreshold", 60.0)
    findings = []
    for word in words:
        phonemes = word.get("phonemes") or []
        for phoneme in phonemes:
            symbol = phoneme.get("phoneme")
            accuracy = phoneme.get("accuracyScore")
            if symbol in triggers and accuracy is not None and accuracy < threshold:
                explanation = cfg["explanationTemplate"].format(word=word["word"], phoneme=symbol)
                findings.append(_finding("nasalVowel", word["word"], explanation))
    return findings


def _classify_r_position(phonemes: list[dict[str, Any]], index: int) -> str:
    is_first = index == 0
    is_last = index == len(phonemes) - 1
    if is_last:
        return "coda"
    if is_first:
        return "onset"
    return "cluster"


def _detect_french_r(words: list[dict[str, Any]], rules: dict) -> list[dict[str, Any]]:
    cfg = rules.get("frenchR")
    if not cfg:
        return []
    triggers = set(cfg.get("ipaTriggers", []))
    threshold = cfg.get("lowAccuracyThreshold", 60.0)
    positions = cfg.get("positions", {})
    findings = []
    for word in words:
        phonemes = word.get("phonemes") or []
        for i, phoneme in enumerate(phonemes):
            symbol = phoneme.get("phoneme")
            accuracy = phoneme.get("accuracyScore")
            if symbol in triggers and accuracy is not None and accuracy < threshold:
                position_key = _classify_r_position(phonemes, i)
                position_desc = positions.get(position_key, position_key)
                explanation = cfg["explanationTemplate"].format(word=word["word"], position=position_desc)
                findings.append(_finding("frenchR", word["word"], explanation))
    return findings


def _detect_silent_letters(words: list[dict[str, Any]], rules: dict) -> list[dict[str, Any]]:
    """Expected IPA has no phoneme for a final orthographic consonant, but
    Azure reports Insertion or an anomalous final-word duration — per plan
    §6, detection is anchored on Azure's own signal (errorType == 'extra',
    i.e. Insertion), not on spelling alone."""
    cfg = rules.get("silentLetters")
    if not cfg:
        return []
    final_consonants = set(cfg.get("finalConsonants", []))
    findings = []
    for word in words:
        text = word.get("word", "")
        if not text:
            continue
        last_letter = text[-1].lower()
        if last_letter not in final_consonants:
            continue
        if word.get("errorType") != "extra":
            continue
        explanation = cfg["explanationTemplate"].format(word=text, letter=last_letter)
        findings.append(_finding("silentLetter", text, explanation))
    return findings


def _detect_liaison(words: list[dict[str, Any]], rules: dict) -> list[dict[str, Any]]:
    """Cross-word: word i ends in a latent consonant, word i+1 starts with a
    vowel/mute-h, and the gap between them (offset[i+1] - (offset[i] +
    duration[i])) is small enough to imply the words were run together. This
    is inference from timing, not a direct measurement — ceilinged
    accordingly. Words within 150ms of a chunk seam are excluded per the
    aggregator's nearChunkBoundary flag (plan §4: cross-seam liaison is not
    assessed at all)."""
    cfg = rules.get("liaison")
    if not cfg:
        return []
    max_gap_ms = cfg.get("maxGapMs", 80)
    latent_consonants = ("s", "t", "d", "x", "z", "n")
    vowel_starts = ("a", "e", "i", "o", "u", "y", "h")
    findings = []
    for i in range(len(words) - 1):
        word_a, word_b = words[i], words[i + 1]
        if word_a.get("nearChunkBoundary") or word_b.get("nearChunkBoundary"):
            continue
        text_a, text_b = word_a.get("word", ""), word_b.get("word", "")
        if not text_a or not text_b:
            continue
        if not text_a.lower().endswith(latent_consonants):
            continue
        if not text_b.lower().startswith(vowel_starts):
            continue
        offset_a, duration_a = word_a.get("offsetMs"), word_a.get("durationMs")
        offset_b = word_b.get("offsetMs")
        if offset_a is None or duration_a is None or offset_b is None:
            continue
        gap_ms = offset_b - (offset_a + duration_a)
        if gap_ms > max_gap_ms:
            explanation = cfg["explanationTemplate"].format(wordA=text_a, wordB=text_b)
            findings.append(_finding("liaison", f"{text_a} {text_b}", explanation))
    return findings


def evaluate(words: list[dict[str, Any]], *, locale: str = "fr-FR") -> list[dict[str, Any]]:
    """Runs every phonology rule over a merged word timeline (post-
    aggregation, if chunked) and returns a flat list of PhonologicalFinding
    dicts. Words with no `phonemes` (whisper-heuristic tier) simply produce
    no phoneme-anchored findings — this function does not need to know about
    capability tiers; enforce_capabilities is the single place that nulls
    categories the matrix marks unavailable."""
    rules = _load_rules(locale)
    if not rules:
        return []
    findings: list[dict[str, Any]] = []
    findings.extend(_detect_nasal_vowels(words, rules))
    findings.extend(_detect_french_r(words, rules))
    findings.extend(_detect_silent_letters(words, rules))
    findings.extend(_detect_liaison(words, rules))
    return findings
