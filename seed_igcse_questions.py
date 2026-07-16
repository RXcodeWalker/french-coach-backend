"""
seed_igcse_questions.py -- Validate, lint, hash, and upsert the S11 IGCSE
question bank into Supabase (public.igcse_question_sets).

Mirrors seed_questions.py's idempotent-upsert pattern. For each JSON file in
backend/data/igcse/:
  1. Pydantic-validate against IgcseQuestionSetCreate (the authoritative
     structural gate -- see models/igcse.py). A validation error aborts the
     seed for that file with the full error detail; nothing partial is written.
  2. Project the authored content into the flat, engine-facing
     SessionQuestionSet shape (mirrors src/data/exam/bank/adapter.ts) and
     compute content_hash via lib/hash_question_set.py -- the same
     canonicalization spec the TS side uses (asserted byte-for-byte in
     tests/test_hash_question_set.py).
  3. Enforce the id-reuse guard (Section 8.1): a published questionSetId may
     only be re-seeded if the incoming content_hash differs from the current
     published row's hash (i.e. it's a legitimate revision, not an id
     collision with different content silently overwriting history -- the
     CMS's content_versions trigger keeps the actual history either way, this
     guard only refuses accidental/careless reseeds without visibility).
  4. Upsert into igcse_question_sets, keyed by id (= questionSetId).
     Idempotent: re-seeding an unchanged file is a no-op write (same hash,
     same payload) but still safe to run.

Run once (safe to re-run):
  cd backend
  python seed_igcse_questions.py
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from dotenv import load_dotenv
from pydantic import ValidationError

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.hash_question_set import hash_question_set
from models.igcse import IgcseQuestionSetCreate

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "igcse")


def _to_session_question(q: dict[str, Any]) -> dict[str, Any]:
    """Mirrors adapter.ts toSessionQuestion: keeps only engine-relevant fields."""
    out: dict[str, Any] = {
        "questionId": q["questionId"],
        "part": q["part"],
        "mainText": q["mainText"],
        "alternativeTexts": q["alternativeTexts"],
        "partsExpected": q["partsExpected"],
    }
    if q.get("topicArea") is not None:
        out["topicArea"] = q["topicArea"]
    if q.get("expectedTimeFrame") is not None:
        out["expectedTimeFrame"] = q["expectedTimeFrame"]
    if q.get("secondPartText") is not None:
        out["secondPartText"] = q["secondPartText"]
    return out


def to_session_question_set(authored: dict[str, Any]) -> dict[str, Any]:
    """Mirrors adapter.ts toSessionQuestionSet: flattens rolePlay + topic1 +
    topic2 into one questions array in a fixed, deterministic order -- the
    same order the TS adapter emits, since canonicalization depends on it."""
    content = authored["content"]
    questions = (
        [_to_session_question(t) for t in content["rolePlay"]["tasks"]]
        + [_to_session_question(q) for q in content["topic1"]["questions"]]
        + [_to_session_question(q) for q in content["topic2"]["questions"]]
    )
    return {
        "questionSetId": authored["questionSetId"],
        "questions": questions,
        "furtherQuestions": {
            "topic1": list(content["topic1"]["furtherQuestions"]),
            "topic2": list(content["topic2"]["furtherQuestions"]),
        },
    }


def _lint_report(authored: dict[str, Any]) -> list[str]:
    """Deterministic authoring-quality warnings (architecture plan Section 3.4)
    -- non-blocking, printed for the reviewer. Mirrors the duplicate-mainText
    and weak-alternative checks in bank/lint.ts at a coarse level; the full
    lint ruleset is TS-side (CI), this is a best-effort seed-time echo."""
    warnings: list[str] = []
    content = authored["content"]
    all_texts = (
        [(f"rolePlay.tasks[{i}]", t["mainText"]) for i, t in enumerate(content["rolePlay"]["tasks"])]
        + [(f"topic1.questions[{i}]", q["mainText"]) for i, q in enumerate(content["topic1"]["questions"])]
        + [(f"topic2.questions[{i}]", q["mainText"]) for i, q in enumerate(content["topic2"]["questions"])]
    )
    seen: dict[str, str] = {}
    for path, text in all_texts:
        key = text.strip().lower()
        if key in seen:
            warnings.append(f"duplicate-main-text: {path} matches {seen[key]}")
        else:
            seen[key] = path
    return warnings


def load_authored_sets() -> list[tuple[str, dict[str, Any]]]:
    if not os.path.isdir(DATA_DIR):
        print(f"No data directory at {DATA_DIR} -- nothing to seed.")
        return []
    out: list[tuple[str, dict[str, Any]]] = []
    for filename in sorted(os.listdir(DATA_DIR)):
        if not filename.endswith(".json"):
            continue
        path = os.path.join(DATA_DIR, filename)
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        out.append((filename, raw))
    return out


def seed_igcse_question_sets(dry_run: bool = False) -> None:
    files = load_authored_sets()
    if not files:
        return

    db = None
    existing_by_id: dict[str, dict[str, Any]] = {}
    if not dry_run:
        SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
        SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
        if not SUPABASE_URL or not SUPABASE_KEY:
            print("ERROR: Set SUPABASE_URL and SUPABASE_SERVICE_KEY in backend/.env first.")
            sys.exit(1)
        from supabase import create_client

        db = create_client(SUPABASE_URL, SUPABASE_KEY)
        existing = db.table("igcse_question_sets").select("id,content_hash,status").execute()
        existing_by_id = {row["id"]: row for row in existing.data}

    print(f"Found {len(files)} IGCSE question set file(s) in {DATA_DIR}")
    seeded = 0
    skipped = 0

    for filename, raw in files:
        print(f"\n-- {filename} --")

        try:
            validated = IgcseQuestionSetCreate.model_validate(raw)
        except ValidationError as e:
            print(f"  VALIDATION FAILED, aborting seed for this file:\n{e}")
            continue

        warnings = _lint_report(raw)
        for w in warnings:
            print(f"  WARN: {w}")

        projected = to_session_question_set(raw)
        content_hash = hash_question_set(projected)
        question_set_id = validated.question_set_id

        # Section 8.1 id-reuse guard: refuse a silent hash-changing reseed of a
        # published id without visibility. content_versions still captures the
        # prior row on UPDATE regardless -- this is a seed-time signal, not the
        # only safety net.
        prior = existing_by_id.get(question_set_id)
        if prior and prior["status"] == "published" and prior["content_hash"] != content_hash:
            print(
                f"  NOTE: questionSetId '{question_set_id}' is already published with a "
                f"different content_hash ({prior['content_hash'][:12]}... -> {content_hash[:12]}...). "
                "This will be recorded as a new version via content_versions."
            )

        if prior and prior["content_hash"] == content_hash:
            print(f"  id={question_set_id} content_hash={content_hash[:12]}... unchanged, no-op")
            skipped += 1
            continue

        row = {
            "id": question_set_id,
            "schema_version": validated.schema_version,
            "content_hash": content_hash,
            "payload": raw,
            "status": raw.get("review", {}).get("status") == "approved" and "published" or "draft",
        }

        if dry_run:
            print(f"  DRY RUN: would upsert id={question_set_id} content_hash={content_hash[:12]}... status={row['status']}")
        else:
            assert db is not None
            db.table("igcse_question_sets").upsert(row, on_conflict="id").execute()
            print(f"  Upserted id={question_set_id} content_hash={content_hash[:12]}... status={row['status']}")
        seeded += 1

    print(f"\nDone. {seeded} upserted, {skipped} unchanged/skipped.")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    print("=== IGCSE Question Bank -- Supabase Seed ===")
    if dry_run:
        print("(dry run -- no writes)")
    seed_igcse_question_sets(dry_run=dry_run)
