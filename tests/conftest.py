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
