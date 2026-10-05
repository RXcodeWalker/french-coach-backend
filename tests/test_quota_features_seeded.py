"""Every AI-quota feature this backend charges must be seeded by a migration.

consume_ai_quota writes ai_usage_grants.feature, an FK to ai_quota_limits:
a feature with no row there 503s every call (CLAUDE.md Known Trap). This
collects every feature name passed to consume_ai_quota_or_503 — string
literals, plus the values of main.py's _EXAMINER_QUOTA_FEATURE map, which is
how the examiner route names its features — and checks each one is inserted
into ai_quota_limits by some migration. `exam_pronunciation` is checked too:
its row must deploy before the exam pronunciation route (plan §4).
"""

from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
_SKIP_DIRS = {"tests", "venv", ".venv", "__pycache__", "node_modules", "scripts"}


def _seeded_features() -> set[str]:
    seeded: set[str] = set()
    for path in MIGRATIONS.glob("*.sql"):
        sql = path.read_text()
        for m in re.finditer(r"INSERT\s+INTO\s+public\.ai_quota_limits\s*\([^)]*\)\s*VALUES(.*?);", sql, re.I | re.S):
            seeded |= set(re.findall(r"\(\s*'([a-z_]+)'\s*,\s*\d+\s*\)", m.group(1)))
    return seeded


def _charged_features() -> tuple[set[str], list[str]]:
    literals: set[str] = set()
    unresolved: list[str] = []
    for path in ROOT.rglob("*.py"):
        if _SKIP_DIRS & set(path.relative_to(ROOT).parts):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
            if name != "consume_ai_quota_or_503" or len(node.args) < 3:
                continue
            feature = node.args[2]
            if isinstance(feature, ast.Constant) and isinstance(feature.value, str):
                literals.add(feature.value)
            else:
                unresolved.append(f"{path.relative_to(ROOT)}:{node.lineno} ({ast.unparse(feature)})")
    return literals, unresolved


def test_every_charged_feature_is_seeded():
    import main

    literals, unresolved = _charged_features()
    # The one non-literal call site (the examiner route) names its features
    # through _EXAMINER_QUOTA_FEATURE. A new non-literal call site fails here
    # until this test learns where its feature names come from.
    assert len(unresolved) == 1 and unresolved[0].startswith("main.py:") and unresolved[0].endswith("(feature)"), unresolved
    features = literals | set(main._EXAMINER_QUOTA_FEATURE.values()) | {"exam_pronunciation"}
    missing = features - _seeded_features()
    assert not missing, f"quota features with no ai_quota_limits row in any migration: {sorted(missing)}"
    assert {"pronunciation", "transcribe", "feedback", "roleplay_turn", "exam_turn_feedback"} <= features


def test_the_collector_sees_real_call_sites():
    literals, _ = _charged_features()
    assert {"pronunciation", "transcribe"} <= literals
