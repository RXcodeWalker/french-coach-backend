"""Premium subscriptions through Stripe (owner decision 2026-11-03; main repo
docs/systems/payments.md, ADR 0014).

What Premium is: a monthly or yearly Stripe subscription that raises the launch
usage limits. The limits themselves stay where they always were —
`ai_quota_limits` and `consume_ai_quota` — which now read
`premium_daily_limit` for an account `has_premium()` says is subscribed
(migration 20261104090000). Nothing here grants AI calls directly.

Routes (all under /api/billing):

- POST /checkout              signed in → a Stripe Checkout URL for {plan}.
- POST /portal                signed in → a Stripe Customer Portal URL (cancel,
                              change card, invoices). Stripe hosts both pages,
                              so no card number ever touches this app.
- GET  /status                anyone → {configured, premium, subscription, limits}.
- POST /sync                  signed in, after Checkout returns: re-reads the
                              Checkout session so Premium shows at once even if
                              the webhook is a few seconds behind.
- POST /cancel-for-deletion   signed in: cancels every live subscription now,
                              before the app deletes the account (the DB refuses
                              to delete a profile that is still being charged).
- POST /webhook               Stripe only. The signature (STRIPE_WEBHOOK_SECRET)
                              is the authentication; the raw body is verified
                              before anything is parsed.

Trust model:
- Only this service writes billing_* rows, with the service-role key, and only
  from data it fetched from Stripe itself. A webhook event is used only to
  learn *which* subscription changed; the subscription is then re-read from
  Stripe, so event order, duplicates and replays cannot set a wrong state.
- The account a subscription belongs to comes from the metadata this service
  wrote at Checkout (`user_id`), or from billing_customers — never from the
  browser.
- Guests cannot subscribe (a subscription needs an account to attach to).

Configuration (FastAPI env; all optional — unset = billing off, every route
except /status answers 503 `billing_not_configured`):
  STRIPE_SECRET_KEY        sk_test_… / sk_live_… (or a restricted rk_ key)
  STRIPE_WEBHOOK_SECRET    whsec_… from the webhook endpoint
  STRIPE_PRICE_MONTHLY     price_… (recurring, monthly)
  STRIPE_PRICE_YEARLY      price_… (recurring, yearly; optional)
  BILLING_APP_URL          where Stripe sends the learner back (default: the
                           request's Origin when it is an allowed CORS origin,
                           else https://french.beyondthebasics.me)
  STRIPE_AUTOMATIC_TAX     "1" to turn on Stripe Tax in Checkout (needs Stripe
                           Tax set up in the dashboard first)

No card data and no request body is logged. Sentry gets an error only for a
real Stripe/DB failure (a 5xx raised from its cause).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from lib.auth import verify_supabase_jwt

log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/api/billing", tags=["billing"])

# Statuses that still give Premium (keep in step with has_premium() in the migration).
PREMIUM_STATUSES = {"active", "trialing", "past_due"}
# Statuses that may still charge the card — cancelled before an account is deleted.
LIVE_STATUSES = {"active", "trialing", "past_due", "unpaid", "paused", "incomplete"}
KNOWN_STATUSES = LIVE_STATUSES | {"incomplete_expired", "canceled"}
# Features whose limits the Premium page shows (free vs Premium).
SHOWN_FEATURES = ("learn_answer", "exam_attempt", "pronunciation_lab")
# Events that can change a subscription. Anything else is acknowledged and ignored.
HANDLED_EVENTS = {
    "checkout.session.completed",
    "checkout.session.async_payment_succeeded",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "customer.subscription.paused",
    "customer.subscription.resumed",
    "customer.subscription.trial_will_end",
    "invoice.paid",
    "invoice.payment_failed",
    "invoice.payment_action_required",
}
DEFAULT_APP_URL = "https://french.beyondthebasics.me"


# ── Configuration ────────────────────────────────────────────────────────────

def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def stripe_configured() -> bool:
    return bool(_env("STRIPE_SECRET_KEY") and _env("STRIPE_PRICE_MONTHLY"))


def price_for(plan: str) -> str | None:
    return {"monthly": _env("STRIPE_PRICE_MONTHLY"), "yearly": _env("STRIPE_PRICE_YEARLY")}.get(plan) or None


def plan_for_price(price_id: str | None, interval: str | None = None) -> str | None:
    if price_id and price_id == _env("STRIPE_PRICE_MONTHLY"):
        return "monthly"
    if price_id and price_id == _env("STRIPE_PRICE_YEARLY"):
        return "yearly"
    return {"month": "monthly", "year": "yearly"}.get(interval or "")


def _not_configured() -> HTTPException:
    return HTTPException(status_code=503, detail={"error": "billing_not_configured"})


# Injected by main.py (configure()) so this module never imports main.
_get_db: Callable[[], Any] = lambda: None
_allowed_origins: list[str] = []


def configure(*, get_db: Callable[[], Any], allowed_origins: list[str]) -> None:
    global _get_db, _allowed_origins
    _get_db = get_db
    _allowed_origins = list(allowed_origins)


def _db():
    db = _get_db()
    if db is None:
        raise HTTPException(status_code=503, detail={"error": "billing_unavailable"})
    return db


def _app_url(request: Request) -> str:
    configured = _env("BILLING_APP_URL").rstrip("/")
    if configured:
        return configured
    origin = (request.headers.get("origin") or "").rstrip("/")
    if origin and origin in _allowed_origins:
        return origin
    return DEFAULT_APP_URL


# ── Stripe gateway (the only code that talks to Stripe; replaced in tests) ───

class StripeGateway:
    """Thin wrapper over stripe.StripeClient that returns plain dicts.

    The SDK pins its own API version, so every object read here has one shape:
    `current_period_end` lives on the subscription item (API ≥ 2025-03-31) and
    is read from there, with the older top-level field as a fallback.
    """

    def __init__(self, secret_key: str):
        import stripe

        self._stripe = stripe
        self._client = stripe.StripeClient(secret_key, max_network_retries=2)

    @staticmethod
    def _plain(obj: Any) -> dict:
        return obj.to_dict() if hasattr(obj, "to_dict") else dict(obj)

    def create_customer(self, *, email: str | None, user_id: str) -> dict:
        params: dict[str, Any] = {"metadata": {"user_id": user_id}}
        if email:
            params["email"] = email
        # Same key for the same account: a double-click never makes two customers.
        return self._plain(
            self._client.v1.customers.create(params, {"idempotency_key": f"customer:{user_id}"})
        )

    def create_checkout_session(self, params: dict) -> dict:
        return self._plain(self._client.v1.checkout.sessions.create(params))

    def retrieve_checkout_session(self, session_id: str) -> dict:
        return self._plain(self._client.v1.checkout.sessions.retrieve(session_id))

    def create_portal_session(self, *, customer: str, return_url: str) -> dict:
        return self._plain(
            self._client.v1.billing_portal.sessions.create({"customer": customer, "return_url": return_url})
        )

    def retrieve_price(self, price_id: str) -> dict:
        return self._plain(self._client.v1.prices.retrieve(price_id))

    def retrieve_subscription(self, subscription_id: str) -> dict:
        return self._plain(self._client.v1.subscriptions.retrieve(subscription_id))

    def cancel_subscription(self, subscription_id: str) -> dict:
        return self._plain(self._client.v1.subscriptions.cancel(subscription_id))

    def construct_event(self, payload: bytes, sig_header: str, secret: str) -> dict:
        """Verifies Stripe's signature (and a 5-minute timestamp tolerance) and
        returns the event as a dict. Raises ValueError on any bad signature."""
        try:
            event = self._stripe.Webhook.construct_event(payload, sig_header, secret)
        except self._stripe.SignatureVerificationError as exc:
            raise ValueError("bad signature") from exc
        return self._plain(event)


def _gateway_factory() -> StripeGateway:
    return StripeGateway(_env("STRIPE_SECRET_KEY"))


def _gateway() -> StripeGateway:
    if not stripe_configured():
        raise _not_configured()
    return _gateway_factory()


# ── DB helpers (service role) ────────────────────────────────────────────────

async def _run(fn: Callable[[], Any]) -> Any:
    return await asyncio.to_thread(fn)


async def has_premium(db, user_id: str) -> bool:
    """has_premium() RPC. Any failure reads as "not Premium": the quota RPC
    decides the real limit, so this only ever affects what a screen says."""
    if not user_id or user_id.startswith("guest:") or db is None:
        return False
    try:
        res = await _run(lambda: db.rpc("has_premium", {"p_user_id": user_id}).execute())
        return bool(res.data)
    except Exception as exc:  # noqa: BLE001 — display-only read
        log.warning("billing: has_premium lookup failed: %r", exc)
        return False


async def _customer_id_for(db, user_id: str) -> str | None:
    res = await _run(
        lambda: db.table("billing_customers").select("stripe_customer_id").eq("user_id", user_id).execute()
    )
    rows = res.data or []
    return rows[0]["stripe_customer_id"] if rows else None


async def _user_for_customer(db, customer_id: str) -> str | None:
    res = await _run(
        lambda: db.table("billing_customers").select("user_id").eq("stripe_customer_id", customer_id).execute()
    )
    rows = res.data or []
    return rows[0]["user_id"] if rows else None


async def _subscriptions_for(db, user_id: str) -> list[dict]:
    res = await _run(
        lambda: db.table("billing_subscriptions").select("*").eq("user_id", user_id).execute()
    )
    return list(res.data or [])


def _ts(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()


def subscription_row(sub: dict, user_id: str) -> dict:
    """The billing_subscriptions row for a Stripe subscription (pure)."""
    items = ((sub.get("items") or {}).get("data")) or []
    first = items[0] if items else {}
    price = first.get("price") or {}
    period_end = first.get("current_period_end") or sub.get("current_period_end")
    status = sub.get("status")
    if status not in KNOWN_STATUSES:
        # A status Stripe adds later is treated as not paying until someone maps it.
        log.warning("billing: unknown subscription status %r — stored as incomplete", status)
        status = "incomplete"
    customer = sub.get("customer")
    if isinstance(customer, dict):
        customer = customer.get("id")
    return {
        "stripe_subscription_id": sub["id"],
        "user_id": user_id,
        "stripe_customer_id": customer,
        "status": status,
        "price_id": price.get("id"),
        "plan": plan_for_price(price.get("id"), (price.get("recurring") or {}).get("interval")),
        "current_period_end": _ts(period_end),
        "cancel_at_period_end": bool(sub.get("cancel_at_period_end")),
        "canceled_at": _ts(sub.get("canceled_at")),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


async def sync_subscription(db, gateway: StripeGateway, subscription_id: str) -> dict | None:
    """Re-read one subscription from Stripe and upsert it. Returns the row, or
    None when the subscription belongs to no account this app knows."""
    sub = await _run(lambda: gateway.retrieve_subscription(subscription_id))
    user_id = (sub.get("metadata") or {}).get("user_id")
    customer = sub.get("customer")
    customer_id = customer.get("id") if isinstance(customer, dict) else customer
    known = await _user_for_customer(db, customer_id) if customer_id else None
    if known:
        # The customer table is the stronger link (written by this service at Checkout).
        user_id = known
    if not user_id:
        log.warning("billing: subscription %s has no linked account — ignored", subscription_id)
        return None
    row = subscription_row(sub, user_id)
    await _run(lambda: db.table("billing_subscriptions").upsert(row, on_conflict="stripe_subscription_id").execute())
    return row


def subscription_id_from_event(event: dict) -> str | None:
    """Which subscription an event is about (pure). Handles the invoice shape
    of API ≥ 2025-03-31 (parent.subscription_details) and the older one."""
    obj = ((event.get("data") or {}).get("object")) or {}
    etype = event.get("type", "")
    if etype.startswith("customer.subscription."):
        return obj.get("id")
    if etype.startswith("checkout.session."):
        if obj.get("mode") != "subscription":
            return None
        sub = obj.get("subscription")
        return sub.get("id") if isinstance(sub, dict) else sub
    if etype.startswith("invoice."):
        parent = obj.get("parent") or {}
        details = parent.get("subscription_details") or {}
        sub = details.get("subscription") or obj.get("subscription")
        return sub.get("id") if isinstance(sub, dict) else sub
    return None


def _caller(authorization: str | None) -> tuple[str, str | None]:
    """(user_id, email) of a signed-in caller. Guests are refused: Premium
    attaches to an account."""
    if not authorization:
        raise HTTPException(status_code=401, detail={"error": "sign_in_required"})
    payload = verify_supabase_jwt(authorization)
    user_id = str(payload.get("sub") or "")
    if not user_id:
        raise HTTPException(status_code=401, detail={"error": "sign_in_required"})
    return user_id, (payload.get("email") or None)


def _stripe_failure(stage: str, exc: Exception) -> HTTPException:
    """A 502 the caller raises `from exc`: Sentry's FastAPI integration reports
    it once, with the Stripe error as its cause (backend CLAUDE.md), so it is
    not captured here as well. log.error is only a breadcrumb."""
    log.error("billing: Stripe %s failed: %r", stage, exc)
    return HTTPException(status_code=502, detail={"error": "payment_provider_error"})


def public_subscription(row: dict | None) -> dict | None:
    if not row:
        return None
    return {
        "status": row.get("status"),
        "plan": row.get("plan"),
        "currentPeriodEnd": row.get("current_period_end"),
        "cancelAtPeriodEnd": bool(row.get("cancel_at_period_end")),
    }


def _best_subscription(rows: list[dict]) -> dict | None:
    """The row to show: a Premium-giving one first, then the most recent."""
    if not rows:
        return None
    rank = lambda r: (r.get("status") in PREMIUM_STATUSES, r.get("updated_at") or "")
    return sorted(rows, key=rank, reverse=True)[0]


# ── Routes ───────────────────────────────────────────────────────────────────

class CheckoutRequest(BaseModel):
    plan: Literal["monthly", "yearly"] = "monthly"


@router.post("/checkout")
async def create_checkout(
    body: CheckoutRequest, request: Request, authorization: str | None = Header(None)
) -> dict[str, Any]:
    user_id, email = _caller(authorization)
    gateway = _gateway()
    price = price_for(body.plan)
    if not price:
        raise HTTPException(status_code=422, detail={"error": "plan_not_available", "plan": body.plan})
    db = _db()

    if await has_premium(db, user_id):
        raise HTTPException(status_code=409, detail={"error": "already_premium"})

    try:
        customer_id = await _customer_id_for(db, user_id)
        if not customer_id:
            customer = await _run(lambda: gateway.create_customer(email=email, user_id=user_id))
            customer_id = customer["id"]
            await _run(
                lambda: db.table("billing_customers")
                .upsert({"user_id": user_id, "stripe_customer_id": customer_id}, on_conflict="user_id")
                .execute()
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise _stripe_failure("customer", exc) from exc

    app_url = _app_url(request)
    params: dict[str, Any] = {
        "mode": "subscription",
        "customer": customer_id,
        "line_items": [{"price": price, "quantity": 1}],
        "client_reference_id": user_id,
        "metadata": {"user_id": user_id},
        "subscription_data": {"metadata": {"user_id": user_id}},
        "allow_promotion_codes": True,
        "success_url": f"{app_url}/premium?checkout=success&session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{app_url}/premium?checkout=cancelled",
    }
    if _env("STRIPE_AUTOMATIC_TAX") in ("1", "true", "yes"):
        params["automatic_tax"] = {"enabled": True}
        params["customer_update"] = {"address": "auto"}
    try:
        session = await _run(lambda: gateway.create_checkout_session(params))
    except Exception as exc:
        raise _stripe_failure("checkout", exc) from exc
    return {"url": session["url"]}


@router.post("/portal")
async def create_portal(request: Request, authorization: str | None = Header(None)) -> dict[str, Any]:
    user_id, _ = _caller(authorization)
    gateway = _gateway()
    db = _db()
    customer_id = await _customer_id_for(db, user_id)
    if not customer_id:
        raise HTTPException(status_code=404, detail={"error": "no_billing_account"})
    try:
        session = await _run(
            lambda: gateway.create_portal_session(customer=customer_id, return_url=f"{_app_url(request)}/premium")
        )
    except Exception as exc:
        raise _stripe_failure("portal", exc) from exc
    return {"url": session["url"]}


async def _limits(db) -> dict[str, dict[str, int]]:
    try:
        res = await _run(
            lambda: db.table("ai_quota_limits").select("feature, daily_limit, premium_daily_limit")
            .in_("feature", list(SHOWN_FEATURES)).execute()
        )
    except Exception as exc:  # noqa: BLE001 — display-only
        log.warning("billing: limits lookup failed: %r", exc)
        return {}
    out: dict[str, dict[str, int]] = {}
    for row in res.data or []:
        free = int(row["daily_limit"])
        premium = row.get("premium_daily_limit")
        out[row["feature"]] = {"free": free, "premium": int(premium) if premium is not None else free}
    return out


# Prices change rarely; the Premium page should not cost a Stripe call per view.
_PRICE_TTL_S = 600.0
_price_cache: dict[str, tuple[float, dict]] = {}


async def _prices() -> dict[str, dict]:
    """{plan: {amount, currency, interval}} for each configured plan, from Stripe
    (amount in the currency's smallest unit, as Stripe gives it). A plan whose
    price cannot be read is left out — the page then shows no number for it."""
    if not stripe_configured():
        return {}
    out: dict[str, dict] = {}
    gateway = None
    for plan in ("monthly", "yearly"):
        price_id = price_for(plan)
        if not price_id:
            continue
        cached = _price_cache.get(price_id)
        if cached and time.monotonic() - cached[0] < _PRICE_TTL_S:
            out[plan] = cached[1]
            continue
        try:
            gateway = gateway or _gateway_factory()
            price = await _run(lambda: gateway.retrieve_price(price_id))
        except Exception as exc:  # noqa: BLE001 — display-only
            log.warning("billing: price lookup failed for %s: %r", plan, exc)
            continue
        info = {
            "amount": price.get("unit_amount"),
            "currency": price.get("currency"),
            "interval": (price.get("recurring") or {}).get("interval"),
        }
        _price_cache[price_id] = (time.monotonic(), info)
        out[plan] = info
    return out


@router.get("/status")
async def billing_status(authorization: str | None = Header(None)) -> dict[str, Any]:
    """Anyone may read it (the Premium page shows plans to guests too). With a
    token, it also says whether this account is Premium."""
    db = _get_db()
    configured = stripe_configured()
    result: dict[str, Any] = {
        "configured": configured,
        "plans": [p for p in ("monthly", "yearly") if price_for(p)] if configured else [],
        "prices": await _prices(),
        "premium": False,
        "subscription": None,
        "limits": await _limits(db) if db is not None else {},
    }
    if authorization and db is not None:
        user_id = str(verify_supabase_jwt(authorization).get("sub") or "")
        if user_id:
            try:
                rows = await _subscriptions_for(db, user_id)
            except Exception as exc:  # noqa: BLE001 — display-only
                log.warning("billing: subscription lookup failed: %r", exc)
                rows = []
            result["premium"] = await has_premium(db, user_id)
            result["subscription"] = public_subscription(_best_subscription(rows))
    return result


class SyncRequest(BaseModel):
    sessionId: str


@router.post("/sync")
async def sync_after_checkout(body: SyncRequest, authorization: str | None = Header(None)) -> dict[str, Any]:
    """Called once when Checkout sends the learner back. Only syncs a Checkout
    session that this same account started."""
    user_id, _ = _caller(authorization)
    gateway = _gateway()
    db = _db()
    if not body.sessionId.startswith("cs_"):
        raise HTTPException(status_code=422, detail={"error": "invalid_session"})
    try:
        session = await _run(lambda: gateway.retrieve_checkout_session(body.sessionId))
    except Exception as exc:
        raise _stripe_failure("sync", exc) from exc
    if session.get("client_reference_id") != user_id:
        raise HTTPException(status_code=403, detail={"error": "not_your_checkout"})
    sub = session.get("subscription")
    sub_id = sub.get("id") if isinstance(sub, dict) else sub
    if not sub_id:
        return {"premium": False, "subscription": None}
    try:
        row = await sync_subscription(db, gateway, sub_id)
    except Exception as exc:
        raise _stripe_failure("sync", exc) from exc
    return {"premium": await has_premium(db, user_id), "subscription": public_subscription(row)}


@router.post("/cancel-for-deletion")
async def cancel_for_deletion(authorization: str | None = Header(None)) -> dict[str, Any]:
    """Cancels every subscription that could still charge this account, right
    now (no refund of the current period — Stripe's default). The app calls this
    just before delete_my_account, which the DB refuses while one is live."""
    user_id, _ = _caller(authorization)
    db = _db()
    rows = [r for r in await _subscriptions_for(db, user_id) if r.get("status") in LIVE_STATUSES]
    if not rows:
        return {"canceled": 0}
    gateway = _gateway()
    canceled = 0
    for row in rows:
        sub_id = row["stripe_subscription_id"]
        try:
            sub = await _run(lambda: gateway.cancel_subscription(sub_id))
        except Exception as exc:
            raise _stripe_failure("cancel", exc) from exc
        new_row = subscription_row(sub, user_id)
        await _run(
            lambda: db.table("billing_subscriptions").upsert(new_row, on_conflict="stripe_subscription_id").execute()
        )
        canceled += 1
    return {"canceled": canceled}


@router.post("/webhook")
async def stripe_webhook(request: Request, stripe_signature: str | None = Header(None)) -> dict[str, Any]:
    secret = _env("STRIPE_WEBHOOK_SECRET")
    if not secret or not stripe_configured():
        raise _not_configured()
    payload = await request.body()
    if not stripe_signature:
        raise HTTPException(status_code=400, detail={"error": "missing_signature"})
    gateway = _gateway()
    try:
        event = gateway.construct_event(payload, stripe_signature, secret)
    except ValueError:
        raise HTTPException(status_code=400, detail={"error": "bad_signature"})

    event_id, etype = event.get("id"), event.get("type", "")
    if etype not in HANDLED_EVENTS:
        return {"received": True, "handled": False}

    db = _db()
    seen = await _run(lambda: db.table("stripe_webhook_events").select("event_id").eq("event_id", event_id).execute())
    if seen.data:
        return {"received": True, "duplicate": True}

    sub_id = subscription_id_from_event(event)
    if sub_id:
        try:
            await sync_subscription(db, gateway, sub_id)
        except Exception as exc:
            # 500 → Stripe retries the event (for up to 3 days). Reported once by
            # Sentry's FastAPI integration, with the cause.
            log.error("billing: webhook %s (%s) failed: %r", event_id, etype, exc)
            raise HTTPException(status_code=500, detail={"error": "webhook_processing_failed"}) from exc

    # Recorded only after it worked, so a failure is retried, not swallowed.
    # Processing is idempotent (it re-reads Stripe), so a race here is harmless.
    await _run(
        lambda: db.table("stripe_webhook_events")
        .upsert({"event_id": event_id, "type": etype}, on_conflict="event_id", ignore_duplicates=True)
        .execute()
    )
    return {"received": True, "handled": True}
