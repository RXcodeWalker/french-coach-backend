"""The Pronunciation Lab calibration probe's pure parts (no Azure, no ffmpeg):
manifest validation and the fixture the main repo's report reads."""

import asyncio
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from services.pronunciation.lab_calibration import ManifestError, build_fixture, validate_manifest

CLIP = {
    "clipId": "s01_swap", "kind": "detect-swap", "speaker": "owner", "speakerKind": "owner",
    "targetText": "J'ai mangé tout le gâteau hier soir.", "sentenceId": "s01",
    "swapWord": "tout", "swapTo": "tu", "audioPath": "audio/s01_swap.m4a",
}


def _manifest(*clips):
    return {"manifestFormat": 1, "setVersion": "lab-calibration-set-v1", "clips": list(clips)}


def test_a_good_manifest_is_accepted():
    assert validate_manifest(_manifest(CLIP, {**CLIP, "clipId": "s01_correct", "kind": "detect-correct", "swapWord": None, "swapTo": None})) != []


@pytest.mark.parametrize("bad, message", [
    ({"clips": [CLIP]}, "not a manifest"),
    (_manifest(), "no clips"),
    (_manifest({**CLIP, "clipId": "../x"}), "bad clipId"),
    (_manifest(CLIP, CLIP), "duplicate clipId"),
    (_manifest({**CLIP, "kind": "weird"}), "unknown kind"),
    (_manifest({**CLIP, "speakerKind": "nobody"}), "unknown speakerKind"),
    (_manifest({**CLIP, "targetText": " "}), "no targetText"),
    (_manifest({**CLIP, "audioPath": ""}), "no audioPath"),
    (_manifest({**CLIP, "swapTo": None}), "needs swapWord and swapTo"),
])
def test_a_bad_manifest_is_refused_with_the_reason(bad, message):
    with pytest.raises(ManifestError, match=message):
        validate_manifest(bad)


def test_the_fixture_holds_the_scripted_response_a_take_would_get_and_never_the_audio():
    azure_result = {
        "score": 71, "transcript": "j'ai mangé tu le gâteau hier soir", "issues": [],
        "words": [{"word": "tout", "accuracyScore": 31.0, "errorType": "mispronounced", "confidence": None,
                   "phonemes": [{"phoneme": "", "accuracyScore": 30.0}], "offsetMs": 800, "durationMs": 300,
                   "nearChunkBoundary": None}],
        "provider": "azure", "subScores": {"accuracy": 70.0, "fluency": 90.0, "completeness": 100.0, "prosody": None},
        "couldNotAssess": False, "couldNotAssessReason": None,
    }
    fixture = build_fixture(
        CLIP, azure_result, duration_s=3.14159, assessor_version="pronunciation-v3", azure_region="westeurope",
        recorded_at=datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc),
    )
    assert fixture["fixtureFormat"] == 1
    assert fixture["recordedAt"] == "2026-10-30T12:00:00+00:00"
    assert fixture["audio"] == {"durationS": 3.142}
    assert fixture["clip"]["targetText"] == CLIP["targetText"]  # the ORIGINAL sentence, also for a swap reading
    assert fixture["clip"]["swapWord"] == "tout"
    assert "audioPath" not in fixture["clip"]
    response = fixture["response"]
    assert response["mode"] == "scripted" and response["locale"] == "fr-FR" and response["assessorVersion"] == "pronunciation-v3"
    assert response["words"] == azure_result["words"] and response["provider"] == "azure"
    json.dumps(fixture)  # serialisable
    assert "mode" not in azure_result  # the Azure result itself is not mutated


def test_the_probe_script_imports_and_refuses_a_manifest_with_missing_audio(tmp_path, monkeypatch, capsys):
    path = Path(__file__).resolve().parents[1] / "scripts" / "probe_lab_calibration.py"
    spec = importlib.util.spec_from_file_location("probe_lab_calibration", path)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(_manifest(CLIP)))
    args = type("A", (), {"manifest": str(manifest), "only": [], "force": False, "dry_run": True, "out_dir": None})()
    with pytest.raises(SystemExit) as exit_info:
        asyncio.run(probe._main(args))
    assert exit_info.value.code == 2
    assert "audio file missing for: s01_swap" in capsys.readouterr().err

    (tmp_path / "audio").mkdir()
    (tmp_path / "audio" / "s01_swap.m4a").write_bytes(b"x")
    assert asyncio.run(probe._main(args)) == 0  # dry run: usable, nothing sent
    assert "dry run: 1 clips look usable" in capsys.readouterr().out
