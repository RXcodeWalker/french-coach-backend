"""Grounded pronunciation coaching narrator (accent-analyzer plan §8).

Turns `PhonologicalFinding` dicts (from services/phonology/rules.py) into a
short, specific summary + one topPriority + <=3 tips, using the existing
Groq -> Gemini template chain (mirrors main.py's _call_groq/_call_gemini
pattern, own system prompt rather than reusing get_groq()/get_gemini(),
which pin main.py's unrelated feedback SYSTEM_PROMPT).

Anti-fabrication gate (plan §8, mirrors main.py's
_drop_unevidenced_grammar_items): the prompt is given ONLY the structured
findings, and a post-validator drops any tip/summary/topPriority sentence
that names a French word absent from the findings list. This is the same
pattern already used for grammar items — apply it here rather than invent
a new mechanism.

Delivered via a second, optional request keyed by a findings hash (plan R5)
— wiring lives in routers/pronunciation.py, not here. This module is pure
except for the two network calls, so it degrades to a template summary
built from each finding's own `explanation` (already authored per category
in data/phonology/fr.json) whenever the LLM is unavailable or its output
fails the gate. `grounded=False` marks a template fallback.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Awaitable, Callable

log = logging.getLogger("uvicorn.error")

_SYSTEM_PROMPT = (
    "You are a French pronunciation coach for Cambridge IGCSE learners. "
    "You will be given a JSON list of specific pronunciation findings — each "
    "names one word and one concrete issue. Write coaching that mentions "
    "ONLY the words and issues present in that list. Never invent an error, "
    "a word, or a sound that is not in the findings. Return ONLY JSON."
)

_CATEGORY_LABELS: dict[str, str] = {
    "nasalVowel": "nasal vowels",
    "frenchR": "the French R",
    "silentLetter": "silent final letters",
    "liaison": "liaison",
    "elision": "elision",
    "vowelQuality": "vowel quality",
}


def _findings_words(findings: list[dict[str, Any]]) -> set[str]:
    """Every word/word-pair token that a finding is grounded in — the
    vocabulary a coaching sentence is allowed to mention. Liaison findings
    store "wordA wordB" as their `word`, so both halves count."""
    words: set[str] = set()
    for finding in findings:
        for token in str(finding.get("word", "")).split():
            cleaned = token.strip(".,!?;:'’«»").lower()
            if cleaned:
                words.add(cleaned)
    return words


def _mentions_ungrounded_word(text: str, allowed_words: set[str]) -> bool:
    """True if `text` quotes a French word (inside «» or '' quotes — the
    codebase's existing evidence-marker convention, see main.py's
    _EVIDENCE_MARKER) that is not one of the findings' own words. Plain
    prose with no quoted word is never flagged — only a quoted claim is a
    checkable, droppable claim."""
    quoted = re.findall(r"[«‘']([^»’']{1,40})[»’']", text)
    for quote in quoted:
        token = quote.strip().lower()
        if token and token not in allowed_words:
            return True
    return False


def _build_prompt(findings: list[dict[str, Any]]) -> str:
    payload = [
        {"category": f.get("category"), "word": f.get("word"), "explanation": f.get("explanation")}
        for f in findings
    ]
    return (
        f"FINDINGS:\n{json.dumps(payload, ensure_ascii=False)}\n\n"
        "Return ONLY this JSON (nothing else):\n"
        "{\n"
        '  "summary": "<1-2 sentences on the overall pronunciation pattern, '
        'quoting only words from FINDINGS>",\n'
        '  "topPriority": "<the single most important finding to fix next, '
        'quoting only a word from FINDINGS>",\n'
        '  "tips": ["<actionable tip 1>", "<actionable tip 2>", '
        '"<actionable tip 3, optional>"]\n'
        "}"
    )


def _template_fallback(findings: list[dict[str, Any]]) -> dict[str, Any]:
    """No-LLM (or gate-failed) degrade: built entirely from the findings'
    own pre-authored `explanation` strings — always grounded, since it names
    nothing not already coming from the findings themselves."""
    if not findings:
        return {
            "summary": "No specific pronunciation issues were detected this time.",
            "topPriority": "Keep practising at your current pace.",
            "tips": [],
            "grounded": False,
        }
    top = findings[0]
    category_counts: dict[str, int] = {}
    for f in findings:
        cat = f.get("category", "")
        category_counts[cat] = category_counts.get(cat, 0) + 1
    summary_parts = [
        f"{count} {_CATEGORY_LABELS.get(cat, cat)} issue(s)"
        for cat, count in category_counts.items()
    ]
    summary = "This attempt had " + ", ".join(summary_parts) + "."
    tips = [f.get("explanation", "") for f in findings[:3] if f.get("explanation")]
    return {
        "summary": summary,
        "topPriority": top.get("explanation", ""),
        "tips": tips,
        "grounded": False,
    }


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in model response")
    return json.loads(text[start:end + 1])


def _apply_gate(raw: dict[str, Any], allowed_words: set[str]) -> dict[str, Any] | None:
    """Drops the whole LLM result (falls back to the template) if summary,
    topPriority, or ANY tip names a word absent from the findings — item-
    level dropping (as main.py's grammar gate does) isn't meaningful here
    since summary/topPriority are single required fields, not a list."""
    summary = str(raw.get("summary", ""))
    top_priority = str(raw.get("topPriority", ""))
    tips = [str(t) for t in raw.get("tips", []) if t]

    if _mentions_ungrounded_word(summary, allowed_words):
        return None
    if _mentions_ungrounded_word(top_priority, allowed_words):
        return None
    kept_tips = [t for t in tips if not _mentions_ungrounded_word(t, allowed_words)]
    if len(kept_tips) < len(tips):
        return None

    return {
        "summary": summary,
        "topPriority": top_priority,
        "tips": kept_tips[:3],
        "grounded": True,
    }


async def generate_coaching(
    findings: list[dict[str, Any]],
    *,
    call_groq: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
    call_gemini: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Returns a PronunciationCoaching-shaped dict. `call_groq`/`call_gemini`
    are injected (mirrors routers/pronunciation.py's DI seam) so this module
    never imports main.py. Falls back to the template on any LLM failure or
    anti-fabrication gate failure — coaching never blocks assessment."""
    allowed_words = _findings_words(findings)
    prompt = _build_prompt(findings)

    for caller in (call_groq, call_gemini):
        if caller is None:
            continue
        try:
            raw = await caller(prompt)
            gated = _apply_gate(raw, allowed_words)
            if gated is not None:
                return gated
            log.warning("coach_narrator: LLM output failed anti-fabrication gate, falling back to template")
        except Exception as e:
            log.warning("coach_narrator: LLM call failed, trying next provider: %s", e)

    return _template_fallback(findings)


def findings_hash(findings: list[dict[str, Any]]) -> str:
    """Cache key for the coaching cache (plan R5: "keyed by findings hash").
    Sorted for order-independence, since aggregation order isn't a coaching-
    relevant distinction."""
    import hashlib

    normalized = sorted(
        (f.get("category", ""), f.get("word", ""), f.get("explanation", ""))
        for f in findings
    )
    digest_input = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(digest_input.encode("utf-8")).hexdigest()
