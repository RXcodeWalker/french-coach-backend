"""Admin = role AND allow-listed email (lib.auth.is_admin_payload / require_admin)."""

import pytest
from fastapi import HTTPException

from lib.auth import admin_emails, is_admin_payload, require_admin, verify_supabase_jwt  # noqa: F401

OWNER = "omjhamvar29@gmail.com"


def _p(role="admin", email=OWNER):
    p = {"sub": "u1", "app_metadata": {"role": role} if role else {}}
    if email is not None:
        p["email"] = email
    return p


def test_owner_with_role_is_admin(monkeypatch):
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    assert is_admin_payload(_p())
    assert is_admin_payload(_p(email=OWNER.upper()))


def test_other_email_with_admin_role_is_not_admin(monkeypatch):
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    assert not is_admin_payload(_p(email="someone@example.com"))


def test_owner_without_role_is_not_admin(monkeypatch):
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    assert not is_admin_payload(_p(role=None))


def test_missing_email_claim_is_not_admin(monkeypatch):
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    assert not is_admin_payload(_p(email=None))


def test_env_override_replaces_default(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "a@x.com, B@x.com")
    assert admin_emails() == {"a@x.com", "b@x.com"}
    assert not is_admin_payload(_p())
    assert is_admin_payload(_p(email="b@x.com"))


@pytest.mark.asyncio
async def test_require_admin_403_for_wrong_email(monkeypatch):
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    import lib.auth as a
    monkeypatch.setattr(a, "verify_supabase_jwt", lambda _h: _p(email="x@y.com"))
    with pytest.raises(HTTPException) as e:
        await require_admin("Bearer t")
    assert e.value.status_code == 403
