"""POST /api/native-voice — a natural French voice for an AI-corrected sentence
(Pronunciation Lab Stage 4b; owner decision D2, main repo
docs/plans/pronunciation-lab.md §4.2, §9).

The app plays "▶ Native" from pre-recorded Azure Neural MP3s in the public
`tts-audio` bucket, keyed by `lib/voice_key.py` (= the app's voiceKey.ts). Only
static authored French is recorded by the "Record voices" workflow, so an
AI-corrected sentence has no file. When the app finds no file for such a
sentence it calls this route once; the route synthesises the sentence with
Azure text-to-speech and stores it under the very path the app plays from
(write-through), then answers with that path. The app plays the file from the
bucket as it would any recording. This is the one place Azure TTS is called at
request time — the reversal of the recorded-voices rule that D2 approved, for
this use only.

- A sentence already in the bucket (a recording, or cached by an earlier call
  by anyone) is answered `cached: true` with no charge and no Azure call.
- A new sentence charges `native_voice` (ai_quota_limits; 20 per account per
  day, idempotency key = the object path, so asking twice costs once) and
  reserves its characters in the azure_tts_usage ledger against the monthly
  azure_tts_budget cap (migration 20261028090000). A failed synthesis or
  upload releases both.
- Guests are let through while guest AI is on (lib/guest.py), capped by the
  in-process guest counter; their characters are still metered.
- The sentence is synthesised with the same SSML, voice and MP3 format as
  `scripts/tts/recordVoices.ts`, so a cached sentence sounds like a recorded one.
- No text is logged or stored beyond the MP3 itself; the ledger keeps only the
  character count and the object path.

Failure posture: the app treats every non-200 as "no recording" and falls back
to the browser's French voice, so nothing here can break a screen. Expected
refusals are 4xx (invalid text, quota, monthly cap, Azure's own rate limit);
a real provider/storage failure is a 502 raised from the cause (Sentry reports
it once, with the cause).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from lib.ai_quota import consume_ai_quota_or_503, release_ai_quota_grant
from lib.auth import verify_supabase_jwt
from lib.guest import is_guest, verify_user_or_guest
from lib.voice_key import RECORDED_VOICES, DEFAULT_VOICE, TTS_BUCKET, normalize_spoken_text, voice_object_path

log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/api", tags=["native-voice"])

# Also the literal at the consume call (tests/test_quota_features_seeded.py reads literals).
QUOTA_FEATURE = "native_voice"
# A corrected Learn answer is a few sentences; anything longer is not a
# sentence to listen to (and the bucket's per-file limit is 1 MB ≈ 2.5 min).
MAX_CHARACTERS = 600
# Same as scripts/tts/recordVoices.ts, so a cached sentence matches a recorded one.
SENTENCE_PAUSE = "250ms"
OUTPUT_FORMAT = "audio-24khz-48kbitrate-mono-mp3"
MIN_AUDIO_BYTES = 1024
REQUEST_TIMEOUT_S = 20.0
CACHE_CONTROL = "max-age=31536000"

# Controls other than whitespace (normalisation already turned whitespace into spaces).
_CONTROL = re.compile(r"[\u0000-\u001f\u007f-\u009f]")
_LETTER = re.compile(r"[^\W\d_]")


class NativeVoiceRequest(BaseModel):
    text: str = Field(..., max_length=2000)


class NativeVoiceResponse(BaseModel):
    status: str = "ready"
    path: str
    cached: bool


@dataclass
class _Reservation:
    granted: bool
    reservation_id: str | None = None
    reason: str | None = None


# ── Configuration ────────────────────────────────────────────────────────────

_supabase_admin = None


def _db():
    """Lazy service-role Supabase client (same pattern as routers/pronunciation.py)."""
    global _supabase_admin
    if _supabase_admin is None:
        url, key = _storage_config() or ("", "")
        if not (url and key):
            return None
        from supabase import create_client
        _supabase_admin = create_client(url, key)
    return _supabase_admin


def _storage_config() -> tuple[str, str] | None:
    url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
    return (url, key) if url and key else None


def _azure_config() -> tuple[str, str] | None:
    key = os.getenv("AZURE_SPEECH_KEY", "").strip()
    region = os.getenv("AZURE_SPEECH_REGION", "").strip()
    return (key, region) if key and region else None


def _unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail={"error": "native_voice_unavailable"})


# ── Text ─────────────────────────────────────────────────────────────────────

def _validated_text(raw: str) -> str:
    text = normalize_spoken_text(raw)
    if not text or not _LETTER.search(text) or _CONTROL.search(text):
        raise HTTPException(status_code=422, detail={"error": "invalid_text"})
    if len(text) > MAX_CHARACTERS:
        raise HTTPException(status_code=422, detail={"error": "text_too_long", "max": MAX_CHARACTERS})
    return text


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;")
    )


def build_ssml(text: str, voice: str = DEFAULT_VOICE) -> str:
    """scripts/tts/recordVoices.ts's buildSsml, byte for byte."""
    return (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xmlns:mstts="http://www.w3.org/2001/mstts" xml:lang="fr-FR">'
        f'<voice name="{RECORDED_VOICES[voice]}"><mstts:silence type="Sentenceboundary" value="{SENTENCE_PAUSE}"/>'
        f"{_xml_escape(text)}</voice></speak>"
    )


# ── Storage ──────────────────────────────────────────────────────────────────

def _public_url(base: str, path: str) -> str:
    return f"{base}/storage/v1/object/public/{TTS_BUCKET}/{path}"


async def _exists(base: str, path: str) -> bool:
    """True when the bucket already serves this file. Any doubt reads as
    missing: the upload then answers 'already exists', which is fine."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.head(_public_url(base, path))
        return res.status_code == 200
    except httpx.HTTPError:
        return False


class _ProviderFailure(Exception):
    """Azure or Storage failed after the charge; the route releases and 502s."""


class _AzureBusy(Exception):
    """Azure's own rate limit or quota (F0: 20 requests a minute, 500k characters a month)."""


async def _upload(base: str, service_key: str, path: str, audio: bytes) -> None:
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
        res = await client.post(
            f"{base}/storage/v1/object/{TTS_BUCKET}/{path}",
            headers={
                "apikey": service_key,
                "Authorization": f"Bearer {service_key}",
                "Content-Type": "audio/mpeg",
                # Content-addressed and never overwritten, like the recorded lines.
                "cache-control": CACHE_CONTROL,
                "x-upsert": "false",
            },
            content=audio,
        )
    if res.status_code < 300:
        return
    body = res.text[:200]
    # Someone else stored it first (a concurrent request, or the recording run): same text, same file.
    if res.status_code == 409 or re.search(r"duplicate|already exists", body, re.I):
        return
    raise _ProviderFailure(f"storage upload {res.status_code}: {body}")


# ── Azure text-to-speech ─────────────────────────────────────────────────────

async def _synthesize(key: str, region: str, text: str) -> bytes:
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
            res = await client.post(
                f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1",
                headers={
                    "Ocp-Apim-Subscription-Key": key,
                    "Content-Type": "application/ssml+xml",
                    "X-Microsoft-OutputFormat": OUTPUT_FORMAT,
                    "User-Agent": "french-coach-native-voice",
                },
                content=build_ssml(text).encode("utf-8"),
            )
    except httpx.HTTPError as e:
        raise _ProviderFailure(f"azure tts request failed: {type(e).__name__}") from e
    if res.status_code == 429:
        raise _AzureBusy()
    if res.status_code != 200:
        raise _ProviderFailure(f"azure tts {res.status_code}: {res.text[:200]}")
    if not res.headers.get("content-type", "").startswith("audio/mpeg"):
        raise _ProviderFailure(f"azure tts returned {res.headers.get('content-type') or 'no content type'}")
    if len(res.content) < MIN_AUDIO_BYTES:
        raise _ProviderFailure(f"azure tts returned only {len(res.content)} bytes")
    return res.content


# ── Character ledger (azure_tts_usage) ───────────────────────────────────────

def _ledger_user_id(user_id: str) -> str | None:
    return None if is_guest(user_id) else user_id


async def _reserve_characters(db, user_id: str, path: str, characters: int) -> _Reservation:
    """Fails CLOSED: with no ledger there is no monthly cap, and a guest has
    no other Supabase-backed limit, so an unreadable ledger is a 503."""
    if db is None:
        raise _unavailable()
    try:
        res = await asyncio.to_thread(
            lambda: db.rpc(
                "reserve_azure_tts_characters",
                {"p_user_id": _ledger_user_id(user_id), "p_object_path": path, "p_characters": characters},
            ).execute()
        )
        data = res.data or {}
        if "granted" not in data:
            raise ValueError(f"malformed reserve_azure_tts_characters response: {data!r}")
    except Exception as e:
        log.warning("native_voice: reserve_azure_tts_characters failed: %s", e)
        raise _unavailable() from e
    if not data.get("granted"):
        return _Reservation(granted=False, reason=data.get("reason") or "budget_exhausted")
    rid = data.get("reservation_id")
    return _Reservation(granted=True, reservation_id=str(rid) if rid else None)


async def _release_characters(db, reservation: _Reservation) -> None:
    """Never raises: a failed release over-counts, the safe direction."""
    if db is None or not reservation.reservation_id:
        return
    try:
        await asyncio.to_thread(
            lambda: db.rpc("release_azure_tts_characters", {"p_reservation_id": reservation.reservation_id}).execute()
        )
    except Exception as e:
        log.warning("native_voice: release_azure_tts_characters failed (row stays reserved): %s", e)


# ── Route ────────────────────────────────────────────────────────────────────

@router.post("/native-voice", response_model=NativeVoiceResponse)
async def native_voice(
    body: NativeVoiceRequest,
    request: Request,
    authorization: str | None = Header(None),
) -> NativeVoiceResponse:
    user_id = verify_user_or_guest(authorization, request, lambda a: str(verify_supabase_jwt(a).get("sub") or ""))
    text = _validated_text(body.text)

    storage = _storage_config()
    if storage is None:
        raise _unavailable()
    base, service_key = storage
    path = voice_object_path(text)

    if await _exists(base, path):
        return NativeVoiceResponse(path=path, cached=True)

    azure = _azure_config()
    if azure is None:
        raise _unavailable()

    db = _db()
    idempotency_key = f"native_voice:{path}"
    await consume_ai_quota_or_503(db, user_id, "native_voice", idempotency_key)

    try:
        reservation = await _reserve_characters(db, user_id, path, len(text))
    except BaseException:
        await release_ai_quota_grant(db, user_id, QUOTA_FEATURE, idempotency_key)
        raise
    if not reservation.granted:
        await release_ai_quota_grant(db, user_id, QUOTA_FEATURE, idempotency_key)
        raise HTTPException(status_code=429, detail={"error": "native_voice_budget_exhausted"})

    try:
        audio = await _synthesize(azure[0], azure[1], text)
        await _upload(base, service_key, path, audio)
    except _AzureBusy:
        await _release_characters(db, reservation)
        await release_ai_quota_grant(db, user_id, QUOTA_FEATURE, idempotency_key)
        raise HTTPException(status_code=429, detail={"error": "native_voice_busy"}, headers={"Retry-After": "60"})
    except _ProviderFailure as e:
        await _release_characters(db, reservation)
        await release_ai_quota_grant(db, user_id, QUOTA_FEATURE, idempotency_key)
        log.warning("native_voice: %s", e)
        raise HTTPException(status_code=502, detail={"error": "native_voice_failed"}) from e
    except BaseException:
        await _release_characters(db, reservation)
        await release_ai_quota_grant(db, user_id, QUOTA_FEATURE, idempotency_key)
        raise

    log.info("native_voice stored %s (%d characters)", path, len(text))
    return NativeVoiceResponse(path=path, cached=False)


def set_rate_limiter(rate_limit_decorator, target_router) -> None:
    """Applies main.py's slowapi limit after include_router (see
    routers/pronunciation.py's set_rate_limiter for why it is post-hoc)."""
    limited = rate_limit_decorator("20/minute")(native_voice)
    for route in target_router.routes:
        if getattr(route, "path", None) == "/api/native-voice":
            route.endpoint = limited
            route.dependant.call = limited
