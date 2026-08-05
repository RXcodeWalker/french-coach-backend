"""Azure -> Whisper-heuristic fallback chain for pronunciation assessment.

Never raises past this layer — matches the codebase's existing "never
hard-fail a pronunciation request" pattern already used everywhere else
(see main.py's /api/transcribe, whose faster-whisper branch always returns a
best-effort dict instead of propagating).
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from services.pronunciation.azure_client import assess_pronunciation

log = logging.getLogger("uvicorn.error")

# Injected by routers/pronunciation.py's configure() — main.py's existing
# _align_pronunciation, unchanged.
AlignFn = Callable[[str, str, list[dict[str, Any]]], dict[str, Any]]


async def assess_with_fallback(
    *,
    audio_bytes: bytes,
    target_text: str,
    heard_text: str,
    whisper_words: list[dict[str, Any]],
    align_fn: AlignFn,
    audio_filename: str = "",
    mode: str = "scripted",
    run_with_retries=None,
) -> dict[str, Any]:
    """Try Azure (if configured); on None/exception, fall through to the
    existing Whisper-alignment heuristic. `heard_text`/`whisper_words` are
    the already-transcribed Whisper output the caller obtained (needed either
    way, since the heuristic tier requires it and Azure needs its own audio
    upload regardless of transcript).

    A successful Azure call that determined it couldNotAssess (silence,
    no-match, missing assessment block) is still returned as-is — it is not
    a failure to fall back from, since the whisper-heuristic tier cannot do
    better against audio Azure already found unassessable."""
    try:
        azure_result = await assess_pronunciation(
            audio_bytes, target_text,
            audio_filename=audio_filename, mode=mode, run_with_retries=run_with_retries,
        )
        if azure_result is not None:
            return azure_result
    except Exception as exc:
        log.warning("Azure pronunciation assessment failed, falling back to Whisper heuristic: %s", exc)

    if mode == "freeform":
        # The whisper-heuristic tier's alignment score is a target-vs-heard
        # diff (difflib.SequenceMatcher). In freeform mode target_text IS
        # heard_text (there is no independent reference — see
        # routers/pronunciation.py) so diffing them would always yield a
        # trivial perfect match: a fabricated-looking score, not a real one.
        # Report the transcript with no verdict, same treatment the frontend
        # already gives non-azure results (SayItAgainCard.outcomeFor).
        return {
            "score": None,
            "transcript": heard_text,
            "issues": [],
            "words": [
                {
                    "word": (w.get("word") or "").strip(),
                    "accuracyScore": None,
                    "errorType": None,
                    "confidence": w.get("probability"),
                    "phonemes": None,
                }
                for w in whisper_words
            ],
            "provider": "whisper-heuristic",
            "subScores": None,
            "couldNotAssess": True,
            "couldNotAssessReason": "assessment_unavailable",
        }

    alignment = align_fn(target_text, heard_text, whisper_words)
    # _align_pronunciation returns a 0-10 score; rescale to the 0-100 contract.
    score_100 = max(0, min(100, round(alignment["score"] * 10)))
    return {
        "score": score_100,
        "transcript": heard_text,
        "issues": alignment["issues"],
        "words": [
            {
                "word": (w.get("word") or "").strip(),
                "accuracyScore": None,
                "errorType": None,
                "confidence": w.get("probability"),
                "phonemes": None,
                "offsetMs": None,
                "durationMs": None,
                "nearChunkBoundary": None,
            }
            for w in whisper_words
        ],
        "provider": "whisper-heuristic",
        "subScores": None,
        "couldNotAssess": False,
        "couldNotAssessReason": None,
    }
