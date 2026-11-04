"""routers/billing_razorpay.py — Premium subscriptions through Razorpay.

No network: Razorpay is a fake gateway and Supabase is the in-memory fake from
test_billing. The webhook signature is a real HMAC over the raw body.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import main
from routers import billing
from routers import billing_razorpay as rzp
from test_billing import AUTH, OTHER, PERIOD_END, USER, FakeDb, FakeGateway, _sub

WEBHOOK_SECRET = "rzp_webhook_secret"
OTHER_AUTH = {"Authorization": "Bearer other"}


def _rsub(sub_id="sub_RzpA1b2C3d", status="active", plan_id="plan_month", user_id=USER, current_end=PERIOD_END):
    return {
        "id": sub_id,
        "entity": "subscription",
        "plan_id": plan_id,
        "status": status,
        "current_start": current_end - 30 * 86400 if current_end else None,
        "current_end": current_end,
        "ended_at": None,
        "created_at": int(time.time()),
        "notes": {"user_id": user_id},
        "short_url": "https://rzp.io/i/x",
    }


class FakeRazorpay(rzp.RazorpayGateway):
    def __init__(self):  # noqa: D401 — no HTTP client
        self.calls: list[tuple] = []
        self.subs: dict[str, dict] = {}
        self.counter = 0

    def create_subscription(self, params):
        self.calls.append(("create", params))
        self.counter += 1
        sub_id = f"sub_New{self.counter:07d}"
        self.subs[sub_id] = _rsub(sub_id, status="created", plan_id=params["plan_id"],
                                  user_id=params["notes"]["user_id"], current_end=None)
        return self.subs[sub_id]

    def retrieve_subscription(self, subscription_id):
        self.calls.append(("retrieve", subscription_id))
        return self.subs[subscription_id]

    def cancel_subscription(self, subscription_id, *, at_cycle_end):
        self.calls.append(("cancel", subscription_id, at_cycle_end))
        if not at_cycle_end:
            self.subs[subscription_id] = {**self.subs[subscription_id], "status": "cancelled", "ended_at": int(time.time())}
        return self.subs[subscription_id]

    def retrieve_plan(self, plan_id):
        self.calls.append(("plan", plan_id))
        monthly = plan_id == "plan_month"
        return {"id": plan_id, "period": "monthly" if monthly else "yearly", "interval": 1,
                "item": {"amount": 19900 if monthly else 199900, "currency": "INR"}}


@pytest.fixture()
def env(monkeypatch):
    for name in ("STRIPE_SECRET_KEY", "STRIPE_PRICE_MONTHLY", "STRIPE_PRICE_YEARLY", "STRIPE_WEBHOOK_SECRET",
                 "RAZORPAY_TOTAL_COUNT_MONTHLY", "RAZORPAY_TOTAL_COUNT_YEARLY", "BILLING_APP_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_key")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "rzp_test_secret")
    monkeypatch.setenv("RAZORPAY_PLAN_MONTHLY", "plan_month")
    monkeypatch.setenv("RAZORPAY_PLAN_YEARLY", "plan_year")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    db, gw, stripe_gw = FakeDb(), FakeRazorpay(), FakeGateway()
    monkeypatch.setattr(billing, "_get_db", lambda: db)
    monkeypatch.setattr(main, "get_supabase", lambda: db)
    monkeypatch.setattr(rzp, "_gateway_factory", lambda: gw)
    monkeypatch.setattr(billing, "_gateway_factory", lambda: stripe_gw)
    rzp._price_cache.clear()
    billing._price_cache.clear()
    monkeypatch.setattr(main, "verify_jwt", lambda authorization: USER)
    monkeypatch.setattr(
        billing, "verify_supabase_jwt",
        lambda authorization: {"sub": OTHER if "other" in authorization else USER, "email": "learner@example.test"},
    )
    if main._limiter is not None:
        monkeypatch.setattr(main._limiter, "enabled", False)
    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        yield client, db, gw


def _row(status="active", sub_id="sub_RzpA1b2C3d", user_id=USER, cancel=False, **kw):
    return rzp.subscription_row(_rsub(sub_id, status=status, user_id=user_id, **kw), user_id, cancel_at_period_end=cancel)


def _webhook(event: str, sub: dict, secret: str = WEBHOOK_SECRET, event_id: str | None = "evt_rzp_1"):
    body = json.dumps({"entity": "event", "event": event, "contains": ["subscription"],
                       "payload": {"subscription": {"entity": sub}}}).encode()
    headers = {"X-Razorpay-Signature": hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()}
    if event_id:
        headers["X-Razorpay-Event-Id"] = event_id
    return body, headers


# ── Off until configured ─────────────────────────────────────────────────────

def test_every_razorpay_route_is_503_until_configured(env, monkeypatch):
    client, _, _ = env
    monkeypatch.delenv("RAZORPAY_KEY_SECRET")
    assert client.post("/api/billing/razorpay/subscribe", json={"plan": "monthly"}, headers=AUTH).status_code == 503
    assert client.post("/api/billing/razorpay/sync", json={"subscriptionId": "sub_RzpA1b2C3d"}, headers=AUTH).status_code == 503
    assert client.post("/api/billing/razorpay/cancel", headers=AUTH).status_code == 503
    body, headers = _webhook("subscription.activated", _rsub())
    assert client.post("/api/billing/razorpay/webhook", content=body, headers=headers).status_code == 503
    status = client.get("/api/billing/status").json()
    assert status["configured"] is False and status["providers"] == []


# ── Subscribe ────────────────────────────────────────────────────────────────

def test_subscribe_needs_an_account(env):
    client, _, _ = env
    assert client.post("/api/billing/razorpay/subscribe", json={"plan": "monthly"}).status_code == 401


def test_subscribe_creates_a_subscription_linked_to_the_account(env):
    client, db, gw = env
    res = client.post("/api/billing/razorpay/subscribe", json={"plan": "yearly"}, headers=AUTH)
    assert res.status_code == 200
    data = res.json()
    assert data["keyId"] == "rzp_test_key" and data["subscriptionId"].startswith("sub_")
    assert "rzp_test_secret" not in res.text
    params = [c for c in gw.calls if c[0] == "create"][0][1]
    assert params["plan_id"] == "plan_year" and params["total_count"] == 10 and params["notes"] == {"user_id": USER}
    row = db.tables[rzp.TABLE][0]
    assert row["user_id"] == USER and row["status"] == "created" and row["plan"] == "yearly"


def test_a_second_subscribe_reuses_the_unpaid_subscription(env):
    client, _, gw = env
    first = client.post("/api/billing/razorpay/subscribe", json={"plan": "monthly"}, headers=AUTH).json()
    again = client.post("/api/billing/razorpay/subscribe", json={"plan": "monthly"}, headers=AUTH).json()
    other_plan = client.post("/api/billing/razorpay/subscribe", json={"plan": "yearly"}, headers=AUTH).json()
    assert first["subscriptionId"] == again["subscriptionId"] != other_plan["subscriptionId"]
    assert len([c for c in gw.calls if c[0] == "create"]) == 2


def test_an_old_unpaid_subscription_is_not_reused(env):
    client, db, gw = env
    old = _row(status="created", sub_id="sub_OldOld1", current_end=None)
    old["created_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    old["plan_id"] = "plan_month"
    db.tables[rzp.TABLE] = [old]
    res = client.post("/api/billing/razorpay/subscribe", json={"plan": "monthly"}, headers=AUTH).json()
    assert res["subscriptionId"] != "sub_OldOld1"


def test_total_count_is_configurable(env, monkeypatch):
    client, _, gw = env
    monkeypatch.setenv("RAZORPAY_TOTAL_COUNT_MONTHLY", "24")
    client.post("/api/billing/razorpay/subscribe", json={"plan": "monthly"}, headers=AUTH)
    assert [c for c in gw.calls if c[0] == "create"][0][1]["total_count"] == 24


def test_a_plan_without_an_id_is_refused(env, monkeypatch):
    client, _, _ = env
    monkeypatch.delenv("RAZORPAY_PLAN_YEARLY")
    assert client.post("/api/billing/razorpay/subscribe", json={"plan": "yearly"}, headers=AUTH).status_code == 422


def test_a_premium_account_cannot_subscribe_again_with_either_provider(env):
    client, db, gw = env
    db.tables[rzp.TABLE] = [_row()]
    res = client.post("/api/billing/razorpay/subscribe", json={"plan": "monthly"}, headers=AUTH)
    assert res.status_code == 409 and res.json()["detail"]["error"] == "already_premium"
    # A Stripe subscriber is refused too.
    db.tables[rzp.TABLE] = []
    db.tables["billing_subscriptions"] = [billing.subscription_row(_sub(), USER)]
    assert client.post("/api/billing/razorpay/subscribe", json={"plan": "monthly"}, headers=AUTH).status_code == 409
    assert not [c for c in gw.calls if c[0] == "create"]


def test_a_razorpay_failure_is_a_502_without_the_secret(env):
    client, _, gw = env

    def boom(_params):
        raise rzp.RazorpayError(400, "BAD_REQUEST_ERROR")

    gw.create_subscription = boom
    res = client.post("/api/billing/razorpay/subscribe", json={"plan": "monthly"}, headers=AUTH)
    assert res.status_code == 502 and res.json()["detail"]["error"] == "payment_provider_error"


# ── Sync ─────────────────────────────────────────────────────────────────────

def test_sync_re_reads_the_subscription_and_grants_premium(env):
    client, db, gw = env
    sub_id = client.post("/api/billing/razorpay/subscribe", json={"plan": "monthly"}, headers=AUTH).json()["subscriptionId"]
    gw.subs[sub_id] = {**gw.subs[sub_id], "status": "active", "current_end": PERIOD_END}
    res = client.post("/api/billing/razorpay/sync", json={"subscriptionId": sub_id}, headers=AUTH)
    assert res.status_code == 200
    data = res.json()
    assert data["premium"] is True
    assert data["subscription"] == {"provider": "razorpay", "status": "active", "plan": "monthly",
                                    "currentPeriodEnd": data["subscription"]["currentPeriodEnd"], "cancelAtPeriodEnd": False}
    assert data["subscription"]["currentPeriodEnd"].startswith("2030-03-17")


def test_authenticated_is_not_premium_yet(env):
    client, _, gw = env
    sub_id = client.post("/api/billing/razorpay/subscribe", json={"plan": "monthly"}, headers=AUTH).json()["subscriptionId"]
    gw.subs[sub_id] = {**gw.subs[sub_id], "status": "authenticated"}
    assert client.post("/api/billing/razorpay/sync", json={"subscriptionId": sub_id}, headers=AUTH).json()["premium"] is False


def test_sync_only_for_this_accounts_own_subscription(env):
    client, db, gw = env
    sub_id = client.post("/api/billing/razorpay/subscribe", json={"plan": "monthly"}, headers=AUTH).json()["subscriptionId"]
    assert client.post("/api/billing/razorpay/sync", json={"subscriptionId": sub_id}, headers=OTHER_AUTH).status_code == 403
    # An id this service never created for anyone is refused before Razorpay is asked.
    gw.subs["sub_Stranger1"] = _rsub("sub_Stranger1", user_id=USER)
    assert client.post("/api/billing/razorpay/sync", json={"subscriptionId": "sub_Stranger1"}, headers=AUTH).status_code == 403
    assert ("retrieve", "sub_Stranger1") not in gw.calls
    for bad in ("../plans/plan_month", "sub_x/cancel", "cs_test_1"):
        assert client.post("/api/billing/razorpay/sync", json={"subscriptionId": bad}, headers=AUTH).status_code == 422


# ── Webhook ──────────────────────────────────────────────────────────────────

def test_a_forged_or_unsigned_webhook_is_refused(env):
    client, db, _ = env
    body, headers = _webhook("subscription.activated", _rsub(), secret="wrong")
    assert client.post("/api/billing/razorpay/webhook", content=body, headers=headers).status_code == 400
    assert client.post("/api/billing/razorpay/webhook", content=body).status_code == 400
    assert rzp.TABLE not in db.tables


def test_a_webhook_is_synced_from_razorpay_not_from_the_payload(env):
    client, db, gw = env
    db.tables[rzp.TABLE] = [_row(status="created", current_end=None)]
    gw.subs["sub_RzpA1b2C3d"] = _rsub(status="pending")
    # The payload claims "active" for another account; Razorpay's own copy and our row win.
    body, headers = _webhook("subscription.charged", _rsub(status="active", user_id=OTHER))
    res = client.post("/api/billing/razorpay/webhook", content=body, headers=headers)
    assert res.status_code == 200 and res.json()["handled"] is True
    row = db.tables[rzp.TABLE][0]
    assert row["user_id"] == USER and row["status"] == "pending"


def test_our_row_outranks_the_notes(env):
    client, db, gw = env
    db.tables[rzp.TABLE] = [_row(status="created", current_end=None)]
    gw.subs["sub_RzpA1b2C3d"] = _rsub(user_id=OTHER)
    body, headers = _webhook("subscription.activated", {"id": "sub_RzpA1b2C3d"})
    client.post("/api/billing/razorpay/webhook", content=body, headers=headers)
    assert db.tables[rzp.TABLE][0]["user_id"] == USER


def test_a_duplicate_event_is_processed_once(env):
    client, db, gw = env
    gw.subs["sub_RzpA1b2C3d"] = _rsub()
    body, headers = _webhook("subscription.activated", {"id": "sub_RzpA1b2C3d"})
    client.post("/api/billing/razorpay/webhook", content=body, headers=headers)
    second = client.post("/api/billing/razorpay/webhook", content=body, headers=headers).json()
    assert second == {"received": True, "duplicate": True}
    assert len([c for c in gw.calls if c[0] == "retrieve"]) == 1


def test_an_event_without_an_id_header_is_deduplicated_by_its_body(env):
    client, _, gw = env
    gw.subs["sub_RzpA1b2C3d"] = _rsub()
    body, headers = _webhook("subscription.activated", {"id": "sub_RzpA1b2C3d"}, event_id=None)
    client.post("/api/billing/razorpay/webhook", content=body, headers=headers)
    assert client.post("/api/billing/razorpay/webhook", content=body, headers=headers).json()["duplicate"] is True


def test_a_failed_sync_is_500_and_not_marked_seen(env):
    client, db, gw = env

    def boom(_sid):
        raise rzp.RazorpayError(0, "ConnectError")

    gw.retrieve_subscription = boom
    body, headers = _webhook("subscription.activated", {"id": "sub_RzpA1b2C3d"})
    assert client.post("/api/billing/razorpay/webhook", content=body, headers=headers).status_code == 500
    assert not db.tables.get(rzp.EVENTS_TABLE)


def test_non_subscription_events_are_acknowledged_and_ignored(env):
    client, _, gw = env
    body = json.dumps({"event": "payment.captured", "payload": {"payment": {"entity": {"id": "pay_1"}}}}).encode()
    sig = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    res = client.post("/api/billing/razorpay/webhook", content=body, headers={"X-Razorpay-Signature": sig})
    assert res.json() == {"received": True, "handled": False} and not gw.calls


def test_an_unlinked_subscription_is_ignored(env):
    client, db, gw = env
    gw.subs["sub_RzpA1b2C3d"] = {**_rsub(), "notes": {}}
    body, headers = _webhook("subscription.activated", {"id": "sub_RzpA1b2C3d"})
    assert client.post("/api/billing/razorpay/webhook", content=body, headers=headers).status_code == 200
    assert not db.tables.get(rzp.TABLE)


# ── Rows (pure) ──────────────────────────────────────────────────────────────

def test_an_unknown_status_never_reads_as_paying(monkeypatch):
    monkeypatch.setenv("RAZORPAY_PLAN_MONTHLY", "plan_month")
    row = rzp.subscription_row(_rsub(status="something_new"), USER)
    assert row["status"] == rzp.UNKNOWN_STATUS_AS
    assert row["status"] not in rzp.PREMIUM_STATUSES and row["status"] in rzp.LIVE_STATUSES


def test_statuses_match_the_migration():
    sql = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "supabase", "migrations", "20261105090000_razorpay_billing.sql")).read()
    assert "r.status IN ('active', 'pending')" in sql
    assert "r.status IN ('authenticated', 'active', 'pending', 'halted', 'paused')" in sql
    assert rzp.PREMIUM_STATUSES == {"active", "pending"}
    assert rzp.LIVE_STATUSES == {"authenticated", "active", "pending", "halted", "paused"}
    for status in rzp.KNOWN_STATUSES:
        assert f"'{status}'" in sql


@pytest.mark.parametrize("event,expected", [
    ({"event": "subscription.charged", "payload": {"subscription": {"entity": {"id": "sub_RzpA1b2C3d"}}}}, "sub_RzpA1b2C3d"),
    ({"event": "payment.failed", "payload": {"subscription": {"entity": {"id": "sub_RzpA1b2C3d"}}}}, None),
    ({"event": "subscription.charged", "payload": {"subscription": {"entity": {"id": "../x"}}}}, None),
    ({"event": "subscription.charged", "payload": {}}, None),
])
def test_subscription_id_from_event(event, expected):
    assert rzp.subscription_id_from_event(event) == expected


# ── Status, cancel, deletion ─────────────────────────────────────────────────

def test_status_lists_razorpay_with_its_rupee_prices(env):
    client, _, gw = env
    data = client.get("/api/billing/status").json()
    assert data["configured"] is True
    assert data["providers"] == [{
        "id": "razorpay", "plans": ["monthly", "yearly"],
        "prices": {"monthly": {"amount": 19900, "currency": "INR", "interval": "month"},
                   "yearly": {"amount": 199900, "currency": "INR", "interval": "year"}},
    }]
    # The Stripe-only fields stay empty for an older frontend.
    assert data["plans"] == [] and data["prices"] == {}
    client.get("/api/billing/status")
    assert len([c for c in gw.calls if c[0] == "plan"]) == 2


def test_status_lists_both_providers_when_both_are_configured(env, monkeypatch):
    client, _, _ = env
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("STRIPE_PRICE_MONTHLY", "price_month")
    data = client.get("/api/billing/status").json()
    assert [p["id"] for p in data["providers"]] == ["stripe", "razorpay"]
    assert data["plans"] == ["monthly"]


def test_status_for_a_razorpay_subscriber(env):
    client, db, _ = env
    db.tables[rzp.TABLE] = [_row(status="created", sub_id="sub_Unpaid01", current_end=None), _row(cancel=True)]
    data = client.get("/api/billing/status", headers=AUTH).json()
    assert data["premium"] is True
    assert data["subscription"]["provider"] == "razorpay" and data["subscription"]["cancelAtPeriodEnd"] is True


def test_an_unpaid_razorpay_subscription_is_not_shown(env):
    client, db, _ = env
    db.tables[rzp.TABLE] = [_row(status="created", current_end=None)]
    data = client.get("/api/billing/status", headers=AUTH).json()
    assert data["premium"] is False and data["subscription"] is None


def test_the_premium_razorpay_limit_reaches_the_quota_lookup(env):
    client, db, _ = env
    db.tables[rzp.TABLE] = [_row(status="pending")]
    assert client.get("/api/billing/status", headers=AUTH).json()["premium"] is True
    db.tables[rzp.TABLE] = [_row(status="halted")]
    assert client.get("/api/billing/status", headers=AUTH).json()["premium"] is False


def test_cancel_stops_renewal_but_keeps_premium_until_the_period_ends(env):
    client, db, gw = env
    db.tables[rzp.TABLE] = [_row()]
    gw.subs["sub_RzpA1b2C3d"] = _rsub()
    res = client.post("/api/billing/razorpay/cancel", headers=AUTH)
    assert res.status_code == 200
    assert ("cancel", "sub_RzpA1b2C3d", True) in gw.calls
    data = res.json()
    assert data["premium"] is True and data["subscription"]["cancelAtPeriodEnd"] is True
    # The flag survives a later re-read (Razorpay's subscription has none).
    body, headers = _webhook("subscription.charged", {"id": "sub_RzpA1b2C3d"})
    client.post("/api/billing/razorpay/webhook", content=body, headers=headers)
    assert db.tables[rzp.TABLE][0]["cancel_at_period_end"] is True
    # Nothing left to cancel.
    assert client.post("/api/billing/razorpay/cancel", headers=AUTH).status_code == 404


def test_cancel_without_a_subscription_is_404(env):
    client, _, _ = env
    assert client.post("/api/billing/razorpay/cancel", headers=AUTH).status_code == 404


def test_cancel_for_deletion_cancels_razorpay_now(env):
    client, db, gw = env
    db.tables[rzp.TABLE] = [_row(cancel=True), _row(status="cancelled", sub_id="sub_Gone0001")]
    gw.subs["sub_RzpA1b2C3d"] = _rsub()
    res = client.post("/api/billing/cancel-for-deletion", headers=AUTH)
    assert res.status_code == 200 and res.json() == {"canceled": 1}
    assert ("cancel", "sub_RzpA1b2C3d", False) in gw.calls
    assert not [c for c in gw.calls if c[0] == "cancel" and c[1] == "sub_Gone0001"]
    assert {r["status"] for r in db.tables[rzp.TABLE]} == {"cancelled"}


def test_cancel_for_deletion_skips_razorpay_when_it_is_off(env, monkeypatch):
    client, db, gw = env
    monkeypatch.delenv("RAZORPAY_KEY_ID")
    db.tables[rzp.TABLE] = [_row()]
    assert client.post("/api/billing/cancel-for-deletion", headers=AUTH).json() == {"canceled": 0}
    assert not gw.calls


# ── The real HTTP gateway (no network: httpx's mock transport) ───────────────

def test_the_gateway_sends_basic_auth_and_maps_errors(monkeypatch):
    import httpx

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/cancel"):
            return httpx.Response(400, json={"error": {"code": "BAD_REQUEST_ERROR", "description": "nope"}})
        return httpx.Response(200, json={"id": "sub_RzpA1b2C3d", "status": "active"})

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    gw = rzp.RazorpayGateway("rzp_test_key", "rzp_test_secret")
    assert gw.retrieve_subscription("sub_RzpA1b2C3d")["status"] == "active"
    assert str(seen[0].url) == "https://api.razorpay.com/v1/subscriptions/sub_RzpA1b2C3d"
    assert seen[0].headers["authorization"].startswith("Basic ")
    with pytest.raises(rzp.RazorpayError) as err:
        gw.cancel_subscription("sub_RzpA1b2C3d", at_cycle_end=True)
    assert err.value.status == 400 and err.value.code == "BAD_REQUEST_ERROR"
    assert json.loads(seen[1].content) == {"cancel_at_cycle_end": 1}
    assert "nope" not in str(err.value)
