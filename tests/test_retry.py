"""Regression test for defect #6 (accent-analyzer plan §9, §17): _is_retryable
read getattr(exc, "status_code") directly, but httpx.HTTPStatusError exposes
status on exc.response.status_code — so Azure/any-httpx-provider 429/503
responses were never actually retried. This asserts they now are.

Run: pytest backend/tests/test_retry.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import pytest


def _make_http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.invalid/")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


def test_is_retryable_unwraps_httpx_http_status_error_429():
    import main

    assert main._is_retryable(_make_http_status_error(429)) is True


def test_is_retryable_unwraps_httpx_http_status_error_503():
    import main

    assert main._is_retryable(_make_http_status_error(503)) is True


def test_is_retryable_false_for_httpx_http_status_error_400():
    import main

    assert main._is_retryable(_make_http_status_error(400)) is False


def test_is_retryable_true_for_connect_error():
    import main

    request = httpx.Request("POST", "https://example.invalid/")
    assert main._is_retryable(httpx.ConnectError("connection refused", request=request)) is True


def test_is_retryable_true_for_read_timeout():
    import main

    request = httpx.Request("POST", "https://example.invalid/")
    assert main._is_retryable(httpx.ReadTimeout("timed out", request=request)) is True


def test_run_with_retries_actually_retries_on_429(monkeypatch):
    import main

    monkeypatch.setattr(main, "_RETRY_DELAYS", (0.0, 0.0))
    attempts: list[int] = []

    async def operation():
        attempts.append(1)
        if len(attempts) < 2:
            raise _make_http_status_error(429)
        return {"ok": True}

    async def run():
        return await main._run_with_retries("test-provider", operation, attempts=2)

    result = asyncio.run(run())
    assert result == {"ok": True}
    assert len(attempts) == 2


def test_run_with_retries_does_not_retry_on_400(monkeypatch):
    import main

    monkeypatch.setattr(main, "_RETRY_DELAYS", (0.0, 0.0))
    attempts: list[int] = []

    async def operation():
        attempts.append(1)
        raise _make_http_status_error(400)

    async def run():
        await main._run_with_retries("test-provider", operation, attempts=2)

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(run())
    assert len(attempts) == 1


def _make_groq_error(status_code: int, code: str, headers: dict[str, str] | None = None):
    import groq

    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    body = {"error": {"message": "boom", "type": "invalid_request_error", "code": code}}
    response = httpx.Response(status_code, request=request, json=body, headers=headers or {})
    return groq.Groq(api_key="test")._make_status_error_from_response(response)


def test_is_retryable_true_for_groq_json_validate_failed():
    """Groq json_object mode 400s when the model samples malformed JSON; a
    resample usually succeeds (seen live on the Coached rail, 2026-10-04)."""
    import main

    assert main._is_retryable(_make_groq_error(400, "json_validate_failed")) is True


def test_is_retryable_false_for_other_groq_400():
    import main

    assert main._is_retryable(_make_groq_error(400, "invalid_request")) is False


def test_is_retryable_false_for_gemini_daily_quota():
    from google.api_core.exceptions import ResourceExhausted

    import main

    daily = ResourceExhausted("Quota exceeded quota_id: GenerateRequestsPerDayPerProjectPerModel-FreeTier")
    per_minute = ResourceExhausted("Quota exceeded quota_id: GenerateRequestsPerMinutePerProjectPerModel")
    assert main._is_retryable(daily) is False
    assert main._is_retryable(per_minute) is True


def test_retry_after_seconds_honours_short_hint_only():
    import main

    assert main._retry_after_seconds(_make_groq_error(429, "rate_limit_exceeded", {"retry-after": "7"})) == 7.0
    assert main._retry_after_seconds(_make_groq_error(429, "rate_limit_exceeded", {"retry-after": "3600"})) == 0.0
    assert main._retry_after_seconds(_make_groq_error(429, "rate_limit_exceeded")) == 0.0
    assert main._retry_after_seconds(_make_http_status_error(429)) == 0.0


def test_run_with_retries_waits_for_retry_after(monkeypatch):
    import main

    monkeypatch.setattr(main, "_RETRY_DELAYS", (0.0, 0.0))
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)
    attempts: list[int] = []

    async def operation():
        attempts.append(1)
        if len(attempts) < 2:
            raise _make_groq_error(429, "rate_limit_exceeded", {"retry-after": "7"})
        return {"ok": True}

    result = asyncio.run(main._run_with_retries("test-provider", operation, attempts=2))
    assert result == {"ok": True}
    assert slept == [7.0]


if __name__ == "__main__":
    test_is_retryable_unwraps_httpx_http_status_error_429()
    test_is_retryable_unwraps_httpx_http_status_error_503()
    test_is_retryable_false_for_httpx_http_status_error_400()
    test_is_retryable_true_for_connect_error()
    test_is_retryable_true_for_read_timeout()
    test_run_with_retries_actually_retries_on_429(pytest.MonkeyPatch())
    test_run_with_retries_does_not_retry_on_400(pytest.MonkeyPatch())
    print("All test_retry tests passed.")
