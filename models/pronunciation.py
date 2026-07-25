"""Provider-agnostic pronunciation assessment models.

Vocabulary is deliberately our own, not Azure's — the mapping in
services/pronunciation/azure_client.py is the one place Azure's raw field
names/enum values are allowed to appear; nothing downstream (these models,
the HTTP response, the frontend type) ever sees them.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

PronunciationErrorType = Literal["correct", "mispronounced", "skipped", "extra"]
PronunciationSeverity = Literal["low", "medium", "high"]
PronunciationProvider = Literal["azure", "whisper-heuristic"]


class PronunciationWordResult(BaseModel):
    word: str
    accuracyScore: float | None = None  # 0-100
    errorType: PronunciationErrorType | None = None
    confidence: float | None = None  # 0-1, ASR transcription confidence


class PronunciationIssueOut(BaseModel):
    word: str
    ipaExpected: str = ""
    ipaHeard: str = ""
    problem: str
    severity: PronunciationSeverity
    drill: dict
    expected: str | None = None
    heard: str | None = None


class PronunciationSubScores(BaseModel):
    accuracy: float  # 0-100
    fluency: float  # 0-100
    completeness: float  # 0-100


class PronunciationAssessmentResponse(BaseModel):
    score: int  # 0-100
    transcript: str
    issues: list[PronunciationIssueOut]
    words: list[PronunciationWordResult]
    provider: PronunciationProvider
    subScores: PronunciationSubScores | None = None
