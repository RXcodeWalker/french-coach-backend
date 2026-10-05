"""POST/GET /api/exam/pronunciation — feedback-only pronunciation evidence for
exam turns (exam-pronunciation plan, Batch 4; main repo
docs/systems/exam-pronunciation.md once Batch 7 writes it).

It never changes a mark. The scorer lives in the main repo's Node service and
never reads this route's table (exam_pronunciation_evidence); the stored rows
are read only by the client's deterministic fairness rules.

One turn per request (plan §3b): the client posts each candidate speech turn
as a silence-trimmed 16 kHz mono WAV, sequentially. Steps, in order:

  1. access mode   EXAM_PRONUNCIATION_ACCESS = off | admin | all (default off)
  2. auth          verify_supabase_jwt — no guests; user_id is the JWT `sub`
                   and is never taken from the request
  3. consent       require_speaking_consent (pending / no profile -> 403)
  4. cache         a stored (user, session, turn, assessor_version) row is
                   returned with no Azure call and no charge
  5. daily quota   feature `exam_pronunciation`, key exam-pron:{session}:{part}
                   — every turn of a part replays the same grant
  6. reserve       one azure_speech_usage reservation per Azure request
  7. Whisper       the reference transcript of this same audio (freeform)
  8. Azure         freeform, chunked past the 30 s REST limit, at
                   AZURE_SPEECH_MAX_CONCURRENCY (the process-wide semaphore in
                   azure_client still serialises every call)
  9. settle, store, return

On a failure the part's quota grant is released only if no turn of that part
has a stored result — otherwise turns that already succeeded would be refunded.

GET never analyses: it returns the stored turns for (JWT sub, session_id).
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, File, Form, Header, HTTPException, Query, Request, UploadFile

from lib.ai_quota import consume_ai_quota_or_503, release_ai_quota_grant
from lib.auth import _role_from_payload, verify_supabase_jwt
from lib.azure_budget import (
    release_azure_seconds,
    reserve_azure_seconds,
    settle_azure_seconds,
    wav_duration_seconds,
)
from lib.consent import require_speaking_consent
from models.exam_pronunciation import (
    ExamPronunciationGetResponse,
    ExamPronunciationPostResponse,
    ExamPronunciationTurn,
)
from services.pronunciation import azure_client
from services.pronunciation.aggregator import (
    MAX_CHUNK_SEC,
    SEAM_PROXIMITY_MS,
    aggregate_chunk_results,
    build_chunk_windows,
)
from services.pronunciation.transcript import transcript_is_unusable
from services.pronunciation.wav import read_pcm_wav, slice_wav

log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/api/exam", tags=["exam-pronunciation"])

# Bump when this module's assessment logic changes (chunking, alignment,
# suppression reasons). Part of the evidence row's unique key, so a bump
# re-analyses on the next request instead of serving a stale row.
EXAM_PRONUNCIATION_ASSESSOR_VERSION = "exam-pronunciation-v1"

QUOTA_FEATURE = "exam_pronunciation"
LEDGER_SOURCE = "exam"
TABLE = "exam_pronunciation_evidence"
_UNIQUE_KEY = "user_id,session_id,turn_key,assessor_version"

_ACCESS_MODES = ("off", "admin", "all")
_PARTS = ("rolePlay", "topic1", "topic2")
_RECOGNIZERS = ("webspeech", "whisper")

# Matches the client's EXAM_TURN_MAX_SECONDS (measureExamAudio.ts).
_MAX_TURN_SECONDS = 180.0
_AUDIO_MAX_BYTES = 15 * 1024 * 1024
_AUDIO_READ_CHUNK_BYTES = 1 * 1024 * 1024
# Azure's REST short-audio endpoint rejects anything longer than 30 s.
_AZURE_REQUEST_MAX_SEC = 29.5

# ── Injected from main.py (same DI seam as routers/pronunciation.py) ─────────
_groq_whisper_fn: Callable[[str, str], Awaitable[dict[str, Any]]] | None = None
_faster_whisper_fn: Callable[[str, str], Awaitable[dict[str, Any]]] | None = None
_groq_key_check_fn: Callable[[], bool] | None = None
_run_with_retries_fn: Callable[..., Awaitable[Any]] | None = None

_supabase_admin = None


def configure(groq_whisper_fn, faster_whisper_fn, groq_key_check_fn, run_with_retries_fn=None) -> None:
    global _groq_whisper_fn, _faster_whisper_fn, _groq_key_check_fn, _run_with_retries_fn
    _groq_whisper_fn = groq_whisper_fn
    _faster_whisper_fn = faster_whisper_fn
    _groq_key_check_fn = groq_key_check_fn
    _run_with_retries_fn = run_with_retries_fn


def _db():
    """Lazy service-role Supabase client (same pattern as routers/pronunciation.py)."""
    global _supabase_admin
    if _supabase_admin is None:
        url = os.getenv("SUPABASE_URL", "").strip()
        key = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
        if not (url and key):
            return None
        from supabase import create_client
        _supabase_admin = create_client(url, key)
    return _supabase_admin


# ── Access ───────────────────────────────────────────────────────────────────

def access_mode() -> str:
    """off (default) | admin | all. Anything unrecognised is off: unvalidated
    fairness thresholds must not reach users by a typo (plan §2 release gate)."""
    mode = os.getenv("EXAM_PRONUNCIATION_ACCESS", "off").strip().lower()
    return mode if mode in _ACCESS_MODES else "off"


def _not_enabled() -> HTTPException:
    return HTTPException(status_code=403, detail={"status": "not_enabled"})


def _authorize(authorization: str | None) -> str:
    mode = access_mode()
    if mode == "off":
        raise _not_enabled()
    payload = verify_supabase_jwt(authorization)
    if mode == "admin" and _role_from_payload(payload) != "admin":
        raise _not_enabled()
    return str(payload["sub"])


def _require_db():
    db = _db()
    if db is None:
        raise HTTPException(status_code=503, detail={"error": "evidence_store_unavailable"})
    return db


def _validate_session_id(session_id: str) -> str:
    session_id = (session_id or "").strip()
    if not session_id or len(session_id) > 200:
        raise HTTPException(status_code=422, detail="session_id is required (max 200 characters)")
    return session_id


# ── Evidence store ───────────────────────────────────────────────────────────

async def _fetch_turn(db, user_id: str, session_id: str, turn_key: str) -> dict | None:
    res = await asyncio.to_thread(
        lambda: db.table(TABLE).select("*")
        .eq("user_id", user_id).eq("session_id", session_id).eq("turn_key", turn_key)
        .eq("assessor_version", EXAM_PRONUNCIATION_ASSESSOR_VERSION)
        .limit(1).execute()
    )
    rows = res.data or []
    return rows[0] if rows else None


async def _part_has_stored_turn(db, user_id: str, session_id: str, part: str) -> bool:
    """Never raises: when it can't tell, it says True, so the grant is kept
    (over-charging one part beats refunding turns that already succeeded)."""
    try:
        res = await asyncio.to_thread(
            lambda: db.table(TABLE).select("id")
            .eq("user_id", user_id).eq("session_id", session_id).eq("part", part)
            .eq("assessor_version", EXAM_PRONUNCIATION_ASSESSOR_VERSION)
            .limit(1).execute()
        )
    except Exception as e:
        log.warning("exam pronunciation: stored-turn check failed (grant kept): %s", e)
        return True
    return bool(res.data)


async def _store_turn(db, row: dict) -> dict:
    """Insert, or keep the row a concurrent request already stored; returns the stored row."""
    await asyncio.to_thread(
        lambda: db.table(TABLE).upsert(row, on_conflict=_UNIQUE_KEY, ignore_duplicates=True).execute()
    )
    stored = await _fetch_turn(db, row["user_id"], row["session_id"], row["turn_key"])
    return stored or row


def _row_to_turn(row: dict) -> ExamPronunciationTurn:
    result = row.get("result") or {}
    created_at = row.get("created_at")
    return ExamPronunciationTurn(
        sessionId=row["session_id"],
        part=row["part"],
        turnKey=row["turn_key"],
        assessorVersion=row["assessor_version"],
        fairnessVersion=row["fairness_version"],
        examTranscript=row["exam_transcript"],
        referenceText=row.get("reference_text") or "",
        words=result.get("words") or [],
        couldNotAssess=bool(result.get("couldNotAssess")),
        couldNotAssessReason=result.get("couldNotAssessReason"),
        singleRecognizer=bool(result.get("singleRecognizer")),
        snrDb=result.get("snrDb"),
        azureConfidence=result.get("azureConfidence"),
        chunkCount=result.get("chunkCount", 1),
        chunksFailed=result.get("chunksFailed", 0),
        rawS=float(row["raw_s"]) if row.get("raw_s") is not None else None,
        trimmedS=float(row["trimmed_s"]),
        pauseStats=result.get("pauseStats"),
        clippedRatio=result.get("clippedRatio"),
        createdAt=str(created_at) if created_at is not None else None,
    )


# ── Recognition agreement and suppression ────────────────────────────────────

_TOKEN_RE = re.compile(r"[0-9A-Za-zÀ-ÖØ-öø-ÿŒœÆæ'’-]+")


def _norm(token: str) -> str:
    return token.replace("’", "'").strip("'-").lower()


def _tokens(text: str) -> list[str]:
    return [t.replace("’", "'").strip("'-") for t in _TOKEN_RE.findall(text or "") if _norm(t)]


def align_to_exam_transcript(words: list[str], exam_transcript: str) -> list[tuple[str | None, bool]]:
    """For each (Whisper-reference) word Azure assessed, the exam-transcript
    word aligned to it and whether the two recognisers agree there.
    Exact match after case/apostrophe normalisation; accents count, so a
    disagreement errs towards suppression."""
    exam = _tokens(exam_transcript)
    a = [_norm(w) for w in words]
    b = [_norm(t) for t in exam]
    out: list[tuple[str | None, bool]] = [(None, False)] * len(words)
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                out[i1 + k] = (exam[j1 + k], True)
        elif tag == "replace":
            for k in range(i2 - i1):
                out[i1 + k] = (exam[j1 + k] if j1 + k < j2 else None, False)
    return out


def _letters(word: str) -> int:
    return sum(1 for c in word if c.isalpha())


def suppression_reasons(word: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if word.get("recognizersAgree") is False:
        reasons.append("asr_disagreement")
    if word.get("nearChunkBoundary"):
        reasons.append("near_seam")
    if _letters(word.get("word") or "") <= 2:
        reasons.append("short_word")
    if any(c.isdigit() for c in word.get("word") or ""):
        reasons.append("number")
    return reasons


# ── Chunking ─────────────────────────────────────────────────────────────────

def _timed_units(whisper_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Whisper's timed units: words when it gave word timings (faster-whisper),
    else segments (Groq's verbose_json has segment timings only)."""
    def timed(items, text_key):
        out = []
        for it in items or []:
            end = it.get("end")
            if not isinstance(end, (int, float)):
                continue
            start = it.get("start") if isinstance(it.get("start"), (int, float)) else end
            out.append({"start": float(start), "end": float(end), "text": (it.get(text_key) or "").strip()})
        return out

    return timed(whisper_data.get("words"), "word") or timed(whisper_data.get("segments"), "text")


def plan_chunks(whisper_data: dict[str, Any], total_s: float) -> list[tuple[tuple[float, float], str]]:
    """[((start_s, end_s), reference_text)] — the aggregator's ≤25 s windows on
    Whisper boundaries. A window still over Azure's 30 s limit (one very long
    Whisper unit) is cut evenly; a piece with no unit midpoint inside it gets
    an empty reference and is not sent (counted as a failed chunk)."""
    full_text = (whisper_data.get("text") or "").strip()
    units = _timed_units(whisper_data)
    if total_s <= _AZURE_REQUEST_MAX_SEC and (total_s <= MAX_CHUNK_SEC or not units):
        return [((0.0, total_s), full_text)]

    windows: list[tuple[float, float]] = []
    for start, end in build_chunk_windows(units, total_s):
        if end - start <= _AZURE_REQUEST_MAX_SEC:
            windows.append((start, end))
            continue
        pieces = int((end - start) // MAX_CHUNK_SEC) + 1
        step = (end - start) / pieces
        windows.extend((start + i * step, start + (i + 1) * step) for i in range(pieces))

    planned = []
    for idx, (start, end) in enumerate(windows):
        last = idx == len(windows) - 1
        texts = [
            u["text"] for u in units
            if start <= (u["start"] + u["end"]) / 2 < end or (last and (u["start"] + u["end"]) / 2 >= end)
        ]
        planned.append(((start, end), " ".join(t for t in texts if t).strip()))
    return planned


@dataclass
class _ChunkRun:
    budget_exhausted: bool = False
    raw_results: list[dict[str, Any] | None] = field(default_factory=list)


async def _assess_chunks(
    *, db, user_id: str, session_id: str, part: str, turn_key: str,
    pcm, planned: list[tuple[tuple[float, float], str]],
) -> _ChunkRun:
    run = _ChunkRun(raw_results=[None] * len(planned))
    gate = asyncio.Semaphore(azure_client.azure_max_concurrency())

    async def one(idx: int, window: tuple[float, float], reference: str) -> None:
        if not reference:
            return
        async with gate:
            if run.budget_exhausted:
                return
            chunk = slice_wav(pcm, window[0], window[1])
            seconds = wav_duration_seconds(chunk) or round(window[1] - window[0], 3)
            reservation = await reserve_azure_seconds(
                db, user_id=user_id, source=LEDGER_SOURCE, seconds=seconds,
                session_id=session_id, part=part, turn_key=turn_key,
            )
            if not reservation.granted:
                run.budget_exhausted = True
                return
            try:
                result = await azure_client.assess_pronunciation(
                    chunk, reference, audio_filename="chunk.wav", mode="freeform",
                    run_with_retries=_run_with_retries_fn,
                )
            except azure_client.AzureQuotaExceeded:
                await release_azure_seconds(db, reservation)
                run.budget_exhausted = True
                return
            except Exception as e:
                await release_azure_seconds(db, reservation)
                log.warning("exam pronunciation: Azure chunk %d failed: %s", idx, e)
                return
            except BaseException:
                await release_azure_seconds(db, reservation)
                raise
            if result is None:
                await release_azure_seconds(db, reservation)
                return
            await settle_azure_seconds(db, reservation, seconds)
            run.raw_results[idx] = result

    await asyncio.gather(*(one(i, w, ref) for i, (w, ref) in enumerate(planned)))
    return run


def build_evidence(
    run: _ChunkRun,
    planned: list[tuple[tuple[float, float], str]],
    *,
    exam_transcript: str,
    single_recognizer: bool,
) -> dict[str, Any]:
    """The stored `result` jsonb for one turn. Drops every overall score."""
    windows = [w for w, _ in planned]
    merged = aggregate_chunk_results(run.raw_results, windows)
    seams_ms = [int(start * 1000) for start, _ in windows[1:]]

    words_out: list[dict[str, Any]] = []
    for w in merged.get("words") or []:
        offset, duration = w.get("offsetMs"), w.get("durationMs")
        near_seam = False
        if offset is not None and seams_ms:
            end = offset + (duration or 0)
            near_seam = any(
                abs(offset - s) < SEAM_PROXIMITY_MS or abs(end - s) < SEAM_PROXIMITY_MS or offset < s < end
                for s in seams_ms
            )
        words_out.append({
            "word": w.get("word") or "",
            "accuracyScore": w.get("accuracyScore"),
            "errorType": w.get("errorType"),
            "offsetMs": offset,
            "durationMs": duration,
            "nearChunkBoundary": near_seam,
            "phonemeScores": [p.get("accuracyScore") for p in (w.get("phonemes") or [])],
        })

    alignment = align_to_exam_transcript([w["word"] for w in words_out], exam_transcript)
    for w, (exam_word, agree) in zip(words_out, alignment):
        w["examWord"] = exam_word if not single_recognizer else w["word"]
        w["recognizersAgree"] = None if single_recognizer else agree
        w["suppressed"] = suppression_reasons(w)

    present = [r for r in run.raw_results if r is not None]
    snrs = [r["snrDb"] for r in present if r.get("snrDb") is not None]
    confs = [r["azureConfidence"] for r in present if r.get("azureConfidence") is not None]
    reason = merged.get("couldNotAssessReason")
    if merged.get("couldNotAssess") and len(present) == 1 and present[0].get("couldNotAssess"):
        reason = present[0].get("couldNotAssessReason") or reason
    return {
        "words": words_out,
        "couldNotAssess": bool(merged.get("couldNotAssess")),
        "couldNotAssessReason": reason if merged.get("couldNotAssess") else None,
        "singleRecognizer": single_recognizer,
        "snrDb": min(snrs) if snrs else None,
        "azureConfidence": min(confs) if confs else None,
        "chunkCount": merged.get("chunkCount", len(planned)),
        "chunksFailed": merged.get("chunksFailed", 0),
    }


async def _transcribe(raw: bytes) -> tuple[dict[str, Any], bool]:
    """(whisper_data, transcription_failed). Mirrors /api/pronunciation:
    Groq first, local faster-whisper as the optional fallback."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
        tmp.write(raw)
        tmp_path = tmp.name
    attempted = failed = 0
    try:
        if _groq_whisper_fn is not None and _groq_key_check_fn and _groq_key_check_fn():
            attempted += 1
            try:
                return await _groq_whisper_fn(tmp_path, "fr"), False
            except Exception as e:
                failed += 1
                log.warning("exam pronunciation: Groq Whisper failed, trying faster-whisper: %s", e)
        if _faster_whisper_fn is not None:
            attempted += 1
            try:
                return await _faster_whisper_fn(tmp_path, "fr"), False
            except Exception as e:
                failed += 1
                log.warning("exam pronunciation: faster-whisper failed: %s", e)
        return {}, attempted == 0 or failed == attempted
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def _read_upload(audio: UploadFile) -> bytes:
    total = 0
    chunks: list[bytes] = []
    while True:
        chunk = await audio.read(_AUDIO_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > _AUDIO_MAX_BYTES:
            raise HTTPException(status_code=413, detail="Audio upload exceeds the size limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _optional_float(value: str, name: str, *, upper: float) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{name} must be a number")
    if not (0 <= parsed <= upper):
        raise HTTPException(status_code=422, detail=f"{name} is out of range")
    return round(parsed, 3)


# ── Routes ───────────────────────────────────────────────────────────────────

@router.post("/pronunciation", response_model=ExamPronunciationPostResponse)
async def exam_pronunciation_analyse(
    request: Request,
    audio: UploadFile | None = File(None),
    session_id: str = Form(""),
    part: str = Form(""),
    turn_key: str = Form(""),
    exam_transcript: str = Form(""),
    fairness_version: str = Form(""),
    recognizer: str = Form("webspeech"),
    raw_s: str = Form(""),
    pauses_over_2s: str = Form(""),
    longest_pause_s: str = Form(""),
    clipped_ratio: str = Form(""),
    authorization: str | None = Header(None),
) -> ExamPronunciationPostResponse:
    # 1-3. Access mode, JWT (user_id from `sub` only), consent.
    user_id = _authorize(authorization)
    db = _require_db()
    await require_speaking_consent(db, user_id)

    session_id = _validate_session_id(session_id)
    if part not in _PARTS:
        raise HTTPException(status_code=422, detail="part must be rolePlay, topic1 or topic2")
    turn_key = (turn_key or "").strip()
    if not turn_key.isdigit() or len(turn_key) > 20:
        raise HTTPException(status_code=422, detail="turn_key must be the candidate entry's seq")
    exam_transcript = (exam_transcript or "").strip()
    if not exam_transcript or len(exam_transcript) > 5000:
        raise HTTPException(status_code=422, detail="exam_transcript is required (a typed or empty turn has no audio to assess)")
    fairness_version = (fairness_version or "").strip()
    if not fairness_version or len(fairness_version) > 64:
        raise HTTPException(status_code=422, detail="fairness_version is required")
    if recognizer not in _RECOGNIZERS:
        raise HTTPException(status_code=422, detail="recognizer must be webspeech or whisper")
    raw_seconds = _optional_float(raw_s, "raw_s", upper=3600)
    pauses_n = _optional_float(pauses_over_2s, "pauses_over_2s", upper=10_000)
    longest = _optional_float(longest_pause_s, "longest_pause_s", upper=3600)
    clipped = _optional_float(clipped_ratio, "clipped_ratio", upper=1)
    if audio is None:
        raise HTTPException(status_code=422, detail="audio is required (a typed turn has no audio to assess)")

    raw = await _read_upload(audio)
    pcm = read_pcm_wav(raw)
    if pcm is None:
        raise HTTPException(status_code=415, detail="audio must be a PCM WAV (16 kHz mono)")
    trimmed_s = round(pcm.duration_s, 3)
    if trimmed_s <= 0:
        raise HTTPException(status_code=422, detail="audio is empty")
    if trimmed_s > _MAX_TURN_SECONDS:
        raise HTTPException(status_code=413, detail="audio is longer than one exam turn can be")

    request.state.obs_extra = {"audio_ms": round(trimmed_s * 1000), "part": part}

    # 4. Cache: the evidence row is the cache.
    stored = await _fetch_turn(db, user_id, session_id, turn_key)
    if stored is not None:
        request.state.obs_cached = True
        return ExamPronunciationPostResponse(status="done", turn=_row_to_turn(stored), cached=True)

    if not azure_client.is_configured():
        raise HTTPException(status_code=503, detail={"error": "pronunciation_unavailable"})

    # 5. Daily quota, charged per part (every turn of the part replays the key).
    quota_key = f"exam-pron:{session_id}:{part}"
    await consume_ai_quota_or_503(db, user_id, "exam_pronunciation", quota_key)

    async def release_part_grant_if_unused() -> None:
        if not await _part_has_stored_turn(db, user_id, session_id, part):
            await release_ai_quota_grant(db, user_id, QUOTA_FEATURE, quota_key)

    try:
        # 7. Whisper reference transcript of this same audio.
        whisper_data, transcription_failed = await _transcribe(raw)
        heard = (whisper_data.get("text") or "").strip()
        if transcription_failed:
            await release_part_grant_if_unused()
            return ExamPronunciationPostResponse(status="failed", reason="transcription_failed")
        if transcript_is_unusable(heard):
            # Nothing Azure could grade freeform against; nothing billed, not stored.
            await release_part_grant_if_unused()
            turn = ExamPronunciationTurn(
                sessionId=session_id, part=part, turnKey=turn_key,
                assessorVersion=EXAM_PRONUNCIATION_ASSESSOR_VERSION, fairnessVersion=fairness_version,
                examTranscript=exam_transcript, referenceText=heard, words=[],
                couldNotAssess=True, couldNotAssessReason="no_speech_recognized",
                singleRecognizer=recognizer == "whisper", rawS=raw_seconds, trimmedS=trimmed_s,
            )
            return ExamPronunciationPostResponse(status="done", turn=turn)

        # 6 + 8. Reserve and assess each chunk.
        planned = plan_chunks(whisper_data, trimmed_s)
        run = await _assess_chunks(
            db=db, user_id=user_id, session_id=session_id, part=part, turn_key=turn_key,
            pcm=pcm, planned=planned,
        )
        request.state.obs_provider = "azure"
        request.state.obs_extra = {**request.state.obs_extra, "chunk_count": len(planned)}
        if run.budget_exhausted:
            await release_part_grant_if_unused()
            return ExamPronunciationPostResponse(status="budget_exhausted")
        if all(r is None for r in run.raw_results):
            await release_part_grant_if_unused()
            return ExamPronunciationPostResponse(status="failed", reason="assessment_failed")

        # 9. Store and return.
        result = build_evidence(run, planned, exam_transcript=exam_transcript,
                                single_recognizer=recognizer == "whisper")
        if pauses_n is not None and longest is not None:
            result["pauseStats"] = {"pausesOver2s": int(pauses_n), "longestPauseS": longest}
        result["clippedRatio"] = clipped
        row = {
            "user_id": user_id,
            "session_id": session_id,
            "part": part,
            "turn_key": turn_key,
            "assessor_version": EXAM_PRONUNCIATION_ASSESSOR_VERSION,
            "fairness_version": fairness_version,
            "exam_transcript": exam_transcript,
            "reference_text": heard,
            "result": result,
            "suppressed": [
                {"index": i, "word": w["word"], "reasons": w["suppressed"]}
                for i, w in enumerate(result["words"]) if w["suppressed"]
            ],
            "raw_s": raw_seconds,
            "trimmed_s": trimmed_s,
        }
        try:
            stored = await _store_turn(db, row)
        except Exception as e:
            # E.g. the profile was deleted mid-request (FK): no orphan row.
            log.warning("exam pronunciation: evidence insert failed: %s", e)
            await release_part_grant_if_unused()
            return ExamPronunciationPostResponse(status="failed", reason="store_failed")
        return ExamPronunciationPostResponse(status="done", turn=_row_to_turn(stored))
    except HTTPException:
        raise
    except BaseException:
        await release_part_grant_if_unused()
        raise


@router.get("/pronunciation", response_model=ExamPronunciationGetResponse)
async def exam_pronunciation_stored(
    session_id: str = Query(""),
    authorization: str | None = Header(None),
) -> ExamPronunciationGetResponse:
    user_id = _authorize(authorization)
    session_id = _validate_session_id(session_id)
    db = _require_db()
    res = await asyncio.to_thread(
        lambda: db.table(TABLE).select("*")
        .eq("user_id", user_id).eq("session_id", session_id)
        .eq("assessor_version", EXAM_PRONUNCIATION_ASSESSOR_VERSION)
        .execute()
    )
    rows = sorted(res.data or [], key=lambda r: int(r["turn_key"]) if str(r.get("turn_key", "")).isdigit() else 0)
    return ExamPronunciationGetResponse(
        sessionId=session_id,
        assessorVersion=EXAM_PRONUNCIATION_ASSESSOR_VERSION,
        turns=[_row_to_turn(r) for r in rows],
    )
