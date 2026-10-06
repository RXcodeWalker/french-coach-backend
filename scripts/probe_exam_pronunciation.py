"""Record the exam-pronunciation calibration fixtures (exam-pronunciation plan,
Batch 7; main repo docs/systems/exam-pronunciation.md, "Calibration").

Live and manual — CI never runs this. For each clip in a calibration
manifest (written by the main repo's `npm run pronunciation:calibration:prepare`,
which trims and measures each clip with the production client code) it:

  1. transcribes the trimmed WAV with the production Groq Whisper call
     (main._groq_whisper) — the freeform reference text, as in the route
  2. plans chunks with the route's plan_chunks (Azure's 30 s REST limit)
  3. reserves each chunk's seconds in the azure_speech_usage ledger as
     source `probe` (when SUPABASE_URL/SUPABASE_SERVICE_KEY are set; otherwise
     it runs unmetered and says so), POSTs it with the production Azure
     request (azure_client._post_to_azure, freeform, same content type),
     then settles or releases the reservation
  4. writes tests/fixtures/exam_pronunciation_calibration/<set>/<clipId>.json
     holding the Whisper output, the chunk plan, Azure's raw JSON per chunk,
     and the evidence those replay to (services/pronunciation/calibration_replay.py)

The audio itself is never written into the fixtures, and nothing is stored in
exam_pronunciation_evidence. Existing fixtures are kept unless --force.

Usage (from backend/, with backend/.env holding the keys):
    python scripts/probe_exam_pronunciation.py --manifest <prepared dir>/manifest.json
        [--only <clipId> ...] [--force]

Requires AZURE_SPEECH_KEY, AZURE_SPEECH_REGION and GROQ_API_KEY.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)  # main.py's load_dotenv() reads backend/.env

FIXTURE_DIR = BACKEND_DIR / "tests" / "fixtures" / "exam_pronunciation_calibration"
LEDGER_SOURCE = "probe"
SETS = ("clear", "accented", "unclear")


def _fail(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(2)


def _ledger_db():
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
    if not (url and key):
        print("note: SUPABASE_URL/SUPABASE_SERVICE_KEY not set — Azure calls are NOT metered in the ledger")
        return None
    from supabase import create_client
    return create_client(url, key)


async def _record_clip(entry: dict, wav: bytes, db, main, *, fairness_version: str) -> dict:
    from lib.azure_budget import release_azure_seconds, reserve_azure_seconds, settle_azure_seconds, wav_duration_seconds
    from routers.exam_pronunciation import EXAM_PRONUNCIATION_ASSESSOR_VERSION, plan_chunks
    from services.pronunciation import azure_client
    from services.pronunciation.calibration_replay import replay_turn
    from services.pronunciation.transcript import transcript_is_unusable
    from services.pronunciation.wav import read_pcm_wav, slice_wav

    pcm = read_pcm_wav(wav)
    if pcm is None:
        raise ValueError("not a PCM WAV")
    trimmed_s = round(pcm.duration_s, 3)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
        tmp.write(wav)
        tmp_path = tmp.name
    try:
        whisper = await main._groq_whisper(tmp_path, "fr")
    finally:
        os.unlink(tmp_path)

    chunks: list[dict] = []
    if not transcript_is_unusable((whisper.get("text") or "").strip()):
        key, region = azure_client._is_configured()
        content_type = azure_client._content_type_for("chunk.wav")
        for (start, end), reference in plan_chunks(whisper, trimmed_s):
            chunk = {"window": [start, end], "referenceText": reference, "azureRaw": None}
            chunks.append(chunk)
            if not reference:
                continue
            audio = slice_wav(pcm, start, end)
            seconds = wav_duration_seconds(audio) or round(end - start, 3)
            reservation = await reserve_azure_seconds(
                db, user_id=None, source=LEDGER_SOURCE, seconds=seconds,
                session_id=f"calibration:{entry['clipId']}", part=None, turn_key=None,
            )
            if not reservation.granted:
                raise RuntimeError("Azure monthly budget exhausted (azure_speech_budget.cap_seconds reached)")

            async def operation(audio=audio, reference=reference):
                return await azure_client._post_to_azure(
                    key, region, audio, reference, content_type=content_type, enable_miscue=False,
                )
            try:
                chunk["azureRaw"] = await main._run_with_retries("azure-speech", operation)
            except BaseException:
                await release_azure_seconds(db, reservation)
                raise
            await settle_azure_seconds(db, reservation, seconds)

    clip = {k: entry.get(k) for k in (
        "clipId", "set", "reading", "source", "recognizer", "examTranscript", "expectedReported",
        "knownMisreadings", "rawS", "trimmedS", "pausesOver2s", "longestPauseS", "clippedRatio",
    )}
    clip["trimmedS"] = trimmed_s
    return {
        "fixtureFormat": 1,
        "recordedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "assessorVersion": EXAM_PRONUNCIATION_ASSESSOR_VERSION,
        "fairnessVersion": fairness_version,
        "azureRegion": os.getenv("AZURE_SPEECH_REGION", "").strip(),
        "clip": clip,
        "whisper": whisper,
        "chunks": chunks,
        "evidence": replay_turn(clip, whisper, chunks, fairness_version=fairness_version),
    }


async def _main(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    fairness_version = manifest.get("fairnessVersion") or _fail("manifest has no fairnessVersion")
    entries = [e for e in manifest["clips"] if not args.only or e["clipId"] in args.only]
    if not entries:
        _fail("no clips selected")

    for name in ("AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION", "GROQ_API_KEY"):
        if not os.getenv(name, "").strip():
            from dotenv import load_dotenv
            load_dotenv(BACKEND_DIR / ".env")
            if not os.getenv(name, "").strip():
                _fail(f"{name} is not set")

    import main  # noqa: E402 — the production Whisper call and retry helper
    db = _ledger_db()

    recorded = skipped = failed = 0
    for entry in entries:
        if entry["set"] not in SETS:
            _fail(f"{entry['clipId']}: unknown set {entry['set']!r}")
        out = FIXTURE_DIR / entry["set"] / f"{entry['clipId']}.json"
        if out.exists() and not args.force:
            skipped += 1
            continue
        wav = (manifest_path.parent / entry["wavPath"]).read_bytes()
        try:
            fixture = await _record_clip(entry, wav, db, main, fairness_version=fairness_version)
        except Exception as e:  # keep going; a failed clip is reported, not silently dropped
            failed += 1
            print(f"FAILED {entry['clipId']}: {e}", file=sys.stderr)
            if "budget exhausted" in str(e) or e.__class__.__name__ == "AzureQuotaExceeded":
                break
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        recorded += 1
        print(f"recorded {entry['set']}/{entry['clipId']} ({fixture['clip']['trimmedS']} s, "
              f"{len(fixture['chunks'])} chunk(s))")

    print(f"done: {recorded} recorded, {skipped} kept (use --force to re-record), {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--only", nargs="*", default=[])
    parser.add_argument("--force", action="store_true")
    sys.exit(asyncio.run(_main(parser.parse_args())))
