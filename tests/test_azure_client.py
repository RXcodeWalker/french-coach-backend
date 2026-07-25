"""Offline test for the Azure response normalizer — no live call needed.

Sample JSON payload shape copied from Microsoft's published Pronunciation
Assessment docs (NBest[0].PronunciationAssessment + Words[].PronunciationAssessment),
adapted to a short French phrase for this app.

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


def test_normalize_maps_scores_without_rescale():
    result = _normalize_azure_response(SAMPLE_AZURE_RESPONSE, "Un bon vin blanc.")
    assert result["score"] == 85
    assert result["subScores"] == {"accuracy": 82.0, "fluency": 90.0, "completeness": 100.0}
    assert result["provider"] == "azure"
    assert result["transcript"] == "Un bon vin blanc."


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


def test_normalize_leaves_ipa_fields_empty():
    result = _normalize_azure_response(SAMPLE_AZURE_RESPONSE, "Un bon vin blanc.")
    for issue in result["issues"]:
        assert issue["ipaExpected"] == ""
        assert issue["ipaHeard"] == ""


if __name__ == "__main__":
    test_normalize_maps_scores_without_rescale()
    test_normalize_maps_error_types_to_own_vocabulary()
    test_normalize_synthesizes_issues_for_non_correct_words()
    test_normalize_leaves_ipa_fields_empty()
    print("All test_azure_client tests passed.")
