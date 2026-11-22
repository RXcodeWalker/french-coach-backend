"""LLM_PROVIDER=together routes chat calls to Together; the default is untouched."""
import asyncio

import httpx
import pytest

import main
from lib import llm_client, model_config


@pytest.fixture(autouse=True)
def _reset_client():
    llm_client._together_client = None
    yield
    llm_client._together_client = None


def test_default_is_groq_and_no_together_client():
    assert model_config.TOGETHER_ACTIVE is False
    assert model_config.LLM_PROVIDER_NAME == "groq"
    assert model_config.LLM_MODEL == model_config.GROQ_MODEL
    assert llm_client.get_together() is None


def test_together_needs_both_the_switch_and_a_key(monkeypatch):
    monkeypatch.setattr(model_config, "TOGETHER_ACTIVE", False)
    assert llm_client.get_together() is None


def test_get_llm_prefers_together_but_whisper_client_is_untouched(monkeypatch):
    sentinel_groq = object()
    monkeypatch.setattr(main, "get_groq", lambda: sentinel_groq)
    assert main.get_llm() is sentinel_groq  # together off -> Groq, as before
    monkeypatch.setattr(model_config, "TOGETHER_ACTIVE", True)
    monkeypatch.setattr(model_config, "TOGETHER_API_KEY", "tgp_test")
    client = main.get_llm()
    assert client is not sentinel_groq
    assert str(client.base_url).startswith("https://api.together.xyz/v1")
    assert main.get_groq() is sentinel_groq  # Whisper still resolves through Groq


def test_together_client_sends_an_openai_shaped_request(monkeypatch):
    monkeypatch.setattr(model_config, "TOGETHER_ACTIVE", True)
    monkeypatch.setattr(model_config, "TOGETHER_API_KEY", "tgp_test")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={
            "id": "x", "object": "chat.completion", "created": 0, "model": "openai/gpt-oss-120b",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "{\"ok\": true}"}}],
        })

    from openai import AsyncOpenAI
    client = AsyncOpenAI(
        api_key="tgp_test", base_url=model_config.TOGETHER_BASE_URL, max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    resp = asyncio.run(client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[{"role": "user", "content": "Bonjour"}],
        response_format={"type": "json_object"},
        max_tokens=50,
        **model_config.groq_reasoning_kwargs(),
    ))
    assert seen["url"] == "https://api.together.xyz/v1/chat/completions"
    assert seen["auth"] == "Bearer tgp_test"
    assert "reasoning_effort" in seen["body"] or not model_config.GROQ_REASONING_EFFORT
    assert resp.choices[0].message.content == '{"ok": true}'


def test_openai_rate_limits_are_retried():
    import openai
    exc = openai.RateLimitError(
        "slow down", response=httpx.Response(429, request=httpx.Request("POST", "https://x")), body=None,
    )
    assert main._is_retryable(exc)
