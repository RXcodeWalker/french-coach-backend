"""Record the Pronunciation Lab calibration fixtures (main repo
docs/plans/pronunciation-lab.md §7 — the gate before word marks go live).

Live and manual — CI never runs this. For each recording in a manifest (written
by the main repo's `npm run lab:calibration:prepare`) it:

  1. converts the file to a 16 kHz mono 16-bit WAV with ffmpeg — what the Lab's
     `normalizeToWav16kMono` sends; no silence is trimmed (the Lab does not trim)
  2. reserves its seconds in the azure_speech_usage ledger as source `probe`
     (when SUPABASE_URL/SUPABASE_SERVICE_KEY are set; otherwise it runs
     unmetered and says so)
  3. POSTs it to Azure with the production scripted request
     (azure_client.assess_pronunciation, mode="scripted", the target sentence as
     the reference, the production retry helper) — the same code path as
     POST /api/pronunciation for a Lab take
  4. settles or releases the reservation, and writes
     tests/fixtures/pronunciation_lab_calibration/<clipId>.json

The audio is never written into the fixtures. Existing fixtures are kept unless
--force. Azure F0 allows one request at a time and ~20 a minute; a full set
(40 + 20 per speaker clips of a few seconds) is a few minutes.

Usage (from backend/, with backend/.env holding the keys, ffmpeg installed):
    python scripts/probe_lab_calibration.py --manifest <folder>/manifest.json
        [--only <clipId> ...] [--force] [--dry-run] [--out-dir <folder>]

`--out-dir` writes the fixtures somewhere else (smoke tests, practice runs). Only
fixtures in tests/fixtures/pronunciation_lab_calibration/ are the evidence the
release gate reads.

Requires AZURE_SPEECH_KEY and AZURE_SPEECH_REGION. Then, in the main repo:
    npm run lab:calibration:report
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)  # main.py's load_dotenv() reads backend/.env

FIXTURE_DIR = BACKEND_DIR / "tests" / "fixtures" / "pronunciation_lab_calibration"
LEDGER_SOURCE = "probe"


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


def convert_to_wav(path: Path) -> bytes:
    """Any audio file -> 16 kHz mono 16-bit PCM WAV bytes (what the Lab uploads)."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "clip.wav"
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(path), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0 or not out.exists():
            raise RuntimeError(f"ffmpeg could not read {path.name}: {proc.stderr.strip()[:200]}")
        return out.read_bytes()


async def _record_clip(entry: dict, wav: bytes, db, main) -> dict:
    from lib.azure_budget import release_azure_seconds, reserve_azure_seconds, settle_azure_seconds, wav_duration_seconds
    from routers.pronunciation import ASSESSOR_VERSION
    from services.pronunciation import azure_client
    from services.pronunciation.lab_calibration import MAX_CLIP_SECONDS, MIN_CLIP_SECONDS, build_fixture

    seconds = wav_duration_seconds(wav)
    if seconds is None:
        raise ValueError("ffmpeg produced something that is not a PCM WAV")
    if seconds < MIN_CLIP_SECONDS:
        raise ValueError(f"too short ({seconds:.2f} s) — the Lab refuses clips under {MIN_CLIP_SECONDS} s")
    if seconds > MAX_CLIP_SECONDS:
        raise ValueError(f"too long ({seconds:.1f} s) — a Lab take is one sentence (limit {MAX_CLIP_SECONDS:.0f} s)")

    reservation = await reserve_azure_seconds(
        db, user_id=None, source=LEDGER_SOURCE, seconds=seconds,
        session_id=f"lab-calibration:{entry['clipId']}", part=None, turn_key=None,
    )
    if not reservation.granted:
        raise PermissionError("Azure monthly budget exhausted (azure_speech_budget.cap_seconds reached)")
    try:
        result = await azure_client.assess_pronunciation(
            wav, entry["targetText"], audio_filename="clip.wav", mode="scripted",
            run_with_retries=main._run_with_retries,
        )
    except BaseException:
        await release_azure_seconds(db, reservation)
        raise
    if result is None:
        await release_azure_seconds(db, reservation)
        raise RuntimeError("Azure Speech is not configured (AZURE_SPEECH_KEY / AZURE_SPEECH_REGION)")
    await settle_azure_seconds(db, reservation, seconds)
    return build_fixture(
        entry, result, duration_s=seconds, assessor_version=ASSESSOR_VERSION,
        azure_region=os.getenv("AZURE_SPEECH_REGION", "").strip(),
    )


async def _main(args: argparse.Namespace) -> int:
    from services.pronunciation.lab_calibration import ManifestError, validate_manifest

    manifest_path = Path(args.manifest).resolve()
    out_dir = Path(args.out_dir).resolve() if args.out_dir else FIXTURE_DIR
    try:
        entries = validate_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))
    except (ManifestError, json.JSONDecodeError, OSError) as e:
        _fail(str(e))
    entries = [e for e in entries if not args.only or e["clipId"] in args.only]
    if not entries:
        _fail("no clips selected")

    missing = [e["clipId"] for e in entries if not (manifest_path.parent / e["audioPath"]).is_file()]
    if missing:
        _fail(f"audio file missing for: {', '.join(missing[:10])}{' …' if len(missing) > 10 else ''}")

    if args.dry_run:
        print(f"dry run: {len(entries)} clips look usable; nothing sent to Azure.")
        for e in entries[:5]:
            print(f"  {e['clipId']}: {e['kind']} / {e['speaker']} — “{e['targetText']}”")
        return 0

    for name in ("AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION"):
        if not os.getenv(name, "").strip():
            from dotenv import load_dotenv
            load_dotenv(BACKEND_DIR / ".env")
            if not os.getenv(name, "").strip():
                _fail(f"{name} is not set")

    import main  # noqa: E402 — the production retry helper
    db = _ledger_db()

    recorded = skipped = failed = 0
    for entry in entries:
        out = out_dir / f"{entry['clipId']}.json"
        if out.exists() and not args.force:
            skipped += 1
            continue
        try:
            wav = convert_to_wav(manifest_path.parent / entry["audioPath"])
            fixture = await _record_clip(entry, wav, db, main)
        except Exception as e:  # keep going; a failed clip is reported, not silently dropped
            failed += 1
            print(f"FAILED {entry['clipId']}: {e}", file=sys.stderr)
            # Budget, Azure quota and bad credentials fail every later clip the same way.
            if isinstance(e, PermissionError) or "budget exhausted" in str(e) \
                    or e.__class__.__name__ == "AzureQuotaExceeded":
                break
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        recorded += 1
        flag = " (could not assess)" if fixture["response"].get("couldNotAssess") else ""
        print(f"recorded {entry['clipId']} ({fixture['audio']['durationS']} s){flag}")

    print(f"done: {recorded} recorded, {skipped} kept (use --force to re-record), {failed} failed")
    where = f" -- --fixtures {out_dir}" if out_dir != FIXTURE_DIR else ""
    print(f"next: in the main repo, `npm run lab:calibration:report{where}`")
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--only", nargs="*", default=[])
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out-dir", default=None)
    sys.exit(asyncio.run(_main(parser.parse_args())))
