"""Provider model IDs and reasoning settings, shared by every module that calls Groq/Gemini.

One definition on purpose: exam_controller.py and scenario_generator.py used to
carry their own fallback defaults (llama-3.3-70b-versatile / gemini-2.0-flash).
When Groq retired the Llama line, main.py's default was updated but those two
weren't, so /api/exam/interpret, the legacy topic examiner and scenario
generation 404'd (`model_not_found`) on every call while main.py's paths worked.
Env vars still win; only the fallback lives here. See main.py's config block
for why these particular defaults.
"""
from __future__ import annotations

import os
from typing import Any

GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash").strip()
# "" when GROQ_MODEL is a non-reasoning model, which would reject the parameter.
GROQ_REASONING_EFFORT = os.getenv("GROQ_REASONING_EFFORT", "low").strip()
GROQ_REASONING_TOKEN_RESERVE = int(os.getenv("GROQ_REASONING_TOKEN_RESERVE", "512"))


def groq_reasoning_kwargs() -> dict[str, Any]:
    """Extra chat-completion params for a reasoning GROQ_MODEL; empty otherwise."""
    return {"reasoning_effort": GROQ_REASONING_EFFORT} if GROQ_REASONING_EFFORT else {}


def groq_token_budget(answer_tokens: int) -> int:
    """Grow an answer-token budget so the reasoning phase cannot consume it."""
    return answer_tokens + (GROQ_REASONING_TOKEN_RESERVE if GROQ_REASONING_EFFORT else 0)
