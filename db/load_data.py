import sqlite3
import json
import sys

SCHEMA = """
CREATE TABLE IF NOT EXISTS Paper (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    year INTEGER NOT NULL,
    session TEXT NOT NULL,
    variant INTEGER NOT NULL,
    UNIQUE(year, session, variant)
);

CREATE TABLE IF NOT EXISTS RolePlayCard (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER NOT NULL,
    scenario TEXT NOT NULL,
    FOREIGN KEY (paper_id) REFERENCES Paper(id),
    UNIQUE(paper_id, scenario)
);

CREATE TABLE IF NOT EXISTS RolePlayPrompt (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    roleplay_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    FOREIGN KEY (roleplay_id) REFERENCES RolePlayCard(id),
    UNIQUE(roleplay_id, text)
);

CREATE TABLE IF NOT EXISTS Topic (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    FOREIGN KEY (paper_id) REFERENCES Paper(id),
    UNIQUE(paper_id, name)
);

CREATE TABLE IF NOT EXISTS Question (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER NOT NULL,
    question_number INTEGER NOT NULL CHECK (question_number BETWEEN 1 AND 5),
    text TEXT NOT NULL,
    FOREIGN KEY (topic_id) REFERENCES Topic(id),
    UNIQUE(topic_id, question_number)
);

CREATE INDEX IF NOT EXISTS idx_roleplaycard_paper_id ON RolePlayCard(paper_id);
CREATE INDEX IF NOT EXISTS idx_topic_paper_id ON Topic(paper_id);
CREATE INDEX IF NOT EXISTS idx_question_topic_id ON Question(topic_id);
"""

def create_schema(conn):
    conn.executescript(SCHEMA)

def insert_papers(cur, papers):
    count = 0
    for p in papers:
        cur.execute(
            "INSERT OR IGNORE INTO Paper (year, session, variant) VALUES (?, ?, ?)",
            (p["year"], p["session"], p["variant"]),
        )
        count += cur.rowcount
    return count

def insert_roleplays(cur, roleplays):
    card_count = 0
    prompt_count = 0
    for rp in roleplays:
        cur.execute(
            "SELECT id FROM Paper WHERE year=? AND session=? AND variant=?",
            (rp["paper"]["year"], rp["paper"]["session"], rp["paper"]["variant"]),
        )
        row = cur.fetchone()
        if row is None:
            print(f"WARNING: no paper found for roleplay {rp}, skipping", file=sys.stderr)
            continue
        paper_id = row[0]

        cur.execute(
            "INSERT OR IGNORE INTO RolePlayCard (paper_id, scenario) VALUES (?, ?)",
            (paper_id, rp["scenario"]),
        )
        card_count += cur.rowcount

        cur.execute(
            "SELECT id FROM RolePlayCard WHERE paper_id=? AND scenario=?",
            (paper_id, rp["scenario"]),
        )
        card_id = cur.fetchone()[0]

        for prompt_text in rp.get("prompts", []):
            cur.execute(
                "INSERT OR IGNORE INTO RolePlayPrompt (roleplay_id, text) VALUES (?, ?)",
                (card_id, prompt_text),
            )
            prompt_count += cur.rowcount

    return card_count, prompt_count

def insert_topics(cur, topics):
    count = 0
    for t in topics:
        cur.execute(
            "SELECT id FROM Paper WHERE year=? AND session=? AND variant=?",
            (t["paper"]["year"], t["paper"]["session"], t["paper"]["variant"]),
        )
        row = cur.fetchone()
        if row is None:
            print(f"WARNING: no paper found for topic {t}, skipping", file=sys.stderr)
            continue
        paper_id = row[0]

        cur.execute(
            "INSERT OR IGNORE INTO Topic (paper_id, name) VALUES (?, ?)",
            (paper_id, t["name"]),
        )
        count += cur.rowcount

    return count

def insert_questions(cur, questions):
    count = 0
    for q in questions:
        cur.execute(
            """
            SELECT t.id FROM Topic t
            JOIN Paper p ON t.paper_id = p.id
            WHERE p.year=? AND p.session=? AND p.variant=? AND t.name=?
            """,
            (
                q["paper"]["year"],
                q["paper"]["session"],
                q["paper"]["variant"],
                q["topic"],
            ),
        )
        row = cur.fetchone()
        if row is None:
            print(f"WARNING: no topic found for question {q}, skipping", file=sys.stderr)
            continue
        topic_id = row[0]

        cur.execute(
            "INSERT OR IGNORE INTO Question (topic_id, question_number, text) VALUES (?, ?, ?)",
            (topic_id, q["question_number"], q["text"]),
        )
        count += cur.rowcount

    return count

def load(db_path, data):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")

    create_schema(conn)

    with conn:
        cur = conn.cursor()

        papers_inserted = insert_papers(cur, data.get("papers", []))
        cards_inserted, prompts_inserted = insert_roleplays(cur, data.get("roleplays", []))
        topics_inserted = insert_topics(cur, data.get("topics", []))
        questions_inserted = insert_questions(cur, data.get("questions", []))

    print(f"Papers inserted:         {papers_inserted}")
    print(f"RolePlayCards inserted:  {cards_inserted}")
    print(f"RolePlayPrompts inserted:{prompts_inserted}")
    print(f"Topics inserted:         {topics_inserted}")
    print(f"Questions inserted:      {questions_inserted}")

    conn.close()

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python load_data.py <data.json> <database.db>")
        sys.exit(1)

    json_path, db_path = sys.argv[1], sys.argv[2]

    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    load(db_path, data)
