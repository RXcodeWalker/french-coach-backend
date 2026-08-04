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
    run_with_retries=None,
) -> dict[str, Any]:
    """Try Azure (if configured); on None/exception, fall through to the
    existing Whisper-alignment heuristic. `heard_text`/`whisper_words` are
    the already-transcribed Whisper output the caller obtained (needed either
    way, since the heuristic tier requires it and Azure needs its own audio
    upload regardless of transcript)."""
    try:
        azure_result = await assess_pronunciation(
            audio_bytes, target_text, run_with_retries=run_with_retries
        )
        if azure_result is not None:
            return azure_result
    except Exception as exc:
        log.warning("Azure pronunciation assessment failed, falling back to Whisper heuristic: %s", exc)

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
            }
            for w in whisper_words
        ],
        "provider": "whisper-heuristic",
        "subScores": None,
    }
