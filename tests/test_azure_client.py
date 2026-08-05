"""Offline test for the Azure response normalizer — no live call needed.

Two fixture families:
  - SAMPLE_AZURE_RESPONSE / SAMPLE_WITH_SKIPPED_PHONEME_SCORE: the SDK-nested
    shape (PronunciationAssessment wrapper), kept to prove the accessor's
    fallback branch actually works, not because a REST endpoint returns it.
  - SAMPLE_AZURE_RESPONSE_REST_SHAPE: copied verbatim (values only, not real
    audio) from a live capture against a real Azure resource on 2026-08-04 —
    see backend/scripts/_probe_output/response_corrected_legacy_host.json.
    This is the shape defect #7 was about: scores flat on NBest[0]/Words[],
    RecognitionStatus as a string. Every prior version of this test suite
    only exercised the nested shape and stayed green against a normalizer
    that could not read a real response — do not remove this fixture.

Run: pytest backend/tests/test_azure_client.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.pronunciation.azure_client import _normalize_azure_response

SAMPLE_AZURE_RESPONSE = {
    "RecognitionStatus": "Success",
    "DisplayText": "Un bon vin blanc.",
    "NBest": [
        {
            "Confidence": 0.87,
            "Display": "Un bon vin blanc.",
            "PronunciationAssessment": {
                "AccuracyScore": 82.0,
                "FluencyScore": 90.0,
                "CompletenessScore": 100.0,
                "PronScore": 85.0,
            },
            "Words": [
                {
                    "Word": "Un",
                    "PronunciationAssessment": {
                        "AccuracyScore": 95.0,
                        "ErrorType": "None",
                    },
                },
                {
                    "Word": "bon",
                    "PronunciationAssessment": {
                        "AccuracyScore": 88.0,
                        "ErrorType": "None",
                    },
                },
                {
                    "Word": "vin",
                    "PronunciationAssessment": {
                        "AccuracyScore": 35.0,
                        "ErrorType": "Mispronunciation",
                    },
                },
                {
                    "Word": "blanc",
                    "PronunciationAssessment": {
                        "AccuracyScore": 0.0,
                        "ErrorType": "Omission",
                    },
                },
            ],
        }
    ],
}

# Flat REST shape — see module docstring. Values adapted from a real capture.
SAMPLE_AZURE_RESPONSE_REST_SHAPE = {
    "RecognitionStatus": "Success",
    "DisplayText": "Un bon vin blanc.",
    "NBest": [
        {
            "Confidence": 0.87,
            "Display": "Un bon vin blanc.",
            "AccuracyScore": 82.0,
            "FluencyScore": 90.0,
            "CompletenessScore": 100.0,
            "PronScore": 85.0,
            "Words": [
                {"Word": "Un", "AccuracyScore": 95.0, "ErrorType": "None"},
                {"Word": "bon", "AccuracyScore": 88.0, "ErrorType": "None"},
                {"Word": "vin", "AccuracyScore": 35.0, "ErrorType": "Mispronunciation"},
                {"Word": "blanc", "AccuracyScore": 0.0, "ErrorType": "Omission"},
            ],
        }
    ],
}


def test_normalize_maps_scores_without_rescale():
    result = _normalize_azure_response(SAMPLE_AZURE_RESPONSE, "Un bon vin blanc.")
    assert result["score"] == 85
    assert result["subScores"]["accuracy"] == 82.0
    assert result["subScores"]["fluency"] == 90.0
    assert result["subScores"]["completeness"] == 100.0
    assert result["subScores"]["prosody"] is None
    assert result["provider"] == "azure"
    assert result["transcript"] == "Un bon vin blanc."
    assert result["couldNotAssess"] is False


def test_normalize_maps_error_types_to_own_vocabulary():
    result = _normalize_azure_response(SAMPLE_AZURE_RESPONSE, "Un bon vin blanc.")
    by_word = {w["word"]: w for w in result["words"]}
    assert by_word["Un"]["errorType"] == "correct"
    assert by_word["vin"]["errorType"] == "mispronounced"
    assert by_word["blanc"]["errorType"] == "skipped"
    # Azure's own labels must never leak through.
    for w in result["words"]:
        assert w["errorType"] in ("correct", "mispronounced", "skipped", "extra")


def test_normalize_synthesizes_issues_for_non_correct_words():
    result = _normalize_azure_response(SAMPLE_AZURE_RESPONSE, "Un bon vin blanc.")
    issue_words = {i["word"] for i in result["issues"]}
    assert issue_words == {"vin", "blanc"}
    blanc_issue = next(i for i in result["issues"] if i["word"] == "blanc")
    assert blanc_issue["severity"] == "high"


def test_normalize_leaves_ipa_heard_empty_without_nbest_phonemes():
    # Fixed behaviour (was: fell back to the expected phoneme and silently
    # rendered "what you should have said" as "what you said" — defect #3).
    result = _normalize_azure_response(SAMPLE_AZURE_RESPONSE, "Un bon vin blanc.")
    for issue in result["issues"]:
        assert issue["ipaHeard"] == ""


def test_normalize_rest_shape_produces_nonzero_scores():
    """Defect #7 regression: the flat REST shape must not silently zero out
    every score the way `best.get("PronunciationAssessment") or {}` did."""
    result = _normalize_azure_response(SAMPLE_AZURE_RESPONSE_REST_SHAPE, "Un bon vin blanc.")
    assert result["score"] == 85
    assert result["subScores"]["accuracy"] == 82.0
    assert result["couldNotAssess"] is False
    by_word = {w["word"]: w for w in result["words"]}
    assert by_word["Un"]["accuracyScore"] == 95.0
    assert by_word["Un"]["errorType"] == "correct"
    assert by_word["blanc"]["errorType"] == "skipped"


def test_normalize_recognition_status_no_match_yields_could_not_assess_not_zero():
    response = {"RecognitionStatus": "NoMatch", "DisplayText": ""}
    result = _normalize_azure_response(response, "Un bon vin blanc.")
    assert result["couldNotAssess"] is True
    assert result["couldNotAssessReason"] == "no_speech_recognized"
    assert result["score"] is None


def test_normalize_initial_silence_timeout_yields_could_not_assess():
    response = {"RecognitionStatus": "InitialSilenceTimeout", "DisplayText": ""}
    result = _normalize_azure_response(response, "Un bon vin blanc.")
    assert result["couldNotAssess"] is True
    assert result["couldNotAssessReason"] == "silence"
    assert result["score"] is None


def test_normalize_missing_assessment_block_yields_could_not_assess_not_zero():
    """Defect #8: a 200 OK with plain STT output and no assessment block
    (malformed/rejected Pronunciation-Assessment header) must not silently
    become score: 0."""
    response = {
        "RecognitionStatus": "Success",
        "DisplayText": "Un bon vin blanc.",
        "NBest": [{"Confidence": 0.87, "Display": "Un bon vin blanc."}],
    }
    result = _normalize_azure_response(response, "Un bon vin blanc.")
    assert result["couldNotAssess"] is True
    assert result["couldNotAssessReason"] == "assessment_unavailable"
    assert result["score"] is None


SAMPLE_WITH_SKIPPED_PHONEME_SCORE = {
    "RecognitionStatus": "Success",
    "DisplayText": "Un bon vin blanc.",
    "NBest": [
        {
            "Confidence": 0.87,
            "Display": "Un bon vin blanc.",
            "PronunciationAssessment": {
                "AccuracyScore": 82.0,
                "FluencyScore": 90.0,
                "CompletenessScore": 100.0,
                "PronScore": 85.0,
            },
            "Words": [
                {
                    "Word": "vin",
                    "PronunciationAssessment": {
                        "AccuracyScore": None,
                        "ErrorType": "Omission",
                    },
                    # A skipped word can still carry Phonemes[] with no
                    # per-phoneme AccuracyScore (Azure omits the key entirely
                    # rather than sending 0) — the coercion must not choke on
                    # a bare .get() returning None.
                    "Phonemes": [
                        {"Phoneme": "v", "PronunciationAssessment": {}},
                        {"Phoneme": "e", "PronunciationAssessment": {"AccuracyScore": None}},
                    ],
                },
            ],
        }
    ],
}


def test_normalize_coerces_missing_phoneme_score_to_none_not_error():
    result = _normalize_azure_response(SAMPLE_WITH_SKIPPED_PHONEME_SCORE, "Un bon vin blanc.")
    vin = next(w for w in result["words"] if w["word"] == "vin")
    assert vin["accuracyScore"] is None
    for phoneme in vin["phonemes"]:
        assert phoneme["accuracyScore"] is None


if __name__ == "__main__":
    test_normalize_maps_scores_without_rescale()
    test_normalize_maps_error_types_to_own_vocabulary()
    test_normalize_synthesizes_issues_for_non_correct_words()
    test_normalize_leaves_ipa_heard_empty_without_nbest_phonemes()
    test_normalize_rest_shape_produces_nonzero_scores()
    test_normalize_recognition_status_no_match_yields_could_not_assess_not_zero()
    test_normalize_initial_silence_timeout_yields_could_not_assess()
    test_normalize_missing_assessment_block_yields_could_not_assess_not_zero()
    test_normalize_coerces_missing_phoneme_score_to_none_not_error()
    print("All test_azure_client tests passed.")
