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
PronunciationMode = Literal["scripted", "freeform"]
# Matches capabilities.py's CapabilityLevel — duplicated as a Literal here
# (rather than imported) because this is the wire vocabulary, not the
# internal matrix-lookup vocabulary; the two are allowed to diverge later.
PronunciationProvenance = Literal["authoritative", "derived", "inferred"]
PhonologicalCategory = Literal["liaison", "nasalVowel", "frenchR", "silentLetter", "elision", "vowelQuality"]


class PronunciationPhoneme(BaseModel):
    phoneme: str
    accuracyScore: float | None = None  # 0-100


class PronunciationWordResult(BaseModel):
    word: str
    accuracyScore: float | None = None  # 0-100
    errorType: PronunciationErrorType | None = None
    confidence: float | None = None  # 0-1, ASR transcription confidence
    phonemes: list[PronunciationPhoneme] | None = None
    # Phase 1 additions — always present as keys once emitted by a chunked
    # aggregation pass; None on a single-chunk (unaggregated) response.
    offsetMs: int | None = None
    durationMs: int | None = None
    nearChunkBoundary: bool | None = None


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
    completeness: float | None = None  # 0-100; null in freeform mode (no reference to omit words from)
    prosody: float | None = None  # 0-100, absent even when requested per Azure docs


class PronunciationRhythmMetrics(BaseModel):
    """Derived (never Azure-authoritative) — see accent-analyzer plan §7.
    Absent entirely (None on the parent field) whenever the capability
    matrix marks rhythmMetrics unavailable for the request's (mode, tier)."""
    speechRateWpm: float | None = None
    articulationRateSyllPerSec: float | None = None
    pauseCount: int | None = None
    longestPauseMs: int | None = None
    pauseRatio: float | None = None
    rhythmRegularity: float | None = None  # normalised pairwise variability
    finalSyllableLengthening: bool | None = None


class PhonologicalFinding(BaseModel):
    category: PhonologicalCategory
    word: str
    explanation: str
    confidence: float  # ceilinged per capabilities.confidence_ceiling()
    provenance: PronunciationProvenance = "inferred"


class AudioQuality(BaseModel):
    snrDb: float | None = None
    durationMs: int | None = None
    recognitionStatus: str | None = None
    clipped: bool = False


class PronunciationConfidence(BaseModel):
    overall: float  # 0-1; UNVALIDATED weights, see accent-analyzer plan §11
    basis: list[str] = []
    transcriptAgreement: float | None = None  # 0-1, Whisper vs Azure DisplayText


class PronunciationCoaching(BaseModel):
    summary: str
    topPriority: str
    tips: list[str] = []
    grounded: bool = True  # False when the LLM pass failed and this is a template fallback


class PronunciationAssessmentResponse(BaseModel):
    score: int | None  # 0-100; null when couldNotAssess is True — never a fabricated 0
    transcript: str
    issues: list[PronunciationIssueOut]
    words: list[PronunciationWordResult]
    provider: PronunciationProvider
    subScores: PronunciationSubScores | None = None
    couldNotAssess: bool = False
    couldNotAssessReason: str | None = None  # e.g. "no_speech_recognized", "silence", "noise"

    # ── Phase 1 additions (all additive; optional-with-null so v2 parsers and
    # the .passthrough() Zod schema stay valid — see accent-analyzer plan §15) ──
    mode: PronunciationMode = "scripted"
    locale: str = "fr-FR"
    assessorVersion: str = "pronunciation-v3"
    chunkCount: int = 1
    chunksFailed: int = 0
    prosodyMetrics: PronunciationRhythmMetrics | None = None
    phonologicalFindings: list[PhonologicalFinding] = []
    audioQuality: AudioQuality | None = None
    confidence: PronunciationConfidence | None = None
    coaching: PronunciationCoaching | None = None
