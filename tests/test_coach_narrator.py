"""Tests for services/pronunciation/coach_narrator.py (accent-analyzer plan
§8). The mandatory test (plan §17): a mocked LLM naming a word absent from
findings gets dropped — the anti-fabrication gate must actually reject it,
not just document the intent to.

Plain sync test functions driving the async module via asyncio.run(),
matching this suite's existing convention (no pytest-asyncio config exists
in this repo) rather than introducing @pytest.mark.asyncio.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.pronunciation.coach_narrator import (
    findings_hash,
    generate_coaching,
)

_FINDINGS = [
    {
        "category": "nasalVowel",
        "word": "vin",
        "explanation": "'vin' has a nasal vowel that was denasalised.",
        "confidence": 0.6,
        "provenance": "inferred",
    },
    {
        "category": "frenchR",
        "word": "rouge",
        "explanation": "The French R in 'rouge' was mispronounced.",
        "confidence": 0.6,
        "provenance": "inferred",
    },
]


async def _groq_ok(prompt: str):
    return {
        "summary": "Watch the nasal vowel in «vin» and the R in «rouge».",
        "topPriority": "Fix the nasal vowel in «vin» first.",
        "tips": ["Practise «vin» in isolation.", "Practise «rouge» slowly."],
    }


async def _groq_fabricates_word(prompt: str):
    # "fromage" is not in _FINDINGS at all — a fabricated claim.
    return {
        "summary": "Watch out for «fromage», which was mispronounced.",
        "topPriority": "Fix the nasal vowel in «vin» first.",
        "tips": ["Practise «vin» in isolation."],
    }


async def _groq_fabricates_in_a_tip(prompt: str):
    return {
        "summary": "Watch the nasal vowel in «vin» and the R in «rouge».",
        "topPriority": "Fix the nasal vowel in «vin» first.",
        "tips": ["Practise «vin» in isolation.", "Also work on «chocolat»."],
    }


async def _groq_raises(prompt: str):
    raise RuntimeError("groq unreachable")


def test_grounded_llm_output_is_kept():
    result = asyncio.run(generate_coaching(_FINDINGS, call_groq=_groq_ok))
    assert result["grounded"] is True
    assert "vin" in result["summary"]
    assert len(result["tips"]) == 2


def test_fabricated_word_in_summary_falls_back_to_template():
    result = asyncio.run(generate_coaching(_FINDINGS, call_groq=_groq_fabricates_word))
    assert result["grounded"] is False
    # Template fallback is built only from findings' own explanations.
    assert "fromage" not in result["summary"]
    assert "fromage" not in result["topPriority"]
    for tip in result["tips"]:
        assert "fromage" not in tip


def test_fabricated_word_in_a_tip_falls_back_to_template():
    result = asyncio.run(generate_coaching(_FINDINGS, call_groq=_groq_fabricates_in_a_tip))
    assert result["grounded"] is False
    for tip in result["tips"]:
        assert "chocolat" not in tip


def test_llm_failure_falls_back_to_template_never_raises():
    result = asyncio.run(generate_coaching(_FINDINGS, call_groq=_groq_raises))
    assert result["grounded"] is False
    assert result["summary"]
    assert result["topPriority"]


def test_llm_failure_tries_gemini_before_template():
    result = asyncio.run(generate_coaching(_FINDINGS, call_groq=_groq_raises, call_gemini=_groq_ok))
    assert result["grounded"] is True
    assert "vin" in result["summary"]


def test_empty_findings_yields_no_error_template():
    result = asyncio.run(generate_coaching([], call_groq=_groq_ok))
    assert result["grounded"] is False
    assert result["tips"] == []


def test_template_fallback_names_top_finding_first():
    result = asyncio.run(generate_coaching(_FINDINGS, call_groq=_groq_raises))
    assert "vin" in result["topPriority"] or "nasal" in result["topPriority"].lower()


def test_findings_hash_is_order_independent():
    reversed_findings = list(reversed(_FINDINGS))
    assert findings_hash(_FINDINGS) == findings_hash(reversed_findings)


def test_findings_hash_changes_with_content():
    other = _FINDINGS + [{"category": "silentLetter", "word": "petit", "explanation": "x"}]
    assert findings_hash(_FINDINGS) != findings_hash(other)


if __name__ == "__main__":
    import inspect
    names = [n for n, f in list(globals().items()) if n.startswith("test_") and inspect.isfunction(f)]
    for name in names:
        globals()[name]()
    print(f"All {len(names)} test_coach_narrator tests passed.")
