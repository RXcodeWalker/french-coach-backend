"""`sessions.feedback_detail` (migration 20261121090000) is write-only debugging data.

The teacher functions return the whole `feedback` blob and the parent report reads keys out
of it, which is why the full feedback lives in its own column. This pins that nothing on the
server reads the new column: no later migration (RPC, view, policy) and no backend code.
The owner reads it with the service role in the SQL editor.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIGRATION = "20261121090000_sessions_feedback_detail.sql"


def test_only_its_own_migration_mentions_feedback_detail():
    hits = sorted(
        p.name
        for p in (ROOT / "supabase" / "migrations").glob("*.sql")
        if "feedback_detail" in p.read_text(encoding="utf-8")
    )
    assert hits == [MIGRATION]


def test_no_backend_code_reads_feedback_detail():
    skip = {"tests", "node_modules", ".venv", "venv", ".git", "supabase"}
    hits = [
        str(p.relative_to(ROOT))
        for p in ROOT.rglob("*.py")
        if not (set(p.relative_to(ROOT).parts) & skip) and "feedback_detail" in p.read_text(encoding="utf-8")
    ]
    assert hits == []


def test_migration_caps_size_and_shape():
    sql = (ROOT / "supabase" / "migrations" / MIGRATION).read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS feedback_detail jsonb" in sql
    assert "jsonb_typeof(feedback_detail) = 'object'" in sql
    assert "octet_length(feedback_detail::text) <= 32768" in sql
