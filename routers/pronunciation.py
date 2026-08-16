"""POST /api/pronunciation — Azure phoneme-level assessment, degrading to the
existing Whisper-alignment heuristic when Azure isn't configured or fails.

Dependency-injected from main.py (configure()) rather than importing
main.py's Whisper helpers at module load time, to avoid a circular import
(main.py -> routers.pronunciation -> main). Mirrors the pattern already used
by routers/content.py (set_cache) and routers/admin.py (set_cache_invalidator).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
import uuid
from typing import Annotated, Any, Awaitable, Callable

from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile

from lib.auth import verify_supabase_jwt
from models.pronunciation import PronunciationAssessmentResponse
from services.cache import BoundedTTLCache
from services.phonology import rules as phonology_rules
from services.pronunciation.coach_narrator import generate_shadowing_coaching
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

# ── Shadowing detailed-coaching quota (Phase 4, plan §4) ────────────────────
# COACHING_DAILY_LIMIT is used ONLY for the degraded-quota dicts below (the
# shapes returned when the RPC was never reached at all) — the RPC's own
# v_limit is the single source of truth for the real limit, and the client
# reads `limit` off the response rather than hardcoding it.
COACHING_DAILY_LIMIT = 3

# Shadowing coaching cache: keyed by a hash of the FULL normalized context
# (target text + per-word accuracy scores + sub-scores + rhythm), not just
# findings — a retry-dedup cache, not a cross-user sharing cache. Its job is
# to stop an identical replay costing a second Groq call; two users landing
# on the same key implies identical assessments, in which case identical
# coaching is correct, not a leak.
_SHADOWING_COACHING_CACHE_MAX = 200
_SHADOWING_COACHING_CACHE_TTL_SEC = 3600.0
_shadowing_coaching_cache: BoundedTTLCache[dict] = BoundedTTLCache(
    _SHADOWING_COACHING_CACHE_MAX, _SHADOWING_COACHING_CACHE_TTL_SEC
)

_supabase_admin = None


def _db():
    """Lazy service-role Supabase client (copied from routers/admin.py's
    `_db()` — same pattern, separate instance since this router has no
    other reason to import admin.py)."""
    global _supabase_admin
    if _supabase_admin is None:
        url = os.getenv("SUPABASE_URL", "").strip()
        key = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
        if not (url and key):
            return None
        from supabase import create_client
        _supabase_admin = create_client(url, key)
    return _supabase_admin


def _degraded_quota(reason: str) -> dict[str, Any]:
    return {"used": 0, "limit": COACHING_DAILY_LIMIT, "granted": False, "reason": reason}


def _coaching_user_id(authorization: str | None) -> str | None:
    """Never raises — every failure (missing/malformed/expired/forged JWT,
    or SUPABASE_JWT_SECRET unset) becomes None, which the caller degrades to
    the 'unauthenticated' quota reason. Degrading rather than 401ing is
    *stricter* (no coaching) and satisfies "never fail the shadowing
    attempt" (plan §4)."""
    try:
        payload = verify_supabase_jwt(authorization)
    except Exception as e:
        log.warning("shadowing coaching: auth failed, degrading to unauthenticated: %s", e)
        return None
    sub = payload.get("sub")
    return str(sub) if sub else None


async def _consume_quota(user_id: str, idempotency_key: str) -> dict[str, Any]:
    """Fail closed on spend, open on the attempt: any Supabase/RPC error
    degrades to 'quota_unavailable' rather than raising, so a misconfigured
    or unreachable Supabase project never fails the underlying assessment."""
    db = _db()
    if db is None:
        return _degraded_quota("quota_unavailable")
    try:
        res = await asyncio.to_thread(
            lambda: db.rpc(
                "consume_shadowing_coaching_quota",
                {"p_user_id": user_id, "p_idempotency_key": idempotency_key},
            ).execute()
        )
        data = res.data or {}
        return {
            "used": data.get("used", 0),
            "limit": data.get("limit", COACHING_DAILY_LIMIT),
            "granted": bool(data.get("granted", False)),
            "reason": data.get("reason"),
        }
    except Exception as e:
        log.warning("shadowing coaching: consume_shadowing_coaching_quota RPC failed: %s", e)
        return _degraded_quota("quota_unavailable")


async def _release_quota(user_id: str, idempotency_key: str) -> dict[str, Any]:
    """A failing release leaves the user charged; logged at WARNING, never
    surfaced as an error — the assessment result must not be blocked by a
    refund failure."""
    db = _db()
    if db is None:
        return _degraded_quota("quota_unavailable")
    try:
        res = await asyncio.to_thread(
            lambda: db.rpc(
                "release_shadowing_coaching_grant",
                {"p_user_id": user_id, "p_idempotency_key": idempotency_key},
            ).execute()
        )
        data = res.data or {}
        return {
            "used": data.get("used", 0),
            "limit": data.get("limit", COACHING_DAILY_LIMIT),
            "granted": False,
            "reason": None,
        }
    except Exception as e:
        log.warning("shadowing coaching: release_shadowing_coaching_grant RPC failed (user stays charged): %s", e)
        return _degraded_quota("quota_unavailable")


def _build_shadowing_context(result: dict[str, Any], target_text: str) -> dict[str, Any]:
    """Rounds floats (scores to int, ratios to 2 dp) so trivial jitter
    doesn't destroy cache hits (plan §4, review item 6)."""
    words = result.get("words") or []
    mispronounced = [
        {"word": w.get("word"), "accuracyScore": round(w["accuracyScore"]) if w.get("accuracyScore") is not None else None}
        for w in words if w.get("errorType") in ("mispronounced", "skipped")
    ]
    correct = [
        {"word": w.get("word"), "accuracyScore": round(w["accuracyScore"]) if w.get("accuracyScore") is not None else None}
        for w in words if w.get("errorType") == "correct"
    ]
    sub_scores = result.get("subScores") or {}
    rounded_sub_scores = {
        k: (round(v) if isinstance(v, (int, float)) else v) for k, v in sub_scores.items()
    }
    prosody = result.get("prosodyMetrics")
    rounded_prosody = None
    if prosody is not None:
        rounded_prosody = {
            k: (round(v, 2) if isinstance(v, float) else v) for k, v in prosody.items()
        }
    findings = [
        {"category": f.get("category"), "word": f.get("word"), "explanation": f.get("explanation")}
        for f in (result.get("phonologicalFindings") or [])
    ]
    return {
        "targetText": target_text,
        "score": round(result["score"]) if result.get("score") is not None else None,
        "subScores": rounded_sub_scores,
        "prosodyMetrics": rounded_prosody,
        "mispronouncedWords": mispronounced,
        "correctWords": correct,
        "findings": findings,
    }


def _shadowing_cache_key(ctx: dict[str, Any]) -> str:
    """Hashes the ENTIRE payload handed to the prompt builder, so no context
    field can differ without changing the key (plan §4, review item 6)."""
    digest_input = json.dumps(ctx, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(digest_input.encode("utf-8")).hexdigest()


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


def set_rate_limiter(rate_limit_decorator, target_router) -> None:
    """Applies main.py's slowapi-based rate_limit("20/minute") to the already
    -registered route. Applied post-hoc (rather than as a decorator at
    definition time) because main.py's `_limiter` doesn't exist until main.py
    has started executing, and this module is imported by main.py.

    Must run AFTER `app.include_router(router)`, and must mutate the route
    object living on `target_router` (the app / its router), not on this
    module's own `router`. `include_router` copies each route by rebuilding
    its parameter dependant from `route.endpoint` — rebuilding it from the
    slowapi wrapper (rather than the original, correctly-annotated
    `pronunciation_evaluate`) resolves the `from __future__ import
    annotations` string annotations against slowapi's module globals, which
    don't have `UploadFile`/`File` in scope. That silently drops the `audio`
    param to a query parameter, breaking every upload with a 422. Registering
    first (so the dependant is built from the real function) and only then
    swapping the already-built route's callable avoids that."""
    limited = rate_limit_decorator("20/minute")(pronunciation_evaluate)
    for route in target_router.routes:
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
    coaching_request_id: str = Form(""),
    authorization: str | None = Header(None),
) -> PronunciationAssessmentResponse:
    # _faster_whisper_fn is deliberately NOT required: main.py passes None for
    # it when the local-model fallback is disabled (see PRONUNCIATION_LOCAL_WHISPER
    # there). Transcription is a best-effort input to this endpoint, not the
    # assessment itself, so its absence degrades the result rather than
    # refusing the request.
    if _align_fn is None or _groq_whisper_fn is None:
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

        if not whisper_data and _faster_whisper_fn is not None:
            # Guarded exactly like /api/transcribe's faster-whisper branch in
            # main.py. Transcription is a best-effort input here, not the
            # assessment itself: Azure grades from the audio, and the
            # whisper-heuristic tier degrades to couldNotAssess on an empty
            # transcript. Never let it fail the request.
            try:
                whisper_data = await _faster_whisper_fn(tmp_path, "fr")
            except Exception as e:
                log.warning("faster-whisper failed in /api/pronunciation, continuing without a transcript: %s", e)
                whisper_data = {}

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

        if mode == "freeform" and not reference_text:
            # No transcript => no reference text at all in this mode. Azure
            # would be asked to grade against an empty ReferenceText, which
            # yields a meaningless result rather than an error. Report the
            # honest outcome instead.
            return PronunciationAssessmentResponse(
                score=None,
                transcript="",
                issues=[],
                words=[],
                provider="whisper-heuristic",
                subScores=None,
                couldNotAssess=True,
                couldNotAssessReason="no_speech_recognized",
                mode=mode,
                locale=LOCALE,
                assessorVersion=ASSESSOR_VERSION,
            )

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

        # Coaching (Phase 4 — Shadowing Mode, plan §4): a second, optional,
        # server-metered pass over the findings that already survived
        # enforce_capabilities. Never computed when coaching=none (the
        # default for Drills/Learn/SayItAgainCard — none of which currently
        # send coaching='full'), and never blocks assessment on failure —
        # every helper below degrades to a dict rather than raising.
        # Quota is consumed BEFORE the Groq call and refunded whenever the
        # narrator's `grounded` flag comes back False (Groq unavailable,
        # timed out, malformed JSON, or failed the per-claim grounding gate)
        # — a user is never charged for feedback they didn't receive.
        if coaching == "full":
            if result.get("couldNotAssess"):
                quota = _degraded_quota("could_not_assess")
            elif _coach_call_groq is None:
                quota = _degraded_quota("coaching_unavailable")
            else:
                user_id = _coaching_user_id(authorization)
                if user_id is None:
                    quota = _degraded_quota("unauthenticated")
                else:
                    # Empty coaching_request_id => server-generated uuid4().
                    # This loses replay protection for that one request (a
                    # retried call would consume a second slot), but the
                    # client always sends one in practice; documented rather
                    # than silently defaulting to something replay-safe.
                    request_id = coaching_request_id or str(uuid.uuid4())
                    quota = await _consume_quota(user_id, request_id)
                    if quota["granted"]:
                        ctx = _build_shadowing_context(result, target_text)
                        coach_key = _shadowing_cache_key(ctx)
                        cached = await _shadowing_coaching_cache.get(coach_key)
                        if cached is not None:
                            result["coaching"] = cached
                        else:
                            out = await generate_shadowing_coaching(
                                ctx,
                                result.get("phonologicalFindings") or [],
                                call_groq=_coach_call_groq,
                            )
                            if out["grounded"]:
                                result["coaching"] = out
                                await _shadowing_coaching_cache.set(coach_key, out)
                            else:
                                result["coaching"] = out
                                quota = await _release_quota(user_id, request_id)
            result["coachingQuota"] = quota

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
