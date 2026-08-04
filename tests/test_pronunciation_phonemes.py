"""Unit test for _normalize_azure_response's Phonemes[]/ProsodyScore mapping
(Phase 4.1). Calls the pure function directly with a synthetic Azure JSON
payload shaped like the canonical nested example from Microsoft's
how-to-pronunciation-assessment docs.

Run: pytest backend/tests/test_pronunciation_phonemes.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.pronunciation.azure_client import _normalize_azure_response

_AZURE_PAYLOAD = {
    "DisplayText": "Un bon vin blanc.",
    "NBest": [
        {
            "Display": "Un bon vin blanc.",
            "PronunciationAssessment": {
                "AccuracyScore": 82.0,
                "FluencyScore": 90.0,
                "CompletenessScore": 100.0,
                "PronScore": 85.0,
                "ProsodyScore": 88.0,
            },
            "Words": [
                {
                    "Word": "vin",
                    "PronunciationAssessment": {"AccuracyScore": 35.0, "ErrorType": "Mispronunciation"},
                    "Phonemes": [
                        {
                            "Phoneme": "v",
                            "PronunciationAssessment": {
                                "AccuracyScore": 92.0,
                                "NBestPhonemes": [{"Phoneme": "v", "Score": 95.0}],
                            },
                        },
                        {
                            "Phoneme": "ɛ̃",
                            "PronunciationAssessment": {
                                "AccuracyScore": 20.0,
                                "NBestPhonemes": [
                                    {"Phoneme": "in", "Score": 60.0},
                                    {"Phoneme": "ɛ̃", "Score": 15.0},
                                ],
                            },
                        },
                    ],
                },
            ],
        }
    ],
}


def test_normalize_azure_response_maps_phonemes_and_prosody():
    result = _normalize_azure_response(_AZURE_PAYLOAD, "Un bon vin blanc.")

    assert result["subScores"]["prosody"] == 88.0

    vin_word = next(w for w in result["words"] if w["word"] == "vin")
    assert vin_word["phonemes"] == [
        {"phoneme": "v", "accuracyScore": 92.0},
        {"phoneme": "ɛ̃", "accuracyScore": 20.0},
    ]

    vin_issue = next(i for i in result["issues"] if i["word"] == "vin")
    assert vin_issue["ipaExpected"] == "v ɛ̃"
    assert vin_issue["ipaHeard"] == "v in"
    assert vin_issue["ipaExpected"] != vin_issue["ipaHeard"]


if __name__ == "__main__":
    test_normalize_azure_response_maps_phonemes_and_prosody()
    print("All test_pronunciation_phonemes tests passed.")
