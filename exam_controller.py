"""
Exam mode controller — full Cambridge IGCSE French (0520) speaking exam pipeline.

Endpoints (all registered under /api/exam):
  POST /api/exam/start    — Create session, serve roleplay card, begin 10-min prep
  POST /api/exam/respond  — Advance state machine with candidate response or action
  POST /api/exam/finish   — Trigger full evaluation and return scored results

Does NOT touch /api/feedback/igcse or any existing endpoint.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import random
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from exam_sessions import create_session, delete_session, get_session, update_session
from evaluator_service import evaluate_full_exam
from state_manager import advance_state, state_name

_SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
_SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
_supabase_client: Any = None


def _get_supabase() -> Any:
    global _supabase_client
    if _supabase_client is None and _SUPABASE_URL and _SUPABASE_KEY:
        from supabase import create_client
        _supabase_client = create_client(_SUPABASE_URL, _SUPABASE_KEY)
    return _supabase_client

load_dotenv()

log = logging.getLogger("french-coach.exam")

router = APIRouter(prefix="/api/exam", tags=["exam"])

# ── AI client helpers (independent from main.py to avoid circular imports) ────

_GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
_GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

_groq_client: Any = None


def _get_groq() -> Any:
    global _groq_client
    if _groq_client is None and _GROQ_API_KEY:
        from groq import AsyncGroq

        _groq_client = AsyncGroq(api_key=_GROQ_API_KEY)
    return _groq_client


# ── Topic areas (Cambridge IGCSE 0520) ───────────────────────────────────────

_TOPIC_AREAS: dict[str, str] = {
    "A": "La vie quotidienne et la famille (daily life, family, home, routine, friends)",
    "B": "L'école et l'éducation (school, studies, teachers, subjects, education system)",
    "C": "Le monde du travail (work experience, part-time jobs, careers, future plans)",
    "D": "Les loisirs et les médias (free time, sport, hobbies, music, TV, technology)",
    "E": "Le monde international (travel, holidays, environment, global issues, culture)",
}

_TOPIC_SYSTEM_PROMPT = """\
Tu es un examinateur de Cambridge IGCSE French (0520) conduisant une conversation orale.

RÈGLES ABSOLUES:
- Parle UNIQUEMENT en français — aucun mot anglais
- Ne JAMAIS corriger la grammaire du candidat
- Ne JAMAIS enseigner, expliquer, ni donner de conseils
- Utilise des accusés neutres et naturels: "D'accord", "Très bien", "Je vois", "Intéressant"
- Génère UNE seule question ouverte à la fois
- Reste dans le sujet indiqué
- Les questions de suivi doivent découler naturellement de la réponse du candidat
- Types de questions: Qu'est-ce que...? Comment...? Pourquoi...? Qu'en penses-tu? Décris-moi...

IMPORTANT: Ta réponse doit contenir UNIQUEMENT la question — aucun commentaire, aucune explication.\
"""


async def _generate_topic_question(
    area: str,
    history: list[dict[str, Any]],
) -> str:
    area_desc = _TOPIC_AREAS.get(area, area)

    if not history:
        user_content = f"Pose la première question ouverte sur le sujet: {area_desc}"
    else:
        # Use the last 4 exchanges for context (prevents prompt bloat)
        recent = history[-4:]
        exchanges = "\n".join(
            f"Examinateur: {t['question']}\nCandidat: {t.get('response') or '...'}"
            for t in recent
        )
        user_content = (
            f"Sujet: {area_desc}\n\n"
            f"Conversation jusqu'ici:\n{exchanges}\n\n"
            "Pose maintenant une question de suivi naturelle basée sur la dernière réponse."
        )

    raw: str | None = None

    groq = _get_groq()
    if groq:
        try:
            resp = await groq.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": _TOPIC_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                max_tokens=120,
                temperature=0.7,
            )
            raw = resp.choices[0].message.content.strip()
        except Exception as exc:
            log.warning("Topic question Groq failed: %s", exc)

    if not raw and _GEMINI_API_KEY:
        try:
            import google.generativeai as genai

            genai.configure(api_key=_GEMINI_API_KEY)
            model = genai.GenerativeModel(
                "gemini-2.0-flash",
                system_instruction=_TOPIC_SYSTEM_PROMPT,
            )
            resp = await asyncio.to_thread(model.generate_content, user_content)
            raw = resp.text.strip()
        except Exception as exc:
            log.warning("Topic question Gemini failed: %s", exc)

    return raw or f"Parlez-moi davantage de {area_desc.split('(')[0].strip()}."


# ── Roleplay card loading ─────────────────────────────────────────────────────

def _load_roleplay_cards() -> list[dict[str, Any]]:
    cards_path = pathlib.Path(__file__).parent / "roleplay_cards.json"
    if cards_path.exists():
        with open(cards_path, encoding="utf-8") as fh:
            return json.load(fh)
    return _builtin_roleplay_cards()


def _builtin_roleplay_cards() -> list[dict[str, Any]]:
    """Fallback cards if roleplay_cards.json is missing."""
    return [
        {
            "id": "rp_tourism_01",
            "title": "À l'office de tourisme",
            "setting": (
                "You are at a tourist office in France. "
                "The examiner plays the tourist office worker."
            ),
            "tasks": [
                {
                    "task_id": 1,
                    "prompt_fr": "Bonjour. Qu'est-ce que vous cherchez?",
                    "prompt_en": "Say hello and ask for information about the town.",
                },
                {
                    "task_id": 2,
                    "prompt_fr": "Quand voulez-vous visiter la région?",
                    "prompt_en": "Say when you want to visit and for how long.",
                },
                {
                    "task_id": 3,
                    "prompt_fr": "Vous préférez quel type d'activités?",
                    "prompt_en": "Say what type of activities you prefer and why.",
                },
                {
                    "task_id": 4,
                    "prompt_fr": "Avez-vous des questions sur les transports en commun?",
                    "prompt_en": "Ask about transport options to get around.",
                },
                {
                    "task_id": 5,
                    "prompt_fr": "Autre chose que je peux faire pour vous?",
                    "prompt_en": "Ask for a map and say thank you.",
                },
            ],
        },
        {
            "id": "rp_hotel_01",
            "title": "À l'hôtel",
            "setting": (
                "You are checking into a hotel in France. "
                "The examiner plays the hotel receptionist."
            ),
            "tasks": [
                {
                    "task_id": 1,
                    "prompt_fr": "Bonsoir. Vous avez une réservation?",
                    "prompt_en": "Say you have a reservation and give your name.",
                },
                {
                    "task_id": 2,
                    "prompt_fr": "Pour combien de nuits, et quel type de chambre?",
                    "prompt_en": "Say how many nights and what type of room you want.",
                },
                {
                    "task_id": 3,
                    "prompt_fr": "Vous préférez la vue sur mer ou sur le jardin?",
                    "prompt_en": "Express a preference and give a reason.",
                },
                {
                    "task_id": 4,
                    "prompt_fr": "Le petit-déjeuner est servi de sept à dix heures. Vous avez besoin d'autre chose?",
                    "prompt_en": "Ask about WiFi availability and parking.",
                },
                {
                    "task_id": 5,
                    "prompt_fr": "Voici votre clé. Je vous souhaite un bon séjour!",
                    "prompt_en": "Ask where the lift is and say thank you.",
                },
            ],
        },
        {
            "id": "rp_restaurant_01",
            "title": "Au restaurant",
            "setting": (
                "You are ordering a meal at a restaurant in France. "
                "The examiner plays the waiter."
            ),
            "tasks": [
                {
                    "task_id": 1,
                    "prompt_fr": "Bonsoir. Vous avez réservé?",
                    "prompt_en": "Greet the waiter and say you have a reservation for two.",
                },
                {
                    "task_id": 2,
                    "prompt_fr": "Voici la carte. Vous désirez une entrée?",
                    "prompt_en": "Order a starter and say what you would like to drink.",
                },
                {
                    "task_id": 3,
                    "prompt_fr": "Et comme plat principal?",
                    "prompt_en": "Order a main course and ask what the dish of the day is.",
                },
                {
                    "task_id": 4,
                    "prompt_fr": "Vous avez des allergies alimentaires?",
                    "prompt_en": "Mention a food allergy or intolerance.",
                },
                {
                    "task_id": 5,
                    "prompt_fr": "Vous désirez un dessert ou un café?",
                    "prompt_en": "Order a dessert and ask for the bill.",
                },
            ],
        },
        {
            "id": "rp_train_01",
            "title": "À la gare",
            "setting": (
                "You are at a train station in France. "
                "The examiner plays the ticket agent."
            ),
            "tasks": [
                {
                    "task_id": 1,
                    "prompt_fr": "Bonjour, je peux vous aider?",
                    "prompt_en": "Say where you want to go and when.",
                },
                {
                    "task_id": 2,
                    "prompt_fr": "Aller simple ou aller-retour?",
                    "prompt_en": "Say you want a return ticket and ask which class is available.",
                },
                {
                    "task_id": 3,
                    "prompt_fr": "Il y a un train toutes les heures. Vous voulez partir à quelle heure?",
                    "prompt_en": "Ask what time the next available train departs.",
                },
                {
                    "task_id": 4,
                    "prompt_fr": "Vous voulez réserver une place assise?",
                    "prompt_en": "Ask for a window seat.",
                },
                {
                    "task_id": 5,
                    "prompt_fr": "Ça fait vingt-deux euros. Comment vous payez?",
                    "prompt_en": "Say how you want to pay and ask if there are any delays.",
                },
            ],
        },
        {
            "id": "rp_camping_01",
            "title": "Au camping",
            "setting": (
                "You are arriving at a campsite in France. "
                "The examiner plays the campsite manager."
            ),
            "tasks": [
                {
                    "task_id": 1,
                    "prompt_fr": "Bonjour. Vous avez une réservation?",
                    "prompt_en": "Greet them, say you don't have a reservation and ask if there are places available.",
                },
                {
                    "task_id": 2,
                    "prompt_fr": "Oui, il nous reste des emplacements. Vous avez une tente ou un camping-car?",
                    "prompt_en": "Say what you have and how many people are in your group.",
                },
                {
                    "task_id": 3,
                    "prompt_fr": "Combien de nuits restez-vous?",
                    "prompt_en": "Say how many nights and ask about the facilities.",
                },
                {
                    "task_id": 4,
                    "prompt_fr": "La piscine est ouverte de neuf heures à dix-neuf heures.",
                    "prompt_en": "Ask about the camp shop and its opening hours.",
                },
                {
                    "task_id": 5,
                    "prompt_fr": "L'emplacement coûte quinze euros par nuit.",
                    "prompt_en": "Ask where the showers are and say thank you.",
                },
            ],
        },
    ]


# ── Request/Response Models ───────────────────────────────────────────────────


class StartRequest(BaseModel):
    candidate_id: str = ""
    card_id: str | None = None  # force a specific card (for testing)


class RespondRequest(BaseModel):
    session_id: str
    response: str = ""
    # Valid actions:
    #   "prep_complete"  — end STATE_0 prep window
    #   "repeat"         — repeat current roleplay task (once only)
    #   "end_topic1"     — finish topic conversation 1
    #   "end_topic2"     — finish topic conversation 2
    action: str = ""


class FinishRequest(BaseModel):
    session_id: str


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("/start")
async def exam_start(req: StartRequest) -> dict[str, Any]:
    """
    Create a new exam session.
    Returns the roleplay card and session_id.
    Candidate must call /respond with action='prep_complete' after the 10-min prep window.
    """
    cards = _load_roleplay_cards()

    if req.card_id:
        card = next((c for c in cards if c["id"] == req.card_id), None)
        if card is None:
            raise HTTPException(status_code=404, detail=f"Card '{req.card_id}' not found")
    else:
        card = random.choice(cards)

    session = await create_session(card, req.candidate_id)

    return {
        "session_id": session["session_id"],
        "state": 0,
        "state_name": "PREPARATION",
        "card": {
            "id": card["id"],
            "title": card["title"],
            "setting": card["setting"],
            "tasks": card["tasks"],
        },
        "prep_duration_sec": 600,
        "message": (
            "Vous avez 10 minutes pour préparer votre jeu de rôle. "
            "Lisez attentivement la carte. "
            "Les dictionnaires et les notes sont strictement interdits."
        ),
    }


@router.post("/respond")
async def exam_respond(req: RespondRequest) -> dict[str, Any]:
    """
    Advance the exam state machine with a candidate response or a control action.

    State flow:
      STATE_0 (prep)      → action='prep_complete' → STATE_1 (greeting) + STATE_2 start
      STATE_1 (greeting)  → any response           → STATE_2 (roleplay task 1)
      STATE_2 (roleplay)  → response per task      → next task, or → STATE_3 after task 5
                          → action='repeat'        → repeat current task (once only)
      STATE_3 (topic 1)   → response               → AI follow-up question
                          → action='end_topic1'    → STATE_4 (topic 2)
      STATE_4 (topic 2)   → response               → AI follow-up question
                          → action='end_topic2'    → STATE_5 (terminated)
    """
    try:
        session = await get_session(req.session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Session not found or expired")

    state = session["state"]

    # ── STATE_0: Preparation window ───────────────────────────────────────────
    if state == 0:
        if req.action != "prep_complete":
            raise HTTPException(
                status_code=400,
                detail=(
                    "Exam is in preparation phase (STATE_0). "
                    "Send action='prep_complete' when the candidate is ready."
                ),
            )
        advance_state(session)  # 0 → 1 (GREETING)
        await update_session(req.session_id, session)
        return {
            "state": 1,
            "state_name": "GREETING",
            "examiner_message": (
                "Bonjour. Je m'appelle votre examinateur aujourd'hui. "
                "Comment allez-vous? Quel est votre numéro de candidat?"
            ),
            "instruction": "Respond to the greeting. This phase is not assessed.",
        }

    # ── STATE_1: Greeting (non-assessed) ─────────────────────────────────────
    if state == 1:
        # Greeting response is NOT stored in the graded transcript
        advance_state(session)  # 1 → 2 (ROLEPLAY)
        session["current_task"] = 0
        session["repeat_used"] = False
        await update_session(req.session_id, session)

        first_task = session["roleplay_card"]["tasks"][0]
        return {
            "state": 2,
            "state_name": "ROLEPLAY",
            "examiner_message": (
                f"Très bien. Commençons le jeu de rôle. {first_task['prompt_fr']}"
            ),
            "task": {
                "task_id": first_task["task_id"],
                "task_number": 1,
                "total_tasks": 5,
                "instruction": first_task["prompt_en"],
                "is_repeat": False,
            },
        }

    # ── STATE_2: Role Play ────────────────────────────────────────────────────
    if state == 2:
        tasks: list[dict[str, Any]] = session["roleplay_card"]["tasks"]
        task_idx: int = session["current_task"]
        current_task = tasks[task_idx]

        # Repeat request — allowed once per task
        if req.action == "repeat":
            if session["repeat_used"]:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Repeat already used for this task. "
                        "Please respond to the question."
                    ),
                )
            session["repeat_used"] = True
            await update_session(req.session_id, session)
            return {
                "state": 2,
                "state_name": "ROLEPLAY",
                "examiner_message": current_task["prompt_fr"],
                "task": {
                    "task_id": current_task["task_id"],
                    "task_number": task_idx + 1,
                    "total_tasks": 5,
                    "instruction": current_task["prompt_en"],
                    "is_repeat": True,
                },
            }

        # Store candidate's response
        session["transcript"]["roleplay"].append(
            {
                "task_id": current_task["task_id"],
                "prompt": current_task["prompt_fr"],
                "response": req.response,
            }
        )

        task_idx += 1
        session["current_task"] = task_idx
        session["repeat_used"] = False

        # More tasks remain
        if task_idx < 5:
            next_task = tasks[task_idx]
            await update_session(req.session_id, session)
            return {
                "state": 2,
                "state_name": "ROLEPLAY",
                "examiner_message": next_task["prompt_fr"],
                "task": {
                    "task_id": next_task["task_id"],
                    "task_number": task_idx + 1,
                    "total_tasks": 5,
                    "instruction": next_task["prompt_en"],
                    "is_repeat": False,
                },
            }

        # All 5 tasks done → STATE_3
        advance_state(session)  # 2 → 3 (TOPIC_1)
        area = random.choice(["A", "B"])
        session["topic1_area"] = area
        question = await _generate_topic_question(area, [])
        session["current_question"] = question
        await update_session(req.session_id, session)

        return {
            "state": 3,
            "state_name": "TOPIC_1",
            "examiner_message": f"Très bien. Maintenant parlons d'un autre sujet. {question}",
            "topic_area": area,
            "topic_description": _TOPIC_AREAS[area],
            "exchanges_so_far": 0,
            "can_end_topic": False,
        }

    # ── STATE_3: Topic Conversation 1 ─────────────────────────────────────────
    if state == 3:
        if req.action == "end_topic1":
            advance_state(session)  # 3 → 4 (TOPIC_2)
            # Topic 2 must be from a different area group
            area = random.choice(["C", "D", "E"])
            session["topic2_area"] = area
            question = await _generate_topic_question(area, [])
            session["current_question"] = question
            await update_session(req.session_id, session)
            return {
                "state": 4,
                "state_name": "TOPIC_2",
                "examiner_message": (
                    f"D'accord. Passons maintenant à un nouveau sujet. {question}"
                ),
                "topic_area": area,
                "topic_description": _TOPIC_AREAS[area],
                "exchanges_so_far": 0,
                "can_end_topic": False,
            }

        # Store response and generate follow-up
        session["transcript"]["topic1"].append(
            {
                "question": session["current_question"],
                "response": req.response,
            }
        )
        question = await _generate_topic_question(
            session["topic1_area"],
            session["transcript"]["topic1"],
        )
        session["current_question"] = question
        await update_session(req.session_id, session)

        exchanges = len(session["transcript"]["topic1"])
        return {
            "state": 3,
            "state_name": "TOPIC_1",
            "examiner_message": question,
            "topic_area": session["topic1_area"],
            "exchanges_so_far": exchanges,
            # Frontend may show "End topic" button after 3 exchanges (~3 min)
            "can_end_topic": exchanges >= 3,
        }

    # ── STATE_4: Topic Conversation 2 ─────────────────────────────────────────
    if state == 4:
        if req.action == "end_topic2":
            advance_state(session)  # 4 → 5 (TERMINATED)
            await update_session(req.session_id, session)
            return {
                "state": 5,
                "state_name": "TERMINATED",
                "examiner_message": (
                    "Très bien. C'est la fin de l'examen. "
                    "Merci beaucoup. Au revoir."
                ),
                "message": "Exam complete. Call POST /api/exam/finish to receive your evaluation.",
            }

        session["transcript"]["topic2"].append(
            {
                "question": session["current_question"],
                "response": req.response,
            }
        )
        question = await _generate_topic_question(
            session["topic2_area"],
            session["transcript"]["topic2"],
        )
        session["current_question"] = question
        await update_session(req.session_id, session)

        exchanges = len(session["transcript"]["topic2"])
        return {
            "state": 4,
            "state_name": "TOPIC_2",
            "examiner_message": question,
            "topic_area": session["topic2_area"],
            "exchanges_so_far": exchanges,
            "can_end_topic": exchanges >= 3,
        }

    # ── STATE_5: Already terminated ───────────────────────────────────────────
    if state == 5:
        raise HTTPException(
            status_code=400,
            detail="Exam is already terminated. Call POST /api/exam/finish to get results.",
        )

    raise HTTPException(status_code=500, detail=f"Unknown exam state: {state}")


@router.post("/finish")
async def exam_finish(req: FinishRequest) -> dict[str, Any]:
    """
    Evaluate the complete exam transcript and return final scores.
    Can be called from STATE_4 (early finish) or STATE_5 (normal termination).
    Cleans up the session after evaluation.
    """
    try:
        session = await get_session(req.session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Session not found or expired")

    if session["state"] not in (4, 5):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Exam can only be finished from STATE_4 or STATE_5. "
                f"Current state: {session['state']} ({state_name(session['state'])})"
            ),
        )

    # Auto-advance to STATE_5 if called from STATE_4 (early finish)
    if session["state"] == 4:
        advance_state(session)
        await update_session(req.session_id, session)

    try:
        evaluation = await evaluate_full_exam(
            transcript=session["transcript"],
            card=session["roleplay_card"],
            groq_client=_get_groq(),
            gemini_api_key=_GEMINI_API_KEY,
        )
    except Exception as exc:
        log.error("Evaluation failed for session %s: %s", req.session_id, exc)
        raise HTTPException(
            status_code=503,
            detail=f"Evaluation service unavailable: {exc}",
        )

    # Preserve transcript before deletion for the response
    transcript_snapshot = dict(session["transcript"])

    await delete_session(req.session_id)

    # Persist result to Supabase (fire-and-forget — don't block the response)
    db = _get_supabase()
    if db:
        try:
            scores = evaluation.get("scores", {})
            await asyncio.to_thread(
                db.table("exam_results").insert({
                    "session_id": req.session_id,
                    "user_id": session.get("user_id"),
                    "total_score": evaluation.get("total_score"),
                    "grade_band": evaluation.get("grade_band"),
                    "role_play_score": scores.get("role_play"),
                    "topic1_score": scores.get("topic1"),
                    "topic2_score": scores.get("topic2"),
                    "feedback_json": json.dumps(evaluation),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }).execute
            )
        except Exception as exc:
            log.warning("Failed to persist exam result for session %s: %s", req.session_id, exc)

    return {
        "session_id": req.session_id,
        "evaluation": evaluation,
        "transcript": transcript_snapshot,
    }


# ── Stateless evaluate endpoint (no session required) ─────────────────────────


class EvaluateRequest(BaseModel):
    """
    Evaluate a complete exam transcript without going through the session pipeline.
    Used by the frontend after collecting RP + Topic1 + Topic2 transcripts locally.
    """
    roleplay_transcripts: list[str]          # one entry per task (5 items)
    topic1_transcripts: list[str]            # one entry per question answered
    topic2_transcripts: list[str]            # one entry per question answered
    roleplay_card: dict[str, Any]            # the paper object (id, title, tasks, etc.)
    topic1_prompts: list[str] = []           # examiner questions used for topic 1
    topic2_prompts: list[str] = []           # examiner questions used for topic 2


@router.post("/evaluate")
async def exam_evaluate(req: EvaluateRequest) -> dict[str, Any]:
    """
    Stateless full-exam evaluation.
    Accepts transcripts collected on the frontend and returns the 40-point Cambridge score.
    Does NOT require a prior /start session.
    """
    # Build the same transcript shape that evaluate_full_exam() expects
    card = req.roleplay_card
    tasks = card.get("tasks") or []
    bullet_points = card.get("bullet_points") or []

    roleplay_entries = []
    for i, resp in enumerate(req.roleplay_transcripts):
        if tasks and i < len(tasks):
            prompt = tasks[i].get("examiner_prompt", f"Task {i + 1}")
        elif i < len(bullet_points):
            prompt = bullet_points[i]
        else:
            prompt = f"Task {i + 1}"
        roleplay_entries.append({"task_id": i + 1, "prompt": prompt, "response": resp})

    def _build_qa(responses: list[str], prompts: list[str]) -> list[dict[str, Any]]:
        out = []
        for i, resp in enumerate(responses):
            q = prompts[i] if i < len(prompts) else f"Question {i + 1}"
            out.append({"question": q, "response": resp})
        return out

    transcript = {
        "roleplay": roleplay_entries,
        "topic1": _build_qa(req.topic1_transcripts, req.topic1_prompts),
        "topic2": _build_qa(req.topic2_transcripts, req.topic2_prompts),
    }

    try:
        evaluation = await evaluate_full_exam(
            transcript=transcript,
            card=card,
            groq_client=_get_groq(),
            gemini_api_key=_GEMINI_API_KEY,
        )
    except Exception as exc:
        log.error("Stateless evaluation failed: %s", exc)
        raise HTTPException(status_code=503, detail=f"Evaluation service unavailable: {exc}")

    return {"evaluation": evaluation, "transcript": transcript}


# ── Utility endpoint ──────────────────────────────────────────────────────────


@router.get("/cards")
async def list_roleplay_cards() -> list[dict[str, Any]]:
    """Return available roleplay cards (metadata only, no tasks)."""
    cards = _load_roleplay_cards()
    return [
        {"id": c["id"], "title": c["title"], "setting": c["setting"]}
        for c in cards
    ]
