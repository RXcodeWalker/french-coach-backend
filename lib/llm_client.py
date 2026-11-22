"""Together chat client (OpenAI-compatible). Groq's own client stays in main.py."""
from __future__ import annotations

from typing import Any

from lib import model_config

_together_client: Any = None


def get_together() -> Any:
    """AsyncOpenAI pointed at Together, or None unless Together is active.

    max_retries=0 for the same reason as the Groq client: all retrying happens in
    main._run_with_retries, where it is capped and logged.
    """
    global _together_client
    if not model_config.TOGETHER_ACTIVE:
        return None
    if _together_client is None:
        from openai import AsyncOpenAI

        _together_client = AsyncOpenAI(
            api_key=model_config.TOGETHER_API_KEY,
            base_url=model_config.TOGETHER_BASE_URL,
            max_retries=0,
        )
    return _together_client
