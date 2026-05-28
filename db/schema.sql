CREATE TABLE Paper (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    year INTEGER NOT NULL,
    session TEXT NOT NULL,
    variant INTEGER NOT NULL,
    UNIQUE(year, session, variant)
);

CREATE TABLE RolePlayCard (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER NOT NULL,
    scenario TEXT NOT NULL,
    FOREIGN KEY (paper_id) REFERENCES Paper(id),
    UNIQUE(paper_id, scenario)
);

CREATE TABLE RolePlayPrompt (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    roleplay_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    FOREIGN KEY (roleplay_id) REFERENCES RolePlayCard(id),
    UNIQUE(roleplay_id, text)
);

CREATE TABLE Topic (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    FOREIGN KEY (paper_id) REFERENCES Paper(id),
    UNIQUE(paper_id, name)
);

CREATE TABLE Question (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER NOT NULL,
    question_number INTEGER NOT NULL CHECK (question_number BETWEEN 1 AND 5),
    text TEXT NOT NULL,
    FOREIGN KEY (topic_id) REFERENCES Topic(id),
    UNIQUE(topic_id, question_number)
);

CREATE INDEX idx_roleplaycard_paper_id ON RolePlayCard(paper_id);
CREATE INDEX idx_topic_paper_id ON Topic(paper_id);
CREATE INDEX idx_question_topic_id ON Question(topic_id);
