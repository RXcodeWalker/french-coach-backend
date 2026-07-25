"""Azure AI Speech Pronunciation Assessment client.

Plain HTTP via httpx — consistent with every other provider integration in
this backend. Do NOT add `azure-cognitiveservices-speech` (a native-compiled
SDK); it would be the only non-HTTP provider dependency here.

Returns None (not an exception) when AZURE_SPEECH_KEY/AZURE_SPEECH_REGION are
unset, so callers can fall through to the whisper-heuristic tier without a
try/except for the "not configured" case specifically.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

import httpx

# Azure's own vocabulary — allowed to appear ONLY in this module.
_ERROR_TYPE_MAP: dict[str, str] = {
    "None": "correct",
    "Mispronunciation": "mispronounced",
    "Omission": "skipped",
    "Insertion": "extra",
}

# Placeholder threshold — tune once real data exists.
_LOW_ACCURACY_THRESHOLD = 60.0


def _is_configured() -> tuple[str, str] | None:
    key = os.getenv("AZURE_SPEECH_KEY", "").strip()
    region = os.getenv("AZURE_SPEECH_REGION", "").strip()
    if not key or not region:
        return None
    return key, region


def _severity_for(error_type: str, accuracy_score: float | None) -> str:
    if error_type in ("skipped",):
        return "high"
    if accuracy_score is not None and accuracy_score < 40:
        return "high"
    if error_type == "mispronounced":
        return "medium"
    return "low"


def _problem_text(word: str, error_type: str) -> str:
    if error_type == "mispronounced":
        return f"'{word}' was mispronounced"
    if error_type == "skipped":
        return f"Word '{word}' was not heard"
    if error_type == "extra":
        return f"An extra word was heard near '{word}'"
    return f"'{word}' needs practice"


def _normalize_azure_response(raw_json: dict[str, Any], target_text: str) -> dict[str, Any]:
    """Pure mapping: Azure's NBest[0] shape -> this module's own vocabulary.

    Maps AccuracyScore/FluencyScore/CompletenessScore/PronScore -> subScores +
    score (already 0-100, no rescale), and Words[].ErrorType
    (None/Mispronunciation/Omission/Insertion) -> correct/mispronounced/
    skipped/extra. If Azure doesn't return IPA strings for fr-FR, ipaExpected/
    ipaHeard are left empty rather than invented.
    """
    n_best = raw_json.get("NBest") or []
    best = n_best[0] if n_best else {}
    assessment = best.get("PronunciationAssessment") or {}
    transcript = (raw_json.get("DisplayText") or best.get("Display") or "").strip()

    sub_scores = {
        "accuracy": float(assessment.get("AccuracyScore", 0.0)),
        "fluency": float(assessment.get("FluencyScore", 0.0)),
        "completeness": float(assessment.get("CompletenessScore", 0.0)),
    }
    score = round(float(assessment.get("PronScore", 0.0)))

    words_out: list[dict[str, Any]] = []
    issues_out: list[dict[str, Any]] = []
    for w in best.get("Words") or []:
        word = (w.get("Word") or "").strip()
        if not word:
            continue
        w_assessment = w.get("PronunciationAssessment") or {}
        azure_error = w_assessment.get("ErrorType", "None")
        error_type = _ERROR_TYPE_MAP.get(azure_error, "correct")
        accuracy_score = w_assessment.get("AccuracyScore")
        accuracy_score = float(accuracy_score) if accuracy_score is not None else None

        words_out.append({
            "word": word,
            "accuracyScore": accuracy_score,
            "errorType": error_type,
            "confidence": None,
        })

        needs_issue = error_type != "correct" or (
            accuracy_score is not None and accuracy_score < _LOW_ACCURACY_THRESHOLD
        )
        if needs_issue:
            severity = _severity_for(error_type, accuracy_score)
            issues_out.append({
                "word": word,
                "ipaExpected": "",
                "ipaHeard": "",
                "problem": _problem_text(word, error_type),
                "severity": severity,
                "drill": {
                    "hint": f"Practise '{word}' slowly, then say it in the full phrase.",
                    "repeatPhrase": target_text,
                },
                "expected": word,
                "heard": None if error_type == "skipped" else word,
            })

    return {
        "score": max(0, min(100, score)),
        "transcript": transcript,
        "issues": issues_out,
        "words": words_out,
        "provider": "azure",
        "subScores": sub_scores,
    }


async def _post_to_azure(key: str, region: str, audio_bytes: bytes, target_text: str) -> dict[str, Any]:
    pronunciation_params = {
        "ReferenceText": target_text,
        "GradingSystem": "HundredMark",
        "Granularity": "Phoneme",
        "Dimension": "Comprehensive",
    }
    header_value = base64.b64encode(json.dumps(pronunciation_params).encode("utf-8")).decode("ascii")

    url = f"https://{region}.stt.speech.microsoft.com/speech/recognition/conversation/cognitiveservices/v1"
    params = {"language": "fr-FR", "format": "detailed"}
    headers = {
        "Ocp-Apim-Subscription-Key": key,
        "Content-Type": "audio/webm; codecs=opus",
        "Accept": "application/json",
        "Pronunciation-Assessment": header_value,
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(url, params=params, headers=headers, content=audio_bytes)

    if response.status_code in (401, 403):
        # Non-retryable: a bad key should fail straight to fallback.
        raise PermissionError(f"Azure Speech auth failed: {response.status_code}")
    response.raise_for_status()
    return response.json()


async def assess_pronunciation(
    audio_bytes: bytes,
    target_text: str,
    *,
    run_with_retries=None,
) -> dict[str, Any] | None:
    """POST audio to Azure Pronunciation Assessment. Returns the normalized
    dict, or None if Azure isn't configured. Raises on request/auth failure
    (caller's fallback chain is responsible for catching).

    `run_with_retries`, when provided, is main.py's `_run_with_retries` —
    injected rather than imported, to avoid a circular import (main.py ->
    routers.pronunciation -> this module -> main.py). Falls back to a single
    direct call when not provided (e.g. in offline unit tests).
    """
    configured = _is_configured()
    if configured is None:
        return None
    key, region = configured

    async def operation() -> dict[str, Any]:
        return await _post_to_azure(key, region, audio_bytes, target_text)

    raw_json = await (run_with_retries("azure-speech", operation) if run_with_retries else operation())
    return _normalize_azure_response(raw_json, target_text)
