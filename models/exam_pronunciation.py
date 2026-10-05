"""Wire models for POST/GET /api/exam/pronunciation (exam-pronunciation plan,
Batch 4).

This is stored *evidence*, read by the client's deterministic fairness rules
(main repo src/domain/examPronunciation/). It carries Azure's per-word
accuracy because those rules need it; it carries no overall pronunciation
score, no sub-scores and no fluency score, and nothing here is ever shown to a
learner as a number. Marks are not touched: the scorer never reads this.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from models.pronunciation import PronunciationErrorType

ExamPart = Literal["rolePlay", "topic1", "topic2"]
ExamPronunciationStatus = Literal["done", "budget_exhausted", "failed"]

# Per-word recognition-trust reasons the assessor computes (no tuned
# thresholds). Threshold-based reasons live client-side in FAIRNESS_CONFIG.
SuppressionReason = Literal["asr_disagreement", "near_seam", "short_word", "number"]


class ExamPronunciationWord(BaseModel):
    word: str
    accuracyScore: float | None = None
    errorType: PronunciationErrorType | None = None
    # Milliseconds inside the uploaded (trimmed) clip.
    offsetMs: int | None = None
    durationMs: int | None = None
    # Within 150 ms of an internal chunk seam (the clip's own start/end don't count).
    nearChunkBoundary: bool = False
    phonemeScores: list[float | None] = []
    # The exam-transcript word aligned to this (Whisper reference) word, if any.
    examWord: str | None = None
    # None in single-recogniser mode (the exam transcript is itself Whisper).
    recognizersAgree: bool | None = None
    suppressed: list[SuppressionReason] = []


class ExamPronunciationPauseStats(BaseModel):
    pausesOver2s: int
    longestPauseS: float


class ExamPronunciationTurn(BaseModel):
    sessionId: str
    part: ExamPart
    turnKey: str
    assessorVersion: str
    fairnessVersion: str
    examTranscript: str
    referenceText: str
    words: list[ExamPronunciationWord]
    couldNotAssess: bool = False
    couldNotAssessReason: str | None = None
    singleRecognizer: bool = False
    # Turn-level signal quality: the worst chunk's value.
    snrDb: float | None = None
    azureConfidence: float | None = None
    chunkCount: int = 1
    chunksFailed: int = 0
    rawS: float | None = None
    trimmedS: float
    # Pause statistics measured client-side before trimming (fluency note).
    pauseStats: ExamPronunciationPauseStats | None = None
    # Share of samples at full scale in the untrimmed recording (client-measured).
    clippedRatio: float | None = None
    createdAt: str | None = None


class ExamPronunciationPostResponse(BaseModel):
    status: ExamPronunciationStatus
    turn: ExamPronunciationTurn | None = None
    cached: bool = False
    reason: str | None = None


class ExamPronunciationGetResponse(BaseModel):
    sessionId: str
    assessorVersion: str
    turns: list[ExamPronunciationTurn]
