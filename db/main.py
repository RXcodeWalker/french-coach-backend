"""
IGCSE French Speaking Exam API

Run:
    cd backend/db
    uvicorn main:app --reload --port 8001
"""

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

import db_logic

app = FastAPI(
    title="IGCSE French Speaking Exam API",
    description="Cambridge 0520 oral exam simulator",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db_error(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/papers", summary="List all available papers")
def list_papers():
    return db_logic.get_papers()


@app.get("/papers/{paper_id}/topics", summary="List all topics for a paper")
def list_topics(paper_id: str):
    try:
        return db_logic.get_topics_by_paper(paper_id)
    except ValueError as exc:
        raise _db_error(exc)


@app.get("/practice/topic", summary="Get 5 questions for a topic")
def practice_topic(
    paper_id: str = Query(..., description="Paper ID"),
    topic_name: str = Query(..., description="Topic name"),
):
    try:
        questions = db_logic.get_topic_questions(paper_id, topic_name)
    except ValueError as exc:
        raise _db_error(exc)
    return {"paper_id": paper_id, "topic_name": topic_name, "questions": questions}


@app.get("/practice/roleplays", summary="Get all roleplay cards for a paper")
def practice_roleplays(
    paper_id: str = Query(..., description="Paper ID"),
):
    try:
        return db_logic.get_all_roleplays(paper_id)
    except ValueError as exc:
        raise _db_error(exc)


@app.get("/exam", summary="Generate a full IGCSE exam (1 roleplay + 2 topics)")
def generate_exam(
    paper_id: str = Query(..., description="Paper ID"),
):
    try:
        return db_logic.generate_exam(paper_id)
    except ValueError as exc:
        raise _db_error(exc)


@app.get("/health", include_in_schema=False)
def health():
    return {"status": "ok"}
