"""Admin content CRUD, bulk ops, versioning, diff preview, and reference tracking.

All routes depend on `require_admin`. Writes go to Supabase using the
service-role client; the versioning trigger snapshots every UPDATE into
content_versions automatically.

Cache invalidation: main.py injects `set_cache_invalidator()` so admin writes
purge the public content cache.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException

from lib.auth import require_admin
from models.content import (
    BulkRequest,
    QuestionCreate,
    QuestionUpdate,
    ScenarioCreate,
    ScenarioUpdate,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])

# ── Supabase service client (lazy) ───────────────────────────────────────────
_supabase = None


def _db():
    global _supabase
    if _supabase is None:
        url = os.getenv("SUPABASE_URL", "").strip()
        key = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
        if not (url and key):
            raise HTTPException(status_code=503, detail="Database not configured")
        from supabase import create_client
        _supabase = create_client(url, key)
    return _supabase


# ── Cache invalidation hook ──────────────────────────────────────────────────
_invalidate: Callable[[str], None] | None = None


def set_cache_invalidator(fn: Callable[[str], None]) -> None:
    global _invalidate
    _invalidate = fn


def _purge(kind: str) -> None:
    if _invalidate:
        try:
            _invalidate(kind)
        except Exception:
            pass


async def _run(query):
    return await asyncio.to_thread(query.execute)


def compute_diff(current: dict, version: dict) -> list[dict]:
    all_keys = set(current.keys()) | set(version.keys())
    return [
        {"field": k, "from": current.get(k), "to": version.get(k)}
        for k in sorted(all_keys)
        if current.get(k) != version.get(k)
    ]


# ════════════════════════════════════════════════════════════════════════════
# Questions
# ════════════════════════════════════════════════════════════════════════════
@router.post("/questions")
async def create_question(body: QuestionCreate, _=Depends(require_admin)):
    db = _db()
    payload = body.model_dump()
    payload["key_vocab"] = [v.model_dump() if hasattr(v, "model_dump") else v
                            for v in payload.get("key_vocab", [])]
    res = await _run(db.table("questions").insert(payload))
    if not res.data:
        raise HTTPException(status_code=400, detail="Insert failed")
    _purge("questions")
    return res.data[0]


@router.put("/questions/{question_id}")
async def update_question(question_id: str, body: QuestionUpdate, _=Depends(require_admin)):
    db = _db()
    data = {k: v for k, v in body.model_dump(exclude_none=True).items()
            if k != "expected_updated_at"}
    if "key_vocab" in data:
        data["key_vocab"] = [v.model_dump() if hasattr(v, "model_dump") else v
                             for v in data["key_vocab"]]
    if not data:
        raise HTTPException(status_code=400, detail="No fields to update")
    data["updated_at"] = "now()"

    query = db.table("questions").update(data).eq("id", question_id)
    if body.expected_updated_at:
        query = query.eq("updated_at", body.expected_updated_at)
    res = await _run(query)
    if not res.data:
        # Distinguish "not found" from "lock conflict"
        existing = await _run(db.table("questions").select("id").eq("id", question_id))
        if not existing.data:
            raise HTTPException(status_code=404, detail="Question not found")
        raise HTTPException(status_code=409, detail={
            "error": "conflict",
            "message": "This record was modified by another editor. Reload and retry.",
        })
    _purge("questions")
    return res.data[0]


@router.delete("/questions/{question_id}")
async def archive_question(question_id: str, _=Depends(require_admin)):
    db = _db()
    res = await _run(
        db.table("questions").update({"status": "archived", "updated_at": "now()"})
        .eq("id", question_id)
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Question not found")
    _purge("questions")
    return {"id": question_id, "status": "archived"}


@router.post("/questions/bulk")
async def bulk_questions(body: BulkRequest, _=Depends(require_admin)):
    return await _bulk("questions", body)


@router.get("/questions/{question_id}/versions")
async def question_versions(question_id: str, _=Depends(require_admin)):
    return await _versions("questions", question_id)


@router.get("/questions/{question_id}/versions/{v}/preview")
async def question_version_preview(question_id: str, v: int, _=Depends(require_admin)):
    return await _version_preview("questions", question_id, v)


@router.post("/questions/{question_id}/restore/{v}")
async def restore_question(question_id: str, v: int, body: dict | None = None,
                           _=Depends(require_admin)):
    return await _restore("questions", question_id, v,
                          (body or {}).get("expected_updated_at"))


@router.get("/questions/{question_id}/references")
async def question_references(question_id: str, _=Depends(require_admin)):
    db = _db()
    q = await _run(
        db.table("questions").select("topic_key,is_past_paper,paper_code").eq("id", question_id).limit(1)
    )
    if not q.data:
        raise HTTPException(status_code=404, detail="Question not found")
    q = q.data[0]
    refs: list[dict] = []

    topic = await _run(db.table("topics").select("key,label_en").eq("key", q["topic_key"]).limit(1))
    if topic.data:
        refs.append({"type": "topic", "id": topic.data[0]["key"], "label": topic.data[0]["label_en"]})
    else:
        refs.append({"type": "topic", "id": q["topic_key"], "label": q["topic_key"]})

    es = await _run(
        db.table("exam_sets").select("id,label").contains("question_ids", [question_id])
    )
    for s in es.data or []:
        refs.append({"type": "exam_set", "id": s["id"], "label": s["label"]})

    if q.get("is_past_paper") and q.get("paper_code"):
        refs.append({"type": "igcse_paper", "id": q["paper_code"],
                     "label": f"IGCSE Paper {q['paper_code']}"})

    return {"id": question_id, "references": refs}


# ════════════════════════════════════════════════════════════════════════════
# Scenarios
# ════════════════════════════════════════════════════════════════════════════
@router.post("/scenarios")
async def create_scenario(body: ScenarioCreate, _=Depends(require_admin)):
    db = _db()
    res = await _run(db.table("scenarios").insert(body.model_dump()))
    if not res.data:
        raise HTTPException(status_code=400, detail="Insert failed")
    _purge("scenarios")
    return res.data[0]


@router.put("/scenarios/{scenario_id}")
async def update_scenario(scenario_id: str, body: ScenarioUpdate, _=Depends(require_admin)):
    db = _db()
    data = {k: v for k, v in body.model_dump(exclude_none=True).items()
            if k != "expected_updated_at"}
    if not data:
        raise HTTPException(status_code=400, detail="No fields to update")
    data["updated_at"] = "now()"

    query = db.table("scenarios").update(data).eq("id", scenario_id)
    if body.expected_updated_at:
        query = query.eq("updated_at", body.expected_updated_at)
    res = await _run(query)
    if not res.data:
        existing = await _run(db.table("scenarios").select("id").eq("id", scenario_id))
        if not existing.data:
            raise HTTPException(status_code=404, detail="Scenario not found")
        raise HTTPException(status_code=409, detail={
            "error": "conflict",
            "message": "This record was modified by another editor. Reload and retry.",
        })
    _purge("scenarios")
    return res.data[0]


@router.delete("/scenarios/{scenario_id}")
async def archive_scenario(scenario_id: str, _=Depends(require_admin)):
    db = _db()
    res = await _run(
        db.table("scenarios").update({"status": "archived", "updated_at": "now()"})
        .eq("id", scenario_id)
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Scenario not found")
    _purge("scenarios")
    return {"id": scenario_id, "status": "archived"}


@router.post("/scenarios/bulk")
async def bulk_scenarios(body: BulkRequest, _=Depends(require_admin)):
    return await _bulk("scenarios", body)


@router.get("/scenarios/{scenario_id}/versions")
async def scenario_versions(scenario_id: str, _=Depends(require_admin)):
    return await _versions("scenarios", scenario_id)


@router.get("/scenarios/{scenario_id}/versions/{v}/preview")
async def scenario_version_preview(scenario_id: str, v: int, _=Depends(require_admin)):
    return await _version_preview("scenarios", scenario_id, v)


@router.post("/scenarios/{scenario_id}/restore/{v}")
async def restore_scenario(scenario_id: str, v: int, body: dict | None = None,
                           _=Depends(require_admin)):
    return await _restore("scenarios", scenario_id, v,
                          (body or {}).get("expected_updated_at"))


@router.get("/scenarios/{scenario_id}/references")
async def scenario_references(scenario_id: str, _=Depends(require_admin)):
    db = _db()
    refs: list[dict] = []
    s = await _run(db.table("scenarios").select("status").eq("id", scenario_id).limit(1))
    if not s.data:
        raise HTTPException(status_code=404, detail="Scenario not found")
    if s.data[0]["status"] == "published":
        refs.append({"type": "screen", "id": "explore",
                     "label": "Explore screen (all published scenarios)"})

    # Cross-scenario links: scan other scenarios whose data references this id.
    others = await _run(db.table("scenarios").select("id,title,data").neq("id", scenario_id))
    for sc in others.data or []:
        if _data_links_to(sc.get("data"), scenario_id):
            refs.append({"type": "scenario_link", "id": sc["id"], "label": sc["title"]})

    return {"id": scenario_id, "references": refs}


def _data_links_to(data: Any, target: str) -> bool:
    if not isinstance(data, dict):
        return False
    for state in data.values():
        if not isinstance(state, dict):
            continue
        if state.get("next") == target:
            return True
        intents = state.get("intents")
        if isinstance(intents, dict) and target in intents.values():
            return True
    return False


# ════════════════════════════════════════════════════════════════════════════
# Shared helpers — versions, restore, bulk
# ════════════════════════════════════════════════════════════════════════════
async def _versions(content_type: str, content_id: str):
    db = _db()
    res = await _run(
        db.table("content_versions")
        .select("version_number,created_by,created_at")
        .eq("content_type", content_type).eq("content_id", content_id)
        .order("version_number", desc=True)
    )
    return {"id": content_id, "versions": res.data or []}


async def _fetch_current(content_type: str, content_id: str) -> dict:
    db = _db()
    res = await _run(db.table(content_type).select("*").eq("id", content_id).limit(1))
    if not res.data:
        raise HTTPException(status_code=404, detail="Record not found")
    return res.data[0]


async def _fetch_version(content_type: str, content_id: str, v: int) -> dict:
    db = _db()
    res = await _run(
        db.table("content_versions").select("data")
        .eq("content_type", content_type).eq("content_id", content_id)
        .eq("version_number", v).limit(1)
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Version not found")
    return res.data[0]["data"]


async def _version_preview(content_type: str, content_id: str, v: int):
    current = await _fetch_current(content_type, content_id)
    version = await _fetch_version(content_type, content_id, v)
    return {
        "id": content_id,
        "version": v,
        "current": current,
        "target": version,
        "diff": compute_diff(current, version),
    }


# Immutable / system columns that must not be overwritten on restore.
_RESTORE_SKIP = {"id", "created_at", "updated_at", "is_active"}


async def _restore(content_type: str, content_id: str, v: int,
                   expected_updated_at: str | None):
    db = _db()
    current = await _fetch_current(content_type, content_id)
    version = await _fetch_version(content_type, content_id, v)
    data = {k: val for k, val in version.items() if k not in _RESTORE_SKIP}
    data["updated_at"] = "now()"

    query = db.table(content_type).update(data).eq("id", content_id)
    if expected_updated_at:
        query = query.eq("updated_at", expected_updated_at)
    res = await _run(query)
    if not res.data:
        raise HTTPException(status_code=409, detail={
            "error": "conflict",
            "message": "This record was modified by another editor. Reload and retry.",
        })
    _purge(content_type)
    return res.data[0]


async def _bulk(content_type: str, body: BulkRequest):
    db = _db()
    if not body.ids:
        return {"updated": 0, "skipped": 0, "errors": []}

    if body.action in ("publish", "archive", "draft"):
        status = {"publish": "published", "archive": "archived", "draft": "draft"}[body.action]
        update = {"status": status, "updated_at": "now()"}
    elif body.action == "reassign_topic":
        if content_type != "questions":
            raise HTTPException(status_code=400, detail="reassign_topic only valid for questions")
        if not body.topic_key:
            raise HTTPException(status_code=400, detail="topic_key required")
        update = {"topic_key": body.topic_key, "updated_at": "now()"}
    elif body.action == "set_difficulty":
        if content_type != "questions":
            raise HTTPException(status_code=400, detail="set_difficulty only valid for questions")
        if body.difficulty is None:
            raise HTTPException(status_code=400, detail="difficulty required")
        update = {"difficulty": body.difficulty, "updated_at": "now()"}
    else:  # pragma: no cover - guarded by enum
        raise HTTPException(status_code=400, detail="Unknown action")

    res = await _run(db.table(content_type).update(update).in_("id", body.ids))
    updated_ids = {r["id"] for r in (res.data or [])}
    skipped = [i for i in body.ids if i not in updated_ids]
    _purge(content_type)
    return {"updated": len(updated_ids), "skipped": len(skipped), "errors": []}
