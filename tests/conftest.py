import pytest


@pytest.fixture(autouse=True)
def _guest_ai_off_by_default(monkeypatch):
    """The suite's auth tests pin the JWT-only contract; guest access (lib/guest.py)
    is opt-in per test via monkeypatch.setenv("GUEST_AI_ENABLED", "1")."""
    monkeypatch.setenv("GUEST_AI_ENABLED", "0")
