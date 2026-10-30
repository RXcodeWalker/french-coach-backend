"""Pure helpers for the Pronunciation Lab calibration probe (main repo plan
docs/plans/pronunciation-lab.md §7; scripts/probe_lab_calibration.py).

No network, no ffmpeg, no environment: manifest validation and the shape of the
recorded fixture. The fixture's `response` is exactly what POST /api/pronunciation
returns for a scripted take (the normalised Azure result plus `mode`/`locale`/
`assessorVersion`), which is what the main repo's `buildLabTake` consumes — so
the main repo's report judges what a learner would have seen.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

FIXTURE_FORMAT = 1
CLIP_KINDS = ("detect-correct", "detect-swap", "fair", "common-voice")
SPEAKER_KINDS = ("owner", "accented", "common-voice")
CLIP_ID = re.compile(r"^[A-Za-z0-9._-]+$")

# The Lab uploads a 16 kHz mono WAV of one sentence or one word. Azure's REST
# short-audio limit is 60 s; the client refuses clips under 0.4 s.
MIN_CLIP_SECONDS = 0.4
MAX_CLIP_SECONDS = 60.0

_CLIP_FIELDS = ("clipId", "kind", "speaker", "speakerKind", "targetText", "sentenceId", "swapWord", "swapTo")


class ManifestError(ValueError):
    """The manifest cannot be probed; the message names the clip and the problem."""


def validate_manifest(manifest: Any) -> list[dict[str, Any]]:
    """Returns the manifest's clip entries, or raises ManifestError."""
    if not isinstance(manifest, dict) or manifest.get("manifestFormat") != 1:
        raise ManifestError("not a manifest (manifestFormat 1) — write it with `npm run lab:calibration:prepare`")
    clips = manifest.get("clips")
    if not isinstance(clips, list) or not clips:
        raise ManifestError("the manifest has no clips")
    seen: set[str] = set()
    for entry in clips:
        clip_id = entry.get("clipId") if isinstance(entry, dict) else None
        if not isinstance(clip_id, str) or not CLIP_ID.match(clip_id):
            raise ManifestError(f"bad clipId {clip_id!r}")
        if clip_id in seen:
            raise ManifestError(f"{clip_id}: duplicate clipId")
        seen.add(clip_id)
        if entry.get("kind") not in CLIP_KINDS:
            raise ManifestError(f"{clip_id}: unknown kind {entry.get('kind')!r}")
        if entry.get("speakerKind") not in SPEAKER_KINDS:
            raise ManifestError(f"{clip_id}: unknown speakerKind {entry.get('speakerKind')!r}")
        if not str(entry.get("speaker") or "").strip():
            raise ManifestError(f"{clip_id}: no speaker")
        if not str(entry.get("targetText") or "").strip():
            raise ManifestError(f"{clip_id}: no targetText")
        if not str(entry.get("audioPath") or "").strip():
            raise ManifestError(f"{clip_id}: no audioPath")
        if entry["kind"] == "detect-swap" and not (entry.get("swapWord") and entry.get("swapTo")):
            raise ManifestError(f"{clip_id}: a swap reading needs swapWord and swapTo")
    return clips


def build_fixture(
    entry: dict[str, Any],
    result: dict[str, Any],
    *,
    duration_s: float,
    assessor_version: str,
    azure_region: str,
    recorded_at: datetime | None = None,
) -> dict[str, Any]:
    """The fixture file's contents for one probed clip. The audio itself is never stored."""
    response = dict(result)
    response["mode"] = "scripted"
    response.setdefault("locale", "fr-FR")
    response["assessorVersion"] = assessor_version
    stamp = (recorded_at or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    return {
        "fixtureFormat": FIXTURE_FORMAT,
        "recordedAt": stamp,
        "assessorVersion": assessor_version,
        "azureRegion": azure_region,
        "clip": {k: entry.get(k) for k in _CLIP_FIELDS},
        "audio": {"durationS": round(duration_s, 3)},
        "response": response,
    }
