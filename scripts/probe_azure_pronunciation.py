"""Phase 0 probe (accent-analyzer plan, Implementation order step 1).

One-shot, throwaway script — not part of the app. Synthesizes a French
sentence via Azure TTS, then POSTs it to the Pronunciation Assessment REST
endpoint twice:

  1. "as-is": exactly the request services/pronunciation/azure_client.py
     sends today (webm content-type, no EnableMiscue) — to confirm defects
     #1 and #7 reproduce against a real resource.
  2. "corrected": wav/pcm content-type + EnableMiscue=True — to capture a
     real successful REST response shape and resolve the three §18 unknowns:
       (a) does NBestPhonemeCount surface Words[].Phonemes[].NBestPhonemes?
       (b) does the legacy {region}.stt.speech... host still work?
       (c) which JSON shape (flat vs SDK-nested) does this resource return?

Writes raw JSON responses to backend/scripts/_probe_output/ for inspection
and for use as a committed test fixture. Requires AZURE_SPEECH_KEY and
AZURE_SPEECH_REGION in the environment (loaded from backend/.env).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import wave
from pathlib import Path

import httpx

SCRIPT_DIR = Path(__file__).resolve().parent
OUT_DIR = SCRIPT_DIR / "_probe_output"
BACKEND_DIR = SCRIPT_DIR.parent

SENTENCE = "Je voudrais aller au marché avec ma mère demain matin."


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


async def synthesize_wav(key: str, region: str, text: str) -> bytes:
    """Azure TTS -> 16kHz mono PCM WAV (matches the format the REST
    pronunciation endpoint documents as its one guaranteed-good input)."""
    url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
    ssml = (
        '<speak version="1.0" xml:lang="fr-FR">'
        '<voice xml:lang="fr-FR" xml:gender="Female" name="fr-FR-DeniseNeural">'
        f"{text}"
        "</voice></speak>"
    )
    headers = {
        "Ocp-Apim-Subscription-Key": key,
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": "riff-16khz-16bit-mono-pcm",
        "User-Agent": "accent-analyzer-phase0-probe",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, headers=headers, content=ssml.encode("utf-8"))
    print(f"[TTS] status={resp.status_code} bytes={len(resp.content)}")
    resp.raise_for_status()
    return resp.content


def wav_info(wav_bytes: bytes) -> str:
    import io

    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        return (
            f"channels={w.getnchannels()} rate={w.getframerate()} "
            f"sampwidth={w.getsampwidth()} frames={w.getnframes()} "
            f"duration={w.getnframes() / w.getframerate():.2f}s"
        )


async def call_pronunciation_rest(
    *,
    key: str,
    region: str,
    audio_bytes: bytes,
    reference_text: str,
    content_type: str,
    enable_miscue: bool,
    nbest_phoneme_count: int | None,
    host_template: str,
    label: str,
) -> httpx.Response:
    params_obj: dict = {
        "ReferenceText": reference_text,
        "GradingSystem": "HundredMark",
        "Granularity": "Phoneme",
        "Dimension": "Comprehensive",
        "PhonemeAlphabet": "IPA",
        "EnableProsodyAssessment": "True",
        "EnableMiscue": enable_miscue,
    }
    if nbest_phoneme_count is not None:
        params_obj["NBestPhonemeCount"] = nbest_phoneme_count

    header_value = base64.b64encode(json.dumps(params_obj).encode("utf-8")).decode("ascii")
    url = host_template.format(region=region)
    query = {"language": "fr-FR", "format": "detailed"}
    headers = {
        "Ocp-Apim-Subscription-Key": key,
        "Content-Type": content_type,
        "Accept": "application/json",
        "Pronunciation-Assessment": header_value,
    }

    print(f"\n[{label}] POST {url}")
    print(f"[{label}] Content-Type={content_type!r} EnableMiscue={enable_miscue} NBestPhonemeCount={nbest_phoneme_count}")

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, params=query, headers=headers, content=audio_bytes)

    print(f"[{label}] status={resp.status_code}")
    return resp


async def main() -> int:
    _load_dotenv(BACKEND_DIR / ".env")
    key = os.getenv("AZURE_SPEECH_KEY", "").strip()
    region = os.getenv("AZURE_SPEECH_REGION", "").strip()
    if not key or not region:
        print("AZURE_SPEECH_KEY / AZURE_SPEECH_REGION not set — aborting probe.", file=sys.stderr)
        return 1

    OUT_DIR.mkdir(exist_ok=True)

    print(f"[setup] region={region} sentence={SENTENCE!r}")
    wav_bytes = await synthesize_wav(key, region, SENTENCE)
    (OUT_DIR / "synth.wav").write_bytes(wav_bytes)
    print(f"[setup] wav: {wav_info(wav_bytes)}")

    legacy_host = "https://{region}.stt.speech.microsoft.com/speech/recognition/conversation/cognitiveservices/v1"
    resource_host = "https://{region}.cognitiveservices.azure.com/stt/speech/recognition/conversation/cognitiveservices/v1"

    # 1. AS-IS: reproduce today's code exactly (webm content-type on real WAV
    #    bytes, no EnableMiscue) against the legacy host.
    resp_as_is = await call_pronunciation_rest(
        key=key,
        region=region,
        audio_bytes=wav_bytes,
        reference_text=SENTENCE,
        content_type="audio/webm; codecs=opus",
        enable_miscue=False,
        nbest_phoneme_count=None,
        host_template=legacy_host,
        label="AS-IS (defect repro)",
    )
    (OUT_DIR / "response_as_is.json").write_text(
        json.dumps({"status_code": resp_as_is.status_code, "body": _safe_json(resp_as_is)}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # 2. CORRECTED: proper content-type + EnableMiscue + NBestPhonemeCount,
    #    against the legacy host (resolves unknown (b) implicitly if this 200s).
    resp_corrected = await call_pronunciation_rest(
        key=key,
        region=region,
        audio_bytes=wav_bytes,
        reference_text=SENTENCE,
        content_type="audio/wav; codecs=audio/pcm; samplerate=16000",
        enable_miscue=True,
        nbest_phoneme_count=5,
        host_template=legacy_host,
        label="CORRECTED (legacy host)",
    )
    (OUT_DIR / "response_corrected_legacy_host.json").write_text(
        json.dumps({"status_code": resp_corrected.status_code, "body": _safe_json(resp_corrected)}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # 3. CORRECTED against the resource-scoped host, only if (2) failed —
    #    resolves unknown (b) explicitly.
    if resp_corrected.status_code >= 400:
        resp_resource_host = await call_pronunciation_rest(
            key=key,
            region=region,
            audio_bytes=wav_bytes,
            reference_text=SENTENCE,
            content_type="audio/wav; codecs=audio/pcm; samplerate=16000",
            enable_miscue=True,
            nbest_phoneme_count=5,
            host_template=resource_host,
            label="CORRECTED (resource host)",
        )
        (OUT_DIR / "response_corrected_resource_host.json").write_text(
            json.dumps(
                {"status_code": resp_resource_host.status_code, "body": _safe_json(resp_resource_host)},
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    print(f"\n[done] raw responses written to {OUT_DIR}")
    print("[done] inspect response_corrected_legacy_host.json for: NBest[0] shape (flat vs nested),")
    print("       RecognitionStatus type, Words[].Phonemes[].NBestPhonemes presence.")
    return 0


def _safe_json(resp: httpx.Response):
    try:
        return resp.json()
    except Exception:
        return {"_non_json_text": resp.text[:2000]}


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
