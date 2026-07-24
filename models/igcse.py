"""Pydantic models for the S11 IGCSE question bank.

IgcseQuestionSetCreate.content is the authoritative gate for the structural
invariants defined in the S11 architecture plan (Section 3.2, blocking bucket)
- mirrors src/data/exam/bank/validate.ts on the TypeScript side. The client
Zod/TS mirror is fast-feedback only; a 422 here is the real gate before a set
can be upserted by seed_igcse_questions.py.

Deliberately 0520-specific - no board abstraction (CLAUDE.md hard constraint #1).
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from models.content import ContentStatus

TopicArea = Literal["A", "B", "C", "D", "E"]
Difficulty = Literal["foundation", "core", "higher"]
SessionPart = Literal["rolePlay", "topic1", "topic2"]
TimeFrame = Literal["past", "present", "future", "conditional"]
TargetStructure = Literal[
    "present",
    "perfect",
    "imperfect",
    "near-future",
    "simple-future",
    "conditional",
    "opinion",
    "justification",
    "comparison",
    "negation",
]

QUESTION_BANK_SCHEMA_VERSION = "question-bank-v1"

_ID_FORMAT = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Reserved canonicalization delimiters (hashQuestionSet.ts Sec 3.5.1): U+001D/1E/1F.
# C0 (U+0000-U+001F) and C1 (U+0080-U+009F) control characters - all forbidden.
# Built from chr() codepoints (not embedded in the regex literal) to keep the
# source file free of raw control bytes.
_RESERVED_DELIMITER_CHARS = chr(0x1D) + chr(0x1E) + chr(0x1F)
_RESERVED_DELIMITERS = re.compile("[" + _RESERVED_DELIMITER_CHARS + "]")
_CONTROL_CHARS = re.compile(
    "[" + chr(0x00) + "-" + chr(0x1F) + chr(0x80) + "-" + chr(0x9F) + "]"
)


def _check_canonicalization_safety(value: str, field_path: str) -> None:
    if _CONTROL_CHARS.search(value):
        raise ValueError(f"{field_path} contains a C0/C1 control character")
    if _RESERVED_DELIMITERS.search(value):
        raise ValueError(f"{field_path} contains a reserved canonicalization delimiter (U+001D/1E/1F)")
    try:
        value.encode("utf-16", "strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_path} contains an unpaired UTF-16 surrogate") from exc


class AuthoredQuestion(BaseModel):
    question_id: str = Field(alias="questionId")
    part: SessionPart
    main_text: str = Field(alias="mainText", min_length=1)
    alternative_texts: list[str] = Field(alias="alternativeTexts")
    topic_area: TopicArea | None = Field(default=None, alias="topicArea")
    sub_topic: str | None = Field(default=None, alias="subTopic")
    difficulty: Difficulty | None = None
    target_structures: list[TargetStructure] | None = Field(default=None, alias="targetStructures")
    expected_time_frame: TimeFrame | None = Field(default=None, alias="expectedTimeFrame")
    parts_expected: Literal[1, 2] = Field(alias="partsExpected")
    second_part_text: str | None = Field(default=None, alias="secondPartText")

    model_config = {"populate_by_name": True}

    @field_validator("question_id")
    @classmethod
    def _check_id_format(cls, v: str) -> str:
        if not _ID_FORMAT.match(v):
            raise ValueError(f'questionId "{v}" is not lowercase-kebab')
        return v

    @model_validator(mode="after")
    def _check_invariants(self) -> "AuthoredQuestion":
        _check_canonicalization_safety(self.question_id, "questionId")
        _check_canonicalization_safety(self.main_text, "mainText")
        for i, alt in enumerate(self.alternative_texts):
            _check_canonicalization_safety(alt, f"alternativeTexts[{i}]")
        if self.second_part_text is not None:
            _check_canonicalization_safety(self.second_part_text, "secondPartText")

        if self.parts_expected == 2:
            if not self.second_part_text or not self.second_part_text.strip():
                raise ValueError("partsExpected===2 requires non-empty secondPartText")
            if self.second_part_text == self.main_text:
                raise ValueError("secondPartText must differ from mainText")
        elif self.second_part_text is not None:
            raise ValueError("partsExpected===1 must not carry secondPartText")

        return self


class AuthoredTopicQuestion(AuthoredQuestion):
    """A topic question - the four selection/coaching + expectedTimeFrame tags are required."""

    @model_validator(mode="after")
    def _check_topic_metadata_required(self) -> "AuthoredTopicQuestion":
        if self.topic_area is None:
            raise ValueError("topic question requires topicArea")
        if not self.sub_topic or not self.sub_topic.strip():
            raise ValueError("topic question requires non-empty subTopic")
        if self.difficulty is None:
            raise ValueError("topic question requires difficulty")
        if not self.target_structures:
            raise ValueError("topic question requires >=1 targetStructures")
        if self.expected_time_frame is None:
            raise ValueError("topic question requires expectedTimeFrame")
        return self


class RolePlayScenario(BaseModel):
    scenario_id: str = Field(alias="scenarioId")
    topic_area: TopicArea = Field(alias="topicArea")
    title: str = Field(min_length=1)
    setup: str = Field(min_length=1)
    tasks: list[AuthoredQuestion]

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _check_tasks(self) -> "RolePlayScenario":
        _check_canonicalization_safety(self.setup, "rolePlay.setup")
        if len(self.tasks) != 5:
            raise ValueError(f"rolePlay.tasks must have exactly 5 tasks, got {len(self.tasks)}")
        for i, task in enumerate(self.tasks):
            if task.part != "rolePlay":
                raise ValueError(f'rolePlay.tasks[{i}].part must be "rolePlay", got "{task.part}"')
        return self


class AuthoredTopic(BaseModel):
    topic_area: TopicArea = Field(alias="topicArea")
    sub_topic: str = Field(alias="subTopic")
    questions: list[AuthoredTopicQuestion]
    further_questions: tuple[str, str] = Field(alias="furtherQuestions")

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _check_questions(self) -> "AuthoredTopic":
        if len(self.questions) != 5:
            raise ValueError(f"topic must have exactly 5 questions, got {len(self.questions)}")
        for i, q in enumerate(self.questions):
            # Q3-Q5 (index 2..4) require >=1 alternative.
            if i >= 2 and len(q.alternative_texts) == 0:
                raise ValueError(f"topic Q{i + 1} requires >=1 alternativeText")
        _check_canonicalization_safety(self.further_questions[0], "furtherQuestions[0]")
        _check_canonicalization_safety(self.further_questions[1], "furtherQuestions[1]")
        return self


class AuthoredContent(BaseModel):
    role_play: RolePlayScenario = Field(alias="rolePlay")
    topic1: AuthoredTopic
    topic2: AuthoredTopic

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _check_unique_ids(self) -> "AuthoredContent":
        ids = (
            [t.question_id for t in self.role_play.tasks]
            + [q.question_id for q in self.topic1.questions]
            + [q.question_id for q in self.topic2.questions]
        )
        seen: set[str] = set()
        for qid in ids:
            if qid in seen:
                raise ValueError(f'questionId "{qid}" is not unique within the set')
            seen.add(qid)
        return self


class ReviewStatus(BaseModel):
    status: Literal["draft", "approved"]
    reviewed_by: str | None = Field(default=None, alias="reviewedBy")
    reviewed_at: str | None = Field(default=None, alias="reviewedAt")
    notes: str | None = None

    model_config = {"populate_by_name": True}


class IgcseQuestionSetCreate(BaseModel):
    """The authoritative structural gate - mirrors validateAuthoredQuestionSet's
    blocking-error bucket (bank/validate.ts). A 422 here means the set may not
    be seeded/upserted."""

    question_set_id: str = Field(alias="questionSetId")
    schema_version: Literal["question-bank-v1"] = Field(alias="schemaVersion")
    content: AuthoredContent
    provenance: Literal["original-practice"]
    review: ReviewStatus

    model_config = {"populate_by_name": True}

    @field_validator("question_set_id")
    @classmethod
    def _check_set_id_format(cls, v: str) -> str:
        if not _ID_FORMAT.match(v):
            raise ValueError(f'questionSetId "{v}" is not lowercase-kebab')
        _check_canonicalization_safety(v, "questionSetId")
        return v

    @model_validator(mode="after")
    def _check_approved(self) -> "IgcseQuestionSetCreate":
        if self.review.status != "approved":
            raise ValueError(
                'review.status must be "approved" to enter the live registry/seed '
                "(unapproved sets may exist only in a draft list)"
            )
        return self


class IgcseQuestionSetRow(BaseModel):
    """The Supabase row shape - what actually gets upserted into igcse_question_sets."""

    id: str
    schema_version: str
    content_hash: str
    payload: dict
    status: ContentStatus = "draft"
