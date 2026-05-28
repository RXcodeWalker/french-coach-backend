import json
import sqlite3
import os

DB_PATH = "backend/data/igcse_speaking.db"
JSON_PATH = "backend/data/igcse_master.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS Paper (
    id TEXT PRIMARY KEY,
    year INTEGER NOT NULL,
    session TEXT NOT NULL,
    variant TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS RolePlayCard (
    id TEXT PRIMARY KEY,
    paper_id TEXT NOT NULL,
    scenario TEXT NOT NULL,
    FOREIGN KEY (paper_id) REFERENCES Paper(id)
);

CREATE TABLE IF NOT EXISTS RolePlayPrompt (
    id TEXT PRIMARY KEY,
    roleplay_id TEXT NOT NULL,
    text TEXT NOT NULL,
    FOREIGN KEY (roleplay_id) REFERENCES RolePlayCard(id)
);

CREATE TABLE IF NOT EXISTS Topic (
    id TEXT PRIMARY KEY,
    paper_id TEXT NOT NULL,
    name TEXT NOT NULL,
    FOREIGN KEY (paper_id) REFERENCES Paper(id)
);

CREATE TABLE IF NOT EXISTS Question (
    id TEXT PRIMARY KEY,
    topic_id TEXT NOT NULL,
    question_number INTEGER NOT NULL CHECK (question_number BETWEEN 1 AND 5),
    text TEXT NOT NULL,
    FOREIGN KEY (topic_id) REFERENCES Topic(id)
);

CREATE INDEX IF NOT EXISTS idx_roleplay_paper_id ON RolePlayCard(paper_id);
CREATE INDEX IF NOT EXISTS idx_topic_paper_id ON Topic(paper_id);
CREATE INDEX IF NOT EXISTS idx_question_topic_id ON Question(topic_id);
"""

def migrate():
    if not os.path.exists(JSON_PATH):
        print(f"Error: {JSON_PATH} not found.")
        return

    with open(JSON_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Create schema
    cursor.executescript(SCHEMA)

    # Insert Papers
    for p in data['papers']:
        cursor.execute("INSERT OR REPLACE INTO Paper (id, year, session, variant) VALUES (?, ?, ?, ?)",
                       (p['id'], p['year'], p['session'], p['variant']))

    # Insert RolePlayCards and Prompts
    for rpc in data['role_play_cards']:
        cursor.execute("INSERT OR REPLACE INTO RolePlayCard (id, paper_id, scenario) VALUES (?, ?, ?)",
                       (rpc['id'], rpc['paper_id'], rpc['scenario']))
        
        for idx, prompt in enumerate(rpc['prompts']):
            prompt_id = f"{rpc['id']}_p{idx+1}"
            cursor.execute("INSERT OR REPLACE INTO RolePlayPrompt (id, roleplay_id, text) VALUES (?, ?, ?)",
                           (prompt_id, rpc['id'], prompt))

    # Insert Topics
    for t in data['topics']:
        cursor.execute("INSERT OR REPLACE INTO Topic (id, paper_id, name) VALUES (?, ?, ?)",
                       (t['id'], t['paper_id'], t['name']))

    # Insert Questions
    for q in data['questions']:
        cursor.execute("INSERT OR REPLACE INTO Question (id, topic_id, question_number, text) VALUES (?, ?, ?, ?)",
                       (q['id'], q['topic_id'], q['question_number'], q['text']))

    conn.commit()
    conn.close()
    print(f"Migration successful! Database created at {DB_PATH}")

if __name__ == "__main__":
    migrate()
