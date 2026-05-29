"""
French AI Speaking Coach — backend service.

Endpoints:
  POST /api/feedback          Grade a transcript (Groq primary, Gemini fallback).
  POST /api/transcribe        French speech-to-text via faster-whisper.
  GET  /api/questions         List questions from Supabase DB.
  GET  /api/questions/random  Random question (optional topic filter).
  GET  /api/questions/daily   Today's daily challenge.
  GET  /api/exam-sets         All exam sets.
  GET  /api/exam-sets/{id}    Specific exam set with hydrated questions.
  POST /api/sessions          Save a session (auth required).
  GET  /api/sessions          Fetch user's sessions (auth required).
  GET  /api/profile           User profile + streak (auth required).
  GET  /health                Liveness probe.

Run locally:
  cd backend
  pip install -r requirements.txt
  cp .env.example .env    # fill in GROQ_API_KEY, SUPABASE_URL, etc.
  uvicorn main:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import sqlite3
import tempfile
import traceback
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import jwt as pyjwt
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

try:
    from google.api_core.exceptions import ResourceExhausted
except Exception:  # pragma: no cover - google libs may be absent in local dev
    class ResourceExhausted(Exception):
        pass

load_dotenv()

log = logging.getLogger("french-coach")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# ── Config ────────────────────────────────────────────────────────────────────
GROQ_API_KEY        = os.getenv("GROQ_API_KEY", "").strip()
GEMINI_API_KEY      = os.getenv("GEMINI_API_KEY", "").strip()
SUPABASE_URL        = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY        = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "").strip()

WHISPER_MODEL        = os.getenv("WHISPER_MODEL", "small")
WHISPER_DEVICE       = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")

APP_DIR = Path(__file__).resolve().parent
DEFAULT_CORS_ORIGINS = (
    "http://localhost:5173,"
    "http://localhost:3000,"
    "https://frenchcoach.vercel.app,"
    "https://french.beyondthebasics.me"
)
CORS_ORIGINS = [
    o.strip().rstrip("/")
    for o in os.getenv("CORS_ORIGINS", DEFAULT_CORS_ORIGINS).split(",")
    if o.strip()
]

# ── Groq lazy init ────────────────────────────────────────────────────────────
_groq_client = None

def get_groq():
    global _groq_client
    if _groq_client is None and GROQ_API_KEY:
        from groq import AsyncGroq
        _groq_client = AsyncGroq(api_key=GROQ_API_KEY)
    return _groq_client

# ── Gemini lazy init ──────────────────────────────────────────────────────────
_gemini_model = None

def get_gemini():
    global _gemini_model
    if _gemini_model is None and GEMINI_API_KEY:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        _gemini_model = genai.GenerativeModel(
            "gemini-2.0-flash",
            system_instruction=SYSTEM_PROMPT,
        )
    return _gemini_model

# ── Gemini multimodal lazy init (audio-aware pronunciation prompt) ────────────
_gemini_multimodal_model = None

def get_gemini_multimodal():
    global _gemini_multimodal_model
    if _gemini_multimodal_model is None and GEMINI_API_KEY:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        _gemini_multimodal_model = genai.GenerativeModel(
            "gemini-2.0-flash",
            system_instruction=MULTIMODAL_SYSTEM_PROMPT,
        )
    return _gemini_multimodal_model

# ── Gemini IGCSE lazy init (separate model with IGCSE system instruction) ─────
_gemini_igcse_model = None

def get_gemini_igcse():
    global _gemini_igcse_model
    if _gemini_igcse_model is None and GEMINI_API_KEY:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        _gemini_igcse_model = genai.GenerativeModel(
            "gemini-2.0-flash",
            system_instruction=IGCSE_SYSTEM_PROMPT,
        )
    return _gemini_igcse_model

# ── Supabase lazy init ────────────────────────────────────────────────────────
_supabase = None

def get_supabase():
    global _supabase
    if _supabase is None and SUPABASE_URL and SUPABASE_KEY:
        from supabase import create_client
        _supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase

# ── Whisper lazy init ─────────────────────────────────────────────────────────
_whisper = None

def get_whisper():
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        log.info("Loading faster-whisper model=%s device=%s compute=%s",
                 WHISPER_MODEL, WHISPER_DEVICE, WHISPER_COMPUTE_TYPE)
        _whisper = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE_TYPE)
    return _whisper

# ── App setup ─────────────────────────────────────────────────────────────────
app = FastAPI(title="French AI Speaking Coach")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    log.error(
        "Unhandled error on %s %s: %s\n%s",
        request.method,
        request.url.path,
        repr(exc),
        traceback.format_exc(),
    )
    return JSONResponse(
        status_code=500,
        content={
            "ok": False,
            "error": "internal_server_error",
            "detail": "An unexpected backend error occurred.",
        },
    )

# ── JWT verification ──────────────────────────────────────────────────────────
def verify_jwt(authorization: str | None) -> str:
    """Verify Supabase JWT. Returns user_id (UUID string) or raises HTTP 401."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.split(" ", 1)[1]
    if not SUPABASE_JWT_SECRET:
        raise HTTPException(status_code=503, detail="Auth not configured on server")
    try:
        payload = pyjwt.decode(
            token,
            SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
        return payload["sub"]
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

# ── /health ───────────────────────────────────────────────────────────────────
@app.get("/health")
def health() -> dict[str, Any]:
    db_path = Path(os.getenv("IGCSE_DB_PATH", str(APP_DIR / "data" / "igcse_speaking.db")))
    return {
        "ok": True,
        "service": "french-ai-backend",
        "groq_configured": bool(GROQ_API_KEY),
        "gemini_configured": bool(GEMINI_API_KEY),
        "supabase_configured": bool(SUPABASE_URL and SUPABASE_KEY),
        "whisper_model": WHISPER_MODEL,
        "igcse_db_configured": db_path.exists(),
    }

# ── AI Feedback models + logic ────────────────────────────────────────────────
class WordProbability(BaseModel):
    word: str
    probability: float | None = None

class FeedbackMetrics(BaseModel):
    wordCount: int | None = None
    durationSec: float | None = None
    wordsPerMinute: int | None = None
    pauseCount: int | None = None
    sentenceCount: int | None = None
    avgWordsPerSentence: int | None = None
    hasAccents: bool | None = None
    hasPastTense: bool | None = None
    hasConnectives: bool | None = None
    hasOpinion: bool | None = None
    hasConditional: bool | None = None
    fluencyScore: float | None = None
    wordProbabilities: list[WordProbability] | None = None


class FeedbackRequest(BaseModel):
    question: str = Field(..., description="The question the student was answering")
    transcript: str = Field(..., description="Student's spoken answer, transcribed")
    metrics: FeedbackMetrics | None = None
    model: str | None = None      # "groq" | "gemini" | None (auto)
    detailed: bool = False         # True = expanded feedback with more items


class IGCSEFeedbackRequest(BaseModel):
    question: str
    transcript: str
    metrics: FeedbackMetrics | None = None
    bullet_points: list[str] = []
    model: str | None = None


class VocabItem(BaseModel):
    fr: str
    en: str
    type: str


class VocabPrepResponse(BaseModel):
    vocab: list[VocabItem]
    phrases: list[VocabItem]


# ── IGCSE Themes (Cambridge 0520) ─────────────────────────────────────────────
IGCSE_THEMES = {
    1: "Everyday life",
    2: "Personal and social life",
    3: "The world around us",
    4: "The world of work",
    5: "The international world"
}

# ── IGCSE Papers (static seed — move to Supabase igcse_papers table later) ────
#
# TASK TYPE KEY (Cambridge 0520 format):
#   "statement"     (•)  Candidate provides information or makes a statement
#   "question"      (?)  Candidate must ASK the examiner something
#   "unpredictable" (!)  Examiner asks something not shown on candidate's card;
#                        candidate must respond without preparation
#
# TOPIC GROUP KEY:
#   "A"  Personal / school life  → used for Topic Conversation 1
DB_PATH = Path(os.getenv("IGCSE_DB_PATH", str(APP_DIR / "data" / "igcse_speaking.db")))


def get_db_connection():
    if not DB_PATH.exists():
        raise RuntimeError(f"SQLite database not found at {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_igcse_papers_from_db() -> list[dict[str, Any]]:
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT p.id, p.year, p.session, p.variant, r.id as rpc_id, r.scenario
                FROM Paper p
                LEFT JOIN RolePlayCard r ON p.id = r.paper_id
            """)
            rows = cursor.fetchall()
    except Exception as exc:
        log.warning("SQLite IGCSE paper list unavailable, using static fallback: %s", exc)
        return [
            {
                "id": p["id"],
                "year": p.get("year"),
                "session": p.get("paper_code"),
                "variant": p.get("type"),
                "role_play_cards": [p] if p.get("type") == "role_play" else [],
            }
            for p in globals().get("IGCSE_PAPERS", [])
        ]

    papers = {}
    for row in rows:
        pid = row["id"]
        if pid not in papers:
            papers[pid] = {
                "id": pid,
                "year": row["year"],
                "session": row["session"],
                "variant": row["variant"],
                "role_play_cards": [],
            }
        if row["rpc_id"]:
            papers[pid]["role_play_cards"].append({
                "id": row["rpc_id"],
                "scenario": row["scenario"],
            })
    return list(papers.values())


def fetch_igcse_paper_details(paper_id: str) -> dict[str, Any] | None:
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM Paper WHERE id = ?", (paper_id,))
            paper_row = cursor.fetchone()
            if not paper_row:
                return None

            paper = dict(paper_row)
            cursor.execute("SELECT * FROM RolePlayCard WHERE paper_id = ?", (paper_id,))
            rpc_rows = cursor.fetchall()
            paper["role_play_cards"] = []
            for r_row in rpc_rows:
                rpc = dict(r_row)
                cursor.execute("SELECT text FROM RolePlayPrompt WHERE roleplay_id = ? ORDER BY id", (rpc["id"],))
                rpc["prompts"] = [p["text"] for p in cursor.fetchall()]
                paper["role_play_cards"].append(rpc)

            cursor.execute("SELECT * FROM Topic WHERE paper_id = ?", (paper_id,))
            topic_rows = cursor.fetchall()
            paper["topics"] = []
            for t_row in topic_rows:
                topic = dict(t_row)
                cursor.execute("SELECT text FROM Question WHERE topic_id = ? ORDER BY question_number", (topic["id"],))
                topic["questions"] = [q["text"] for q in cursor.fetchall()]
                paper["topics"].append(topic)
            return paper
    except Exception as exc:
        log.warning("SQLite IGCSE paper detail unavailable, using static fallback: %s", exc)
        return next((p for p in globals().get("IGCSE_PAPERS", []) if p.get("id") == paper_id), None)

# IGCSE_PAPERS = [ ... ]  <-- We will remove the static list and update the endpoints below
# Temporary static fallback (used by legacy endpoints while DB migration completes)
IGCSE_PAPERS = [
    {
        "id": "rp-24-c2",
        "year": 2024,
        "paper_code": "0520/S24/RP2",
        "topic": "Camping (Portable Perdu)",
        "type": "role_play",
        "scenario": "Vous avez perdu votre portable au camping. Vous allez parler au réceptionniste (l'examinateur) pour signaler la perte.",
        "candidate_role": "campeur / campeuse",
        "examiner_role": "réceptionniste du camping",
        "tasks": [
            {
                "task_id": 1, "type": "statement", "symbol": "•",
                "candidate_instruction": "Expliquez que vous avez perdu votre portable et demandez de l'aide.",
                "examiner_prompt": "Bonjour. Je peux vous aider ?"
            },
            {
                "task_id": 2, "type": "statement", "symbol": "•",
                "candidate_instruction": "Décrivez le portable : marque, couleur, taille.",
                "examiner_prompt": "C'est quel modèle de portable ? Quelle couleur ?"
            },
            {
                "task_id": 3, "type": "statement", "symbol": "•",
                "candidate_instruction": "Dites où et quand vous l'avez vu pour la dernière fois.",
                "examiner_prompt": "Où est-ce que vous l'aviez la dernière fois ?"
            },
            {
                "task_id": 4, "type": "unpredictable", "symbol": "!",
                "candidate_instruction": "Répondez à la question de l'examinateur.",
                "examiner_prompt": "Il y a votre numéro de chambre dans le téléphone ? Vous pouvez me donner votre nom complet ?"
            },
            {
                "task_id": 5, "type": "question", "symbol": "?",
                "candidate_instruction": "Demandez s'il est possible d'être contacté(e) rapidement si on retrouve le téléphone.",
                "examiner_prompt": "[Le réceptionniste répond à votre question sur la façon d'être contacté]"
            },
        ],
        "bullet_points": [
            "Expliquez la perte du portable et demandez de l'aide",
            "Décrivez le portable (marque, couleur, taille)",
            "Dites où et quand vous l'avez vu pour la dernière fois",
            "Répondez à la question inattendue de l'examinateur",
            "Demandez comment vous serez contacté(e) si on le retrouve",
        ],
        "examiner_prompts": [
            "Bonjour. Je peux vous aider ?",
            "C'est quel modèle de portable ? Quelle couleur ?",
            "Où est-ce que vous l'aviez la dernière fois ?",
            "Il y a votre numéro de chambre dans le téléphone ? Vous pouvez me donner votre nom complet ?",
            "[Le réceptionniste répond à votre question sur la façon d'être contacté]",
        ],
        "difficulty": 3,
        "time_limit_sec": 300,
    },
    {
        "id": "rp-24-c3",
        "year": 2024,
        "paper_code": "0520/S24/RP3",
        "topic": "Location de Bateau",
        "type": "role_play",
        "scenario": "Vous êtes en Martinique et vous voulez louer un bateau pour la journée. L'examinateur joue le rôle du loueur.",
        "candidate_role": "client(e) souhaitant louer un bateau",
        "examiner_role": "loueur de bateaux",
        "tasks": [
            {
                "task_id": 1, "type": "statement", "symbol": "•",
                "candidate_instruction": "Dites que vous voulez louer un bateau et précisez le type.",
                "examiner_prompt": "Bonjour. Vous désirez ?"
            },
            {
                "task_id": 2, "type": "statement", "symbol": "•",
                "candidate_instruction": "Précisez pour combien de temps et pour combien de personnes.",
                "examiner_prompt": "Vous voulez le bateau pour combien d'heures ? Et vous serez combien ?"
            },
            {
                "task_id": 3, "type": "question", "symbol": "?",
                "candidate_instruction": "Demandez si des gilets de sauvetage sont fournis.",
                "examiner_prompt": "[Le loueur répond à votre question sur les gilets de sauvetage]"
            },
            {
                "task_id": 4, "type": "unpredictable", "symbol": "!",
                "candidate_instruction": "Répondez à la question de l'examinateur.",
                "examiner_prompt": "Est-ce que vous avez déjà conduit un bateau ? Vous avez un permis bateau ?"
            },
            {
                "task_id": 5, "type": "question", "symbol": "?",
                "candidate_instruction": "Demandez le prix total et comment payer.",
                "examiner_prompt": "[Le loueur vous donne le prix total de la location]"
            },
        ],
        "bullet_points": [
            "Dites que vous voulez louer un bateau et précisez le type",
            "Précisez pour combien de temps et pour combien de personnes",
            "Demandez si des gilets de sauvetage sont fournis",
            "Répondez à la question inattendue sur votre expérience",
            "Demandez le prix total et comment payer",
        ],
        "examiner_prompts": [
            "Bonjour. Vous désirez ?",
            "Vous voulez le bateau pour combien d'heures ? Et vous serez combien ?",
            "[Le loueur répond à votre question sur les gilets de sauvetage]",
            "Est-ce que vous avez déjà conduit un bateau ? Vous avez un permis bateau ?",
            "[Le loueur vous donne le prix total de la location]",
        ],
        "difficulty": 3,
        "time_limit_sec": 300,
    },
    {
        "id": "rp-24-c4",
        "year": 2024,
        "paper_code": "0520/S24/RP4",
        "topic": "Concert de Rock",
        "type": "role_play",
        "scenario": "Vous voulez acheter des billets pour un concert de rock. L'examinateur joue le rôle du vendeur au guichet.",
        "candidate_role": "client(e) achetant des billets",
        "examiner_role": "vendeur(euse) au guichet",
        "tasks": [
            {
                "task_id": 1, "type": "statement", "symbol": "•",
                "candidate_instruction": "Demandez des billets pour le concert et précisez la date.",
                "examiner_prompt": "Bonjour. C'est pour quel concert et pour quelle date ?"
            },
            {
                "task_id": 2, "type": "statement", "symbol": "•",
                "candidate_instruction": "Précisez le nombre de billets et le type de place souhaité.",
                "examiner_prompt": "Il vous en faut combien, et vous préférez quelle catégorie ?"
            },
            {
                "task_id": 3, "type": "question", "symbol": "?",
                "candidate_instruction": "Demandez à quelle heure commence le concert et s'il y a une première partie.",
                "examiner_prompt": "[Le vendeur répond à votre question sur les horaires]"
            },
            {
                "task_id": 4, "type": "unpredictable", "symbol": "!",
                "candidate_instruction": "Répondez à la question de l'examinateur.",
                "examiner_prompt": "Vous connaissez bien ce groupe ? Vous les avez déjà vus en concert ?"
            },
            {
                "task_id": 5, "type": "question", "symbol": "?",
                "candidate_instruction": "Demandez s'il y a un parking à proximité et comment payer.",
                "examiner_prompt": "[Le vendeur répond sur le parking et les modes de paiement]"
            },
        ],
        "bullet_points": [
            "Demandez des billets pour le concert et précisez la date",
            "Précisez le nombre de billets et le type de place",
            "Demandez l'heure du début et s'il y a une première partie",
            "Répondez à la question inattendue sur votre connaissance du groupe",
            "Demandez s'il y a un parking et comment payer",
        ],
        "examiner_prompts": [
            "Bonjour. C'est pour quel concert et pour quelle date ?",
            "Il vous en faut combien, et vous préférez quelle catégorie ?",
            "[Le vendeur répond à votre question sur les horaires]",
            "Vous connaissez bien ce groupe ? Vous les avez déjà vus en concert ?",
            "[Le vendeur répond sur le parking et les modes de paiement]",
        ],
        "difficulty": 3,
        "time_limit_sec": 300,
    },
    {
        "id": "rp-24-c5",
        "year": 2024,
        "paper_code": "0520/S24/RP5",
        "topic": "Gare (Réservation)",
        "type": "role_play",
        "scenario": "Il y a un problème avec votre réservation de train. Vous parlez à un employé de la gare (l'examinateur).",
        "candidate_role": "voyageur(euse) avec un problème de billet",
        "examiner_role": "employé(e) de la gare",
        "tasks": [
            {
                "task_id": 1, "type": "statement", "symbol": "•",
                "candidate_instruction": "Expliquez qu'il y a un problème avec votre réservation et décrivez le problème.",
                "examiner_prompt": "Bonjour. Quel est le souci ?"
            },
            {
                "task_id": 2, "type": "statement", "symbol": "•",
                "candidate_instruction": "Donnez votre numéro de place et expliquez où vous allez.",
                "examiner_prompt": "C'est quel numéro de place sur votre billet ? Et vous allez vers quelle ville ?"
            },
            {
                "task_id": 3, "type": "question", "symbol": "?",
                "candidate_instruction": "Demandez s'il y a un autre train disponible et à quelle heure.",
                "examiner_prompt": "[L'employé répond à votre question sur les prochains trains]"
            },
            {
                "task_id": 4, "type": "unpredictable", "symbol": "!",
                "candidate_instruction": "Répondez à la question de l'examinateur.",
                "examiner_prompt": "Vous avez une carte d'identité ou un passeport sur vous ? Je dois vérifier votre identité."
            },
            {
                "task_id": 5, "type": "statement", "symbol": "•",
                "candidate_instruction": "Acceptez la nouvelle place proposée et remerciez l'employé.",
                "examiner_prompt": "Voici votre nouveau billet, place 14, voiture 3. Ça vous convient ?"
            },
        ],
        "bullet_points": [
            "Expliquez le problème avec votre réservation",
            "Donnez votre numéro de place et votre destination",
            "Demandez s'il y a un autre train et à quelle heure",
            "Répondez à la question inattendue sur votre identité",
            "Acceptez la nouvelle place et remerciez",
        ],
        "examiner_prompts": [
            "Bonjour. Quel est le souci ?",
            "C'est quel numéro de place sur votre billet ? Et vous allez vers quelle ville ?",
            "[L'employé répond à votre question sur les prochains trains]",
            "Vous avez une carte d'identité ou un passeport sur vous ? Je dois vérifier votre identité.",
            "Voici votre nouveau billet, place 14, voiture 3. Ça vous convient ?",
        ],
        "difficulty": 3,
        "time_limit_sec": 300,
    },
    {
        "id": "rp-24-c6",
        "year": 2024,
        "paper_code": "0520/S24/RP6",
        "topic": "Shopping (Cadeaux)",
        "type": "role_play",
        "scenario": "Vous cherchez un cadeau pour un(e) ami(e). Vous parlez à un(e) vendeur(euse) dans un magasin (l'examinateur).",
        "candidate_role": "client(e) cherchant un cadeau",
        "examiner_role": "vendeur(euse)",
        "tasks": [
            {
                "task_id": 1, "type": "statement", "symbol": "•",
                "candidate_instruction": "Expliquez que vous cherchez un cadeau et pour qui c'est.",
                "examiner_prompt": "Bonjour. Je peux vous conseiller ?"
            },
            {
                "task_id": 2, "type": "statement", "symbol": "•",
                "candidate_instruction": "Décrivez les goûts et les intérêts de votre ami(e).",
                "examiner_prompt": "Qu'est-ce qu'il ou elle aime en général ?"
            },
            {
                "task_id": 3, "type": "question", "symbol": "?",
                "candidate_instruction": "Demandez le prix d'un article et s'il y a d'autres options.",
                "examiner_prompt": "[Le vendeur vous répond sur les prix et les options disponibles]"
            },
            {
                "task_id": 4, "type": "unpredictable", "symbol": "!",
                "candidate_instruction": "Répondez à la question de l'examinateur.",
                "examiner_prompt": "Vous avez un budget particulier en tête ? C'est pour quelle occasion exactement ?"
            },
            {
                "task_id": 5, "type": "question", "symbol": "?",
                "candidate_instruction": "Demandez si l'article peut être emballé cadeau et quel est le délai de livraison.",
                "examiner_prompt": "[Le vendeur répond sur l'emballage et la livraison]"
            },
        ],
        "bullet_points": [
            "Expliquez que vous cherchez un cadeau et pour qui",
            "Décrivez les goûts de votre ami(e)",
            "Demandez le prix et s'il y a d'autres options",
            "Répondez à la question inattendue sur le budget et l'occasion",
            "Demandez l'emballage cadeau et le délai de livraison",
        ],
        "examiner_prompts": [
            "Bonjour. Je peux vous conseiller ?",
            "Qu'est-ce qu'il ou elle aime en général ?",
            "[Le vendeur vous répond sur les prix et les options disponibles]",
            "Vous avez un budget particulier en tête ? C'est pour quelle occasion exactement ?",
            "[Le vendeur répond sur l'emballage et la livraison]",
        ],
        "difficulty": 3,
        "time_limit_sec": 300,
    },
    {
        "id": "rp-24-c7",
        "year": 2024,
        "paper_code": "0520/S24/RP7",
        "topic": "Match de Football",
        "type": "role_play",
        "scenario": "Vous planifiez d'aller voir un match de football avec un(e) ami(e) (l'examinateur).",
        "candidate_role": "ami(e) qui organise la sortie",
        "examiner_role": "ami(e) français(e)",
        "tasks": [
            {
                "task_id": 1, "type": "statement", "symbol": "•",
                "candidate_instruction": "Proposez d'aller voir le match ce soir et expliquez pourquoi.",
                "examiner_prompt": "Salut ! Tu fais quoi ce soir ? Tu veux qu'on fasse quelque chose ensemble ?"
            },
            {
                "task_id": 2, "type": "statement", "symbol": "•",
                "candidate_instruction": "Dites quelle équipe vous supportez et depuis combien de temps.",
                "examiner_prompt": "Moi j'adore le PSG ! Et toi, tu supportes quelle équipe ?"
            },
            {
                "task_id": 3, "type": "question", "symbol": "?",
                "candidate_instruction": "Suggérez une heure de rendez-vous et demandez à votre ami(e) si ça lui convient.",
                "examiner_prompt": "[L'ami répond à votre suggestion pour l'heure du rendez-vous]"
            },
            {
                "task_id": 4, "type": "unpredictable", "symbol": "!",
                "candidate_instruction": "Répondez à la question de l'examinateur.",
                "examiner_prompt": "Tu as déjà assisté à un match en direct ? C'était comment ?"
            },
            {
                "task_id": 5, "type": "statement", "symbol": "•",
                "candidate_instruction": "Proposez où aller manger après le match et justifiez votre choix.",
                "examiner_prompt": "J'aurai vraiment faim après le match. On va où pour manger ?"
            },
        ],
        "bullet_points": [
            "Proposez d'aller voir le match et expliquez pourquoi",
            "Dites quelle équipe vous supportez et depuis combien de temps",
            "Suggérez une heure de rendez-vous et demandez si ça convient",
            "Répondez à la question inattendue sur un match précédent",
            "Proposez où manger après le match",
        ],
        "examiner_prompts": [
            "Salut ! Tu fais quoi ce soir ? Tu veux qu'on fasse quelque chose ensemble ?",
            "Moi j'adore le PSG ! Et toi, tu supportes quelle équipe ?",
            "[L'ami répond à votre suggestion pour l'heure du rendez-vous]",
            "Tu as déjà assisté à un match en direct ? C'était comment ?",
            "J'aurai vraiment faim après le match. On va où pour manger ?",
        ],
        "difficulty": 3,
        "time_limit_sec": 300,
    },
    {
        "id": "rp-24-c8",
        "year": 2024,
        "paper_code": "0520/S24/RP8",
        "topic": "Visite du Château",
        "type": "role_play",
        "scenario": "Vous visitez un château historique. L'examinateur joue le rôle du guide touristique.",
        "candidate_role": "touriste visitant le château",
        "examiner_role": "guide touristique",
        "tasks": [
            {
                "task_id": 1, "type": "question", "symbol": "?",
                "candidate_instruction": "Demandez si une visite guidée en français est disponible et à quelle heure.",
                "examiner_prompt": "Bonjour, bienvenue au château. Je peux vous renseigner ?"
            },
            {
                "task_id": 2, "type": "statement", "symbol": "•",
                "candidate_instruction": "Expliquez pourquoi vous vous intéressez à l'histoire et aux vieux châteaux.",
                "examiner_prompt": "Vous aimez les châteaux historiques ? Qu'est-ce qui vous attire dans ce genre d'endroit ?"
            },
            {
                "task_id": 3, "type": "question", "symbol": "?",
                "candidate_instruction": "Demandez si vous pouvez prendre des photos à l'intérieur.",
                "examiner_prompt": "[Le guide répond à votre question sur les photos]"
            },
            {
                "task_id": 4, "type": "unpredictable", "symbol": "!",
                "candidate_instruction": "Répondez à la question de l'examinateur.",
                "examiner_prompt": "Vous connaissez quelque chose sur l'histoire de cette région ? D'où venez-vous ?"
            },
            {
                "task_id": 5, "type": "question", "symbol": "?",
                "candidate_instruction": "Demandez où se trouve la boutique de souvenirs et ce qu'on peut y acheter.",
                "examiner_prompt": "[Le guide répond à votre question sur la boutique de souvenirs]"
            },
        ],
        "bullet_points": [
            "Demandez si une visite guidée en français est disponible",
            "Expliquez votre intérêt pour l'histoire et les châteaux",
            "Demandez si vous pouvez prendre des photos",
            "Répondez à la question inattendue sur votre origine et l'histoire",
            "Demandez où se trouve la boutique de souvenirs",
        ],
        "examiner_prompts": [
            "Bonjour, bienvenue au château. Je peux vous renseigner ?",
            "Vous aimez les châteaux historiques ? Qu'est-ce qui vous attire dans ce genre d'endroit ?",
            "[Le guide répond à votre question sur les photos]",
            "Vous connaissez quelque chose sur l'histoire de cette région ? D'où venez-vous ?",
            "[Le guide répond à votre question sur la boutique de souvenirs]",
        ],
        "difficulty": 3,
        "time_limit_sec": 300,
    },
    {
        "id": "rp-24-c9",
        "year": 2024,
        "paper_code": "0520/S24/RP9",
        "topic": "Serveur (Entretien)",
        "type": "role_play",
        "scenario": "Vous passez un entretien d'embauche pour un job d'été comme serveur/serveuse. L'examinateur joue le rôle du patron.",
        "candidate_role": "candidat(e) à un poste de serveur/serveuse",
        "examiner_role": "patron(ne) du restaurant",
        "tasks": [
            {
                "task_id": 1, "type": "statement", "symbol": "•",
                "candidate_instruction": "Présentez-vous et expliquez pourquoi vous voulez ce poste.",
                "examiner_prompt": "Bonjour. Alors, pourquoi est-ce que vous voulez travailler ici ?"
            },
            {
                "task_id": 2, "type": "statement", "symbol": "•",
                "candidate_instruction": "Parlez de votre expérience de travail passée et de vos compétences.",
                "examiner_prompt": "Est-ce que vous avez déjà travaillé dans un café ou un restaurant ?"
            },
            {
                "task_id": 3, "type": "question", "symbol": "?",
                "candidate_instruction": "Demandez quels sont les horaires de travail et si le week-end est obligatoire.",
                "examiner_prompt": "[Le patron répond à votre question sur les horaires]"
            },
            {
                "task_id": 4, "type": "unpredictable", "symbol": "!",
                "candidate_instruction": "Répondez à la question de l'examinateur.",
                "examiner_prompt": "Est-ce que vous parlez d'autres langues ? Nous avons beaucoup de clients étrangers."
            },
            {
                "task_id": 5, "type": "statement", "symbol": "•",
                "candidate_instruction": "Dites à partir de quand vous êtes disponible et posez une question sur le salaire.",
                "examiner_prompt": "Vous seriez disponible à partir de quand ?"
            },
        ],
        "bullet_points": [
            "Présentez-vous et expliquez pourquoi vous voulez ce poste",
            "Parlez de votre expérience et de vos compétences",
            "Demandez les horaires de travail",
            "Répondez à la question inattendue sur vos langues",
            "Dites votre disponibilité et demandez le salaire",
        ],
        "examiner_prompts": [
            "Bonjour. Alors, pourquoi est-ce que vous voulez travailler ici ?",
            "Est-ce que vous avez déjà travaillé dans un café ou un restaurant ?",
            "[Le patron répond à votre question sur les horaires]",
            "Est-ce que vous parlez d'autres langues ? Nous avons beaucoup de clients étrangers.",
            "Vous seriez disponible à partir de quand ?",
        ],
        "difficulty": 3,
        "time_limit_sec": 300,
    },

    # ── TOPIC CONVERSATIONS (1–9) ──────────────────────────────────────────────
    # group "A" → Topic Conversation 1 (personal life, school, family, everyday)
    # group "B" → Topic Conversation 2 (world, work, environment, future)
    {
        "id": "topic-24-1",
        "year": 2024,
        "paper_code": "0520/S24/T1",
        "topic": "La Famille",
        "type": "topic",
        "group": "A",
        "theme": "Vie personnelle et relations familiales",
        "scenario": "Conversation sur votre famille et vos relations avec les membres de votre famille.",
        "examiner_role": "examinateur",
        "opening_question": "Parle-moi un peu de ta famille — vous êtes combien chez toi ?",
        "bullet_points": [
            "Décrire votre famille (composition, âges)",
            "Parler de vos rapports avec vos parents ou frères/sœurs",
            "Décrire une activité récente en famille",
            "Parler de ce que vous ferez ensemble prochainement",
            "Donner votre avis sur l'importance de la famille aujourd'hui",
        ],
        "examiner_prompts": [
            "Parle-moi un peu de ta famille — vous êtes combien chez toi ?",
            "Comment est-ce que tu t'entends avec tes parents ?",
            "Qu'est-ce que vous avez fait ensemble le week-end dernier ?",
            "Qu'est-ce que vous avez prévu de faire pour les prochaines vacances ?",
            "À ton avis, est-ce que c'est important d'avoir une grande famille ?",
        ],
        "difficulty": 3,
        "time_limit_sec": 300,
    },
    {
        "id": "topic-24-2",
        "year": 2024,
        "paper_code": "0520/S24/T2",
        "topic": "Les Amis",
        "type": "topic",
        "group": "A",
        "theme": "Vie sociale et amitié",
        "scenario": "Conversation sur vos amis, vos sorties et l'importance de l'amitié.",
        "examiner_role": "examinateur",
        "opening_question": "Décris-moi ton ou ta meilleur(e) ami(e).",
        "bullet_points": [
            "Décrire votre meilleur(e) ami(e) (personnalité, apparence)",
            "Dire ce que vous aimez faire ensemble",
            "Raconter une sortie récente avec des amis",
            "Parler de ce que vous prévoyez de faire ensemble prochainement",
            "Donner les qualités essentielles d'un bon ami selon vous",
        ],
        "examiner_prompts": [
            "Décris-moi ton ou ta meilleur(e) ami(e).",
            "Qu'est-ce que vous faites d'habitude quand vous êtes ensemble ?",
            "Parle-moi d'une sortie récente avec tes amis.",
            "Qu'est-ce que vous avez prévu de faire le week-end prochain ?",
            "Pour toi, quelles sont les qualités les plus importantes d'un bon ami ?",
        ],
        "difficulty": 3,
        "time_limit_sec": 300,
    },
    {
        "id": "topic-24-3",
        "year": 2024,
        "paper_code": "0520/S24/T3",
        "topic": "Mon École",
        "type": "topic",
        "group": "A",
        "theme": "École et éducation",
        "scenario": "Conversation sur votre vie scolaire, vos matières et votre avis sur l'école.",
        "examiner_role": "examinateur",
        "opening_question": "Fais-moi une description de ton école.",
        "bullet_points": [
            "Décrire votre école (taille, type, équipements)",
            "Parler de votre matière préférée et expliquer pourquoi",
            "Donner votre avis sur les règles ou l'uniforme scolaire",
            "Raconter une journée scolaire mémorable",
            "Dire ce que vous ferez après les examens",
        ],
        "examiner_prompts": [
            "Fais-moi une description de ton école.",
            "Quelle est ta matière préférée ? Pourquoi ?",
            "Qu'est-ce que tu penses des règles dans ton école — l'uniforme, les portables, etc. ?",
            "Raconte-moi une journée à l'école que tu as particulièrement aimée.",
            "Qu'est-ce que tu vas faire quand tu auras fini tes examens ?",
        ],
        "difficulty": 3,
        "time_limit_sec": 300,
    },
    {
        "id": "topic-24-4",
        "year": 2024,
        "paper_code": "0520/S24/T4",
        "topic": "Les Vacances",
        "type": "topic",
        "group": "A",
        "theme": "Voyages et vacances",
        "scenario": "Conversation sur vos habitudes de vacances, vos destinations préférées et vos expériences de voyage.",
        "examiner_role": "examinateur",
        "opening_question": "Où est-ce que tu vas d'habitude en vacances ?",
        "bullet_points": [
            "Dire où vous allez d'habitude en vacances et avec qui",
            "Décrire vos activités préférées en vacances",
            "Raconter vos dernières vacances en détail",
            "Dire où vous aimeriez aller l'année prochaine et pourquoi",
            "Donner les avantages et inconvénients de partir à l'étranger",
        ],
        "examiner_prompts": [
            "Où est-ce que tu vas d'habitude en vacances ?",
            "Qu'est-ce que tu aimes faire quand tu es en vacances ?",
            "Parle-moi de tes dernières vacances — où es-tu allé(e) et qu'est-ce que tu as fait ?",
            "Si tu avais le choix, où aimerais-tu aller l'été prochain ?",
            "Quels sont les avantages de voyager dans d'autres pays ?",
        ],
        "difficulty": 3,
        "time_limit_sec": 300,
    },
    {
        "id": "topic-24-5",
        "year": 2024,
        "paper_code": "0520/S24/T5",
        "topic": "Ma Routine",
        "type": "topic",
        "group": "A",
        "theme": "Vie quotidienne et routine",
        "scenario": "Conversation sur votre journée typique, vos habitudes et votre vie quotidienne.",
        "examiner_role": "examinateur",
        "opening_question": "Parle-moi de ta routine le matin avant de partir à l'école.",
        "bullet_points": [
            "Décrire votre routine du matin en détail",
            "Dire ce que vous faites pour aider à la maison",
            "Raconter ce que vous avez fait hier après l'école",
            "Décrire à quoi ressemblera votre journée de demain",
            "Expliquer ce que vous changeriez à votre routine si vous pouviez",
        ],
        "examiner_prompts": [
            "Parle-moi de ta routine le matin avant de partir à l'école.",
            "Qu'est-ce que tu fais pour aider tes parents à la maison ?",
            "Qu'est-ce que tu as fait hier après l'école ?",
            "Comment sera ta journée de demain ?",
            "Si tu pouvais, qu'est-ce que tu changerais à ta routine quotidienne ?",
        ],
        "difficulty": 3,
        "time_limit_sec": 300,
    },
    {
        "id": "topic-24-6",
        "year": 2024,
        "paper_code": "0520/S24/T6",
        "topic": "Le Monde du Travail",
        "type": "topic",
        "group": "B",
        "theme": "Travail, carrière et projets d'avenir",
        "scenario": "Conversation sur vos projets professionnels, vos ambitions et votre vision du monde du travail.",
        "examiner_role": "examinateur",
        "opening_question": "Quel travail est-ce que tu voudrais faire à l'avenir ?",
        "bullet_points": [
            "Dire quel métier vous voulez exercer plus tard et pourquoi",
            "Expliquer quelles qualifications ou études sont nécessaires",
            "Parler d'une expérience de travail ou d'un stage",
            "Donner votre opinion sur le travail à l'étranger",
            "Discuter de l'importance du salaire vs. la passion dans un métier",
        ],
        "examiner_prompts": [
            "Quel travail est-ce que tu voudrais faire à l'avenir ?",
            "Quelles études ou qualifications faut-il pour faire ce métier ?",
            "Est-ce que tu as déjà fait un stage ou un petit job ?",
            "Est-ce que tu aimerais travailler dans un autre pays ? Pourquoi ?",
            "À ton avis, est-ce que le salaire est le plus important dans un job ?",
        ],
        "difficulty": 3,
        "time_limit_sec": 300,
    },
    {
        "id": "topic-24-7",
        "year": 2024,
        "paper_code": "0520/S24/T7",
        "topic": "L'Environnement",
        "type": "topic",
        "group": "B",
        "theme": "Environnement et développement durable",
        "scenario": "Conversation sur les problèmes écologiques, vos actions pour la planète et l'avenir de l'environnement.",
        "examiner_role": "examinateur",
        "opening_question": "À ton avis, quel est le plus grand problème pour l'environnement aujourd'hui ?",
        "bullet_points": [
            "Identifier les problèmes écologiques les plus graves selon vous",
            "Décrire ce que vous faites personnellement pour protéger l'environnement",
            "Raconter une action écologique faite récemment (à l'école ou chez vous)",
            "Imaginer comment sera la planète dans cinquante ans",
            "Donner votre avis sur l'utilisation des transports publics vs. la voiture",
        ],
        "examiner_prompts": [
            "À ton avis, quel est le plus grand problème pour l'environnement aujourd'hui ?",
            "Qu'est-ce que tu fais personnellement pour recycler ou protéger l'environnement ?",
            "Qu'est-ce que ton école a fait récemment pour l'environnement ?",
            "Comment est-ce que tu imagines la Terre dans cinquante ans ?",
            "Est-ce que tu penses que les gens devraient utiliser davantage les transports en commun ?",
        ],
        "difficulty": 3,
        "time_limit_sec": 300,
    },
    {
        "id": "topic-24-8",
        "year": 2024,
        "paper_code": "0520/S24/T8",
        "topic": "Les Loisirs",
        "type": "topic",
        "group": "B",
        "theme": "Loisirs, sport et temps libre",
        "scenario": "Conversation sur vos passe-temps, votre façon de vous détendre et vos opinions sur les loisirs.",
        "examiner_role": "examinateur",
        "opening_question": "Qu'est-ce que tu aimes faire pendant ton temps libre ?",
        "bullet_points": [
            "Décrire vos loisirs et passe-temps préférés",
            "Expliquer combien de temps vous y consacrez par semaine",
            "Raconter ce que vous avez fait le week-end dernier pour vous détendre",
            "Dire quel sport ou activité vous aimeriez essayer bientôt",
            "Donner votre avis sur les écrans et les réseaux sociaux chez les jeunes",
        ],
        "examiner_prompts": [
            "Qu'est-ce que tu aimes faire pendant ton temps libre ?",
            "Est-ce que tu as beaucoup de temps pour tes loisirs, avec l'école et tout ça ?",
            "Qu'est-ce que tu as fait samedi dernier pour te détendre ?",
            "Quel sport ou activité aimerais-tu commencer bientôt ? Pourquoi ?",
            "Qu'est-ce que tu penses des jeunes qui passent trop de temps sur les écrans ?",
        ],
        "difficulty": 3,
        "time_limit_sec": 300,
    },
    {
        "id": "topic-24-9",
        "year": 2024,
        "paper_code": "0520/S24/T9",
        "topic": "La Nourriture",
        "type": "topic",
        "group": "B",
        "theme": "Alimentation, santé et culture culinaire",
        "scenario": "Conversation sur vos habitudes alimentaires, vos plats préférés et votre opinion sur la nourriture saine.",
        "examiner_role": "examinateur",
        "opening_question": "Quel est ton plat préféré ? Pourquoi est-ce que tu l'aimes ?",
        "bullet_points": [
            "Décrire votre plat préféré et expliquer pourquoi vous l'aimez",
            "Dire ce que vous mangez habituellement à midi (cantine, maison, etc.)",
            "Raconter un repas spécial au restaurant pour une occasion particulière",
            "Parler de ce que vous allez manger ce soir ou préparer demain",
            "Donner votre avis sur la difficulté de manger sainement de nos jours",
        ],
        "examiner_prompts": [
            "Quel est ton plat préféré ? Pourquoi est-ce que tu l'aimes ?",
            "Qu'est-ce que tu manges normalement à la cantine ou pour le déjeuner ?",
            "Parle-moi d'une fois où tu es allé(e) au restaurant pour une occasion spéciale.",
            "Qu'est-ce que tu vas manger ce soir ou préparer demain ?",
            "Est-ce que c'est facile de manger équilibré de nos jours ? Pourquoi ?",
        ],
        "difficulty": 3,
        "time_limit_sec": 300,
    },
]


# ── IGCSE Cambridge mark-scheme prompt ────────────────────────────────────────
IGCSE_SYSTEM_PROMPT = """You are an IGCSE French oral examiner applying the Cambridge 0520/0680 mark scheme.
Return ONLY a raw JSON object — no markdown, no code fences, no prose outside the JSON.
ALL feedback text must be in English. French appears only inside « … » when quoting the student.

Mark the student's response on exactly 4 criteria (0–5 each):

CRITERION 1 — SYLLABUS COVERAGE
  5: All bullet points addressed fully with relevant detail
  4: Most bullet points addressed; minor omissions
  3: About half the points addressed
  2: Only a small portion of the task completed
  1: Very limited attempt; most points missed
  0: Nothing relevant

CRITERION 2 — COMMUNICATION
  5: Message fully clear, natural and fluent throughout
  4: Message mostly clear; minor hesitations or imprecision
  3: Message gets through despite errors
  2: Significant effort required to understand
  1: Very difficult to follow
  0: No real communication

CRITERION 3 — RANGE OF LANGUAGE
  5: Wide vocab, varied tenses, complex structures, idiomatic expression
  4: Good range; occasional repetition or simple structures
  3: Adequate; reliant on simple vocab and present tense
  2: Limited; basic words, very restricted tense use
  1: Minimal; formulaic phrases only
  0: No meaningful language

CRITERION 4 — ACCURACY
  5: Mostly accurate; only minor slips
  4: Generally accurate; some errors with complex structures
  3: More accurate than inaccurate overall
  2: Frequent errors; inconsistent accuracy
  1: Errors throughout; very little correct
  0: No accurate language

Grade bands (total /20): A*:18-20 | A:15-17 | B:12-14 | C:9-11 | D:6-8 | E:3-5 | U:0-2

Return exactly this JSON (no extra keys):
{
  "scores": { "coverage": <0-5>, "communication": <0-5>, "range": <0-5>, "accuracy": <0-5> },
  "total": <0-20>,
  "grade_band": "<A*/A/B/C/D/E/U>",
  "per_criterion_feedback": {
    "coverage": "<2-3 English sentences explaining the score and quoting evidence>",
    "communication": "<2-3 English sentences>",
    "range": "<2-3 English sentences>",
    "accuracy": "<2-3 English sentences>"
  },
  "bullet_point_coverage": [
    { "bullet": "<bullet text>", "addressed": <true/false>, "comment": "<brief English note>" }
  ],
  "corrected_sample": "<A 60-90 word model French response that would score 5/5 on all criteria>",
  "overall_advice": "<2-3 actionable English sentences for improving the score>"
}"""

NEWS_SYSTEM_PROMPT = """You are a professional French News Editor for a language learning platform.
Your job is to generate a short, engaging news snippet in French (B1 level) for students to practice listening.
Return ONLY a raw JSON object — no prose, no markdown fences, no code blocks.

Guidelines:
1. transcript: 3-4 sentences (approx 40-60 words). Clear, standard French.
2. translation: An accurate English translation of the transcript.
3. headline: Catchy and descriptive (in French).
4. keywords: 4-6 essential French words used in the text.
5. summaryPoints: 3-5 concise English sentences covering the key facts. These will be used to grade user comprehension.
6. Difficulty: Ensure it is suitable for Intermediate (B1) level — avoid overly technical jargon but use natural phrasing.

JSON schema:
{
  "id": "news-YYYY-MM-DD",
  "date": "YYYY-MM-DD",
  "headline": "string",
  "transcript": "string",
  "translation": "string",
  "keywords": ["string"],
  "summaryPoints": ["string"]
}
"""

VOCAB_SYSTEM_PROMPT = """You are a professional French language instructor.
Provide a list of 10 essential French words and 3 useful phrases (with English translations) for a roleplay scenario about a specific topic.
Return ONLY a raw JSON object — no prose, no markdown fences, no code blocks.

JSON schema:
{
  "vocab": [
    { "fr": "French word", "en": "English translation", "type": "word" },
    ...
  ],
  "phrases": [
    { "fr": "French phrase", "en": "English translation", "type": "phrase" },
    ...
  ]
}
"""

SYSTEM_PROMPT = """You are a strict, expert IGCSE French speaking examiner with 15 years of experience.
You analyse a student's spoken French answer and return ONLY a raw JSON object — no prose, no markdown fences, no code blocks.

LANGUAGE RULE — CRITICAL: ALL feedback text must be written in English. The ONLY French allowed is:
- Quoting the student's exact words inside « … » when correcting or praising them
- The followUpQuestion field (which must be in French)
- The upgrade/example fields in vocabulary (which show the French phrase)
Do NOT write explanations, grammar notes, or encouragement in French. English only.

JSON schema (return exactly this shape):
{
  "fluency": number,         // 0.0–10.0 (one decimal). Strict: 8+ = genuinely impressive. Most answers score 4–6.
  "grammar": string[],       // 3–5 items (standard) or 5–8 items (detailed). Each MUST quote exact student words with « … » and explain the error or praise correct usage. Written in English.
  "vocabulary": [            // 2–4 items (standard) or 4–7 items (detailed). Each references a word the student actually used and suggests a richer upgrade. All explanations in English.
    { "basic": string, "upgrade": string, "example": string }
  ],
  "structure": string[],     // 2–3 items (standard) or 3–5 items (detailed). English commentary on answer length, connectives, tense variety, opinion phrases — tied to this specific answer.
  "pronunciationTips": string[], // 1–3 items OR []. English explanation of the phonetic issue (nasal vowels, silent letters, liaisons). Only include if pronunciation data flags issues.
  "encouragement": string,   // 1–2 warm, specific sentences in English referencing something the student actually did well.
  "followUpQuestion": string, // ONE natural French follow-up question that directly continues THIS conversation.
  "igcseLevel": string,      // Exactly one of: "Foundation — Developing" | "Core — Secure" | "Extended — Mid Band" | "Extended — High Band"
  "pronunciation": {
    "score": number,         // 0–10. Based ONLY on what you heard (or on whisper confidence data if no audio).
    "issues": [              // [] if no issues. Max 6 items. Only flag real problems.
      {
        "word": string,      // exact word from transcript
        "problem": string,   // what went wrong, phonetically specific (English)
        "expected": string,  // how it should sound, simple phonetic description (English)
        "severity": string,  // "low" | "medium" | "high"
        "timestamp": number  // seconds from start, or null
      }
    ]
  }
}

CRITICAL rules:
1. ALL explanatory text is in English. Quote student French with « … » but explain it in English.
2. Every grammar and vocabulary comment must quote the student's actual words from the transcript.
3. If the student used something correctly, say so and quote it — positive reinforcement matters.
4. Fluency score: factor in word count, tense variety, connectives (parce que, donc, cependant), opinion phrases. A 30-word answer with no connectives is 4.0–5.0 max.
5. followUpQuestion must directly reference something the student mentioned — make it feel like a real conversation.
6. igcseLevel: Foundation = minimal/broken French; Core = adequate but simple; Extended Mid = good range with some errors; Extended High = impressive range, accuracy, fluency.
7. Output raw JSON only — no wrapping text, code fences, or anything outside the JSON object.
"""

MULTIMODAL_SYSTEM_PROMPT = """You are a professional French oral examiner with specialist phonetics training (IPA-certified).
You receive BOTH an audio recording AND the transcript of a student's spoken French.
Return ONLY a raw JSON object — no prose, no markdown, nothing outside the JSON.

CRITICAL: You MUST analyze the actual AUDIO for pronunciation. The transcript alone cannot reveal accent, liaison, nasal vowels, or vowel quality. Listen carefully.

PRONUNCIATION ANALYSIS — examine the audio for:
1. Nasal vowels: /ɑ̃/ (an/en/am), /ɛ̃/ (in/im/ain/ein), /ɔ̃/ (on/om), /œ̃/ (un/um)
   → Many learners de-nasalize these (sound like English vowels). Flag it.
2. Silent letters: final consonants are usually silent (petit, beaucoup, trop, chat, blanc)
   → Learners often pronounce final -t, -s, -p, -x, -z incorrectly.
3. Liaison: required liaisons must be made (les_enfants /lez‿ɑ̃fɑ̃/, vous_avez /vuz‿ave/)
   → Flag missing required liaisons AND incorrect liaisons where silence is required.
4. French R: uvular /ʁ/ (produced at back of throat), NOT the English retroflex /r/
5. French U: front rounded /y/ (like saying "ee" with lips rounded) — NOT "oo"
6. É vs È: closed /e/ (été) vs open /ɛ/ (être, elles) — learners often merge these
7. Rhythm: French has approximately equal syllable length (syllable-timed). No stress accent.
8. Intonation: rising at end of yes/no questions; falling for statements.

For each issue found in the audio, return an object in pronunciation.issues[].
If pronunciation is good, return pronunciation.issues = [].

PHONEME-LEVEL WORD ANALYSIS — for each word with score < 8 (maximum 8 words):
Identify exactly what phoneme(s) the student got wrong and why. For each such word produce an entry in "words[]".
Include "ipa_expected" (correct IPA), "ipa_heard" (what the student appeared to produce), and a "phonemes[]" array listing each phoneme discrepancy.
Include a "drill" object with: correct IPA, a simple step-by-step hint, a short repeat phrase, and the context sentence.

ALSO evaluate grammar, vocabulary, structure, and fluency from the transcript.

JSON schema (return EXACTLY this, no extra keys):
{
  "fluency": <0.0–10.0>,
  "grammar": ["<English feedback quoting student words in « … »>"],
  "vocabulary": [{"basic": "...", "upgrade": "...", "example": "..."}],
  "structure": ["<English structure tip>"],
  "pronunciationTips": ["<concise English phonetic tip>"],
  "encouragement": "<1-2 warm English sentences about something specific the student did well>",
  "followUpQuestion": "<ONE natural French follow-up that continues THIS specific conversation>",
  "igcseLevel": "<Foundation — Developing | Core — Secure | Extended — Mid Band | Extended — High Band>",
  "pronunciation": {
    "score": <0–10>,
    "issues": [
      {
        "word": "<exact word from transcript>",
        "problem": "<phonetically specific description of what was wrong>",
        "expected": "<how it should sound — simple description, no IPA required>",
        "severity": "<low|medium|high>",
        "timestamp": <seconds from start, or null>
      }
    ]
  },
  "words": [
    {
      "text": "<exact word from transcript — only include words with score < 8>",
      "score": <0–10 integer pronunciation score for this word>,
      "ipa_expected": "<correct IPA of this French word, e.g. /boku/>",
      "ipa_heard": "<IPA approximation of what the student appeared to say>",
      "phonemes": [
        {
          "expected": "<the IPA phoneme(s) that should have been produced>",
          "actual": "<the IPA phoneme(s) the student produced instead>",
          "issue": "<concise label, e.g. 'nasal vowel denasalized', 'final consonant pronounced', 'R not uvular'>",
          "severity": "<low|medium|high>",
          "explanation": "<one sentence: WHY this is wrong, e.g. 'French /ɑ̃/ is a nasal vowel — air must pass through the nose, not just the mouth'>"
        }
      ],
      "drill": {
        "ipa": "<full IPA of the word>",
        "hint": "<2-3 step guide to produce this word correctly, e.g. '1. Round lips to say ü. 2. Keep tongue front. 3. Vibrate throat for the R.'>",
        "repeat_phrase": "<short French phrase (5-8 words) to practice this word in natural context>",
        "context_phrase": "<the sentence from the student transcript that contains this word>"
      }
    }
  ]
}
"""


def build_user_prompt(req: FeedbackRequest) -> str:
    m = req.metrics.model_dump(exclude_none=True) if req.metrics else {}

    # Extract word probabilities for pronunciation assessment
    pron_section = ""
    if req.metrics and req.metrics.wordProbabilities:
        low_conf = [
            wp for wp in req.metrics.wordProbabilities
            if wp.probability is not None and wp.probability < 0.70
        ]
        if low_conf:
            words_str = ", ".join(
                f"« {wp.word} » ({int(wp.probability * 100)}% confidence)"
                for wp in low_conf[:8]
            )
            pron_section = (
                f"\n\nPRONUNCIATION DATA (from speech recognition):\n"
                f"These words had low recognition confidence — likely mispronounced:\n"
                f"{words_str}\n"
                f"Please include targeted French pronunciation tips for these words."
            )

    # Remove wordProbabilities from metrics dict for the prompt (too verbose)
    m.pop("wordProbabilities", None)

    detail_instruction = (
        "\n\nDETAILED MODE: Provide maximum depth. Use the upper end of all item ranges "
        "(5–8 grammar items, 4–7 vocabulary items, 3–5 structure items). "
        "Go beyond surface corrections — explain WHY each error matters for IGCSE, "
        "what mark band it affects, and give a corrected model sentence for each grammar issue."
        if req.detailed else ""
    )

    return (
        f"⚠️ IMPORTANT: Write ALL feedback, grammar notes, vocabulary explanations, structure tips, and encouragement in ENGLISH. "
        f"The only French permitted is: quoting student phrases inside « … », the followUpQuestion field, and vocabulary upgrade examples.\n\n"
        f"QUESTION (French): {req.question}\n\n"
        f"STUDENT TRANSCRIPT (French): {req.transcript}\n\n"
        f"DELIVERY METRICS: {json.dumps(m, ensure_ascii=False)}"
        f"{pron_section}"
        f"{detail_instruction}\n\n"
        f"Return the JSON feedback now. Remember: feedback text in ENGLISH only."
    )


def extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of the model response."""
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in model response")
    return json.loads(text[start:end + 1])


AI_PROVIDER_TIMEOUT_SEC = float(os.getenv("AI_PROVIDER_TIMEOUT_SEC", "25"))
AI_PROVIDER_RETRIES = int(os.getenv("AI_PROVIDER_RETRIES", "2"))


def _log_provider_failure(provider: str, exc: Exception, attempt: int | None = None) -> None:
    attempt_part = f" attempt={attempt}" if attempt is not None else ""
    log.error(
        "%s failed%s: %s\n%s",
        provider,
        attempt_part,
        repr(exc),
        traceback.format_exc(),
    )


async def _run_with_retries(
    provider: str,
    operation,
    *,
    attempts: int = AI_PROVIDER_RETRIES,
    timeout_sec: float = AI_PROVIDER_TIMEOUT_SEC,
) -> dict[str, Any]:
    last_exc: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return await asyncio.wait_for(operation(), timeout=timeout_sec)
        except ResourceExhausted:
            raise
        except Exception as exc:
            last_exc = exc
            _log_provider_failure(provider, exc, attempt)
            if attempt >= attempts:
                break
            await asyncio.sleep(min(0.75 * attempt, 2.0))

    if last_exc:
        raise last_exc
    raise RuntimeError(f"{provider} failed without an exception")


def _metric_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


async def _call_groq(prompt: str, detailed: bool = False) -> dict[str, Any]:
    groq = get_groq()
    if not groq:
        raise RuntimeError("Groq not configured")

    async def operation() -> dict[str, Any]:
        resp = await groq.chat.completions.create(
            model="llama-3.3-70b-versatile",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.4,
            max_tokens=2048 if detailed else 1024,
        )
        result = extract_json(resp.choices[0].message.content)
        result["modelUsed"] = "groq/llama-3.3-70b-versatile"
        return result

    return await _run_with_retries("groq/llama-3.3-70b-versatile", operation)


async def _call_gemini(prompt: str) -> dict[str, Any]:
    """Text-only Gemini call (standard feedback prompt)."""
    gemini = get_gemini()
    if not gemini:
        raise RuntimeError("Gemini not configured")

    async def operation() -> dict[str, Any]:
        response = await asyncio.to_thread(gemini.generate_content, prompt)
        result = extract_json(getattr(response, "text", "") or "")
        result["modelUsed"] = "gemini/gemini-2.0-flash"
        return result

    try:
        return await _run_with_retries("gemini/gemini-2.0-flash", operation)
    except ResourceExhausted as exc:
        _log_provider_failure("gemini/gemini-2.0-flash quota exhausted", exc)
        raise


async def _call_gemini_multimodal(
    prompt: str,
    audio_path: str,
    mime_type: str = "audio/webm",
) -> dict[str, Any]:
    """Gemini audio + transcript feedback with timeout, retries, and quota-aware errors."""
    from google.generativeai import types as gtypes

    gemini = get_gemini_multimodal()
    if not gemini:
        raise RuntimeError("Gemini not configured")

    with open(audio_path, "rb") as f:
        audio_bytes = f.read()

    audio_part = gtypes.Part(
        inline_data=gtypes.Blob(mime_type=mime_type, data=audio_bytes)
    )

    async def operation() -> dict[str, Any]:
        response = await asyncio.to_thread(
            gemini.generate_content,
            [audio_part, prompt],
        )
        result = extract_json(getattr(response, "text", "") or "")
        result["modelUsed"] = "gemini/gemini-2.0-flash-multimodal"
        return result

    try:
        return await _run_with_retries("gemini/gemini-2.0-flash-multimodal", operation)
    except ResourceExhausted as exc:
        _log_provider_failure("gemini/gemini-2.0-flash-multimodal quota exhausted", exc)
        raise


def _offline_feedback(req: FeedbackRequest, provider_errors: list[dict[str, str]] | None = None) -> dict[str, Any]:
    transcript = (req.transcript or "").strip()
    words = re.findall(r"\b[\w'-]+\b", transcript, flags=re.UNICODE)
    word_count = len(words)
    metrics = req.metrics.model_dump(exclude_none=True) if req.metrics else {}
    words_per_minute = _metric_float(metrics.get("wordsPerMinute"))

    length_score = min(10.0, max(2.0, word_count / 8.0))
    fluency_score = _metric_float(metrics.get("fluencyScore"), length_score) or length_score
    if words_per_minute:
        if 80 <= words_per_minute <= 150:
            fluency_score = max(fluency_score, 7.0)
        elif words_per_minute < 45:
            fluency_score = min(fluency_score, 5.0)

    has_past = bool(re.search(r"\b(ai|as|a|avons|avez|ont|suis|es|est|sommes|etes|sont)\b", transcript, re.I))
    has_connective = bool(re.search(r"\b(parce que|mais|aussi|donc|cependant|puis|ensuite)\b", transcript, re.I))
    has_opinion = bool(re.search(r"\b(je pense|j aime|j'aime|je n aime pas|je n'aime pas|a mon avis|selon moi)\b", transcript, re.I))

    grammar = []
    if word_count < 12:
        grammar.append("The answer is understandable but quite short; add one or two extra details to show control of sentence structure.")
    if not has_past:
        grammar.append("Try to include a past-tense detail when the question allows it, for example something you did recently.")
    if not has_connective:
        grammar.append("Use a connective such as 'parce que', 'mais' or 'ensuite' to link ideas more naturally.")
    if not grammar:
        grammar.append("The response has a clear basic structure. Keep checking verb endings and agreement as you expand your answer.")

    structure = []
    if not has_opinion:
        structure.append("Add a clear opinion and a reason so the answer feels more developed.")
    structure.append("Aim for a simple pattern: answer the question, add a detail, then give a reason or example.")

    low_conf_words = []
    if req.metrics and req.metrics.wordProbabilities:
        low_conf_words = [
            wp.word for wp in req.metrics.wordProbabilities
            if wp.probability is not None and wp.probability < 0.7
        ][:5]

    pronunciation_tips = (
        [f"Practise the pronunciation of '{word}' slowly, then repeat it inside the full sentence." for word in low_conf_words[:3]]
        if low_conf_words
        else ["Pronunciation detail is limited because the AI audio provider is unavailable; record again later for word-level analysis."]
    )

    return {
        "fluency": round(max(0.0, min(10.0, fluency_score)), 1),
        "grammar": grammar,
        "vocabulary": [
            {
                "basic": "tres bien",
                "upgrade": "vraiment interessant",
                "example": "C'est vraiment interessant parce que cela me permet de progresser.",
            }
        ],
        "structure": structure,
        "pronunciationTips": pronunciation_tips,
        "encouragement": "Your answer has enough information to build from. Keep extending it with reasons, examples and time phrases.",
        "followUpQuestion": "Peux-tu me donner un exemple ?",
        "igcseLevel": "Core - Secure" if word_count >= 25 else "Foundation - Developing",
        "pronunciation": {
            "score": None,
            "issues": [
                {
                    "word": word,
                    "problem": "Speech recognition confidence was low for this word.",
                    "expected": "Repeat slowly, then blend it back into the sentence.",
                    "severity": "medium",
                    "timestamp": None,
                }
                for word in low_conf_words
            ],
        },
        "words": [],
        "providerStatus": "offline_fallback",
        "providerErrors": provider_errors or [],
        "modelUsed": "offline/local-evaluator",
    }


async def _try_feedback_provider(
    provider_name: str,
    operation,
    errors: list[dict[str, str]],
) -> dict[str, Any] | None:
    try:
        return await operation()
    except ResourceExhausted as exc:
        _log_provider_failure(f"{provider_name} quota exhausted", exc)
        errors.append({"provider": provider_name, "type": "quota_exhausted", "message": str(exc)})
    except (asyncio.TimeoutError, TimeoutError) as exc:
        _log_provider_failure(f"{provider_name} timeout", exc)
        errors.append({"provider": provider_name, "type": "timeout", "message": str(exc)})
    except (ValueError, json.JSONDecodeError) as exc:
        _log_provider_failure(f"{provider_name} malformed_response", exc)
        errors.append({"provider": provider_name, "type": "malformed_response", "message": str(exc)})
    except Exception as exc:
        _log_provider_failure(provider_name, exc)
        errors.append({"provider": provider_name, "type": exc.__class__.__name__, "message": str(exc)})
    return None


async def call_ai_feedback(
    req: FeedbackRequest,
    audio_path: str | None = None,
    audio_mime: str = "audio/webm",
) -> dict[str, Any]:
    prompt = build_user_prompt(req)
    detailed = req.detailed
    has_audio = bool(audio_path)
    provider_errors: list[dict[str, str]] = []

    if has_audio:
        result = await _try_feedback_provider(
            "gemini/gemini-2.0-flash-multimodal",
            lambda: _call_gemini_multimodal(prompt, audio_path, mime_type=audio_mime),
            provider_errors,
        )
    else:
        result = await _try_feedback_provider(
            "gemini/gemini-2.0-flash",
            lambda: _call_gemini(prompt),
            provider_errors,
        )
    if result:
        result.setdefault("providerStatus", "primary")
        return result

    result = await _try_feedback_provider(
        "groq/llama-3.3-70b-versatile",
        lambda: _call_groq(prompt, detailed),
        provider_errors,
    )
    if result:
        result.setdefault("providerStatus", "fallback")
        result["fallbackReason"] = provider_errors[0]["type"] if provider_errors else "primary_unavailable"
        result["providerErrors"] = provider_errors
        return result

    return _offline_feedback(req, provider_errors)


def enrich_feedback(fb: dict[str, Any], req: FeedbackRequest) -> dict[str, Any]:
    """Ensure the response matches what coach.js / the UI expect."""
    m = req.metrics.model_dump(exclude_none=True) if req.metrics else {}
    m.pop("wordProbabilities", None)
    fb.setdefault("wordCount", len(req.transcript.split()))

    # Preserve Gemini's pronunciation.issues if present; otherwise build delivery metrics
    existing_pron = fb.get("pronunciation", {})
    delivery_metrics = {
        "wordsPerMinute": m.get("wordsPerMinute"),
        "pauseCount": m.get("pauseCount"),
        "sentenceCount": m.get("sentenceCount"),
        "avgWordsPerSentence": m.get("avgWordsPerSentence"),
    }
    if isinstance(existing_pron, dict) and "issues" in existing_pron:
        # Gemini returned structured pronunciation — merge delivery metrics in
        existing_pron.update({k: v for k, v in delivery_metrics.items() if v is not None})
        fb["pronunciation"] = existing_pron
    else:
        # No structured pronunciation from AI — build from delivery metrics only
        fb["pronunciation"] = {**delivery_metrics, "score": None, "issues": []}

    # Preserve phoneme-level word data from multimodal Gemini
    if "words" not in fb:
        fb["words"] = []

    fb.setdefault("pronunciationTips", [])
    for k in ("hasAccents", "hasPastTense", "hasConnectives", "hasOpinion", "hasConditional"):
        if k in m:
            fb.setdefault(k, m[k])
    fb.setdefault("source", "groq")
    return fb


async def _feedback_impl(
    question: str,
    transcript: str,
    model: str,
    detailed: bool,
    metrics_json: str,
    audio: UploadFile | None,
) -> dict[str, Any]:
    """
    Unified feedback endpoint. Two modes:
      • With audio:    transcribe → multimodal Gemini (pronunciation-aware)
      • Without audio: use provided transcript → text-only AI
    """
    tmp_path: str | None = None
    audio_mime = "audio/webm"

    try:
        # ── Step 1: Transcribe audio if present ───────────────────────────────
        whisper_data: dict[str, Any] = {}

        if audio and audio.filename:
            suffix   = os.path.splitext(audio.filename)[1] or ".webm"
            audio_mime = audio.content_type or f"audio/{suffix.lstrip('.')}" or "audio/webm"
            raw = await audio.read()

            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(raw)
                tmp_path = tmp.name

            # Try Groq Whisper first (fastest, no local model needed)
            if GROQ_API_KEY:
                try:
                    whisper_data = await _groq_whisper(tmp_path, "fr")
                except Exception as e:
                    log.warning("Groq Whisper failed, using faster-whisper: %s", e)

            if not whisper_data:
                whisper_data = await _faster_whisper(tmp_path, "fr")

            transcript = (whisper_data.get("text") or transcript or "").strip()

        if not transcript.strip():
            raise HTTPException(status_code=400, detail="No transcript provided and audio was empty or unrecognisable")

        # ── Step 2: Parse frontend metrics ────────────────────────────────────
        try:
            metrics_dict = json.loads(metrics_json) if metrics_json and metrics_json != "{}" else {}
        except json.JSONDecodeError:
            metrics_dict = {}

        # Merge Whisper word-level data into metrics
        whisper_words = whisper_data.get("words", [])
        if whisper_words:
            metrics_dict["wordProbabilities"] = whisper_words

        try:
            metrics_obj = FeedbackMetrics(**metrics_dict)
        except Exception:
            metrics_obj = FeedbackMetrics(wordProbabilities=whisper_words or None)

        req = FeedbackRequest(
            question=question,
            transcript=transcript,
            metrics=metrics_obj,
            model=model,
            detailed=detailed,
        )

        # ── Step 3: AI feedback (multimodal if audio present) ─────────────────
        fb = await call_ai_feedback(req, audio_path=tmp_path, audio_mime=audio_mime)
        result = enrich_feedback(fb, req)

        result["transcript"]       = transcript
        result["whisper_segments"] = whisper_data.get("segments", [])
        result["whisper_words"]    = whisper_words   # word-level confidence from Whisper
        result["audio_analyzed"]   = tmp_path is not None
        # Ensure words[] is present (phoneme-level data from multimodal Gemini)
        result.setdefault("words", [])

        return result

    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


@app.post("/api/feedback")
@app.post("/api/feedback/v2")
@app.post("/api/feedback/v3")
async def feedback(request: Request) -> dict[str, Any]:
    """
    Backward-compatible feedback endpoint that accepts:
      - multipart/form-data (with optional audio file), or
      - application/json (transcript-only flow)
    """
    content_type = (request.headers.get("content-type") or "").lower()

    question = ""
    transcript = ""
    model = "gemini"
    detailed = False
    metrics_json = "{}"
    audio: UploadFile | None = None

    if "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        question = str(form.get("question") or "")
        transcript = str(form.get("transcript") or "")
        model = str(form.get("model") or "gemini")
        detailed = str(form.get("detailed") or "false").lower() == "true"
        metrics_json = str(form.get("metrics_json") or "{}")
        maybe_audio = form.get("audio")
        if isinstance(maybe_audio, UploadFile) or (
            hasattr(maybe_audio, "read") and hasattr(maybe_audio, "filename")
        ):
            audio = maybe_audio
    else:
        try:
            payload = await request.json()
        except Exception as e:
            raise HTTPException(status_code=400, detail="Invalid JSON body") from e
        question = str(payload.get("question") or payload.get("prompt") or "")
        transcript = str(payload.get("transcript") or payload.get("text") or "")
        model = str(payload.get("model") or "gemini")
        detailed = bool(payload.get("detailed", False))
        metrics = payload.get("metrics")
        if isinstance(metrics, dict):
            metrics_json = json.dumps(metrics)
        elif isinstance(payload.get("metrics_json"), str):
            metrics_json = payload.get("metrics_json") or "{}"

    try:
        return await _feedback_impl(
            question=question,
            transcript=transcript,
            model=model,
            detailed=detailed,
            metrics_json=metrics_json,
            audio=audio,
        )
    except HTTPException:
        raise
    except Exception as exc:
        _log_provider_failure("feedback_endpoint_unhandled", exc)
        fallback_req = FeedbackRequest(
            question=question or "General French speaking practice",
            transcript=transcript or "",
            metrics=None,
            model=model,
            detailed=detailed,
        )
        return enrich_feedback(
            _offline_feedback(
                fallback_req,
                [{"provider": "feedback_endpoint", "type": exc.__class__.__name__, "message": str(exc)}],
            ),
            fallback_req,
        )


# ── /api/repair — micro-repair loop ──────────────────────────────────────────

@app.post("/api/repair")
async def repair_pronunciation(
    audio: UploadFile = File(...),
    word: str = Form(...),
    context: str = Form(""),        # surrounding phrase for context
    original_problem: str = Form(""), # the issue description shown to user
) -> dict[str, Any]:
    """
    Evaluate a single word/phrase re-recording.
    Returns {score, improved, feedback, phonetics_guide}.
    """
    suffix = os.path.splitext(audio.filename or "")[1] or ".webm"
    audio_mime = audio.content_type or "audio/webm"
    raw = await audio.read()

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(raw)
        tmp_path = tmp.name

    try:
        # Transcribe what the user actually said
        whisper_data: dict[str, Any] = {}
        if GROQ_API_KEY:
            try:
                whisper_data = await _groq_whisper(tmp_path, "fr")
            except Exception as e:
                log.warning("Groq Whisper repair failed: %s", e)
        if not whisper_data:
            try:
                whisper_data = await _faster_whisper(tmp_path, "fr")
            except Exception:
                pass

        heard = (whisper_data.get("text") or "").strip()

        # Shared prompt text for both Groq (text) and Gemini (multimodal)
        repair_prompt = (
            f"A French learner is trying to improve their pronunciation of the word/phrase: «{word}»\n"
            f"Context sentence: {context or '(none provided)'}\n"
            f"Original pronunciation issue: {original_problem or '(not specified)'}\n"
            f"What speech recognition heard the learner say: {heard or '(unclear)'}\n\n"
            f"Evaluate ONLY the pronunciation of «{word}» based on the information above.\n\n"
            f"Return ONLY this JSON (nothing else):\n"
            f'{{\n'
            f'  "score": <0-10, where 10 = perfect native pronunciation>,\n'
            f'  "improved": <true if noticeably better than described issue>,\n'
            f'  "heard": "{heard or word}",\n'
            f'  "feedback": "<1-2 sentences: what was good, what still needs work>",\n'
            f'  "phonetics_guide": "<simple step-by-step guide to produce this sound correctly>",\n'
            f'  "tip": "<one specific actionable tip for this exact word>"\n'
            f'}}'
        )

        result = None

        # ── Primary: Groq (text-only, uses Whisper transcript) ───────────────
        groq = get_groq()
        if groq:
            try:
                resp = await groq.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": "You are a French pronunciation expert. Return only valid JSON."},
                        {"role": "user", "content": repair_prompt},
                    ],
                    temperature=0.3,
                    max_tokens=400,
                )
                result = extract_json(resp.choices[0].message.content)
                result["source"] = "groq"
            except Exception as e:
                log.warning("Groq repair failed, trying Gemini: %s", e)

        # ── Fallback: Gemini multimodal (sends actual audio) ─────────────────
        if result is None and GEMINI_API_KEY:
            try:
                import google.generativeai as genai
                from google.generativeai import types as gtypes
                gemini = get_gemini_multimodal()
                with open(tmp_path, "rb") as f:
                    audio_bytes = f.read()
                audio_part = gtypes.Part(
                    inline_data=gtypes.Blob(mime_type=audio_mime, data=audio_bytes)
                )
                gemini_prompt = repair_prompt.replace(
                    "Evaluate ONLY the pronunciation",
                    "Listen to the audio recording and evaluate ONLY the pronunciation"
                )
                response = await asyncio.to_thread(gemini.generate_content, [audio_part, gemini_prompt])
                result = extract_json(response.text)
                result["source"] = "gemini"
            except Exception as e:
                log.warning("Gemini repair also failed: %s", e)

        # ── Both failed: return graceful degraded response ────────────────────
        if result is None:
            return {
                "word": word,
                "heard": heard or word,
                "score": None,
                "improved": None,
                "feedback": "Pronunciation analysis is temporarily unavailable. Keep practising — record yourself again and compare with the model IPA above.",
                "phonetics_guide": "Try listening to the word on Forvo or Google Translate, then record yourself matching the rhythm and sounds.",
                "tip": f"Break «{word}» into syllables and practise each one slowly before combining them.",
                "source": "unavailable",
            }

        result["word"] = word
        result["heard"] = result.get("heard") or heard
        return result

    except HTTPException:
        raise
    except Exception as e:
        _log_provider_failure("repair-endpoint", e)
        return {
            "word": word,
            "heard": word,
            "score": None,
            "improved": None,
            "feedback": "Pronunciation analysis is temporarily unavailable. Please try again shortly.",
            "phonetics_guide": "Break the word into syllables, practise each sound slowly, then rebuild the full word.",
            "tip": f"Repeat {word} three times slowly, then once in the full sentence.",
            "source": "offline_fallback",
        }
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ── /api/drill — generate a 3-step practice drill for a word ─────────────────

@app.post("/api/drill")
async def generate_drill(
    word: str = Form(...),
    context: str = Form(""),
    ipa: str = Form(""),
    issue: str = Form(""),
) -> dict[str, Any]:
    """
    Generate a targeted pronunciation drill for a single French word.
    Returns {ipa, hint, sentences: [{fr, en}], tip}.
    """
    prompt = (
        f"A student is drilling the French word «{word}».\n"
        f"Pronunciation issue: {issue or '(not specified)'}\n"
        f"IPA of the word: {ipa or '(unknown)'}\n"
        f"Original context: {context or '(none)'}\n\n"
        f"Return ONLY this JSON (no markdown):\n"
        f'{{\n'
        f'  "ipa": "<correct IPA of {word}>",\n'
        f'  "hint": "<3-step guide to produce each phoneme correctly>",\n'
        f'  "sentences": [\n'
        f'    {{"fr": "<easy sentence with {word}>", "en": "<English translation>"}},\n'
        f'    {{"fr": "<medium sentence with {word}>", "en": "<English translation>"}},\n'
        f'    {{"fr": "<harder sentence with {word}>", "en": "<English translation>"}}\n'
        f'  ],\n'
        f'  "tip": "<one final actionable tip for this exact word>"\n'
        f'}}'
    )

    try:
        gemini = get_gemini()
        if not gemini:
            raise RuntimeError("Gemini not configured")
        response = await asyncio.wait_for(
            asyncio.to_thread(gemini.generate_content, prompt),
            timeout=AI_PROVIDER_TIMEOUT_SEC,
        )
        result = extract_json(response.text)
        result["word"] = word
        return result
    except Exception as e:
        _log_provider_failure("drill-gemini", e)
        return {
            "word": word,
            "ipa": ipa or "",
            "hint": f"Say '{word}' slowly, break it into syllables, then repeat it in the original sentence.",
            "sentences": [
                {"fr": context or f"Je repete {word}.", "en": "Practice sentence"},
                {"fr": f"Je peux dire {word} clairement.", "en": "I can say the word clearly."},
                {"fr": f"Je pratique {word} avec confiance.", "en": "I practise the word with confidence."},
            ],
            "tip": issue or "Focus on one sound at a time, then rebuild the full word.",
            "source": "offline_fallback",
        }


# ── IGCSE feedback ────────────────────────────────────────────────────────────

def build_igcse_prompt(req: IGCSEFeedbackRequest) -> str:
    bullets = "\n".join(f"  • {b}" for b in req.bullet_points) if req.bullet_points else "  (none provided)"
    m = req.metrics.model_dump(exclude_none=True) if req.metrics else {}
    m.pop("wordProbabilities", None)
    return (
        f"TASK: {req.question}\n\n"
        f"BULLET POINTS THE STUDENT MUST ADDRESS:\n{bullets}\n\n"
        f"STUDENT TRANSCRIPT: {req.transcript}\n\n"
        f"DELIVERY METRICS: {json.dumps(m, ensure_ascii=False)}\n\n"
        f"Apply the Cambridge 0520 mark scheme and return the JSON now."
    )


async def _call_groq_igcse(prompt: str) -> dict[str, Any]:
    groq = get_groq()
    if not groq:
        raise RuntimeError("Groq not configured")

    async def operation() -> dict[str, Any]:
        resp = await groq.chat.completions.create(
            model="llama-3.3-70b-versatile",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": IGCSE_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=1500,
        )
        result = extract_json(resp.choices[0].message.content)
        result["modelUsed"] = "groq/llama-3.3-70b-versatile"
        return result

    return await _run_with_retries("groq-igcse", operation)


async def _call_gemini_igcse(prompt: str) -> dict[str, Any]:
    gemini = get_gemini_igcse()
    if not gemini:
        raise RuntimeError("Gemini not configured")

    async def operation() -> dict[str, Any]:
        response = await asyncio.to_thread(gemini.generate_content, prompt)
        result = extract_json(getattr(response, "text", "") or "")
        result["modelUsed"] = "gemini/gemini-2.0-flash"
        return result

    return await _run_with_retries("gemini-igcse", operation)


def _offline_igcse_feedback(req: IGCSEFeedbackRequest, provider_errors: list[dict[str, str]] | None = None) -> dict[str, Any]:
    words = re.findall(r"\b[\w'-]+\b", req.transcript or "", flags=re.UNICODE)
    word_count = len(words)
    base = 2 if word_count < 20 else 3 if word_count < 60 else 4
    scores = {
        "coverage": min(5, base + (1 if req.bullet_points and word_count >= 40 else 0)),
        "communication": min(5, base),
        "range": min(5, base),
        "accuracy": min(5, max(1, base - 1)),
    }
    total = sum(scores.values())
    return {
        "scores": scores,
        "total": total,
        "grade_band": "A" if total >= 15 else "B" if total >= 12 else "C" if total >= 9 else "D" if total >= 6 else "E",
        "per_criterion_feedback": {
            "coverage": "Offline estimate: coverage was inferred from transcript length and available bullet points.",
            "communication": "Offline estimate: the response appears usable, but AI grading is temporarily unavailable.",
            "range": "Offline estimate: add varied tenses, opinions, and connectives to improve range.",
            "accuracy": "Offline estimate: detailed grammar checking is unavailable while providers are down.",
        },
        "strengths": ["You produced a response that can be reviewed and improved."],
        "next_steps": ["Try again later for full AI marking.", "Add reasons, examples, and time phrases."],
        "modelUsed": "offline/local-igcse-evaluator",
        "providerStatus": "offline_fallback",
        "providerErrors": provider_errors or [],
    }


async def call_igcse_feedback(req: IGCSEFeedbackRequest) -> dict[str, Any]:
    prompt = build_igcse_prompt(req)
    provider_errors: list[dict[str, str]] = []

    result = await _try_feedback_provider(
        "gemini-igcse",
        lambda: _call_gemini_igcse(prompt),
        provider_errors,
    )
    if result:
        return result

    result = await _try_feedback_provider(
        "groq-igcse",
        lambda: _call_groq_igcse(prompt),
        provider_errors,
    )
    if result:
        result["providerErrors"] = provider_errors
        return result

    return _offline_igcse_feedback(req, provider_errors)


@app.post("/api/feedback/igcse")
async def igcse_feedback(req: IGCSEFeedbackRequest) -> dict[str, Any]:
    if not req.transcript.strip():
        raise HTTPException(status_code=400, detail="transcript is empty")
    result = await call_igcse_feedback(req)
    if "total" not in result and "scores" in result:
        s = result["scores"]
        result["total"] = sum(s.get(k, 0) for k in ("coverage", "communication", "range", "accuracy"))
    return result


@app.get("/api/igcse-papers")
async def get_igcse_papers() -> list[dict]:
    return fetch_igcse_papers_from_db()

@app.get("/api/igcse-papers/{paper_id}")
async def get_igcse_paper(paper_id: str) -> dict:
    paper = fetch_igcse_paper_details(paper_id)
    if paper:
        return paper
    raise HTTPException(status_code=404, detail="IGCSE paper not found")



# ── /api/transcribe ───────────────────────────────────────────────────────────

async def _groq_whisper(tmp_path: str, language: str) -> dict[str, Any]:
    """Transcribe via Groq's hosted Whisper (fast, free-tier, no local model)."""
    groq = get_groq()
    if not groq:
        raise RuntimeError("Groq not configured")
    with open(tmp_path, "rb") as f:
        content = f.read()
    filename = os.path.basename(tmp_path)
    transcription = await groq.audio.transcriptions.create(
        file=(filename, content),
        model="whisper-large-v3-turbo",
        language=language,
        response_format="verbose_json",
    )
    text = (transcription.text or "").strip()
    segments_out = []
    if hasattr(transcription, "segments") and transcription.segments:
        for seg in transcription.segments:
            segments_out.append({
                "start": getattr(seg, "start", None),
                "end": getattr(seg, "end", None),
                "text": (getattr(seg, "text", "") or "").strip(),
            })
    return {
        "text": text,
        "language": language,
        "segments": segments_out,
        "words": [],        # Groq verbose_json doesn't include word probabilities
        "source": "groq-whisper",
    }


async def _faster_whisper(tmp_path: str, language: str) -> dict[str, Any]:
    """Transcribe via local faster-whisper with phonetic analysis."""
    model = get_whisper()
    segments_iter, info = await asyncio.to_thread(
        model.transcribe,
        tmp_path,
        language=language,
        vad_filter=True,
        word_timestamps=True,
        beam_size=5,
    )
    segs_out, words_out, parts = [], [], []
    word_count = 0
    total_prob = 0
    
    for seg in segments_iter:
        segs_out.append({
            "start": round(seg.start, 3),
            "end": round(seg.end, 3),
            "text": seg.text.strip(),
        })
        parts.append(seg.text.strip())
        if seg.words:
            for w in seg.words:
                word_count += 1
                total_prob += (w.probability or 0)
                words_out.append({
                    "word": w.word.strip(),
                    "start": round(w.start, 3),
                    "end": round(w.end, 3),
                    "probability": round(w.probability, 3) if w.probability else None,
                    # Flag common French phonetic triggers
                    "is_nasal": any(n in w.word.lower() for n in ["on", "an", "en", "in", "un"]),
                    "is_vibrant": "r" in w.word.lower(),
                    "is_silent_end": w.word.lower().endswith(("t", "d", "s", "x", "z"))
                })
    
    avg_prob = (total_prob / word_count) if word_count > 0 else 0
    wpm = round((word_count / (info.duration / 60))) if info.duration > 0 else 0

    return {
        "text": " ".join(parts).strip(),
        "language": info.language,
        "language_probability": round(info.language_probability, 3),
        "segments": segs_out,
        "words": words_out,
        "metrics": {
            "avg_probability": round(avg_prob, 3),
            "wpm": wpm,
            "duration": round(info.duration, 2),
            "clarity_score": round(avg_prob * 10, 1)
        },
        "source": "faster-whisper-phonetic",
    }


@app.post("/api/transcribe")
async def transcribe(
    audio: UploadFile = File(...),
    language: str = Form("fr"),
) -> dict[str, Any]:
    """Transcribe uploaded audio. Tries Groq Whisper first, falls back to faster-whisper."""
    suffix = os.path.splitext(audio.filename or "")[1] or ".webm"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await audio.read())
        tmp_path = tmp.name

    try:
        if GROQ_API_KEY:
            try:
                return await _groq_whisper(tmp_path, language)
            except Exception as e:
                _log_provider_failure("groq-whisper", e)
        try:
            return await _faster_whisper(tmp_path, language)
        except Exception as e:
            _log_provider_failure("faster-whisper", e)
            return {
                "text": "",
                "language": language,
                "segments": [],
                "words": [],
                "source": "transcription-unavailable",
                "error": "Transcription is temporarily unavailable.",
            }
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ── /api/questions ────────────────────────────────────────────────────────────
def _require_supabase():
    try:
        db = get_supabase()
    except Exception as exc:
        _log_provider_failure("supabase-init", exc)
        raise HTTPException(status_code=503, detail="Database initialization failed") from exc
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")
    return db


@app.get("/api/questions")
async def get_questions(
    topic_key: str | None = None,
    difficulty: int | None = None,
    is_past_paper: bool | None = None,
    limit: int = 50,
) -> list[dict]:
    db = _require_supabase()
    query = db.table("questions").select("*").eq("is_active", True).limit(limit)
    if topic_key:
        query = query.eq("topic_key", topic_key)
    if difficulty is not None:
        query = query.eq("difficulty", difficulty)
    if is_past_paper is not None:
        query = query.eq("is_past_paper", is_past_paper)
    result = await asyncio.to_thread(query.execute)
    return result.data


@app.get("/api/questions/random")
async def get_random_question(
    topic_key: str | None = None,
    exclude_ids: str = "",
) -> dict:
    db = _require_supabase()
    query = db.table("questions").select("*").eq("is_active", True)
    if topic_key:
        query = query.eq("topic_key", topic_key)
    result = await asyncio.to_thread(query.execute)
    pool = result.data
    if not pool:
        raise HTTPException(status_code=404, detail="No questions found")

    excluded = {e.strip() for e in exclude_ids.split(",") if e.strip()}
    available = [q for q in pool if q["id"] not in excluded]
    chosen = random.choice(available) if available else random.choice(pool)
    return chosen


@app.get("/api/questions/daily")
async def get_daily_question() -> dict:
    db = _require_supabase()
    today = date.today().isoformat()

    # Try to find a question assigned to today
    result = await asyncio.to_thread(
        db.table("daily_challenges").select("*").eq("active_date", today).eq("is_active", True).execute
    )
    if result.data:
        return result.data[0]

    # Deterministic fallback: pick from pool by day index
    pool_result = await asyncio.to_thread(
        db.table("daily_challenges").select("*").is_("active_date", "null").eq("is_active", True).execute
    )
    pool = pool_result.data
    if not pool:
        raise HTTPException(status_code=404, detail="No daily challenges found")
    day_index = (datetime.now(timezone.utc).toordinal()) % len(pool)
    return pool[day_index]


@app.get("/api/news/daily")
async def generate_daily_news() -> dict:
    """Generate today's news snippet using Gemini."""
    today = date.today().isoformat()
    
    # Randomly pick a topic to ensure variety
    topics = ["Sports", "Technologie", "Culture", "Météo", "Environnement", "Société"]
    chosen_topic = random.choice(topics)
    
    prompt = f"Générez un bulletin d'actualités sur le thème : {chosen_topic}. Date : {today}."
    
    try:
        # Get Gemini with News Prompt
        import google.generativeai as genai
        if not GEMINI_API_KEY:
             raise HTTPException(status_code=503, detail="Gemini not configured")
        
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel(
            "gemini-2.0-flash",
            system_instruction=NEWS_SYSTEM_PROMPT,
        )
        
        response = await asyncio.to_thread(model.generate_content, prompt)
        news = extract_json(response.text)
        
        # Ensure ID and Date are correct
        news["id"] = f"news-{today}"
        news["date"] = today
        
        return news
    except Exception as e:
        log.error("Failed to generate news: %s", e)
        # Fallback to a safe mock if AI fails
        return {
            "id": f"news-{today}",
            "date": today,
            "headline": "Bulletin d'Information Quotidien",
            "transcript": "Bienvenue à votre bulletin d'actualités. Aujourd'hui en France, nous observons une amélioration générale du climat social. Les citoyens se préparent pour les festivités nationales de la semaine prochaine.",
            "keywords": ["actualités", "climat", "social", "festivités"],
            "summaryPoints": [
                "Daily news update",
                "General improvement in social climate in France",
                "Citizens preparing for national festivities next week"
            ]
        }


# ── /api/exam-sets ─────────────────────────────────────────────────────────────
@app.get("/api/exam-sets")
async def get_exam_sets() -> list[dict]:
    db = _require_supabase()
    result = await asyncio.to_thread(
        db.table("exam_sets").select("*").eq("is_active", True).execute
    )
    return result.data


@app.get("/api/exam-sets/{set_id}")
async def get_exam_set(set_id: str) -> dict:
    db = _require_supabase()
    set_result = await asyncio.to_thread(
        db.table("exam_sets").select("*").eq("id", set_id).single().execute
    )
    if not set_result.data:
        raise HTTPException(status_code=404, detail="Exam set not found")
    exam_set = set_result.data
    question_ids = exam_set.get("question_ids", [])

    # Hydrate question objects
    questions = []
    if question_ids:
        q_result = await asyncio.to_thread(
            db.table("questions").select("*").in_("id", question_ids).execute
        )
        # Preserve the order specified in question_ids
        q_map = {q["id"]: q for q in q_result.data}
        questions = [q_map[qid] for qid in question_ids if qid in q_map]

    return {"set": exam_set, "questions": questions}


# ── /api/sessions (auth required) ─────────────────────────────────────────────
class SessionRequest(BaseModel):
    mode: str
    topic_key: str | None = None
    question_id: str | None = None
    question_text: str = ""
    transcript: str = ""
    word_count: int = 0
    score: float | None = None
    duration_sec: int = 0
    feedback_json: dict | None = None
    is_past_paper: bool = False


@app.post("/api/sessions")
async def save_session(
    req: SessionRequest,
    authorization: str | None = Header(None),
) -> dict:
    user_id = verify_jwt(authorization)
    db = _require_supabase()

    row = {
        "user_id": user_id,
        "mode": req.mode,
        "topic_key": req.topic_key,
        "question_id": req.question_id,
        "question_text": req.question_text,
        "transcript": req.transcript,
        "word_count": req.word_count,
        "score": req.score,
        "duration_sec": req.duration_sec,
        "feedback_json": req.feedback_json,
        "is_past_paper": req.is_past_paper,
    }
    result = await asyncio.to_thread(
        db.table("sessions").insert(row).execute
    )
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to save session")

    # Update profile: total_words and streak
    await asyncio.to_thread(_update_profile_stats, db, user_id, req.word_count)

    return result.data[0]


def _update_profile_stats(db: Any, user_id: str, new_words: int) -> None:
    try:
        today = date.today().isoformat()
        profile_result = db.table("profiles").select("streak,streak_last_date,total_words").eq("id", user_id).single().execute()
        profile = profile_result.data
        if not profile:
            return

        last_date = profile.get("streak_last_date")
        streak = profile.get("streak", 0)
        total_words = profile.get("total_words", 0)

        if last_date != today:
            # Check if streak continues (yesterday) or resets
            from datetime import timedelta
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            streak = (streak + 1) if last_date == yesterday else 1

        db.table("profiles").update({
            "streak": streak,
            "streak_last_date": today,
            "total_words": total_words + new_words,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", user_id).execute()
    except Exception as e:
        log.warning("Failed to update profile stats for %s: %s", user_id, e)


@app.get("/api/sessions")
async def get_sessions(
    authorization: str | None = Header(None),
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    user_id = verify_jwt(authorization)
    db = _require_supabase()
    result = await asyncio.to_thread(
        db.table("sessions")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(limit)
        .offset(offset)
        .execute
    )
    return result.data


# ── /api/profile ──────────────────────────────────────────────────────────────
@app.get("/api/profile")
async def get_profile(authorization: str | None = Header(None)) -> dict:
    user_id = verify_jwt(authorization)
    db = _require_supabase()
    result = await asyncio.to_thread(
        db.table("profiles").select("*").eq("id", user_id).single().execute
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Profile not found")
    return result.data


# ── /api/grammar-lesson ───────────────────────────────────────────────────────
GRAMMAR_LESSON_PROMPT = """You are a French grammar tutor. Given a grammar correction note, produce a short lesson.

Return ONLY valid JSON (no markdown fences) with this exact shape:
{
  "rule": "One or two clear sentences explaining the grammar rule.",
  "examples": [
    {"wrong": "Incorrect French sentence", "right": "Corrected French sentence"},
    {"wrong": "Another wrong sentence", "right": "Corrected version"}
  ],
  "practice": [
    "Short French sentence the student should try to correct (English prompt ok)",
    "Another practice prompt"
  ]
}

Grammar note: {topic}"""


@app.get("/api/grammar-lesson")
async def get_grammar_lesson(topic: str) -> dict:
    if not topic or len(topic) > 300:
        raise HTTPException(status_code=400, detail="Invalid topic")

    prompt = GRAMMAR_LESSON_PROMPT.replace("{topic}", topic)
    raw = None

    # Try Groq first (faster)
    groq = get_groq()
    if groq:
        try:
            resp = await groq.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=400,
                temperature=0.3,
            )
            raw = resp.choices[0].message.content.strip()
        except Exception as e:
            log.warning("Grammar lesson Groq failed: %s", e)

    # Fallback to Gemini 2.0 flash
    if not raw:
        gemini = get_gemini()
        if gemini:
            try:
                resp = await asyncio.to_thread(gemini.generate_content, prompt)
                raw = resp.text.strip()
            except Exception as e:
                log.warning("Grammar lesson Gemini-2.0 failed: %s", e)

    # Last resort: Gemini 1.5 flash (separate quota pool)
    if not raw and GEMINI_API_KEY:
        try:
            import google.generativeai as genai
            m15 = genai.GenerativeModel("gemini-1.5-flash")
            resp = await asyncio.to_thread(m15.generate_content, prompt)
            raw = resp.text.strip()
        except Exception as e:
            log.warning("Grammar lesson Gemini-1.5 failed: %s", e)

    if not raw:
        return {
            "rule": "AI grammar lessons are temporarily unavailable. Review the correction note and identify the verb, agreement, or word-order pattern it refers to.",
            "examples": [
                {"wrong": topic, "right": "Rewrite the sentence with the corrected grammar pattern."},
                {"wrong": "Je aller au cinema.", "right": "Je vais au cinema."},
            ],
            "practice": [
                "Write one new sentence using the corrected structure.",
                "Say the sentence aloud twice, slowly then naturally.",
            ],
            "source": "offline_fallback",
        }

    # Strip markdown fences first
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE)
    raw = raw.strip()

    # If there's extra text before/after the JSON object, extract just the object
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        raw = match.group()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Grammar lesson AI returned malformed JSON")
        return {
            "rule": "The AI response could not be parsed, but the correction is still useful practice.",
            "examples": [{"wrong": topic, "right": "Apply the corrected version from your feedback."}],
            "practice": ["Create a new sentence with the same grammar point."],
            "source": "offline_fallback",
        }


# ── /api/roleplay ─────────────────────────────────────────────────────────────
ROLEPLAY_SCENARIOS = [
    {"id": "cafe",          "title": "Au café",              "emoji": "☕", "setting": "You are a waiter in a Parisian café. The customer wants to order food and drinks.", "turns": 6},
    {"id": "hotel",         "title": "À l'hôtel",            "emoji": "🏨", "setting": "You are a hotel receptionist. A guest is checking in and has some questions.", "turns": 6},
    {"id": "gare",          "title": "À la gare",            "emoji": "🚆", "setting": "You are a ticket office assistant at a train station. A traveller needs help.", "turns": 6},
    {"id": "pharmacie",     "title": "À la pharmacie",       "emoji": "💊", "setting": "You are a pharmacist. A customer comes in feeling unwell and needs advice.", "turns": 6},
    {"id": "magasin",       "title": "Dans un magasin",      "emoji": "🛍",  "setting": "You are a shop assistant in a French clothing store. A customer wants to buy something.", "turns": 6},
    {"id": "camping",       "title": "Au camping",           "emoji": "⛺", "setting": "You are the campsite manager. A family arrives to book a pitch and ask about facilities.", "turns": 6},
    {"id": "objets_trouves","title": "Objets trouvés",       "emoji": "🎒", "setting": "You are the lost property officer. A tourist has lost their bag.", "turns": 6},
    {"id": "cinema",        "title": "Au cinéma",            "emoji": "🎬", "setting": "You are the cinema box office. A visitor wants to buy tickets and asks about films.", "turns": 6},
    {"id": "sport",         "title": "Au centre sportif",    "emoji": "🏊", "setting": "You are the receptionist at a sports centre. A visitor wants to join activities.", "turns": 6},
    {"id": "tourisme",      "title": "À l'office de tourisme","emoji": "🗺",  "setting": "You are the tourism office assistant. A visitor asks about local sights and activities.", "turns": 6},
]

ROLEPLAY_SYSTEM_PROMPT = """You are playing a French character in an IGCSE speaking roleplay scenario.
Setting: {setting}

Rules:
- Speak ONLY in French (natural, friendly, B1-level)
- Stay in character at all times
- Each reply should be 1–3 sentences — short and conversational
- Ask one follow-up question to keep the conversation going (unless it is the final turn)
- If the student makes a grammar mistake, gently model the correct French in your reply (don't explicitly correct them)
- When is_final_turn is true, wrap up the conversation naturally and add a brief summary

Return ONLY valid JSON (no markdown fences):
{
  "reply": "Your in-character French reply",
  "is_done": false,
  "hint": "Optional short English hint for the student (what they might say next), or null"
}"""

class RoleplayTurnRequest(BaseModel):
    scenario_id: str
    turn_history: list[dict]  # [{speaker: "examiner"|"student", text: str}]
    student_transcript: str
    is_final_turn: bool = False
    custom_scenario: dict | None = None


class ScenarioGenerateRequest(BaseModel):
    description: str

@app.post("/api/generate-scenario")
async def api_generate_scenario(req: ScenarioGenerateRequest) -> dict:
    from scenario_generator import generate_scenario
    try:
        scenario = await generate_scenario(req.description)
        return scenario
    except Exception as e:
        _log_provider_failure("scenario-generator", e)
        title = (req.description or "Custom scenario").strip()[:80] or "Custom scenario"
        return {
            "title": title,
            "scenario": req.description or "A custom French speaking practice scenario.",
            "npc_name": "Camille",
            "npc_personality": "Patient, friendly, and helpful.",
            "objectives": [
                "Greet the person politely",
                "Explain what you need",
                "Ask one clear question",
                "Thank the person at the end",
            ],
            "key_vocab": [
                {"fr": "bonjour", "en": "hello"},
                {"fr": "s'il vous plait", "en": "please"},
                {"fr": "merci", "en": "thank you"},
                {"fr": "je voudrais", "en": "I would like"},
            ],
            "opening_line": "Bonjour, je peux vous aider ?",
            "source": "offline_fallback",
        }

@app.get("/api/roleplay/scenarios")
async def get_roleplay_scenarios() -> list[dict]:
    return [{"id": s["id"], "title": s["title"], "emoji": s["emoji"], "turns": s["turns"]} for s in ROLEPLAY_SCENARIOS]


@app.post("/api/roleplay/turn")
async def roleplay_turn(req: RoleplayTurnRequest) -> dict:
    scenario = None
    if req.scenario_id == "custom" and req.custom_scenario:
        scenario = {
            "setting": f"Roleplay Scenario: {req.custom_scenario.get('title')}\n"
                       f"Description: {req.custom_scenario.get('scenario')}\n"
                       f"NPC Name: {req.custom_scenario.get('npc_name')}\n"
                       f"NPC Personality: {req.custom_scenario.get('npc_personality')}\n"
                       f"Objectives: {', '.join(req.custom_scenario.get('objectives', []))}"
        }
    else:
        scenario = next((s for s in ROLEPLAY_SCENARIOS if s["id"] == req.scenario_id), None)
    
    if not scenario:
        raise HTTPException(status_code=404, detail="Scenario not found")

    system = ROLEPLAY_SYSTEM_PROMPT.replace("{setting}", scenario["setting"])
    if req.is_final_turn:
        system += "\n\nThis is the final turn. After your reply, set is_done to true."

    # Build conversation messages for the model
    messages = []
    for turn in req.turn_history:
        role = "assistant" if turn["speaker"] == "examiner" else "user"
        messages.append({"role": role, "content": turn["text"]})
    messages.append({"role": "user", "content": req.student_transcript or "(silence)"})

    raw = None

    # Try Groq first (faster for chat)
    groq = get_groq()
    if groq:
        try:
            resp = await groq.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "system", "content": system}] + messages,
                max_tokens=300,
                temperature=0.7,
            )
            raw = resp.choices[0].message.content.strip()
        except Exception as e:
            log.warning("Roleplay Groq failed: %s", e)

    # Fallback to Gemini
    if not raw:
        try:
            import google.generativeai as genai
            model = genai.GenerativeModel("gemini-2.0-flash", system_instruction=system)
            full_prompt = "\n".join(f"{t['speaker'].upper()}: {t['text']}" for t in req.turn_history)
            full_prompt += f"\nSTUDENT: {req.student_transcript or '(silence)'}"
            resp = await asyncio.to_thread(model.generate_content, full_prompt)
            raw = resp.text.strip()
        except Exception as e:
            log.warning("Roleplay Gemini failed: %s", e)

    if not raw:
        return {
            "reply": "D'accord. Pouvez-vous m'en dire un peu plus, s'il vous plait ?",
            "is_done": req.is_final_turn,
            "hint": "Try giving one extra detail in French.",
            "source": "offline_fallback",
        }

    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Attempt to extract a reply from plain text fallback
        return {"reply": raw[:400], "is_done": req.is_final_turn, "hint": None}


@app.get("/api/vocab-prep")
async def vocab_prep(topic: str) -> dict[str, Any]:
    """Generate vocabulary and phrases for a given topic."""
    prompt = f"Generez du vocabulaire et des phrases pour le sujet suivant : {topic}."

    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel(
            "gemini-2.0-flash",
            system_instruction=VOCAB_SYSTEM_PROMPT,
        )
        response = await asyncio.wait_for(
            asyncio.to_thread(model.generate_content, prompt),
            timeout=AI_PROVIDER_TIMEOUT_SEC,
        )
        return extract_json(getattr(response, "text", "") or "")
    except Exception as e:
        _log_provider_failure("vocab-prep-gemini", e)
        return {
            "vocab": [
                {"fr": "important", "en": "important", "type": "adjective"},
                {"fr": "interessant", "en": "interesting", "type": "adjective"},
                {"fr": "je pense que", "en": "I think that", "type": "phrase"},
                {"fr": "parce que", "en": "because", "type": "connective"},
            ],
            "phrases": [
                {"fr": f"Je pense que {topic} est interessant.", "en": f"I think {topic} is interesting.", "type": "opinion"},
                {"fr": f"J'aime parler de {topic} parce que c'est important.", "en": f"I like talking about {topic} because it is important.", "type": "sentence"},
            ],
            "source": "offline_fallback",
        }

# ── Exam mode pipeline ────────────────────────────────────────────────────────
# New parallel pipeline — does NOT touch any existing endpoint.
from exam_controller import router as _exam_router
app.include_router(_exam_router)



@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "french-ai-backend",
        "docs": "/docs",
        "health": "/health",
    }
