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

`generate_shadowing_coaching` (Phase 4 — Shadowing Mode, implementation plan
i-am-implementing-phase-sunny-lagoon.md §4) is a separate function added
alongside `generate_coaching`, not a variant of it: `generate_coaching`,
`_build_prompt`, `_apply_gate`, `_template_fallback`, and `findings_hash`
stay exactly as they were so /api/repair (main.py) keeps its current
behaviour including Gemini. The shadowing path takes no `call_gemini`
parameter at all, by signature, so Gemini can never be reintroduced into it.
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


_SHADOWING_SYSTEM_PROMPT = (
    "You are a French pronunciation coach for Cambridge IGCSE learners doing a "
    "shadowing exercise (listen to a model sentence, repeat it, get scored on "
    "how closely they matched it). You will be given the target sentence, "
    "which words the speech-assessment engine marked as problem words vs "
    "praise words, and (optionally) rhythm metrics. You may claim a problem "
    "ONLY about a word in the problem-words list, and praise ONLY a word in "
    "the praise-words list. Never invent a word, an error, or a strength not "
    "present in those lists. If rhythm metrics are not provided, you MUST "
    "NOT comment on rhythm at all (rhythmNote must be null). Return ONLY JSON."
)


def _shadowing_prompt(context: dict[str, Any], problem_words: set[str], praise_words: set[str]) -> str:
    payload = {
        "targetText": context.get("targetText"),
        "problemWords": sorted(problem_words),
        "praiseWords": sorted(praise_words),
        "rhythmMetrics": context.get("prosodyMetrics"),
    }
    return (
        f"CONTEXT:\n{json.dumps(payload, ensure_ascii=False)}\n\n"
        "Return ONLY this JSON (nothing else):\n"
        "{\n"
        '  "summary": "<1-2 sentences on the overall attempt, quoting only '
        'words from problemWords/praiseWords>",\n'
        '  "strengths": [{"word": "<a word from praiseWords>", "note": "<why it was good>"}],\n'
        '  "problems": [{"word": "<a word from problemWords>", "note": "<a concrete fix>"}],\n'
        '  "rhythmNote": "<a short note on pace/pauses, or null if rhythmMetrics is null>",\n'
        '  "nextRepetition": "<one actionable cue for the next attempt>"\n'
        "}"
    )


def _shadowing_template_fallback(problem_words: set[str], praise_words: set[str]) -> dict[str, Any]:
    """Grounded-by-construction degrade: names at most the words the caller
    already told us about, never anything drawn from the LLM."""
    if not problem_words:
        return {
            "summary": "Nice work — no specific pronunciation problems were flagged this time.",
            "topPriority": "Keep practising at your current pace.",
            "tips": [],
            "grounded": False,
        }
    top = sorted(problem_words)[0]
    tips = [f"Focus on «{w}» next time." for w in sorted(problem_words)[:3]]
    return {
        "summary": f"This attempt had trouble with: {', '.join(sorted(problem_words))}.",
        "topPriority": f"Focus on «{top}» next time.",
        "tips": tips,
        "grounded": False,
    }


def _apply_shadowing_gate(
    raw: dict[str, Any],
    *,
    problem_words: set[str],
    praise_words: set[str],
    target_words: set[str],
    has_rhythm: bool,
) -> dict[str, Any] | None:
    """Per-claim gate (plan §4, review item 5): unlike _apply_gate, this
    validates each problems[]/strengths[] entry against its OWN vocabulary
    (problem_words vs praise_words respectively), not a single combined
    allowed-words set — a claim that "you mispronounced X" must name an
    actual mispronunciation, not merely any word that appeared anywhere.
    All-or-nothing: any single violation drops the whole result."""
    summary = str(raw.get("summary", ""))
    next_repetition = str(raw.get("nextRepetition", ""))
    rhythm_note = raw.get("rhythmNote")
    strengths = raw.get("strengths") or []
    problems = raw.get("problems") or []

    if not isinstance(strengths, list) or not isinstance(problems, list):
        return None

    allowed_all = problem_words | praise_words | target_words

    if _mentions_ungrounded_word(summary, allowed_all):
        return None
    if _mentions_ungrounded_word(next_repetition, allowed_all):
        return None

    if not has_rhythm:
        if rhythm_note not in (None, ""):
            return None
        rhythm_note = None
    else:
        rhythm_note = str(rhythm_note) if rhythm_note else None

    kept_problems: list[dict[str, str]] = []
    for item in problems:
        if not isinstance(item, dict):
            return None
        word = str(item.get("word", "")).strip().lower()
        note = str(item.get("note", ""))
        if word not in problem_words:
            return None
        kept_problems.append({"word": word, "note": note})

    kept_strengths: list[dict[str, str]] = []
    for item in strengths:
        if not isinstance(item, dict):
            return None
        word = str(item.get("word", "")).strip().lower()
        note = str(item.get("note", ""))
        if word not in praise_words:
            return None
        kept_strengths.append({"word": word, "note": note})

    return {
        "summary": summary,
        "strengths": kept_strengths,
        "problems": kept_problems,
        "rhythmNote": rhythm_note,
        "nextRepetition": next_repetition,
    }


def _project_shadowing_result(gated: dict[str, Any]) -> dict[str, Any]:
    """Projects the structured intermediate JSON to the stable wire shape
    {summary, topPriority, tips, grounded} — the response contract does not
    change, so nothing downstream needs updating (plan §4)."""
    problems = gated["problems"]
    tips: list[str] = [p["note"] for p in problems[1:] if p.get("note")]
    if gated.get("rhythmNote"):
        tips.append(gated["rhythmNote"])
    if gated.get("nextRepetition"):
        tips.append(gated["nextRepetition"])
    tips = tips[:3]

    if problems and problems[0].get("note"):
        top_priority = problems[0]["note"]
    else:
        top_priority = gated.get("nextRepetition", "")

    return {
        "summary": gated.get("summary", ""),
        "topPriority": top_priority,
        "tips": tips,
        "grounded": True,
    }


async def generate_shadowing_coaching(
    context: dict[str, Any],
    findings: list[dict[str, Any]],
    *,
    call_groq: Callable[[str], Awaitable[dict[str, Any]]] | None,
) -> dict[str, Any]:
    """Shadowing-specific coaching narrator (plan §4, review item 5). Unlike
    generate_coaching, this validates each claim against a specific vocabulary
    (problem_words vs praise_words), derived from Azure-authoritative word
    results only — never from `findings` alone, since the existing gate
    validates which words are quoted, never what is claimed about them, and
    widening `allowed_words` to the whole target phrase would let the model
    assert "you mispronounced X" about a word Azure scored as correct.

    No `call_gemini` parameter at all, by signature — Gemini cannot be
    reintroduced into this path by a future wiring change. generate_coaching
    (used by /api/repair) is untouched.

    `context` is the shape _build_shadowing_context (routers/pronunciation.py)
    produces: {targetText, score, subScores, prosodyMetrics,
    mispronouncedWords: [{word, accuracyScore}], correctWords: [...], findings}."""
    mispronounced: list[dict[str, Any]] = context.get("mispronouncedWords") or []
    correct: list[dict[str, Any]] = context.get("correctWords") or []

    problem_words = {str(f.get("word", "")).strip().lower() for f in findings if f.get("word")}
    problem_words |= {
        str(w.get("word", "")).strip().lower()
        for w in mispronounced
        if w.get("word")
    }
    problem_words.discard("")

    praise_words = {
        str(w.get("word", "")).strip().lower()
        for w in correct
        if w.get("word")
    }
    praise_words.discard("")

    target_text = str(context.get("targetText", ""))
    target_words = {t.strip(".,!?;:'’«»").lower() for t in target_text.split()}
    target_words.discard("")

    has_rhythm = context.get("prosodyMetrics") is not None

    if call_groq is not None:
        prompt = _shadowing_prompt(context, problem_words, praise_words)
        try:
            raw = await call_groq(prompt)
            gated = _apply_shadowing_gate(
                raw,
                problem_words=problem_words,
                praise_words=praise_words,
                target_words=target_words,
                has_rhythm=has_rhythm,
            )
            if gated is not None:
                return _project_shadowing_result(gated)
            log.warning("coach_narrator: shadowing LLM output failed the per-claim gate, falling back to template")
        except Exception as e:
            log.warning("coach_narrator: shadowing LLM call failed: %s", e)

    return _shadowing_template_fallback(problem_words, praise_words)


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
