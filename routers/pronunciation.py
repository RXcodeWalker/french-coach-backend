"""POST /api/pronunciation — Azure phoneme-level assessment, degrading to the
existing Whisper-alignment heuristic when Azure isn't configured or fails.

Dependency-injected from main.py (configure()) rather than importing
main.py's Whisper helpers at module load time, to avoid a circular import
(main.py -> routers.pronunciation -> main). Mirrors the pattern already used
by routers/content.py (set_cache) and routers/admin.py (set_cache_invalidator).
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from typing import Annotated, Any, Awaitable, Callable

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from models.pronunciation import PronunciationAssessmentResponse
from services.cache import BoundedTTLCache
from services.phonology import rules as phonology_rules
from services.pronunciation.coach_narrator import findings_hash, generate_coaching
from services.pronunciation.capabilities import enforce_capabilities
from services.pronunciation.confidence import compute_confidence
from services.pronunciation.fallback import assess_with_fallback
from services.pronunciation.prosody import compute_rhythm_metrics

log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/api", tags=["pronunciation"])

ASSESSOR_VERSION = "pronunciation-v3"
LOCALE = "fr-FR"

# Audio cache: sha256(audio + reference + mode + locale + assessorVersion),
# TTL 10 min (plan §9) — makes retries and Learn's double-submits free.
# A dedicated instance, not main.py's feedback cache: sharing one would let
# pronunciation traffic evict feedback entries and vice versa.
_AUDIO_CACHE_MAX = 100
_AUDIO_CACHE_TTL_SEC = 600.0
_audio_cache: BoundedTTLCache[dict] = BoundedTTLCache(_AUDIO_CACHE_MAX, _AUDIO_CACHE_TTL_SEC)

# Coaching cache: keyed by findings hash, TTL 1h (plan §9, R5) — two learners
# with the same errors share one LLM call. Separate instance from the audio
# cache: different key space, different eviction pressure.
_COACHING_CACHE_MAX = 200
_COACHING_CACHE_TTL_SEC = 3600.0
_coaching_cache: BoundedTTLCache[dict] = BoundedTTLCache(_COACHING_CACHE_MAX, _COACHING_CACHE_TTL_SEC)


def _audio_cache_key(audio_bytes: bytes, reference_text: str, mode: str) -> str:
    digest = hashlib.sha256(audio_bytes).hexdigest()
    return f"{digest}::{reference_text}::{mode}::{LOCALE}::{ASSESSOR_VERSION}"


def _is_cacheable(result: dict[str, Any]) -> bool:
    # Mirror main.py's _is_cacheable_result rule (never cache couldNotAssess,
    # fallback-tier, or errors) — a cached "couldn't assess" would deny a
    # legitimate retry on the exact same audio.
    return not result.get("couldNotAssess", False) and result.get("provider") == "azure"

# ── Injected from main.py ────────────────────────────────────────────────────
_groq_whisper_fn: Callable[[str, str], Awaitable[dict[str, Any]]] | None = None
_faster_whisper_fn: Callable[[str, str], Awaitable[dict[str, Any]]] | None = None
_align_fn: Callable[[str, str, list[dict[str, Any]]], dict[str, Any]] | None = None
_groq_key_check_fn: Callable[[], bool] | None = None
_run_with_retries_fn: Callable[..., Awaitable[Any]] | None = None
# Coaching narrator LLM callers (plan §8) — separate DI seam from the
# Whisper/align functions above since they're wired from a different call
# in main.py and are both optional (missing key => that provider is skipped).
_coach_call_groq: Callable[[str], Awaitable[dict[str, Any]]] | None = None
_coach_call_gemini: Callable[[str], Awaitable[dict[str, Any]]] | None = None


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


def configure_coaching(call_groq_fn=None, call_gemini_fn=None) -> None:
    """Optional — coaching degrades to the template fallback (never raises)
    when unconfigured, same as leaving GROQ_API_KEY/GEMINI_API_KEY unset."""
    global _coach_call_groq, _coach_call_gemini
    _coach_call_groq = call_groq_fn
    _coach_call_gemini = call_gemini_fn


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
    mode: str = Form("scripted"),
    coaching: str = Form("none"),
) -> PronunciationAssessmentResponse:
    if _align_fn is None or _groq_whisper_fn is None or _faster_whisper_fn is None:
        raise HTTPException(status_code=503, detail="Pronunciation service not configured")
    if mode not in ("scripted", "freeform"):
        raise HTTPException(status_code=422, detail="mode must be 'scripted' or 'freeform'")
    if coaching not in ("none", "full"):
        raise HTTPException(status_code=422, detail="coaching must be 'none' or 'full'")

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

        # Freeform mode (accent-analyzer plan defect #5): the caller's
        # target_text is not a real reference transcript (e.g. Learn used to
        # send the Web Speech API's own, separately-unreliable guess). Azure
        # must grade against what was actually said, not a third
        # recognizer's guess at it — so the reference text becomes Whisper's
        # own transcript of this same audio. EnableMiscue is force-disabled
        # for this mode inside assess_pronunciation (mode == "freeform"):
        # you cannot "omit" a word from your own transcript.
        reference_text = heard_text if mode == "freeform" else target_text

        cache_key = _audio_cache_key(raw, reference_text, mode)
        cached_result = await _audio_cache.get(cache_key)
        if cached_result is not None:
            result = dict(cached_result)
            was_cached = True
        else:
            result = await assess_with_fallback(
                audio_bytes=raw,
                target_text=reference_text,
                heard_text=heard_text,
                whisper_words=whisper_words,
                align_fn=_align_fn,
                audio_filename=audio.filename or "",
                mode=mode,
                run_with_retries=_run_with_retries_fn,
            )
            was_cached = False
            if _is_cacheable(result):
                await _audio_cache.set(cache_key, result)

        result.setdefault("mode", mode)
        result.setdefault("locale", LOCALE)
        result.setdefault("assessorVersion", ASSESSOR_VERSION)
        result.setdefault("chunkCount", 1)
        result.setdefault("chunksFailed", 0)

        tier = result["provider"]

        # L3 guardrails (accent-analyzer plan §6, §7, §11): derived prosody
        # and inferred phonology findings are computed here, unconditionally
        # populated on the dict, then enforce_capabilities (below) nulls out
        # whatever the matrix marks unavailable for this (mode, tier) — the
        # computation itself doesn't need to know about capability gating.
        if not result.get("couldNotAssess"):
            timed_words = [
                w for w in result.get("words", [])
                if w.get("offsetMs") is not None and w.get("durationMs") is not None
            ]
            duration_ms = (
                max(w["offsetMs"] + w["durationMs"] for w in timed_words)
                if timed_words else None
            )
            result["prosodyMetrics"] = compute_rhythm_metrics(result.get("words", []))
            result["phonologicalFindings"] = phonology_rules.evaluate(result.get("words", []), locale=LOCALE)
            result["confidence"] = compute_confidence(
                snr_db=result.get("snrDb"),
                azure_confidence=result.get("azureConfidence"),
                whisper_text=heard_text,
                azure_text=result.get("transcript", ""),
                duration_ms=duration_ms,
            )
            result["audioQuality"] = {
                "snrDb": result.get("snrDb"),
                "durationMs": duration_ms,
                "recognitionStatus": "Success",
                "clipped": False,
            }

        result = enforce_capabilities(result, mode=mode, tier=tier, locale=LOCALE)

        # Coaching (plan §8, R5): a second, optional pass over the findings
        # that already survived enforce_capabilities — never computed when
        # coaching=none (drill mode default), and never blocks assessment on
        # failure (generate_coaching itself always falls back to a template
        # rather than raising). Cached by findings hash, not by audio, since
        # two learners with identical findings should share one LLM call.
        if coaching == "full" and not result.get("couldNotAssess"):
            findings = result.get("phonologicalFindings") or []
            coach_key = findings_hash(findings)
            cached_coaching = await _coaching_cache.get(coach_key)
            if cached_coaching is not None:
                result["coaching"] = cached_coaching
            else:
                coaching_result = await generate_coaching(
                    findings,
                    call_groq=_coach_call_groq,
                    call_gemini=_coach_call_gemini,
                )
                result["coaching"] = coaching_result
                await _coaching_cache.set(coach_key, coaching_result)

        request.state.obs_provider = result.get("provider")
        request.state.obs_cached = was_cached
        request.state.obs_extra = {
            "recognition_status": result.get("couldNotAssessReason") if result.get("couldNotAssess") else "Success",
            "chunk_count": result.get("chunkCount", 1),
            "chunks_failed": result.get("chunksFailed", 0),
            "mode": mode,
            "audio_ms": None,
        }

        return PronunciationAssessmentResponse(**result)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
