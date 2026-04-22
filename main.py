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
import tempfile
from datetime import date, datetime, timezone
from typing import Any

import httpx
import jwt as pyjwt
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

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

CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]

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
            "gemini-2.0-flash-lite",
            system_instruction=SYSTEM_PROMPT,
        )
    return _gemini_model

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
    allow_origins=CORS_ORIGINS or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
    return {
        "ok": True,
        "groq_configured": bool(GROQ_API_KEY),
        "gemini_configured": bool(GEMINI_API_KEY),
        "supabase_configured": bool(SUPABASE_URL and SUPABASE_KEY),
        "whisper_model": WHISPER_MODEL,
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


SYSTEM_PROMPT = """You are an experienced IGCSE French speaking examiner and coach.
You grade a student's spoken French answer and return ONLY a single JSON object
matching the schema below — no prose, no markdown fences.

Schema:
{
  "fluency": number,             // 0.0 to 10.0, one decimal place
  "grammar": string[],           // 1-5 concrete grammar observations/corrections in English, each quoting the French
  "vocabulary": [                // 1-4 vocabulary upgrade suggestions
    { "basic": string, "upgrade": string, "example": string }
  ],
  "structure": string[],         // 1-4 discourse/structure tips in English
  "pronunciationTips": string[], // 1-3 specific pronunciation corrections in English (empty array [] if no issues detected)
  "encouragement": string,       // 1-2 warm sentences in English closing the feedback
  "followUpQuestion": string,    // ONE natural French follow-up question to continue the conversation
  "igcseLevel": string           // one of: "Foundation — Developing", "Core — Secure", "Extended — Mid Band", "Extended — High Band"
}

Rules:
- Be specific; cite phrases from the student's transcript.
- If the answer is very short or empty, still return the schema — score low and give practical next steps.
- Grammar comments go in English, but quote any French with « … ».
- For pronunciationTips: if word probability data is provided, flag words with low confidence (< 0.70). Explain HOW to pronounce them correctly — mention nasal vowels, silent letters, liaisons, stress patterns as relevant.
- Do NOT wrap the JSON in code fences. Output raw JSON only.
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

    return (
        f"QUESTION (French): {req.question}\n\n"
        f"STUDENT TRANSCRIPT (French): {req.transcript}\n\n"
        f"DELIVERY METRICS: {json.dumps(m, ensure_ascii=False)}"
        f"{pron_section}\n\n"
        f"Return the JSON feedback now."
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


async def call_ai_feedback(req: FeedbackRequest) -> dict[str, Any]:
    """Try Groq (Llama 3.3 70B) first, fall back to Gemini Flash-Lite."""
    prompt = build_user_prompt(req)

    groq = get_groq()
    if groq:
        try:
            resp = await groq.chat.completions.create(
                model="llama-3.3-70b-versatile",
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.4,
                max_tokens=1024,
            )
            result = extract_json(resp.choices[0].message.content)
            result["modelUsed"] = "groq/llama-3.3-70b-versatile"
            return result
        except Exception as e:
            log.warning("Groq failed, falling back to Gemini: %s", e)

    gemini = get_gemini()
    if gemini:
        try:
            response = await asyncio.to_thread(gemini.generate_content, prompt)
            result = extract_json(response.text)
            result["modelUsed"] = "gemini/gemini-2.0-flash-lite"
            return result
        except Exception as e:
            log.warning("Gemini also failed: %s", e)

    raise HTTPException(
        status_code=503,
        detail="No AI backend available. Set GROQ_API_KEY or GEMINI_API_KEY in .env"
    )


def enrich_feedback(fb: dict[str, Any], req: FeedbackRequest) -> dict[str, Any]:
    """Ensure the response matches what coach.js / the UI expect."""
    m = req.metrics.model_dump(exclude_none=True) if req.metrics else {}
    m.pop("wordProbabilities", None)
    fb.setdefault("wordCount", len(req.transcript.split()))
    fb.setdefault("pronunciation", {
        "wordsPerMinute": m.get("wordsPerMinute"),
        "pauseCount": m.get("pauseCount"),
        "sentenceCount": m.get("sentenceCount"),
        "avgWordsPerSentence": m.get("avgWordsPerSentence"),
    })
    fb.setdefault("pronunciationTips", [])
    for k in ("hasAccents", "hasPastTense", "hasConnectives", "hasOpinion", "hasConditional"):
        if k in m:
            fb.setdefault(k, m[k])
    fb.setdefault("source", "groq")
    return fb


@app.post("/api/feedback")
async def feedback(req: FeedbackRequest) -> dict[str, Any]:
    if not req.transcript.strip():
        raise HTTPException(status_code=400, detail="transcript is empty")
    fb = await call_ai_feedback(req)
    return enrich_feedback(fb, req)


# ── /api/transcribe ───────────────────────────────────────────────────────────
@app.post("/api/transcribe")
async def transcribe(
    audio: UploadFile = File(...),
    language: str = Form("fr"),
) -> dict[str, Any]:
    """Transcribe uploaded audio (webm/ogg/wav/mp3) via faster-whisper."""
    suffix = os.path.splitext(audio.filename or "")[1] or ".webm"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await audio.read())
        tmp_path = tmp.name

    try:
        model = get_whisper()
        segments, info = model.transcribe(
            tmp_path,
            language=language,
            vad_filter=True,
            word_timestamps=True,
            beam_size=5,
        )
        segs_out = []
        words_out = []
        full_text_parts = []
        for seg in segments:
            segs_out.append({
                "start": round(seg.start, 3),
                "end": round(seg.end, 3),
                "text": seg.text.strip(),
                "avg_logprob": seg.avg_logprob,
                "no_speech_prob": seg.no_speech_prob,
            })
            full_text_parts.append(seg.text.strip())
            if seg.words:
                for w in seg.words:
                    words_out.append({
                        "start": round(w.start, 3) if w.start else None,
                        "end": round(w.end, 3) if w.end else None,
                        "word": w.word,
                        "probability": round(w.probability, 3) if w.probability else None,
                    })
        text = " ".join(full_text_parts).strip()
        return {
            "text": text,
            "language": info.language,
            "language_probability": round(info.language_probability, 3),
            "duration": round(info.duration, 3),
            "segments": segs_out,
            "words": words_out,
            "source": "faster-whisper",
        }
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ── /api/questions ────────────────────────────────────────────────────────────
def _require_supabase():
    db = get_supabase()
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
