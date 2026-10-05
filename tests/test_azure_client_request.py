"""Offline tests for the OUTGOING Azure request shape — Content-Type per
audio format (defect #1) and EnableMiscue per mode (defect #2). Uses
httpx.MockTransport (stdlib-adjacent, already a dependency via httpx; no new
mocking library added) to intercept the request without a network call.

Run: pytest backend/tests/test_azure_client_request.py
"""

from __future__ import annotations

import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import pytest

from services.pronunciation.azure_client import _content_type_for, assess_pronunciation


def test_content_type_for_wav():
    assert _content_type_for("clip.wav") == "audio/wav; codecs=audio/pcm; samplerate=16000"


def test_content_type_for_ogg():
    assert _content_type_for("clip.ogg") == "audio/ogg; codecs=opus"


def test_content_type_for_webm_has_no_accepted_mapping():
    # Real browser MediaRecorder output. Falls through to the historical
    # (still-wrong-for-Azure) default until the client-side normalizer
    # guarantees WAV on every request — see azure_client._content_type_for
    # docstring. This test documents the current, known-incomplete state.
    assert _content_type_for("clip.webm") == "audio/webm; codecs=opus"


def _capture_request_transport(captured: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        captured["content"] = request.content
        return httpx.Response(
            200,
            json={
                "RecognitionStatus": "Success",
                "DisplayText": "Un bon vin blanc.",
                "NBest": [
                    {
                        "Display": "Un bon vin blanc.",
                        "AccuracyScore": 82.0,
                        "FluencyScore": 90.0,
                        "CompletenessScore": 100.0,
                        "PronScore": 85.0,
                        "Words": [],
                    }
                ],
            },
        )

    return httpx.MockTransport(handler)


@pytest.mark.parametrize(
    ("mode", "expected_enable_miscue"),
    [("scripted", True), ("freeform", False)],
)
def test_enable_miscue_set_per_mode(monkeypatch, mode, expected_enable_miscue):
    monkeypatch.setenv("AZURE_SPEECH_KEY", "fake-key")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "fake-region")

    captured: dict = {}
    original_async_client = httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        kwargs["transport"] = _capture_request_transport(captured)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched_async_client)

    import asyncio

    asyncio.run(
        assess_pronunciation(
            b"fake-wav-bytes", "Un bon vin blanc.",
            audio_filename="clip.wav", mode=mode,
        )
    )

    header_value = captured["headers"]["Pronunciation-Assessment"]
    params = json.loads(base64.b64decode(header_value))
    assert params["EnableMiscue"] is expected_enable_miscue


def test_content_type_header_matches_uploaded_extension(monkeypatch):
    monkeypatch.setenv("AZURE_SPEECH_KEY", "fake-key")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "fake-region")

    captured: dict = {}
    original_async_client = httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        kwargs["transport"] = _capture_request_transport(captured)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched_async_client)

    import asyncio

    asyncio.run(
        assess_pronunciation(
            b"fake-wav-bytes", "Un bon vin blanc.",
            audio_filename="clip.wav", mode="scripted",
        )
    )

    assert captured["headers"]["Content-Type"] == "audio/wav; codecs=audio/pcm; samplerate=16000"



def test_request_never_asks_azure_to_store_audio(monkeypatch):
    """Exam-pronunciation plan §5: real-time STT audio is not retained by
    Azure unless logging is switched on — `storeAudio=true` on this URL, a
    custom endpoint, or the SDK option. The request must stay on the base
    model's REST endpoint with only `language` and `format`."""
    monkeypatch.setenv("AZURE_SPEECH_KEY", "fake-key")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "westeurope")

    captured: dict = {}
    original_async_client = httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        transport = _capture_request_transport(captured)

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = request.url
            return transport.handler(request)

        kwargs["transport"] = httpx.MockTransport(handler)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched_async_client)

    import asyncio

    asyncio.run(
        assess_pronunciation(
            b"fake-wav-bytes", "Un bon vin blanc.",
            audio_filename="clip.wav", mode="freeform",
        )
    )

    url = captured["url"]
    assert url.host == "westeurope.stt.speech.microsoft.com"
    assert url.path == "/speech/recognition/conversation/cognitiveservices/v1"
    assert dict(url.params) == {"language": "fr-FR", "format": "detailed"}
    assert "storeaudio" not in str(url).lower()
    assert "cid" not in {k.lower() for k in url.params}  # no custom endpoint id

if __name__ == "__main__":
    import asyncio

    test_content_type_for_wav()
    test_content_type_for_ogg()
    test_content_type_for_webm_has_no_accepted_mapping()
    print("All test_azure_client_request tests passed (run via pytest for the parametrized/monkeypatch cases).")
