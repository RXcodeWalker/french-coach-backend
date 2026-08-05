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


if __name__ == "__main__":
    test_is_retryable_unwraps_httpx_http_status_error_429()
    test_is_retryable_unwraps_httpx_http_status_error_503()
    test_is_retryable_false_for_httpx_http_status_error_400()
    test_is_retryable_true_for_connect_error()
    test_is_retryable_true_for_read_timeout()
    test_run_with_retries_actually_retries_on_429(pytest.MonkeyPatch())
    test_run_with_retries_does_not_retry_on_400(pytest.MonkeyPatch())
    print("All test_retry tests passed.")
