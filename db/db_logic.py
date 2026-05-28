import os
import random
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get(
    "IGCSE_DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "data", "igcse_speaking.db"),
)


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_papers() -> list[dict]:
    """Return all available papers."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, year, session, variant FROM Paper ORDER BY year DESC, session"
        ).fetchall()
    return [dict(r) for r in rows]


def get_topics_by_paper(paper_id: str) -> list[dict]:
    """Return all topics for a paper."""
    with _connect() as conn:
        _require_paper(conn, paper_id)
        rows = conn.execute(
            "SELECT id, name FROM Topic WHERE paper_id = ? ORDER BY name",
            (paper_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_topic_questions(paper_id: str, topic_name: str) -> list[dict]:
    """Return exactly 5 questions for a topic; raises if count != 5."""
    with _connect() as conn:
        _require_paper(conn, paper_id)
        topic = conn.execute(
            "SELECT id FROM Topic WHERE paper_id = ? AND name = ?",
            (paper_id, topic_name),
        ).fetchone()
        if topic is None:
            raise ValueError(f"Topic '{topic_name}' not found in paper '{paper_id}'")

        rows = conn.execute(
            """
            SELECT id, question_number, text
            FROM Question
            WHERE topic_id = ?
            ORDER BY question_number
            """,
            (topic["id"],),
        ).fetchall()

    if len(rows) != 5:
        raise ValueError(
            f"Topic '{topic_name}' in paper '{paper_id}' has {len(rows)} question(s); "
            "exactly 5 are required."
        )
    return [dict(r) for r in rows]


def get_all_roleplays(paper_id: str) -> list[dict]:
    """Return all roleplay cards with their prompts for a paper."""
    with _connect() as conn:
        _require_paper(conn, paper_id)
        cards = conn.execute(
            "SELECT id, scenario FROM RolePlayCard WHERE paper_id = ? ORDER BY id",
            (paper_id,),
        ).fetchall()

        result = []
        for card in cards:
            prompts = conn.execute(
                "SELECT text FROM RolePlayPrompt WHERE roleplay_id = ? ORDER BY id",
                (card["id"],),
            ).fetchall()
            result.append(
                {
                    "id": card["id"],
                    "scenario": card["scenario"],
                    "prompts": [p["text"] for p in prompts],
                }
            )
    return result


def generate_exam(paper_id: str) -> dict:
    """
    Simulate a real IGCSE oral exam:
      - 1 random roleplay card (with prompts)
      - 2 unique random topics that each have exactly 5 questions
    """
    with _connect() as conn:
        _require_paper(conn, paper_id)

        # --- roleplay ---
        cards = conn.execute(
            "SELECT id, scenario FROM RolePlayCard WHERE paper_id = ?",
            (paper_id,),
        ).fetchall()
        if not cards:
            raise ValueError(f"No roleplay cards found for paper '{paper_id}'")

        chosen_card = random.choice(cards)
        prompts = conn.execute(
            "SELECT text FROM RolePlayPrompt WHERE roleplay_id = ? ORDER BY id",
            (chosen_card["id"],),
        ).fetchall()

        # --- topics with exactly 5 questions ---
        topic_rows = conn.execute(
            """
            SELECT t.id, t.name
            FROM Topic t
            WHERE t.paper_id = ?
              AND (SELECT COUNT(*) FROM Question q WHERE q.topic_id = t.id) = 5
            """,
            (paper_id,),
        ).fetchall()

        if len(topic_rows) < 2:
            raise ValueError(
                f"Paper '{paper_id}' has fewer than 2 topics with exactly 5 questions "
                f"(found {len(topic_rows)})."
            )

        chosen_topics = random.sample(topic_rows, 2)

        topics_out = []
        for topic in chosen_topics:
            questions = conn.execute(
                """
                SELECT id, question_number, text
                FROM Question
                WHERE topic_id = ?
                ORDER BY question_number
                """,
                (topic["id"],),
            ).fetchall()
            topics_out.append(
                {
                    "name": topic["name"],
                    "questions": [dict(q) for q in questions],
                }
            )

    return {
        "paper_id": paper_id,
        "roleplay": {
            "scenario": chosen_card["scenario"],
            "prompts": [p["text"] for p in prompts],
        },
        "topics": topics_out,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _require_paper(conn: sqlite3.Connection, paper_id: str) -> None:
    row = conn.execute("SELECT id FROM Paper WHERE id = ?", (paper_id,)).fetchone()
    if row is None:
        raise ValueError(f"Paper '{paper_id}' not found.")
