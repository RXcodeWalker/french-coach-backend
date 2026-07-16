"""S11 content hash - Python re-implementation of
src/domain/igcse/content/hashQuestionSet.ts, byte-for-byte identical per the
frozen canonicalization spec (S11 architecture plan, Section 3.5.1).

Both implementations are asserted against the same
canonicalization-vectors.json in CI (TS side:
src/domain/igcse/content/__tests__/hashQuestionSet.test.ts; Python side:
backend/tests/test_hash_question_set.py) so a drift in either language fails
CI before any content is seeded.

Operates on the flattened, engine-facing SessionQuestionSet shape (the
adapter's output, not AuthoredQuestionSet directly) - the same shape
seed_igcse_questions.py projects a payload into before hashing.
"""

from __future__ import annotations

import hashlib
import unicodedata
from typing import Any

_FIELD_SEP = chr(0x1F)  # unit separator - between fields within one record
_RECORD_SEP = chr(0x1E)  # record separator - between questions/records
_GROUP_SEP = chr(0x1D)  # group separator - between elements of a nested list

_CANONICALIZATION_SCHEME_VERSION = "question-bank-v1"


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def canonicalize_question_set(question_set: dict[str, Any]) -> bytes:
    """Mirrors canonicalizeQuestionSet in hashQuestionSet.ts exactly: fixed
    positional field order (never sorted), NFC-normalized strings, reserved-
    delimiter joining, UTF-8 encoding, no BOM."""
    tokens: list[str] = [_CANONICALIZATION_SCHEME_VERSION, _nfc(question_set["questionSetId"])]

    for q in question_set["questions"]:
        fields = [
            _nfc(q["questionId"]),
            _nfc(q["part"]),
            _nfc(q["mainText"]),
            _GROUP_SEP.join(_nfc(t) for t in q["alternativeTexts"]),
            str(q.get("partsExpected") or 1),
            _nfc(q["secondPartText"]) if q.get("secondPartText") is not None else "",
            _nfc(q["expectedTimeFrame"]) if q.get("expectedTimeFrame") is not None else "",
            _nfc(q["topicArea"]) if q.get("topicArea") is not None else "",
        ]
        tokens.append(_FIELD_SEP.join(fields))

    further = question_set["furtherQuestions"]
    tokens.append(_nfc(further["topic1"][0]))
    tokens.append(_nfc(further["topic1"][1]))
    tokens.append(_nfc(further["topic2"][0]))
    tokens.append(_nfc(further["topic2"][1]))

    canonical = _RECORD_SEP.join(tokens)
    return canonical.encode("utf-8")


def hash_question_set(question_set: dict[str, Any]) -> str:
    """sha256 hex of the canonical bytes."""
    return hashlib.sha256(canonicalize_question_set(question_set)).hexdigest()
