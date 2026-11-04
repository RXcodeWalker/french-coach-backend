"""routers/billing.py — Premium subscriptions through Stripe.

No network: Stripe is a fake gateway (except the webhook signature, which is
checked by the real SDK against a real HMAC), and Supabase is an in-memory fake.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
from routers import billing

USER = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
WHSEC = "whsec_test_secret"
PERIOD_END = 1_900_000_000  # 2030-03-17


# ── Fakes ────────────────────────────────────────────────────────────────────

class _Res:
    def __init__(self, data=None):
        self.data = data


class _Query:
    def __init__(self, db, table):
        self.db, self.table, self.filters, self.ins, self.op, self.payload = db, table, {}, None, "select", None

    def select(self, *_a, **_kw):
        return self

    def eq(self, key, value):
        self.filters[key] = value
        return self

    def in_(self, key, values):
        self.ins = (key, set(values))
        return self

    def upsert(self, row, on_conflict=None, ignore_duplicates=False):
        self.op, self.payload, self.key, self.ignore = "upsert", row, on_conflict, ignore_duplicates
        return self

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        if self.op == "upsert":
            existing = [r for r in rows if r.get(self.key) == self.payload[self.key]]
            if existing and self.ignore:
                return _Res([])
            for r in existing:
                rows.remove(r)
            rows.append(dict(self.payload))
            return _Res([self.payload])
        out = [r for r in rows if all(r.get(k) == v for k, v in self.filters.items())]
        if self.ins:
            out = [r for r in out if r.get(self.ins[0]) in self.ins[1]]
        return _Res(out)


class _Rpc:
    def __init__(self, db, name, params):
        self.db, self.name, self.params = db, name, params

    def execute(self):
        assert self.name == "has_premium"
        uid = self.params["p_user_id"]
        from routers import billing_razorpay

        live = [
            r for r in self.db.tables.get("billing_subscriptions", [])
            if r["user_id"] == uid and r["status"] in billing.PREMIUM_STATUSES
        ] + [
            r for r in self.db.tables.get("billing_razorpay_subscriptions", [])
            if r["user_id"] == uid and r["status"] in billing_razorpay.PREMIUM_STATUSES
        ]
        return _Res(bool(live))


class FakeDb:
    def __init__(self):
        self.tables = {
            "ai_quota_limits": [
                {"feature": "learn_answer", "daily_limit": 3, "premium_daily_limit": 30},
                {"feature": "exam_attempt", "daily_limit": 1, "premium_daily_limit": 1},
                {"feature": "pronunciation_lab", "daily_limit": 30, "premium_daily_limit": 60},
            ]
        }

    def table(self, name):
        return _Query(self, name)

    def rpc(self, name, params):
        return _Rpc(self, name, params)


def _sub(status="active", user_id=USER, sub_id="sub_1", customer="cus_1", price="price_month", cancel=False):
    return {
        "id": sub_id,
        "customer": customer,
        "status": status,
        "metadata": {"user_id": user_id},
        "cancel_at_period_end": cancel,
        "canceled_at": None,
        # API >= 2025-03-31: the period lives on the item.
        "items": {"data": [{"current_period_end": PERIOD_END, "price": {"id": price, "recurring": {"interval": "month"}}}]},
    }


class FakeGateway(billing.StripeGateway):
    def __init__(self):  # noqa: D401 — no real client
        import stripe

        self._stripe = stripe
        self.calls: list[tuple] = []
        self.subs: dict[str, dict] = {"sub_1": _sub()}
        self.checkout = {"id": "cs_test_1", "client_reference_id": USER, "subscription": "sub_1", "mode": "subscription"}

    def create_customer(self, *, email, user_id):
        self.calls.append(("customer", email, user_id))
        return {"id": "cus_1"}

    def create_checkout_session(self, params):
        self.calls.append(("checkout", params))
        return {"id": "cs_test_1", "url": "https://checkout.stripe.com/c/pay/cs_test_1"}

    def retrieve_checkout_session(self, session_id):
        self.calls.append(("retrieve_checkout", session_id))
        return self.checkout

    def create_portal_session(self, *, customer, return_url):
        self.calls.append(("portal", customer, return_url))
        return {"url": "https://billing.stripe.com/p/session/x"}

    def retrieve_price(self, price_id):
        self.calls.append(("price", price_id))
        return {"unit_amount": 499 if price_id == "price_month" else 3999, "currency": "gbp", "recurring": {"interval": "month" if price_id == "price_month" else "year"}}

    def retrieve_subscription(self, subscription_id):
        self.calls.append(("retrieve_sub", subscription_id))
        return self.subs[subscription_id]

    def cancel_subscription(self, subscription_id):
        self.calls.append(("cancel", subscription_id))
        self.subs[subscription_id] = {**self.subs[subscription_id], "status": "canceled", "canceled_at": int(time.time())}
        return self.subs[subscription_id]


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("STRIPE_PRICE_MONTHLY", "price_month")
    monkeypatch.setenv("STRIPE_PRICE_YEARLY", "price_year")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WHSEC)
    monkeypatch.delenv("BILLING_APP_URL", raising=False)
    monkeypatch.delenv("STRIPE_AUTOMATIC_TAX", raising=False)
    db, gw = FakeDb(), FakeGateway()
    monkeypatch.setattr(billing, "_get_db", lambda: db)
    monkeypatch.setattr(main, "get_supabase", lambda: db)
    monkeypatch.setattr(billing, "_gateway_factory", lambda: gw)
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


AUTH = {"Authorization": "Bearer user"}


def _signed(payload: dict, secret: str = WHSEC, ts: int | None = None) -> tuple[bytes, str]:
    body = json.dumps(payload).encode()
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return body, f"t={ts},v1={sig}"


def _event(etype: str, obj: dict, event_id: str = "evt_1") -> dict:
    return {"id": event_id, "object": "event", "type": etype, "data": {"object": obj}}


# ── Off until configured ─────────────────────────────────────────────────────

def test_everything_but_status_is_503_until_stripe_is_configured(env, monkeypatch):
    client, _, _ = env
    monkeypatch.delenv("STRIPE_SECRET_KEY")
    assert client.post("/api/billing/checkout", json={"plan": "monthly"}, headers=AUTH).status_code == 503
    assert client.post("/api/billing/portal", headers=AUTH).status_code == 503
    assert client.post("/api/billing/webhook", content=b"{}", headers={"stripe-signature": "t=1,v1=x"}).status_code == 503
    status = client.get("/api/billing/status").json()
    assert status["configured"] is False and status["plans"] == []


# ── Checkout ─────────────────────────────────────────────────────────────────

def test_checkout_needs_an_account(env):
    client, _, _ = env
    assert client.post("/api/billing/checkout", json={"plan": "monthly"}).status_code == 401


def test_checkout_creates_one_customer_and_a_subscription_session(env):
    client, db, gw = env
    res = client.post(
        "/api/billing/checkout", json={"plan": "yearly"},
        headers={**AUTH, "Origin": "http://localhost:5173"},
    )
    assert res.status_code == 200 and res.json()["url"].startswith("https://checkout.stripe.com/")
    assert db.tables["billing_customers"] == [{"user_id": USER, "stripe_customer_id": "cus_1"}]
    params = [c for c in gw.calls if c[0] == "checkout"][0][1]
    assert params["mode"] == "subscription" and params["customer"] == "cus_1"
    assert params["line_items"] == [{"price": "price_year", "quantity": 1}]
    # The account link the webhook trusts is written here, never by the browser.
    assert params["client_reference_id"] == USER and params["subscription_data"]["metadata"] == {"user_id": USER}
    assert params["success_url"].startswith("http://localhost:5173/premium?checkout=success")
    assert "{CHECKOUT_SESSION_ID}" in params["success_url"]
    assert "automatic_tax" not in params

    # A second checkout reuses the customer.
    client.post("/api/billing/checkout", json={"plan": "monthly"}, headers=AUTH)
    assert len([c for c in gw.calls if c[0] == "customer"]) == 1


def test_an_unknown_origin_never_becomes_the_return_url(env):
    client, _, gw = env
    client.post("/api/billing/checkout", json={"plan": "monthly"}, headers={**AUTH, "Origin": "https://evil.example"})
    params = [c for c in gw.calls if c[0] == "checkout"][0][1]
    assert params["success_url"].startswith(billing.DEFAULT_APP_URL)


def test_automatic_tax_is_opt_in(env, monkeypatch):
    client, _, gw = env
    monkeypatch.setenv("STRIPE_AUTOMATIC_TAX", "1")
    client.post("/api/billing/checkout", json={"plan": "monthly"}, headers=AUTH)
    params = [c for c in gw.calls if c[0] == "checkout"][0][1]
    assert params["automatic_tax"] == {"enabled": True}


def test_a_plan_without_a_price_is_refused(env, monkeypatch):
    client, _, _ = env
    monkeypatch.delenv("STRIPE_PRICE_YEARLY")
    assert client.post("/api/billing/checkout", json={"plan": "yearly"}, headers=AUTH).status_code == 422
    assert client.post("/api/billing/checkout", json={"plan": "lifetime"}, headers=AUTH).status_code == 422


def test_a_premium_account_is_sent_to_the_portal_not_a_second_subscription(env):
    client, db, _ = env
    db.tables["billing_subscriptions"] = [billing.subscription_row(_sub(), USER)]
    res = client.post("/api/billing/checkout", json={"plan": "monthly"}, headers=AUTH)
    assert res.status_code == 409 and res.json()["detail"]["error"] == "already_premium"


# ── Webhook ──────────────────────────────────────────────────────────────────

def test_a_forged_webhook_is_refused(env):
    client, db, _ = env
    body, sig = _signed(_event("customer.subscription.updated", {"id": "sub_1"}), secret="whsec_wrong")
    assert client.post("/api/billing/webhook", content=body, headers={"stripe-signature": sig}).status_code == 400
    assert client.post("/api/billing/webhook", content=body).status_code == 400
    assert "billing_subscriptions" not in db.tables


def test_an_old_signed_webhook_is_refused(env):
    client, _, _ = env
    body, sig = _signed(_event("customer.subscription.updated", {"id": "sub_1"}), ts=int(time.time()) - 3600)
    assert client.post("/api/billing/webhook", content=body, headers={"stripe-signature": sig}).status_code == 400


def test_a_subscription_event_is_synced_from_stripe_not_from_the_payload(env):
    client, db, gw = env
    # The payload claims "active" for another user; Stripe's own copy wins.
    gw.subs["sub_1"] = _sub(status="past_due")
    body, sig = _signed(_event("customer.subscription.updated", {"id": "sub_1", "status": "active", "metadata": {"user_id": OTHER}}))
    res = client.post("/api/billing/webhook", content=body, headers={"stripe-signature": sig})
    assert res.status_code == 200 and res.json()["handled"] is True
    row = db.tables["billing_subscriptions"][0]
    assert row["user_id"] == USER and row["status"] == "past_due" and row["plan"] == "monthly"
    assert row["current_period_end"].startswith("2030-03-17")


def test_the_customer_table_outranks_subscription_metadata(env):
    client, db, gw = env
    db.tables["billing_customers"] = [{"user_id": USER, "stripe_customer_id": "cus_1"}]
    gw.subs["sub_1"] = _sub(user_id=OTHER)
    body, sig = _signed(_event("customer.subscription.created", {"id": "sub_1"}))
    client.post("/api/billing/webhook", content=body, headers={"stripe-signature": sig})
    assert db.tables["billing_subscriptions"][0]["user_id"] == USER


def test_a_duplicate_event_is_processed_once(env):
    client, _, gw = env
    body, sig = _signed(_event("customer.subscription.updated", {"id": "sub_1"}))
    client.post("/api/billing/webhook", content=body, headers={"stripe-signature": sig})
    again = client.post("/api/billing/webhook", content=body, headers={"stripe-signature": sig}).json()
    assert again["duplicate"] is True
    assert len([c for c in gw.calls if c[0] == "retrieve_sub"]) == 1


def test_a_failed_sync_is_500_and_not_marked_seen_so_stripe_retries(env):
    client, db, gw = env

    def boom(_sid):
        raise RuntimeError("stripe down")

    gw.retrieve_subscription = boom
    body, sig = _signed(_event("customer.subscription.updated", {"id": "sub_1"}))
    assert client.post("/api/billing/webhook", content=body, headers={"stripe-signature": sig}).status_code == 500
    assert db.tables.get("stripe_webhook_events", []) == []


def test_unhandled_events_are_acknowledged_and_ignored(env):
    client, db, _ = env
    body, sig = _signed(_event("charge.refunded", {"id": "ch_1"}))
    res = client.post("/api/billing/webhook", content=body, headers={"stripe-signature": sig})
    assert res.status_code == 200 and res.json()["handled"] is False
    assert "billing_subscriptions" not in db.tables


def test_an_unlinked_subscription_is_ignored(env):
    client, db, gw = env
    gw.subs["sub_1"] = {**_sub(), "metadata": {}}
    body, sig = _signed(_event("customer.subscription.created", {"id": "sub_1"}))
    assert client.post("/api/billing/webhook", content=body, headers={"stripe-signature": sig}).status_code == 200
    assert db.tables.get("billing_subscriptions", []) == []


@pytest.mark.parametrize(
    "event,expected",
    [
        (_event("customer.subscription.deleted", {"id": "sub_9"}), "sub_9"),
        (_event("checkout.session.completed", {"mode": "subscription", "subscription": "sub_9"}), "sub_9"),
        (_event("checkout.session.completed", {"mode": "payment", "subscription": None}), None),
        # API >= 2025-03-31 invoice shape
        (_event("invoice.paid", {"parent": {"subscription_details": {"subscription": "sub_9"}}}), "sub_9"),
        # older invoice shape
        (_event("invoice.payment_failed", {"subscription": "sub_9"}), "sub_9"),
    ],
)
def test_subscription_id_from_event(event, expected):
    assert billing.subscription_id_from_event(event) == expected


def test_an_unknown_stripe_status_never_reads_as_paying():
    row = billing.subscription_row({**_sub(), "status": "some_future_status"}, USER)
    assert row["status"] == "incomplete"


def test_the_older_top_level_period_field_still_works(monkeypatch):
    monkeypatch.setenv("STRIPE_PRICE_YEARLY", "price_year")
    sub = {**_sub(), "current_period_end": PERIOD_END, "items": {"data": [{"price": {"id": "price_year"}}]}}
    row = billing.subscription_row(sub, USER)
    assert row["current_period_end"].startswith("2030-03-17") and row["plan"] == "yearly"


# ── Status, sync, portal, cancel ─────────────────────────────────────────────

def test_status_for_a_guest_shows_plans_and_limits(env):
    client, _, _ = env
    data = client.get("/api/billing/status").json()
    assert data["configured"] is True and data["plans"] == ["monthly", "yearly"]
    assert data["premium"] is False and data["subscription"] is None
    assert data["limits"]["learn_answer"] == {"free": 3, "premium": 30}
    assert data["prices"]["monthly"] == {"amount": 499, "currency": "gbp", "interval": "month"}
    assert data["prices"]["yearly"]["amount"] == 3999


def test_prices_are_cached(env):
    client, _, gw = env
    client.get("/api/billing/status")
    client.get("/api/billing/status")
    assert len([c for c in gw.calls if c[0] == "price"]) == 2  # one per plan, not per view


def test_a_price_stripe_cannot_read_is_left_out(env):
    client, _, gw = env

    def boom(_pid):
        raise RuntimeError("stripe down")

    gw.retrieve_price = boom
    data = client.get("/api/billing/status").json()
    assert data["prices"] == {} and data["plans"] == ["monthly", "yearly"]


def test_status_for_a_subscriber(env):
    client, db, _ = env
    db.tables["billing_subscriptions"] = [billing.subscription_row(_sub(cancel=True), USER)]
    data = client.get("/api/billing/status", headers=AUTH).json()
    assert data["premium"] is True
    assert data["subscription"]["plan"] == "monthly" and data["subscription"]["cancelAtPeriodEnd"] is True


def test_sync_after_checkout_only_for_the_same_account(env):
    client, db, _ = env
    res = client.post("/api/billing/sync", json={"sessionId": "cs_test_1"}, headers=AUTH)
    assert res.status_code == 200 and res.json()["premium"] is True
    db.tables["billing_subscriptions"] = []
    other = client.post("/api/billing/sync", json={"sessionId": "cs_test_1"}, headers={"Authorization": "Bearer other"})
    assert other.status_code == 403 and db.tables["billing_subscriptions"] == []
    assert client.post("/api/billing/sync", json={"sessionId": "sub_1"}, headers=AUTH).status_code == 422


def test_portal_needs_a_billing_account(env):
    client, db, gw = env
    assert client.post("/api/billing/portal", headers=AUTH).status_code == 404
    db.tables["billing_customers"] = [{"user_id": USER, "stripe_customer_id": "cus_1"}]
    res = client.post("/api/billing/portal", headers=AUTH)
    assert res.status_code == 200 and ("portal", "cus_1", f"{billing.DEFAULT_APP_URL}/premium") in gw.calls


def test_cancel_for_deletion_cancels_every_live_subscription(env):
    client, db, gw = env
    db.tables["billing_subscriptions"] = [
        billing.subscription_row(_sub(), USER),
        billing.subscription_row(_sub(status="canceled", sub_id="sub_old"), USER),
    ]
    gw.subs["sub_old"] = _sub(status="canceled", sub_id="sub_old")
    res = client.post("/api/billing/cancel-for-deletion", headers=AUTH).json()
    assert res == {"canceled": 1}
    assert ("cancel", "sub_1") in gw.calls and ("cancel", "sub_old") not in gw.calls
    assert {r["status"] for r in db.tables["billing_subscriptions"]} == {"canceled"}


def test_cancel_for_deletion_with_nothing_to_cancel_works_without_stripe(env, monkeypatch):
    client, _, _ = env
    monkeypatch.delenv("STRIPE_SECRET_KEY")
    assert client.post("/api/billing/cancel-for-deletion", headers=AUTH).json() == {"canceled": 0}


# ── Allowances show the Premium limit ────────────────────────────────────────

class _AllowanceQuery(_Query):
    def execute(self):
        if self.table == "ai_usage_grants":
            class R:
                data, count = [], 5
            return R()
        return super().execute()


def test_the_learn_allowance_reports_the_premium_limit(env, monkeypatch):
    client, db, _ = env
    monkeypatch.setattr(db, "table", lambda name: _AllowanceQuery(db, name))
    free = client.get("/api/learn/allowance", headers=AUTH).json()
    assert free["limit"] == 3 and free["remaining"] == 0 and free["premium"] is False
    db.tables["billing_subscriptions"] = [billing.subscription_row(_sub(), USER)]
    paid = client.get("/api/learn/allowance", headers=AUTH).json()
    assert paid["limit"] == 30 and paid["remaining"] == 25 and paid["premium"] is True
    lab = client.get("/api/pronunciation/allowance", headers=AUTH).json()
    assert lab["limit"] == 60 and lab["premium"] is True


def test_the_allowance_still_works_before_the_premium_migration(env, monkeypatch):
    client, db, _ = env

    class _OldSchemaQuery(_AllowanceQuery):
        def select(self, cols="*", *a, **kw):
            self.cols = cols
            return self

        def execute(self):
            if self.table == "ai_quota_limits" and "premium_daily_limit" in self.cols:
                raise RuntimeError("column ai_quota_limits.premium_daily_limit does not exist")
            if self.table == "ai_quota_limits":
                return _Res([{"daily_limit": 3}])
            return super().execute()

    monkeypatch.setattr(db, "table", lambda name: _OldSchemaQuery(db, name))
    data = client.get("/api/learn/allowance", headers=AUTH).json()
    assert data["limit"] == 3 and data["premium"] is False
