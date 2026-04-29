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
    model: str | None = None      # "groq" | "gemini" | None (auto)
    detailed: bool = False         # True = expanded feedback with more items


class IGCSEFeedbackRequest(BaseModel):
    question: str
    transcript: str
    metrics: FeedbackMetrics | None = None
    bullet_points: list[str] = []
    model: str | None = None


# ── IGCSE Themes (Cambridge 0520) ─────────────────────────────────────────────
IGCSE_THEMES = {
    1: "Everyday life",
    2: "Personal and social life",
    3: "The world around us",
    4: "The world of work",
    5: "The international world"
}

# ── IGCSE Papers (static seed — move to Supabase igcse_papers table later) ────
IGCSE_PAPERS = [
    {
        "id": "rp-cafe-2023",
        "year": 2023,
        "paper_code": "0520/11",
        "topic": "Food & Eating Out",
        "theme_number": 1,
        "theme_name": IGCSE_THEMES[1],
        "type": "role_play",
        "scenario": "You are at a café in Paris with a friend. The examiner plays the waiter/waitress.",
        "examiner_role": "café waiter/waitress",
        "bullet_points": [
            "Say what you would like to drink",
            "Ask what today's special dish is",
            "Order a meal for yourself",
            "Ask how much everything costs",
        ],
        "examiner_prompts": [
            "Bonjour ! Je peux vous aider ? Qu'est-ce que vous voudriez boire ?",
            "Très bien. Et voici le menu. Nous avons un plat du jour spécial aujourd'hui.",
            "D'accord, je note ça. Voulez-vous autre chose avec votre repas ?",
            "Entendu. Je vous apporte ça tout de suite."
        ],
        "difficulty": 3,
        "time_limit_sec": 300,
    },
    {
        "id": "rp-doctor-2022",
        "year": 2022,
        "paper_code": "0520/12",
        "topic": "Health & Body",
        "theme_number": 1,
        "theme_name": IGCSE_THEMES[1],
        "type": "role_play",
        "scenario": "You are feeling unwell and visit a French doctor. The examiner plays the doctor.",
        "examiner_role": "French doctor",
        "bullet_points": [
            "Describe your main symptom",
            "Say how long you have been ill",
            "Answer the doctor's question about your lifestyle",
            "Ask what medicine you should take",
        ],
        "examiner_prompts": [
            "Bonjour. Je vois que vous ne vous sentez pas très bien. Quel est le problème ?",
            "Ah, je comprends. Et depuis quand est-ce que vous avez ces symptômes ?",
            "D'accord. Parlez-moi un peu de vos habitudes : est-ce que vous faites du sport ou mangez équilibré ?",
            "Bien. Je vais vous faire une ordonnance. Avez-vous une question pour moi ?"
        ],
        "difficulty": 3,
        "time_limit_sec": 300,
    },
    {
        "id": "rp-train-2023",
        "year": 2023,
        "paper_code": "0520/13",
        "topic": "Travel & Transport",
        "theme_number": 5,
        "theme_name": IGCSE_THEMES[5],
        "type": "role_play",
        "scenario": "You are at a French train station and want to buy tickets. The examiner plays the ticket clerk.",
        "examiner_role": "ticket office clerk",
        "bullet_points": [
            "Say where you want to go",
            "Ask about the next available train",
            "Buy two return tickets",
            "Ask which platform the train departs from",
        ],
        "examiner_prompts": [
            "Bonjour ! Bienvenue à la gare Montparnasse. Où désirez-vous aller ?",
            "C'est une belle destination. Nous avons plusieurs trains chaque jour.",
            "D'accord. Vous voulez un aller simple ou un aller-retour ?",
            "Voilà vos billets. C'est tout ?"
        ],
        "difficulty": 3,
        "time_limit_sec": 300,
    },
    {
        "id": "topic-school-2022",
        "year": 2022,
        "paper_code": "0520/21",
        "topic": "School & Education",
        "theme_number": 4,
        "theme_name": IGCSE_THEMES[4],
        "type": "topic",
        "scenario": "Talk about your school and studies, covering all four points below.",
        "examiner_role": "examiner",
        "bullet_points": [
            "Describe your school (size, location, facilities)",
            "Talk about your favourite subject and why you enjoy it",
            "Explain what you find most challenging at school",
            "Say what you do after school or at the weekend",
        ],
        "examiner_prompts": [
            "Bonjour. Commençons par parler de votre école. Pouvez-vous la décrire ?",
            "C'est intéressant. Et quelle est votre matière préférée ? Pourquoi ?",
            "Je vois. Et qu'est-ce que vous trouvez le plus difficile dans vos études ?",
            "D'accord. Enfin, parlez-moi de vos activités après les cours ou pendant le week-end."
        ],
        "difficulty": 3,
        "time_limit_sec": 360,
    },
    {
        "id": "topic-environment-2023",
        "year": 2023,
        "paper_code": "0520/22",
        "topic": "Environment",
        "theme_number": 3,
        "theme_name": IGCSE_THEMES[3],
        "type": "topic",
        "scenario": "Talk about environmental issues and what can be done about them.",
        "examiner_role": "examiner",
        "bullet_points": [
            "Name two environmental problems that concern you most",
            "Explain what young people can do to help the environment",
            "Describe what your family does to be more eco-friendly",
            "Give your opinion on whether technology can solve environmental problems",
        ],
        "examiner_prompts": [
            "Bonjour. Parlons de l'environnement. Quels sont les deux problèmes qui vous inquiètent le plus ?",
            "C'est vrai. À votre avis, que peuvent faire les jeunes pour aider la planète ?",
            "Bien. Et chez vous, que fait votre famille pour protéger l'environnement ?",
            "D'accord. Pour finir, pensez-vous que la technologie peut résoudre nos problèmes écologiques ?"
        ],
        "difficulty": 3,
        "time_limit_sec": 360,
    },
    {
        "id": "topic-technology-2023",
        "year": 2023,
        "paper_code": "0520/23",
        "topic": "Technology & Social Media",
        "theme_number": 3,
        "theme_name": IGCSE_THEMES[3],
        "type": "topic",
        "scenario": "Talk about technology and its impact on daily life.",
        "examiner_role": "examiner",
        "bullet_points": [
            "Say how you use technology in your daily life",
            "Explain the advantages of social media for young people",
            "Discuss the disadvantages of spending too much time online",
            "Give your opinion on whether life was better before smartphones",
        ],
        "examiner_prompts": [
            "Bonjour. Aujourd'hui, nous allons discuter de la technologie. Comment l'utilisez-vous dans votre vie quotidienne ?",
            "Je vois. Selon vous, quels sont les avantages des réseaux sociaux pour les jeunes ?",
            "D'accord. Et quels sont les inconvénients de passer trop de temps sur Internet ?",
            "C'est intéressant. Pour finir, pensez-vous que la vie était meilleure avant l'invention des smartphones ?"
        ],
        "difficulty": 3,
        "time_limit_sec": 360,
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


async def _call_groq(prompt: str, detailed: bool = False) -> dict[str, Any]:
    groq = get_groq()
    if not groq:
        raise RuntimeError("Groq not configured")
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


async def _call_gemini(prompt: str) -> dict[str, Any]:
    """Text-only Gemini call (standard feedback prompt)."""
    gemini = get_gemini()
    if not gemini:
        raise RuntimeError("Gemini not configured")
    response = await asyncio.to_thread(gemini.generate_content, prompt)
    result = extract_json(response.text)
    result["modelUsed"] = "gemini/gemini-2.0-flash"
    return result


async def _call_gemini_multimodal(
    prompt: str,
    audio_path: str,
    mime_type: str = "audio/webm",
) -> dict[str, Any]:
    """
    Multimodal Gemini call: sends audio + transcript for real pronunciation analysis.
    Uses inline_data (works for files < ~20 MB, i.e. any normal spoken answer).
    Falls back gracefully if Gemini rejects the audio.
    """
    import google.generativeai as genai
    from google.generativeai import types as gtypes

    gemini = get_gemini_multimodal()
    if not gemini:
        raise RuntimeError("Gemini not configured")

    with open(audio_path, "rb") as f:
        audio_bytes = f.read()

    # google-generativeai SDK: inline audio via types.Part + types.Blob
    audio_part = gtypes.Part(
        inline_data=gtypes.Blob(mime_type=mime_type, data=audio_bytes)
    )

    try:
        response = await asyncio.to_thread(
            gemini.generate_content,
            [audio_part, prompt],
        )
    except Exception as exc:
        # Gemini may refuse audio if it thinks it contains harmful content or
        # if the mime type isn't accepted — fall back to text-only
        log.warning("Gemini multimodal rejected audio (%s), retrying text-only", exc)
        text_model = get_gemini()
        if not text_model:
            raise
        response = await asyncio.to_thread(text_model.generate_content, prompt)

    result = extract_json(response.text)
    result["modelUsed"] = "gemini/gemini-2.0-flash-multimodal"
    return result


async def call_ai_feedback(
    req: FeedbackRequest,
    audio_path: str | None = None,
    audio_mime: str = "audio/webm",
) -> dict[str, Any]:
    prompt = build_user_prompt(req)
    requested = (req.model or "").lower()
    detailed = req.detailed
    has_audio = bool(audio_path)

    # Gemini with audio → multimodal analysis (pronunciation-aware)
    if requested == "gemini" and has_audio:
        try:
            return await _call_gemini_multimodal(prompt, audio_path, mime_type=audio_mime)
        except Exception as e:
            log.warning("Gemini multimodal failed, falling back to text-only: %s", e)
            return await _call_gemini(prompt)

    if requested == "gemini":
        return await _call_gemini(prompt)

    if requested == "groq":
        return await _call_groq(prompt, detailed)

    # Auto: try Groq first, then Gemini (multimodal if audio available)
    try:
        return await _call_groq(prompt, detailed)
    except Exception as e:
        log.warning("Groq failed, falling back to Gemini: %s", e)

    try:
        if has_audio:
            return await _call_gemini_multimodal(prompt, audio_path, mime_type=audio_mime)
        return await _call_gemini(prompt)
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


@app.post("/api/feedback")
async def feedback(
    question: str = Form(...),
    transcript: str = Form(""),       # pre-computed transcript (used when no audio)
    model: str = Form("gemini"),
    detailed: str = Form("false"),
    metrics_json: str = Form("{}"),   # JSON-encoded FeedbackMetrics from frontend
    audio: UploadFile | None = File(None),
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
            detailed=(detailed.lower() == "true"),
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
        log.error("Repair failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Repair analysis failed: {e}")
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
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=503, detail="Gemini not configured")

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
        response = await asyncio.to_thread(gemini.generate_content, prompt)
        result = extract_json(response.text)
        result["word"] = word
        return result
    except Exception as e:
        log.error("Drill generation failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Drill generation failed: {e}")


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


async def _call_gemini_igcse(prompt: str) -> dict[str, Any]:
    gemini = get_gemini_igcse()
    if not gemini:
        raise RuntimeError("Gemini not configured")
    response = await asyncio.to_thread(gemini.generate_content, prompt)
    result = extract_json(response.text)
    result["modelUsed"] = "gemini/gemini-2.0-flash"
    return result


async def call_igcse_feedback(req: IGCSEFeedbackRequest) -> dict[str, Any]:
    prompt = build_igcse_prompt(req)
    requested = (req.model or "").lower()

    if requested == "gemini":
        return await _call_gemini_igcse(prompt)

    if requested == "groq":
        return await _call_groq_igcse(prompt)

    try:
        return await _call_groq_igcse(prompt)
    except Exception as e:
        log.warning("Groq IGCSE failed, falling back to Gemini: %s", e)
    try:
        return await _call_gemini_igcse(prompt)
    except Exception as e:
        log.warning("Gemini IGCSE also failed: %s", e)

    raise HTTPException(status_code=503, detail="No AI backend available")


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
    return IGCSE_PAPERS


@app.get("/api/igcse-papers/{paper_id}")
async def get_igcse_paper(paper_id: str) -> dict:
    for paper in IGCSE_PAPERS:
        if paper["id"] == paper_id:
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
                log.warning("Groq Whisper failed, falling back to faster-whisper: %s", e)
        return await _faster_whisper(tmp_path, language)
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
        raise HTTPException(status_code=503, detail="AI not available — check API keys and quota")

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
        raise HTTPException(status_code=502, detail="Could not parse AI response")


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


@app.get("/api/roleplay/scenarios")
async def get_roleplay_scenarios() -> list[dict]:
    return [{"id": s["id"], "title": s["title"], "emoji": s["emoji"], "turns": s["turns"]} for s in ROLEPLAY_SCENARIOS]


@app.post("/api/roleplay/turn")
async def roleplay_turn(req: RoleplayTurnRequest) -> dict:
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
        raise HTTPException(status_code=503, detail="AI not available")

    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Attempt to extract a reply from plain text fallback
        return {"reply": raw[:400], "is_done": req.is_final_turn, "hint": None}
