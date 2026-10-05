"""Process-wide Azure concurrency guard (exam-pronunciation plan §4, §1e).

Every Azure HTTP call passes through one asyncio.Semaphore sized by
AZURE_SPEECH_MAX_CONCURRENCY (default 1 — an F0 resource allows one
concurrent request). The chunker's fan-out reads the same value. Correct only
while the backend runs as one instance with one worker (main repo
docs/systems/topology.md).
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import pytest

from services.pronunciation import aggregator, azure_client

_OK = {
    "RecognitionStatus": "Success",
    "DisplayText": "Bonjour.",
    "NBest": [{"Display": "Bonjour.", "AccuracyScore": 90.0, "FluencyScore": 90.0,
               "CompletenessScore": 100.0, "PronScore": 90.0, "Words": []}],
}


def _patch_transport(monkeypatch, handler):
    original = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)


def _max_in_flight(monkeypatch, calls: int) -> int:
    monkeypatch.setenv("AZURE_SPEECH_KEY", "k")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "westeurope")
    state = {"now": 0, "max": 0}

    async def handler(request):
        state["now"] += 1
        state["max"] = max(state["max"], state["now"])
        await asyncio.sleep(0.05)
        state["now"] -= 1
        return httpx.Response(200, json=_OK)

    _patch_transport(monkeypatch, handler)

    async def run():
        await asyncio.gather(*(
            azure_client.assess_pronunciation(b"wav", "Bonjour.", audio_filename="a.wav", mode="scripted")
            for _ in range(calls)
        ))

    asyncio.run(run())
    return state["max"]


def test_two_concurrent_calls_are_serialised_by_default(monkeypatch):
    monkeypatch.delenv("AZURE_SPEECH_MAX_CONCURRENCY", raising=False)
    assert azure_client.azure_max_concurrency() == 1
    assert _max_in_flight(monkeypatch, 2) == 1


def test_concurrency_is_configurable(monkeypatch):
    monkeypatch.setenv("AZURE_SPEECH_MAX_CONCURRENCY", "2")
    assert _max_in_flight(monkeypatch, 4) == 2


@pytest.mark.parametrize("value", ["0", "-3", "lots"])
def test_bad_values_fall_back_to_one(monkeypatch, value):
    monkeypatch.setenv("AZURE_SPEECH_MAX_CONCURRENCY", value)
    assert azure_client.azure_max_concurrency() == 1


def test_chunk_fan_out_reads_the_same_setting(monkeypatch):
    monkeypatch.delenv("AZURE_SPEECH_MAX_CONCURRENCY", raising=False)
    state = {"now": 0, "max": 0}

    async def one_chunk(audio):
        state["now"] += 1
        state["max"] = max(state["max"], state["now"])
        await asyncio.sleep(0.02)
        state["now"] -= 1
        return None

    asyncio.run(aggregator.assess_chunked([b"a", b"b", b"c"], [(0, 25), (25, 50), (50, 60)], one_chunk))
    assert state["max"] == 1
