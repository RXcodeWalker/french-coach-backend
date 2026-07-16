"""Pydantic models for admin content CRUD + scenario graph validation.

The graph validator is the authoritative gate (the Zod mirror on the client is
for fast feedback only). Validation failures surface as 422 with field detail.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

ContentStatus = Literal["draft", "published", "archived"]


# ── Questions ─────────────────────────────────────────────────────────────────
class VocabItem(BaseModel):
    fr: str
    en: str


class QuestionCreate(BaseModel):
    id: str
    topic_key: str
    text: str
    hint: str = ""
    difficulty: int = Field(1, ge=1, le=3)
    follow_ups: list[str] = Field(default_factory=list)
    model_answer: str = ""
    key_vocab: list[VocabItem] = Field(default_factory=list)
    is_past_paper: bool = False
    year: int | None = None
    paper_code: str | None = None
    status: ContentStatus = "draft"


class QuestionUpdate(BaseModel):
    topic_key: str | None = None
    text: str | None = None
    hint: str | None = None
    difficulty: int | None = Field(None, ge=1, le=3)
    follow_ups: list[str] | None = None
    model_answer: str | None = None
    key_vocab: list[VocabItem] | None = None
    is_past_paper: bool | None = None
    year: int | None = None
    paper_code: str | None = None
    status: ContentStatus | None = None
    # Optimistic lock token — the updated_at the client last saw.
    expected_updated_at: str | None = None


# ── Scenarios ─────────────────────────────────────────────────────────────────
def validate_scenario_graph(data: dict[str, Any]) -> list[str]:
    """Return a list of structural errors for a scenario state machine.

    Rules: must have a `start` state; no dangling next/intent targets; no
    states unreachable from `start`. Terminal nodes (no next/intents) named
    `end` / `end_*` are allowed and never flagged.
    """
    errors: list[str] = []
    if not isinstance(data, dict) or not data:
        return ["Scenario data must be a non-empty object of states"]

    keys = set(data.keys())
    if "start" not in data:
        return ['Scenario must have a "start" state']

    def targets_of(state: Any) -> list[str]:
        if not isinstance(state, dict):
            return []
        out: list[str] = []
        nxt = state.get("next")
        if isinstance(nxt, str):
            out.append(nxt)
        intents = state.get("intents")
        if isinstance(intents, dict):
            for v in intents.values():
                if isinstance(v, str):
                    out.append(v)
        return out

    # Dangling references
    for name, state in data.items():
        for tgt in targets_of(state):
            if tgt not in keys:
                errors.append(f'State "{tgt}" is referenced (from "{name}") but not defined')

    # Unreachable states (BFS from start)
    reachable = {"start"}
    queue: deque[str] = deque(["start"])
    while queue:
        cur = queue.popleft()
        for tgt in targets_of(data.get(cur)):
            if tgt in keys and tgt not in reachable:
                reachable.add(tgt)
                queue.append(tgt)
    unreachable = sorted(keys - reachable)
    if unreachable:
        errors.append(f"Unreachable states: {', '.join(unreachable)}")

    return errors


class ScenarioCreate(BaseModel):
    id: str
    emoji: str = ""
    title: str
    description: str = ""
    turns: int = 15
    data: dict[str, Any]
    status: ContentStatus = "draft"

    @field_validator("data")
    @classmethod
    def _check_graph(cls, v: dict[str, Any]) -> dict[str, Any]:
        errs = validate_scenario_graph(v)
        if errs:
            raise ValueError("; ".join(errs))
        return v


class ScenarioUpdate(BaseModel):
    emoji: str | None = None
    title: str | None = None
    description: str | None = None
    turns: int | None = None
    data: dict[str, Any] | None = None
    status: ContentStatus | None = None
    expected_updated_at: str | None = None

    @field_validator("data")
    @classmethod
    def _check_graph(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        if v is None:
            return v
        errs = validate_scenario_graph(v)
        if errs:
            raise ValueError("; ".join(errs))
        return v


# ── Bulk ──────────────────────────────────────────────────────────────────────
class BulkRequest(BaseModel):
    ids: list[str]
    action: Literal["publish", "archive", "draft", "reassign_topic", "set_difficulty"]
    topic_key: str | None = None
    difficulty: int | None = Field(None, ge=1, le=3)
