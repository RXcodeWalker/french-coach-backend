import pytest


@pytest.fixture(autouse=True)
def _guest_ai_off_by_default(monkeypatch):
    """The suite's auth tests pin the JWT-only contract; guest access (lib/guest.py)
    is opt-in per test via monkeypatch.setenv("GUEST_AI_ENABLED", "1")."""
    monkeypatch.setenv("GUEST_AI_ENABLED", "0")


@pytest.fixture(autouse=True)
def _consent_granted_by_default(monkeypatch):
    """lib/consent.py reads profiles.consent_status with the service key and
    fails closed (503) when it can't. Most tests have no Supabase, so the read
    is stubbed to a consenting account; tests/test_consent_gate.py overrides
    the same seam to exercise the gate itself."""
    import lib.consent as consent

    async def _not_required(db, user_id):
        return "13_plus_not_required"

    monkeypatch.setattr(consent, "_fetch_consent_status", _not_required)


@pytest.fixture(autouse=True)
def _no_real_ai_providers(monkeypatch):
    """No test may reach a real provider, even on a machine whose environment holds real keys (CI has
    none). A local run once spent real Groq tokens through a call path a test had not stubbed. Tests
    that exercise a provider set GROQ_API_KEY / a fake client themselves, which overrides this."""
    import sys

    main = sys.modules.get("main")
    if main is None:
        return
    for name, value in (("GROQ_API_KEY", ""), ("GEMINI_API_KEY", ""), ("_groq_client", None),
                        ("_gemini_model", None), ("_gemini_ladder_check_model", None),
                        ("_gemini_multimodal_model", None), ("_gemini_examiner_model", None)):
        if hasattr(main, name):
            monkeypatch.setattr(main, name, value)
