"""POST /api/pronunciation — Azure phoneme-level assessment, degrading to the
existing Whisper-alignment heuristic when Azure isn't configured or fails.

Dependency-injected from main.py (configure()) rather than importing
main.py's Whisper helpers at module load time, to avoid a circular import
(main.py -> routers.pronunciation -> main). Mirrors the pattern already used
by routers/content.py (set_cache) and routers/admin.py (set_cache_invalidator).
"""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Annotated, Any, Awaitable, Callable

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from models.pronunciation import PronunciationAssessmentResponse
from services.pronunciation.fallback import assess_with_fallback

log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/api", tags=["pronunciation"])

# ── Injected from main.py ────────────────────────────────────────────────────
_groq_whisper_fn: Callable[[str, str], Awaitable[dict[str, Any]]] | None = None
_faster_whisper_fn: Callable[[str, str], Awaitable[dict[str, Any]]] | None = None
_align_fn: Callable[[str, str, list[dict[str, Any]]], dict[str, Any]] | None = None
_groq_key_check_fn: Callable[[], bool] | None = None
_run_with_retries_fn: Callable[..., Awaitable[Any]] | None = None


def configure(
    groq_whisper_fn,
    faster_whisper_fn,
    align_fn,
    groq_key_check_fn,
    run_with_retries_fn=None,
) -> None:
    global _groq_whisper_fn, _faster_whisper_fn, _align_fn, _groq_key_check_fn, _run_with_retries_fn
    _groq_whisper_fn = groq_whisper_fn
    _faster_whisper_fn = faster_whisper_fn
    _align_fn = align_fn
    _groq_key_check_fn = groq_key_check_fn
    _run_with_retries_fn = run_with_retries_fn


def set_rate_limiter(rate_limit_decorator) -> None:
    """Applies main.py's slowapi-based rate_limit("20/minute") to the already
    -registered route. Applied post-hoc (rather than as a decorator at
    definition time) because main.py's `_limiter` doesn't exist until main.py
    has started executing, and this module is imported by main.py."""
    limited = rate_limit_decorator("20/minute")(pronunciation_evaluate)
    for route in router.routes:
        if getattr(route, "path", None) == "/api/pronunciation":
            route.endpoint = limited
            route.dependant.call = limited


@router.post("/pronunciation", response_model=PronunciationAssessmentResponse)
async def pronunciation_evaluate(
    request: Request,
    audio: Annotated[UploadFile, File(...)],
    target_text: str = Form(...),
) -> PronunciationAssessmentResponse:
    if _align_fn is None or _groq_whisper_fn is None or _faster_whisper_fn is None:
        raise HTTPException(status_code=503, detail="Pronunciation service not configured")

    suffix = os.path.splitext(audio.filename or "")[1] or ".webm"
    raw = await audio.read()

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(raw)
        tmp_path = tmp.name

    try:
        whisper_data: dict[str, Any] = {}
        if _groq_key_check_fn and _groq_key_check_fn():
            try:
                whisper_data = await _groq_whisper_fn(tmp_path, "fr")
            except Exception as e:
                log.warning("Groq Whisper failed in /api/pronunciation, trying faster-whisper: %s", e)

        if not whisper_data:
            whisper_data = await _faster_whisper_fn(tmp_path, "fr")

        heard_text = (whisper_data.get("text") or "").strip()
        whisper_words = whisper_data.get("words", [])

        result = await assess_with_fallback(
            audio_bytes=raw,
            target_text=target_text,
            heard_text=heard_text,
            whisper_words=whisper_words,
            align_fn=_align_fn,
            run_with_retries=_run_with_retries_fn,
        )
        return PronunciationAssessmentResponse(**result)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
