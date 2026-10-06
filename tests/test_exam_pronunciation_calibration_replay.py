"""Exam-pronunciation calibration fixtures replay to their stored evidence
(exam-pronunciation plan, Batch 7).

Each fixture under tests/fixtures/exam_pronunciation_calibration/ holds Azure's
raw responses and the evidence they produced when recorded. Re-deriving that
evidence with the current assessor must give the same result, so the main
repo's fairness gate (scripts/examPronunciation/calibration.test.ts), which
reads the stored evidence, is always testing what the live route would store.

If this fails after an assessor change: bump EXAM_PRONUNCIATION_ASSESSOR_VERSION
and re-record (scripts/probe_exam_pronunciation.py --force), then re-run the
main repo's calibration gate. Never edit a fixture's evidence by hand.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from routers.exam_pronunciation import EXAM_PRONUNCIATION_ASSESSOR_VERSION
from services.pronunciation.calibration_replay import replay_turn

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "exam_pronunciation_calibration"
SETS = ("clear", "accented", "unclear")
FIXTURES = sorted(p for s in SETS for p in (FIXTURE_DIR / s).glob("*.json"))


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", FIXTURES, ids=[f"{p.parent.name}/{p.stem}" for p in FIXTURES])
def test_fixture_replays_to_its_stored_evidence(path: Path):
    fx = _load(path)
    assert fx["fixtureFormat"] == 1
    assert fx["clip"]["set"] == path.parent.name
    assert fx["assessorVersion"] == EXAM_PRONUNCIATION_ASSESSOR_VERSION, (
        "fixture recorded under another assessor version — re-record it"
    )
    replayed = replay_turn(fx["clip"], fx["whisper"], fx["chunks"], fairness_version=fx["fairnessVersion"])
    assert json.loads(json.dumps(replayed)) == fx["evidence"]


@pytest.mark.parametrize("path", FIXTURES, ids=[f"{p.parent.name}/{p.stem}" for p in FIXTURES])
def test_fixture_holds_no_audio_and_no_overall_score(path: Path):
    text = path.read_text(encoding="utf-8")
    assert "RIFF" not in text and "base64" not in text.lower()
    assert "score" not in _load(path)["evidence"]


def test_replay_matches_the_route_on_a_synthetic_recording():
    """The replay path itself, on a hand-made Azure response (not calibration data)."""
    clip = {
        "clipId": "synthetic", "set": "clear", "recognizer": "webspeech",
        "examTranscript": "Ils ont un bon chien", "rawS": 2.4, "trimmedS": 2.0,
        "pausesOver2s": 0, "longestPauseS": 0.3, "clippedRatio": 0.0,
    }
    whisper = {"text": "Ils ont un bon chien", "segments": [{"start": 0.0, "end": 2.0, "text": "Ils ont un bon chien"}]}
    words = [
        {"Word": w, "AccuracyScore": acc, "ErrorType": "Mispronunciation" if acc < 60 else "None",
         "Offset": i * 4_000_000, "Duration": 3_000_000, "Phonemes": [{"AccuracyScore": acc}]}
        for i, (w, acc) in enumerate([("ils", 95.0), ("ont", 90.0), ("un", 92.0), ("bon", 20.0), ("chien", 88.0)])
    ]
    raw = {"RecognitionStatus": "Success", "DisplayText": whisper["text"], "SNR": 30.0, "NBest": [{
        "Confidence": 0.9, "Display": whisper["text"], "AccuracyScore": 80.0, "FluencyScore": 90.0,
        "CompletenessScore": None, "PronScore": 85.0, "Words": words,
    }]}
    chunks = [{"window": [0.0, 2.0], "referenceText": whisper["text"], "azureRaw": raw}]
    turn = replay_turn(clip, whisper, chunks, fairness_version="exam-pronunciation-fairness-v1")
    assert turn["sessionId"] == "calibration" and turn["trimmedS"] == 2.0
    assert [w["word"] for w in turn["words"]] == ["ils", "ont", "un", "bon", "chien"]
    bon = turn["words"][3]
    assert bon["errorType"] == "mispronounced" and bon["recognizersAgree"] is True
    assert turn["pauseStats"] == {"pausesOver2s": 0, "longestPauseS": 0.3}
