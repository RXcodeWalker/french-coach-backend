"""Offline test: /api/pronunciation degrades to the whisper-heuristic tier
when AZURE_SPEECH_KEY/AZURE_SPEECH_REGION are unset. No live Azure/Groq/
network call — the router is exercised directly with fake injected functions,
following the DI seam it was built with (configure()).

Run: pytest backend/tests/test_pronunciation.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from routers.pronunciation import configure, pronunciation_evaluate
from models.pronunciation import PronunciationAssessmentResponse
from services.pronunciation.azure_client import _is_configured


def _fake_align(target_text: str, heard_text: str, whisper_words):
    return {"score": 7, "issues": []}


async def _fake_groq_whisper(tmp_path: str, language: str):
    raise RuntimeError("groq not reachable in test")


async def _fake_faster_whisper(tmp_path: str, language: str):
    return {"text": "Un bon vin blanc.", "words": [{"word": "Un", "probability": 0.9}]}


def _build_app() -> FastAPI:
    # Build the route on a fresh APIRouter rather than importing and reusing
    # routers.pronunciation's module-level `router` singleton: when the test
    # suite also imports main.py (which already ran app.include_router() on
    # that same singleton at import time), a second include_router() call on
    # the same route objects makes FastAPI mis-resolve
    # `audio: Annotated[UploadFile, File(...)]` as a query param instead of
    # a body/File param (reproduced directly against fastapi 0.115 — the
    # route's rebuilt `dependant` moves `audio` into query_params on the
    # second registration). Re-registering the same endpoint function on a
    # brand-new router avoids the double-registration entirely.
    configure(_fake_groq_whisper, _fake_faster_whisper, _fake_align, lambda: False, None)
    fresh_router = APIRouter(prefix="/api", tags=["pronunciation"])
    fresh_router.post("/pronunciation", response_model=PronunciationAssessmentResponse)(pronunciation_evaluate)
    app = FastAPI()
    app.include_router(fresh_router)
    return app


def test_pronunciation_degrades_to_whisper_heuristic_without_azure_key(monkeypatch):
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
    assert _is_configured() is None
    client = TestClient(_build_app())

    response = client.post(
        "/api/pronunciation",
        data={"target_text": "Un bon vin blanc."},
        files={"audio": ("clip.webm", b"fake-audio-bytes", "audio/webm")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "whisper-heuristic"
    assert body["transcript"] == "Un bon vin blanc."
    assert body["subScores"] is None
    # Validates against the Pydantic model field-for-field.
    PronunciationAssessmentResponse(**body)


if __name__ == "__main__":
    with pytest.MonkeyPatch.context() as mp:
        test_pronunciation_degrades_to_whisper_heuristic_without_azure_key(mp)
    print("All test_pronunciation tests passed.")
