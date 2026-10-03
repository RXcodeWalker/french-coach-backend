"""Slice 7a (Phase 1 "stop actively miseducating"): enrich_feedback() used to
setdefault scores to {"comm": 5.0, "know": 5.0, "acc": 5.0} whenever a live
provider's response was missing scores — fabricating a mark for a response
that was never actually graded. That default is now replaced with an
explicit providerStatus: "malformed_response" marker (reusing the existing
"offline_fallback" marker instead, if the response is already known to be
one), which the frontend keys off unconditionally in mapBackendFeedback
(never inferred from scores/fluency being present or absent).

Run: pytest backend/tests/test_enrich_feedback.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.pop("AZURE_SPEECH_KEY", None)
os.environ.pop("AZURE_SPEECH_REGION", None)


def test_enrich_feedback_no_longer_fabricates_555_when_scores_missing_from_a_live_response():
    import main

    req = main.FeedbackRequest(question="Q", transcript="Une reponse quelconque avec assez de mots.")
    fb: dict = {"providerStatus": "primary"}  # live provider, no "scores" key

    result = main.enrich_feedback(fb, req)

    assert result.get("scores") != {"comm": 5.0, "know": 5.0, "acc": 5.0}
    assert "scores" not in result
    assert result["providerStatus"] == "malformed_response"


def test_enrich_feedback_preserves_offline_fallback_marker_instead_of_overwriting_it():
    import main

    req = main.FeedbackRequest(question="Q", transcript="Une reponse quelconque avec assez de mots.")
    fb: dict = {"providerStatus": "offline_fallback"}  # already known offline, no "scores" key

    result = main.enrich_feedback(fb, req)

    assert result["providerStatus"] == "offline_fallback"


def test_enrich_feedback_leaves_a_real_scores_object_untouched():
    import main

    req = main.FeedbackRequest(question="Q", transcript="Une reponse quelconque avec assez de mots.")
    real_scores = {"comm": 7.5, "know": 6.0, "acc": 8.0}
    fb: dict = {"providerStatus": "primary", "scores": dict(real_scores)}

    result = main.enrich_feedback(fb, req)

    assert result["scores"] == real_scores
    assert result["providerStatus"] == "primary"


def test_legacy_igcse_feedback_route_is_gone():
    """Phase 3 Batch C: /api/feedback/igcse (the legacy third scorer's HTTP
    surface, 0 requests in 30 days of Render logs, no code callers) was
    deleted. A deleted route returns 404, never wrong marks."""
    import main

    paths = {getattr(r, "path", None) for r in main.app.routes}
    assert "/api/feedback/igcse" not in paths
    assert not hasattr(main, "IGCSEFeedbackRequest")
    assert not hasattr(main, "_offline_igcse_feedback")

    from fastapi.testclient import TestClient

    resp = TestClient(main.app).post("/api/feedback/igcse", json={"question": "Q", "transcript": "T"})
    assert resp.status_code in (404, 405)


def test_call_ai_feedback_raises_when_all_providers_exhausted(monkeypatch):
    """Stage 4 item 8: call_ai_feedback must raise (HTTPException 502), never
    fabricate a plausible-looking response, when every provider fails. No live
    network call is made — both provider callables are monkeypatched, matching
    test_engine_preference.py's pattern."""
    import asyncio
    import main

    async def fake_gemini(prompt, *a, **kw):
        raise RuntimeError("gemini down")

    async def fake_groq(prompt, depth="standard"):
        raise RuntimeError("groq down")

    monkeypatch.setattr(main, "_call_gemini", fake_gemini)
    monkeypatch.setattr(main, "_call_gemini_multimodal", fake_gemini)
    monkeypatch.setattr(main, "_call_groq", fake_groq)

    req = main.FeedbackRequest(question="Q", transcript="Une reponse quelconque avec assez de mots.")

    async def _run():
        try:
            await main.call_ai_feedback(req)
            return None
        except main.HTTPException as exc:
            return exc

    exc = asyncio.run(_run())
    assert exc is not None, "call_ai_feedback must raise, not fabricate a response, when both providers fail"
    assert exc.status_code == 502


if __name__ == "__main__":
    test_enrich_feedback_no_longer_fabricates_555_when_scores_missing_from_a_live_response()
    test_enrich_feedback_preserves_offline_fallback_marker_instead_of_overwriting_it()
    test_enrich_feedback_leaves_a_real_scores_object_untouched()
    test_legacy_igcse_feedback_route_is_gone()
    print("All test_enrich_feedback tests passed (run via pytest for the monkeypatch-based exhaustion case).")
