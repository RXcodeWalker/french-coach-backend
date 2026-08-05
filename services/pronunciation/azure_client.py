"""Azure AI Speech Pronunciation Assessment client.

Plain HTTP via httpx — consistent with every other provider integration in
this backend. Do NOT add `azure-cognitiveservices-speech` (a native-compiled
SDK); it would be the only non-HTTP provider dependency here.

Returns None (not an exception) when AZURE_SPEECH_KEY/AZURE_SPEECH_REGION are
unset, so callers can fall through to the whisper-heuristic tier without a
try/except for the "not configured" case specifically.

Response shape: Azure's REST short-audio endpoint (what this module calls)
returns scores FLAT on NBest[0] and on each Word — NOT nested under a
"PronunciationAssessment" key. The nested shape only exists in the Speech
SDK's local object model, never in a real REST JSON body. This was verified
against a live resource on 2026-08-04 (see backend/scripts/_probe_output/);
accessors below are shape-tolerant (flat first, nested fallback) purely as a
defensive belt-and-braces measure, not because the nested shape is expected.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any

import httpx

log = logging.getLogger("uvicorn.error")

# Azure's own vocabulary — allowed to appear ONLY in this module.
_ERROR_TYPE_MAP: dict[str, str] = {
    "None": "correct",
    "Mispronunciation": "mispronounced",
    "Omission": "skipped",
    "Insertion": "extra",
}

# Placeholder threshold — tune once real data exists.
_LOW_ACCURACY_THRESHOLD = 60.0

# Azure REST short-audio endpoint accepts exactly these two Content-Types.
# See: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/rest-speech-to-text-short
_CONTENT_TYPE_BY_EXTENSION: dict[str, str] = {
    ".wav": "audio/wav; codecs=audio/pcm; samplerate=16000",
    ".ogg": "audio/ogg; codecs=opus",
}


def _content_type_for(filename_or_suffix: str) -> str:
    """Best-effort Content-Type for whatever audio was actually uploaded.

    Only .wav and .ogg map to an Azure-accepted value. Anything else (webm,
    mp4/aac — real browser MediaRecorder output) has no accepted mapping;
    Azure will very likely reject or silently degrade it. That is expected
    and unresolved until the client-side normalizer (accent-analyzer plan
    Phase 0 step 4) guarantees 16kHz mono WAV on every request. Sending an
    honest Content-Type here (rather than always claiming webm/opus, which
    was actively wrong for .wav bytes) is this module's whole fix for
    defect #1; it does not by itself make raw browser audio Azure-legal.
    """
    ext = os.path.splitext(filename_or_suffix)[1].lower()
    return _CONTENT_TYPE_BY_EXTENSION.get(ext, "audio/webm; codecs=opus")


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


def _pron_assessment(obj: dict[str, Any]) -> dict[str, Any]:
    """Shape-tolerant accessor: real REST responses put pronunciation scores
    FLAT on the object itself; only the SDK's local object model nests them
    under "PronunciationAssessment". Try flat fields first (the fast, common
    path), fall back to the nested key if present (defensive only — see
    module docstring)."""
    nested = obj.get("PronunciationAssessment")
    if isinstance(nested, dict) and nested:
        return nested
    return obj


# Couldn't-assess reasons — never a fabricated score.
_NO_MATCH_STATUSES = {
    "NoMatch": "no_speech_recognized",
    "InitialSilenceTimeout": "silence",
    "BabbleTimeout": "noise",
}


def _normalize_azure_response(raw_json: dict[str, Any], target_text: str) -> dict[str, Any]:
    """Pure mapping: Azure's REST response -> this module's own vocabulary.

    Maps AccuracyScore/FluencyScore/CompletenessScore/PronScore/ProsodyScore ->
    subScores + score (already 0-100, no rescale), and Words[].ErrorType
    (None/Mispronunciation/Omission/Insertion) -> correct/mispronounced/
    skipped/extra. ipaExpected is derived from Words[].Phonemes[].Phoneme
    (what the reference expects). ipaHeard comes ONLY from
    Phonemes[].NBestPhonemes[0] (what was actually heard, per-phoneme
    recognition N-best) — if NBestPhonemes is absent, ipaHeard is left empty
    rather than silently falling back to the expected phoneme (defect #3:
    "what you said" must never silently render as "what you should have
    said").

    RecognitionStatus is checked before anything else: a non-"Success"
    status, or a response with no assessable NBest content, becomes
    couldNotAssess — never score: 0 (defects #4, #8).
    """
    recognition_status = raw_json.get("RecognitionStatus")
    if recognition_status is not None and recognition_status != "Success":
        return {
            "score": None,
            "transcript": (raw_json.get("DisplayText") or "").strip(),
            "issues": [],
            "words": [],
            "provider": "azure",
            "subScores": None,
            "couldNotAssess": True,
            "couldNotAssessReason": _NO_MATCH_STATUSES.get(recognition_status, "assessment_unavailable"),
        }

    n_best = raw_json.get("NBest") or []
    best = n_best[0] if n_best else {}
    assessment = _pron_assessment(best)
    transcript = (raw_json.get("DisplayText") or best.get("Display") or "").strip()

    if not assessment or assessment.get("PronScore") is None:
        # 200 OK with plain STT output and no assessment block (defect #8) —
        # a malformed/rejected Pronunciation-Assessment header fails this way,
        # silently, rather than as an HTTP error. Never render as score: 0.
        return {
            "score": None,
            "transcript": transcript,
            "issues": [],
            "words": [],
            "provider": "azure",
            "subScores": None,
            "couldNotAssess": True,
            "couldNotAssessReason": "assessment_unavailable",
        }

    sub_scores = {
        "accuracy": float(assessment.get("AccuracyScore", 0.0)),
        "fluency": float(assessment.get("FluencyScore", 0.0)),
        "completeness": (
            float(assessment["CompletenessScore"]) if assessment.get("CompletenessScore") is not None else None
        ),
        "prosody": assessment.get("ProsodyScore"),
    }
    score = round(float(assessment.get("PronScore", 0.0)))

    # Previously discarded fields (plan §5): SNR gates confidence rather than
    # producing a bogus low score; NBest[0].Confidence feeds confidence.py's
    # azure_confidence term. Both optional — absent on some resource/region
    # combinations, never fabricated when missing.
    snr_db = raw_json.get("SNR")
    azure_confidence = best.get("Confidence")

    words_out: list[dict[str, Any]] = []
    issues_out: list[dict[str, Any]] = []
    for w in best.get("Words") or []:
        word = (w.get("Word") or "").strip()
        if not word:
            continue
        w_assessment = _pron_assessment(w)
        azure_error = w_assessment.get("ErrorType", "None")
        error_type = _ERROR_TYPE_MAP.get(azure_error, "correct")
        accuracy_score = w_assessment.get("AccuracyScore")
        accuracy_score = float(accuracy_score) if accuracy_score is not None else None

        phonemes_raw = w.get("Phonemes") or []
        phonemes_out = [
            {
                "phoneme": p.get("Phoneme"),
                "accuracyScore": (
                    float(raw_phoneme_score)
                    if (raw_phoneme_score := _pron_assessment(p).get("AccuracyScore")) is not None
                    else None
                ),
            }
            for p in phonemes_raw
        ]

        # Azure reports Offset/Duration in 100-nanosecond ticks (documented
        # unit for the REST short-audio endpoint) — convert to ms, the unit
        # this backend's own vocabulary uses everywhere else. Raw material
        # for derived prosody (plan §7) and the aggregator's seam handling
        # (plan §4); intentionally read here rather than left discarded.
        offset_ticks = w.get("Offset")
        duration_ticks = w.get("Duration")
        offset_ms = int(offset_ticks / 10_000) if offset_ticks is not None else None
        duration_ms = int(duration_ticks / 10_000) if duration_ticks is not None else None

        words_out.append({
            "word": word,
            "accuracyScore": accuracy_score,
            "errorType": error_type,
            "confidence": None,
            "phonemes": phonemes_out,
            "offsetMs": offset_ms,
            "durationMs": duration_ms,
            "nearChunkBoundary": None,
        })

        needs_issue = error_type != "correct" or (
            accuracy_score is not None and accuracy_score < _LOW_ACCURACY_THRESHOLD
        )
        if needs_issue:
            severity = _severity_for(error_type, accuracy_score)
            ipa_expected = " ".join(p.get("Phoneme", "") for p in phonemes_raw)
            ipa_heard_parts = []
            any_nbest = False
            for p in phonemes_raw:
                nbest = _pron_assessment(p).get("NBestPhonemes")
                if nbest:
                    any_nbest = True
                    ipa_heard_parts.append((nbest[0] or {}).get("Phoneme", ""))
                else:
                    ipa_heard_parts.append("")
            ipa_heard = " ".join(ipa_heard_parts) if any_nbest else ""
            issues_out.append({
                "word": word,
                "ipaExpected": ipa_expected,
                "ipaHeard": ipa_heard,
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
        "couldNotAssess": False,
        "couldNotAssessReason": None,
        "snrDb": float(snr_db) if snr_db is not None else None,
        "azureConfidence": float(azure_confidence) if azure_confidence is not None else None,
    }


async def _post_to_azure(
    key: str,
    region: str,
    audio_bytes: bytes,
    target_text: str,
    *,
    content_type: str,
    enable_miscue: bool,
) -> dict[str, Any]:
    pronunciation_params = {
        "ReferenceText": target_text,
        "GradingSystem": "HundredMark",
        "Granularity": "Phoneme",
        "Dimension": "Comprehensive",
        "PhonemeAlphabet": "IPA",
        "EnableProsodyAssessment": "True",
        "EnableMiscue": enable_miscue,
    }
    header_value = base64.b64encode(json.dumps(pronunciation_params).encode("utf-8")).decode("ascii")

    url = f"https://{region}.stt.speech.microsoft.com/speech/recognition/conversation/cognitiveservices/v1"
    params = {"language": "fr-FR", "format": "detailed"}
    headers = {
        "Ocp-Apim-Subscription-Key": key,
        "Content-Type": content_type,
        "Accept": "application/json",
        "Pronunciation-Assessment": header_value,
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(url, params=params, headers=headers, content=audio_bytes)

    if response.status_code in (401, 403):
        # Non-retryable: a bad key should fail straight to fallback.
        raise PermissionError(f"Azure Speech auth failed: {response.status_code}")
    if response.status_code == 400:
        # Signature of defect #1 (rejected Content-Type/audio format).
        # Never retry a 400 — it will not succeed on retry.
        log.error(
            "Azure pronunciation 400 — Content-Type=%r may not match audio encoding. Body: %s",
            content_type,
            response.text[:500],
        )
    response.raise_for_status()
    return response.json()


async def assess_pronunciation(
    audio_bytes: bytes,
    target_text: str,
    *,
    audio_filename: str = "",
    mode: str = "scripted",
    run_with_retries=None,
) -> dict[str, Any] | None:
    """POST audio to Azure Pronunciation Assessment. Returns the normalized
    dict, or None if Azure isn't configured. Raises on request/auth failure
    (caller's fallback chain is responsible for catching).

    `mode`: "scripted" (caller-supplied reference text — enables miscue
    detection/omission/insertion) or "freeform" (reference text is a Whisper
    transcript of the same audio — miscue is meaningless there, since you
    cannot "omit" a word from your own transcript).

    `run_with_retries`, when provided, is main.py's `_run_with_retries` —
    injected rather than imported, to avoid a circular import (main.py ->
    routers.pronunciation -> this module -> main.py). Falls back to a single
    direct call when not provided (e.g. in offline unit tests).
    """
    configured = _is_configured()
    if configured is None:
        return None
    key, region = configured
    content_type = _content_type_for(audio_filename)
    enable_miscue = mode == "scripted"

    async def operation() -> dict[str, Any]:
        return await _post_to_azure(
            key, region, audio_bytes, target_text,
            content_type=content_type, enable_miscue=enable_miscue,
        )

    raw_json = await (run_with_retries("azure-speech", operation) if run_with_retries else operation())
    return _normalize_azure_response(raw_json, target_text)
