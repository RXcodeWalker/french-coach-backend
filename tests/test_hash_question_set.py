"""S11 canonicalization parity (Python side) - architecture plan Sections 3.5.1, 9.

Asserts lib/hash_question_set.py reproduces every hex in
canonicalization-vectors.json, the SAME file the TS implementation
(src/domain/igcse/content/__tests__/hashQuestionSet.test.ts) is asserted
against. Vendored copy - the frontend file is the source of truth; keep this
copy in sync when the frontend vectors change (see the file's own note in the
architecture plan Section 3.5.1).

Run directly (no pytest dependency in this backend yet):
  cd backend
  python tests/test_hash_question_set.py
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.hash_question_set import canonicalize_question_set, hash_question_set

_VECTORS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "canonicalization-vectors.json")


def _load_vectors() -> list[dict]:
    with open(_VECTORS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def run() -> None:
    vectors = _load_vectors()
    failures: list[str] = []

    for vector in vectors:
        actual = hash_question_set(vector["input"])
        expected = vector["sha256"]
        if actual != expected:
            failures.append(f'  vector "{vector["name"]}": expected {expected}, got {actual}')
        else:
            print(f'PASS: vector "{vector["name"]}" hash matches')

    by_name = {v["name"]: v for v in vectors}
    combining = by_name.get("nfc-fold-combining")
    precomposed = by_name.get("nfc-fold-precomposed")
    if combining and precomposed:
        if combining["sha256"] != precomposed["sha256"]:
            failures.append("  NFC-fold vectors do not hash identically")
        else:
            print("PASS: NFC-fold combining sequence hashes identically to precomposed form")

    curly = by_name.get("apostrophe-curly")
    straight = by_name.get("apostrophe-straight")
    if curly and straight:
        if curly["sha256"] == straight["sha256"]:
            failures.append("  curly vs straight apostrophe vectors hash identically (over-normalization!)")
        else:
            print("PASS: curly vs straight apostrophe hash differently (no over-normalization)")

    full = by_name.get("full-fifteen-question-set")
    if full:
        h1 = hash_question_set(full["input"])
        h2 = hash_question_set(full["input"])
        if h1 != h2:
            failures.append("  hash is not stable across repeated runs")
        else:
            print("PASS: hash is stable across repeated runs")

    snapshot_set = {
        "questionSetId": "snapshot-set",
        "questions": [
            {
                "questionId": "q1",
                "part": "topic1",
                "mainText": "Bonjour",
                "alternativeTexts": ["Salut"],
                "partsExpected": 1,
                "topicArea": "A",
                "expectedTimeFrame": "present",
            }
        ],
        "furtherQuestions": {"topic1": ["a", "b"], "topic2": ["c", "d"]},
    }
    expected_bytes = (
        "question-bank-v1"
        + chr(0x1E)
        + "snapshot-set"
        + chr(0x1E)
        + chr(0x1F).join(["q1", "topic1", "Bonjour", "Salut", "1", "", "present", "A"])
        + chr(0x1E)
        + "a"
        + chr(0x1E)
        + "b"
        + chr(0x1E)
        + "c"
        + chr(0x1E)
        + "d"
    ).encode("utf-8")
    actual_bytes = canonicalize_question_set(snapshot_set)
    if actual_bytes != expected_bytes:
        failures.append("  canonical-bytes snapshot mismatch")
    else:
        print("PASS: canonical-bytes snapshot matches fixed input")

    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print(f)
        sys.exit(1)

    print(f"\nAll {len(vectors)} vectors + parity checks passed.")


if __name__ == "__main__":
    run()
