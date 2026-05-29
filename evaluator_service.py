"""
Full-exam evaluator implementing Cambridge IGCSE 0520 grading criteria.
Grades the complete structured transcript using best-fit, descriptor-based scoring.
This is a NEW system replacing the old 4-criteria model — used only by exam mode.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

log = logging.getLogger("french-coach.evaluator")

_EVAL_SYSTEM_PROMPT = """\
You are a Cambridge IGCSE French (0520) oral examiner applying the official mark scheme.
You will receive a complete exam transcript and must grade it across three criteria.

════════════════════════════════════════
CRITERION 1 — ROLE PLAY (0–10 marks)
════════════════════════════════════════
Score each of the 5 tasks independently:
  2 marks — Task FULLY achieved; meaning clear; minor errors allowed; short correct answers still score 2
  1 mark  — Task PARTIALLY achieved; meaning unclear or ambiguous; errors affect clarity
  0 marks — No relevant response; task not achieved

════════════════════════════════════════
CRITERION 2 — COMMUNICATION (0–15 marks)
════════════════════════════════════════
Scored HOLISTICALLY across BOTH topic conversations combined.

  High Band (11–15): Consistently relevant responses; detailed and extended answers;
                     clear opinions with justification; fluent interaction
  Mid Band  (6–10):  Mostly relevant; some development; opinions present but simple
  Low Band  (1–5):   Short responses; limited relevance; requires prompting;
                     meaning sometimes unclear

AI Heuristic:
  short + no opinions → Band 1–5
  relevant + some detail → Band 6–10
  extended + justified + fluent → Band 11–15

════════════════════════════════════════
CRITERION 3 — QUALITY OF LANGUAGE (0–15 marks)
════════════════════════════════════════
Scored HOLISTICALLY across BOTH topic conversations combined.

  Very Good (12–15): Complex grammar (multiple tenses, subordinate clauses);
                     broad vocabulary; high accuracy; fluent delivery
  Good      (8–11):  Good basic structures; attempts complex forms; some errors;
                     generally clear
  Weak      (1–7):   Simple repetitive sentences; frequent errors;
                     limited vocabulary; hesitation affects fluency

AI Feature Extraction:
  - Sentence complexity
  - Vocabulary diversity
  - Error frequency
  - Fluency / hesitation markers

════════════════════════════════════════
MANDATORY SCORING RULES
════════════════════════════════════════
• Apply POSITIVE MARKING — reward what the candidate CAN do; never deduct for errors
• Use BEST-FIT algorithm — start from lowest band, move upward, select the band
  that BEST describes overall performance
• Must justify ALL scores by referencing ACTUAL responses from the transcript
• Do NOT behave like a tutor; do NOT suggest improvements
• Scores must be integers

════════════════════════════════════════
GRADE BOUNDARIES (out of 40)
════════════════════════════════════════
A*: 36–40 | A: 32–35 | B: 27–31 | C: 22–26 | D: 16–21 | E: 10–15 | U: 0–9

════════════════════════════════════════
OUTPUT FORMAT — return valid JSON ONLY, no prose, no markdown fences
════════════════════════════════════════
{
  "roleplay_score": <integer 0–10>,
  "communication": <integer 0–15>,
  "quality": <integer 0–15>,
  "total": <integer 0–40>,
  "grade": "<A*|A|B|C|D|E|U>",
  "breakdown": {
    "roleplay_tasks": [
      {"task_id": 1, "score": <0|1|2>, "reasoning": "<cite actual response>"},
      {"task_id": 2, "score": <0|1|2>, "reasoning": "<cite actual response>"},
      {"task_id": 3, "score": <0|1|2>, "reasoning": "<cite actual response>"},
      {"task_id": 4, "score": <0|1|2>, "reasoning": "<cite actual response>"},
      {"task_id": 5, "score": <0|1|2>, "reasoning": "<cite actual response>"}
    ],
    "communication_reason": "<band name + justification citing actual responses>",
    "language_reason": "<band name + justification citing language features observed>"
  }
}"""


def _build_eval_prompt(transcript: dict[str, Any], card: dict[str, Any]) -> str:
    parts: list[str] = [
        "# COMPLETE EXAM TRANSCRIPT",
        "",
        f"## ROLE PLAY — {card.get('title', 'Jeu de rôle')}",
        f"Setting: {card.get('setting', '')}",
        "",
    ]
    roleplay = transcript.get("roleplay", [])
    if roleplay:
        for item in roleplay:
            parts.append(f"Task {item['task_id']}: {item['prompt']}")
            parts.append(f"Candidate: {item.get('response') or '(no response)'}")
            parts.append("")
    else:
        parts.append("(No roleplay responses recorded)")
        parts.append("")

    parts += ["## TOPIC CONVERSATION 1", ""]
    topic1 = transcript.get("topic1", [])
    if topic1:
        for item in topic1:
            parts.append(f"Examiner: {item['question']}")
            parts.append(f"Candidate: {item.get('response') or '(no response)'}")
            parts.append("")
    else:
        parts.append("(No topic 1 responses recorded)")
        parts.append("")

    parts += ["## TOPIC CONVERSATION 2", ""]
    topic2 = transcript.get("topic2", [])
    if topic2:
        for item in topic2:
            parts.append(f"Examiner: {item['question']}")
            parts.append(f"Candidate: {item.get('response') or '(no response)'}")
            parts.append("")
    else:
        parts.append("(No topic 2 responses recorded)")

    return "\n".join(parts)


def _grade_from_total(total: int) -> str:
    if total >= 36:
        return "A*"
    if total >= 32:
        return "A"
    if total >= 27:
        return "B"
    if total >= 22:
        return "C"
    if total >= 16:
        return "D"
    if total >= 10:
        return "E"
    return "U"


def _offline_exam_evaluation(transcript: dict[str, Any]) -> dict[str, Any]:
    roleplay = transcript.get("roleplay", [])
    topic1 = transcript.get("topic1", [])
    topic2 = transcript.get("topic2", [])
    all_responses = [
        (item.get("response") or "")
        for section in (roleplay, topic1, topic2)
        for item in section
    ]
    word_count = sum(len(re.findall(r"\b[\w'-]+\b", response, flags=re.UNICODE)) for response in all_responses)
    answered_roleplay = sum(1 for item in roleplay if (item.get("response") or "").strip())
    roleplay_score = min(10, answered_roleplay * 2)
    communication = 3 if word_count < 30 else 7 if word_count < 100 else 11
    quality = 3 if word_count < 30 else 7 if word_count < 100 else 10
    total = roleplay_score + communication + quality
    return {
        "roleplay_score": roleplay_score,
        "communication": communication,
        "quality": quality,
        "total": total,
        "grade": _grade_from_total(total),
        "breakdown": {
            "roleplay_tasks": [
                {
                    "task_id": item.get("task_id", idx + 1),
                    "score": 2 if (item.get("response") or "").strip() else 0,
                    "reasoning": "Offline estimate because AI evaluators were unavailable.",
                }
                for idx, item in enumerate(roleplay[:5])
            ],
            "communication_reason": "Offline estimate based on response length and completion.",
            "language_reason": "Offline estimate; detailed language analysis requires an AI provider.",
        },
        "providerStatus": "offline_fallback",
    }


def _strip_fences(raw: str) -> str:
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\s*```\s*$", "", raw, flags=re.MULTILINE)
    return raw.strip()


async def evaluate_full_exam(
    transcript: dict[str, Any],
    card: dict[str, Any],
    groq_client: Any = None,
    gemini_api_key: str = "",
) -> dict[str, Any]:
    """
    Send full transcript to AI and return structured evaluation matching grading_criteria.md.
    Tries Groq first, falls back to Gemini.
    """
    prompt = _build_eval_prompt(transcript, card)
    raw: str | None = None

    if groq_client:
        try:
            resp = await groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": _EVAL_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=2000,
            )
            raw = resp.choices[0].message.content.strip()
        except Exception as exc:
            log.warning("Evaluator Groq failed: %s", exc)

    if not raw and gemini_api_key:
        try:
            import google.generativeai as genai

            genai.configure(api_key=gemini_api_key)
            model = genai.GenerativeModel(
                "gemini-2.0-flash",
                system_instruction=_EVAL_SYSTEM_PROMPT,
            )
            resp = await asyncio.to_thread(model.generate_content, prompt)
            raw = resp.text.strip()
        except Exception as exc:
            log.warning("Evaluator Gemini failed: %s", exc)

    if not raw:
        log.warning("No AI evaluator available; returning offline exam estimate")
        return _offline_exam_evaluation(transcript)

    try:
        result = json.loads(_strip_fences(raw))
    except Exception as exc:
        log.warning("AI evaluator returned malformed JSON; using offline estimate: %s", exc)
        return _offline_exam_evaluation(transcript)

    # Recalculate total and grade from component scores for consistency
    rp = int(result.get("roleplay_score", 0))
    comm = int(result.get("communication", 0))
    qual = int(result.get("quality", 0))
    total = rp + comm + qual

    result["roleplay_score"] = rp
    result["communication"] = comm
    result["quality"] = qual
    result["total"] = total
    result["grade"] = _grade_from_total(total)

    return result
