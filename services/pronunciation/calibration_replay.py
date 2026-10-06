"""Exam-pronunciation calibration: recorded Azure responses -> stored evidence
(exam-pronunciation plan, Batch 7; main repo docs/systems/exam-pronunciation.md).

The calibration fixtures (tests/fixtures/exam_pronunciation_calibration/)
hold, per clip, Whisper's transcript, the chunk plan and Azure's raw REST
JSON for each chunk. This module turns that recording into the exact turn
evidence POST /api/exam/pronunciation would have stored, using the route's
own pure functions (`build_evidence`, `_row_to_turn`) and the Azure client's
own normaliser — so CI replays the real assessor without calling Azure.

Used twice: scripts/probe_exam_pronunciation.py writes each fixture's
`evidence` with it, and tests/test_exam_pronunciation_calibration_replay.py
re-derives it, so a change to the assessor that would alter stored evidence
fails until the fixtures are re-recorded (and
EXAM_PRONUNCIATION_ASSESSOR_VERSION bumped, which the test also checks).

Calibration clips are not exam sessions: the session id, part and turn key
are fixed placeholders, and no row is written anywhere.
"""

from __future__ import annotations

from typing import Any

from routers.exam_pronunciation import (
    EXAM_PRONUNCIATION_ASSESSOR_VERSION,
    _ChunkRun,
    _row_to_turn,
    build_evidence,
)
from services.pronunciation.azure_client import _normalize_azure_response
from services.pronunciation.transcript import transcript_is_unusable

CALIBRATION_SESSION_ID = "calibration"
CALIBRATION_PART = "topic1"
CALIBRATION_TURN_KEY = "1"


def replay_turn(clip: dict[str, Any], whisper: dict[str, Any], chunks: list[dict[str, Any]],
                *, fairness_version: str) -> dict[str, Any]:
    """The wire-shape turn (ExamPronunciationTurn, camelCase) for one clip."""
    heard = (whisper.get("text") or "").strip()
    single_recognizer = clip.get("recognizer") == "whisper"
    base = {
        "user_id": "calibration",
        "session_id": CALIBRATION_SESSION_ID,
        "part": CALIBRATION_PART,
        "turn_key": CALIBRATION_TURN_KEY,
        "assessor_version": EXAM_PRONUNCIATION_ASSESSOR_VERSION,
        "fairness_version": fairness_version,
        "exam_transcript": clip["examTranscript"],
        "reference_text": heard,
        "raw_s": clip.get("rawS"),
        "trimmed_s": clip["trimmedS"],
    }
    if transcript_is_unusable(heard):
        # The route returns this without storing it; calibration keeps it so a
        # clip Whisper could not hear shows up as not assessed, not as clean.
        result = {"words": [], "couldNotAssess": True, "couldNotAssessReason": "no_speech_recognized",
                  "singleRecognizer": single_recognizer}
    else:
        planned = [((float(c["window"][0]), float(c["window"][1])), c["referenceText"]) for c in chunks]
        run = _ChunkRun(raw_results=[
            _normalize_azure_response(c["azureRaw"], c["referenceText"]) if c.get("azureRaw") else None
            for c in chunks
        ])
        result = build_evidence(run, planned, exam_transcript=clip["examTranscript"],
                                single_recognizer=single_recognizer)
        result["pauseStats"] = {"pausesOver2s": int(clip["pausesOver2s"]), "longestPauseS": clip["longestPauseS"]}
        result["clippedRatio"] = clip["clippedRatio"]
    return _row_to_turn({**base, "result": result}).model_dump(mode="json")
