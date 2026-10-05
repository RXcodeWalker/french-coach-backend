"""Azure client: quota exhaustion and honest prosody-error mapping
(exam-pronunciation plan §4, Batch 1).

- An Azure "out of quota" refusal raises AzureQuotaExceeded (not an auth
  failure, not retried) and assess_with_fallback maps it to the
  whisper-heuristic result plus `azureBudgetExhausted: True`, per request.
- `azure_allowed=False` (the project's own monthly cap is reached) never
  calls Azure.
- Azure's prosody error types (UnexpectedBreak / MissingBreak / Monotone) and
  any unknown value carry no miscue verdict: errorType None, never "correct".
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import pytest

from services.pronunciation import azure_client
from services.pronunciation.azure_client import AzureQuotaExceeded, _normalize_azure_response
from services.pronunciation.fallback import assess_with_fallback


def _patch_transport(monkeypatch, handler, calls=None):
    original = httpx.AsyncClient

    def patched(*args, **kwargs):
        def counted(request):
            if calls is not None:
                calls.append(request)
            return handler(request)
        kwargs["transport"] = httpx.MockTransport(counted)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)


@pytest.fixture
def azure_env(monkeypatch):
    monkeypatch.setenv("AZURE_SPEECH_KEY", "k")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "westeurope")


def _assess():
    return asyncio.run(azure_client.assess_pronunciation(b"wav", "Bonjour.", audio_filename="a.wav"))


@pytest.mark.parametrize(("status", "body"), [
    (403, '{"error":{"code":"403","message":"Out of call volume quota for SpeechServices F0 pricing tier. Please retry after 12 days. To increase your limits please upgrade to the S0 tier."}}'),
    (429, "Quota will be replenished in 11.23:59:00."),
    (403, "Quota exceeded."),
])
def test_quota_refusals_raise_azure_quota_exceeded(monkeypatch, azure_env, status, body):
    _patch_transport(monkeypatch, lambda r: httpx.Response(status, text=body))
    with pytest.raises(AzureQuotaExceeded):
        _assess()


def test_a_plain_403_is_still_an_auth_failure(monkeypatch, azure_env):
    _patch_transport(monkeypatch, lambda r: httpx.Response(403, text="Access denied due to invalid subscription key."))
    with pytest.raises(PermissionError):
        _assess()


def test_a_plain_429_is_still_a_retryable_rate_limit(monkeypatch, azure_env):
    _patch_transport(monkeypatch, lambda r: httpx.Response(429, text="Too many requests"))
    with pytest.raises(httpx.HTTPStatusError) as exc:
        _assess()
    assert exc.value.response.status_code == 429


def _heuristic_kwargs(**overrides):
    kwargs = dict(
        audio_bytes=b"wav",
        target_text="Un bon vin blanc.",
        heard_text="Un bon vin blanc.",
        whisper_words=[{"word": "Un", "probability": 0.9}],
        align_fn=lambda t, h, w: {"score": 7, "issues": []},
        audio_filename="a.wav",
        mode="scripted",
    )
    kwargs.update(overrides)
    return kwargs


def test_quota_exceeded_falls_back_and_flags_budget_exhausted(monkeypatch, azure_env):
    _patch_transport(monkeypatch, lambda r: httpx.Response(403, text="Out of call volume quota."))
    result = asyncio.run(assess_with_fallback(**_heuristic_kwargs()))
    assert result["provider"] == "whisper-heuristic"
    assert result["score"] == 70
    assert result["azureBudgetExhausted"] is True


def test_quota_exceeded_is_not_retried(monkeypatch, azure_env):
    calls: list = []
    _patch_transport(monkeypatch, lambda r: httpx.Response(403, text="Out of call volume quota."), calls)
    import main  # the real retry wrapper the route injects

    asyncio.run(assess_with_fallback(**_heuristic_kwargs(run_with_retries=main._run_with_retries)))
    assert len(calls) == 1


def test_budget_not_allowed_never_calls_azure(monkeypatch, azure_env):
    calls: list = []
    _patch_transport(monkeypatch, lambda r: httpx.Response(200, json={}), calls)
    result = asyncio.run(assess_with_fallback(**_heuristic_kwargs(azure_allowed=False)))
    assert calls == []
    assert result["provider"] == "whisper-heuristic"
    assert result["azureBudgetExhausted"] is True


def test_normal_results_carry_no_budget_flag(monkeypatch):
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    result = asyncio.run(assess_with_fallback(**_heuristic_kwargs()))
    assert "azureBudgetExhausted" not in result


def _response_with_word_error(error_type: str, accuracy: float = 92.0) -> dict:
    return {
        "RecognitionStatus": "Success",
        "DisplayText": "Bonjour madame.",
        "NBest": [{
            "Display": "Bonjour madame.",
            "AccuracyScore": 90.0, "FluencyScore": 90.0, "CompletenessScore": 100.0, "PronScore": 90.0,
            "Words": [
                {"Word": "bonjour", "AccuracyScore": accuracy, "ErrorType": error_type, "Offset": 0, "Duration": 5_000_000},
                {"Word": "madame", "AccuracyScore": 95.0, "ErrorType": "None", "Offset": 5_000_000, "Duration": 5_000_000},
            ],
        }],
    }


@pytest.mark.parametrize("error_type", ["UnexpectedBreak", "MissingBreak", "Monotone", "SomethingNew"])
def test_prosody_error_types_are_not_reported_as_correct(error_type):
    result = _normalize_azure_response(_response_with_word_error(error_type), "Bonjour madame.")
    first, second = result["words"]
    assert first["errorType"] is None
    assert second["errorType"] == "correct"
    # No verdict either way: a well-scored word with a break raises no issue.
    assert result["issues"] == []


def test_a_low_accuracy_word_with_a_break_still_raises_an_issue():
    result = _normalize_azure_response(_response_with_word_error("UnexpectedBreak", accuracy=30.0), "Bonjour madame.")
    assert [i["word"] for i in result["issues"]] == ["bonjour"]
