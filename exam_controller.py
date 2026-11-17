"""
Exam-mode helper routes (all registered under /api/exam):
  POST /api/exam/interpret         — understanding-only speech-act hint for live conduct routing
  GET  /api/exam/interpret/health  — warm-up ping
  GET  /api/exam/cards             — role-play card metadata

The legacy stateful exam pipeline (/start, /respond, /finish, /evaluate) was deleted in the
November 2026 security review (F5): unreached from the app, it put caller text into paid models
with no sign-in or quota. Exams are conducted in the browser and marked by the Node scoring
service (main repo ADR 0003).
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
from typing import Any

from dotenv import load_dotenv
from fastapi import APIRouter, Header, Request
from pydantic import BaseModel

from lib.auth import verify_supabase_jwt
from lib.guest import verify_user_or_guest
from lib.model_config import GROQ_MODEL, groq_reasoning_kwargs, groq_token_budget

load_dotenv()

log = logging.getLogger("french-coach.exam")

router = APIRouter(prefix="/api/exam", tags=["exam"])

# ── AI client helpers (independent from main.py to avoid circular imports) ────

_GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()

_groq_client: Any = None


def _get_groq() -> Any:
    global _groq_client
    if _groq_client is None and _GROQ_API_KEY:
        from groq import AsyncGroq

        _groq_client = AsyncGroq(api_key=_GROQ_API_KEY)
    return _groq_client


# ── Understanding-only utterance interpreter (facts only, no policy) ──────────
#
# Boosts live conduct-routing recall over messy STT. Returns FACTS ONLY — no
# examiner text, no policy, no entities. The frontend treats this as a throwaway
# routing hint that never reaches the scored transcript (blanking authority stays
# the deterministic classifier). Any failure → the frontend falls back to its own
# deterministic classifier, so this endpoint is strictly best-effort.

_INTERPRET_SYSTEM_PROMPT = """\
Tu es un classificateur d'actes de langage pour un examen oral de français (Cambridge IGCSE 0520).
On te donne la transcription (parfois bruitée) d'un tour de parole d'un candidat.
Ta SEULE tâche est de classifier l'acte de langage. Tu n'expliques rien, tu ne corriges rien, tu ne réponds pas au candidat.

Réponds UNIQUEMENT avec un objet JSON valide, sans texte autour, de la forme:
{"speechAct": "<label>", "hesitation": <true|false>, "confidence": <0..1>}

Labels autorisés pour speechAct (choisis-en EXACTEMENT un):
- "substantive_answer"      : une vraie réponse au fond de la question
- "affirmation"            : une simple approbation sans contenu ("oui", "d'accord", "bien sûr")
- "dont_know"              : le candidat dit qu'il ne sait pas / ne comprend pas la question
- "repeat_request"         : le candidat demande de répéter la même question ("répétez", "pardon ?")
- "clarification_request"  : le candidat demande le SENS d'un mot ("que veut dire...", "c'est quoi...")
- "off_language"           : le tour est en anglais ou dans une autre langue que le français
- "silence"                : rien d'exploitable / vide

Règles:
- "hesitation" = true si le tour contient des marques d'hésitation (euh, ben, silences remplis).
- "confidence" = ta confiance dans le label, entre 0 et 1.
- En cas de doute entre "substantive_answer" et un acte méta, préfère "substantive_answer".
- Réponds SANS aucun mot en dehors du JSON.\
"""

_INTERPRET_VALID_ACTS = {
    "substantive_answer",
    "affirmation",
    "dont_know",
    "repeat_request",
    "clarification_request",
    "off_language",
    "silence",
}


class InterpretRequest(BaseModel):
    transcript: str = ""
    part: str = ""  # 'rolePlay' | 'topic1' | 'topic2' — passed through for prompt context only


@router.get("/interpret/health")
async def exam_interpret_health() -> dict[str, Any]:
    """Warm-up ping target (mirrors the scoring service /health). Cheap, no model call."""
    return {"ok": True, "groq": bool(_get_groq())}


@router.post("/interpret")
async def exam_interpret(
    request: Request,
    req: InterpretRequest,
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    """
    Understanding-only speech-act classification for live conduct routing.
    Facts only: {speechAct, hesitation, confidence}. Never returns examiner text.
    On any model failure, returns a low-confidence 'substantive_answer' so the
    frontend's deterministic fallback takes over cleanly.

    verify_jwt only, deliberately NOT metered by consume_ai_quota_or_503 (W7
    reliability — this closes the route's real gap, unauthenticated access,
    documented below at set_rate_limiter; see verification-log.md for the
    full reasoning). Unlike /api/transcribe: a 60-token answer budget (plus
    the shared reasoning reserve on a reasoning GROQ_MODEL), temperature=0.0,
    a fixed server-side prompt, output constrained to a 7-value enum — the
    per-call cost is a rounding error, and the exam session is already
    metered where the real cost is (transcribe, score, and in coached mode
    the rail's examiner-feedback call, which charges `exam_turn_feedback` via
    main.py's _examiner_feedback_route — metered since Phase 3 Batch 0;
    before that this claim was false). Metering this too would double-count
    a side-channel and, being fail-closed, turn a Supabase hiccup into a
    dropped live-routing hint on every remaining turn. If max_tokens or the
    prompt ever loosens, revisit this exemption.
    """
    verify_user_or_guest(authorization, request, lambda a: verify_supabase_jwt(a)["sub"])
    transcript = (req.transcript or "").strip()
    if not transcript:
        return {"speechAct": "silence", "hesitation": False, "confidence": 1.0}

    groq = _get_groq()
    if not groq:
        # No model configured — hand control back to the deterministic classifier.
        return {"speechAct": "substantive_answer", "hesitation": False, "confidence": 0.0}

    part_hint = req.part if req.part in ("rolePlay", "topic1", "topic2") else "topic"
    user_content = f"Partie de l'examen: {part_hint}\nTranscription du candidat: {transcript}"

    try:
        resp = await groq.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": _INTERPRET_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            max_tokens=groq_token_budget(60),
            temperature=0.0,
            response_format={"type": "json_object"},
            **groq_reasoning_kwargs(),
        )
        raw = resp.choices[0].message.content.strip()
        data = json.loads(raw)
    except Exception as exc:
        log.warning("Interpret Groq failed: %s", exc)
        return {"speechAct": "substantive_answer", "hesitation": False, "confidence": 0.0}

    speech_act = data.get("speechAct")
    if speech_act not in _INTERPRET_VALID_ACTS:
        # Unknown label — let the frontend fall back deterministically.
        return {"speechAct": "substantive_answer", "hesitation": False, "confidence": 0.0}

    hesitation = bool(data.get("hesitation", False))
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    return {"speechAct": speech_act, "hesitation": hesitation, "confidence": confidence}


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


# ── Utility endpoint ──────────────────────────────────────────────────────────


@router.get("/cards")
async def list_roleplay_cards() -> list[dict[str, Any]]:
    """Return available roleplay cards (metadata only, no tasks)."""
    cards = _load_roleplay_cards()
    return [
        {"id": c["id"], "title": c["title"], "setting": c["setting"]}
        for c in cards
    ]


# ── Rate limiting (Phase 3, phase-3-plan-tidy-widget.md §3) ──────────────────
# /interpret gained verify_jwt (W7 reliability) — it's the only route here
# the frontend calls. It stays deliberately unmetered by AI-cost quota (see its own
# docstring) so per-IP rate limiting is still its real volume backstop, not
# just an interim one. Applied post-hoc, mirroring
# routers/pronunciation.py's set_rate_limiter: main.py's slowapi `_limiter`
# doesn't exist until main.py has started executing, and this module is
# imported by main.py, so the decorator can't be applied at definition time.
_RATE_LIMITED_ENDPOINTS = {
    "/api/exam/interpret": exam_interpret,
}


def set_rate_limiter(rate_limit_decorator, target_router) -> None:
    """Must run AFTER `app.include_router(router)`, and must mutate the route
    objects living on `target_router` (the app's router), not on this
    module's own `router` — see pronunciation.py's set_rate_limiter for why
    (rebuilding the dependant from the wrapped function, not the original,
    breaks `from __future__ import annotations` string-annotation
    resolution for parameters typed on modules slowapi's wrapper can't see)."""
    limited = {path: rate_limit_decorator("20/minute")(fn) for path, fn in _RATE_LIMITED_ENDPOINTS.items()}
    for route in target_router.routes:
        path = getattr(route, "path", None)
        if path in limited:
            route.endpoint = limited[path]
            route.dependant.call = limited[path]
