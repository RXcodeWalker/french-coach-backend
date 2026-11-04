"""Premium subscriptions through Razorpay — the second payment provider, beside
Stripe (routers/billing.py). Main repo docs/systems/payments.md §6, ADR 0014
amendment.

Why a second provider: Razorpay takes UPI Autopay, RuPay and Indian cards under
the RBI e-mandate rules, in rupees. Premium itself is unchanged — the same
limits, the same `has_premium()` — which now also reads
billing_razorpay_subscriptions (migration 20261105090000).

Routes (all under /api/billing/razorpay):

- POST /subscribe   signed in → {keyId, subscriptionId} for {plan}. The browser
                    opens Razorpay's own Checkout with them (checkout.js, hosted
                    by Razorpay), so no card or UPI detail touches this app.
- POST /sync        signed in, after Checkout closes: re-reads one of THIS
                    account's subscriptions from Razorpay so Premium shows at once.
- POST /cancel      signed in: cancels this account's subscription at the end of
                    the paid period (Razorpay has no customer portal).
- POST /webhook     Razorpay only. X-Razorpay-Signature (HMAC-SHA256 of the raw
                    body with RAZORPAY_WEBHOOK_SECRET) is the authentication.

Trust model — the same as Stripe's:
- Only this service writes billing_razorpay_* rows, with the service role, and
  only from a subscription it re-read from Razorpay. A webhook or a browser only
  says *which* subscription to re-read, so order, duplicates and replays (Razorpay
  signs no timestamp) cannot set a wrong state.
- The account a subscription belongs to is the row this service inserted when it
  created the subscription (then the `user_id` note it wrote), never the browser.
  That is also why /sync needs no Checkout signature: it trusts nothing the
  browser sends except which of its own subscriptions to re-read.

Razorpay has no "cancel at period end" flag on the subscription, so
`cancel_at_period_end` is this app's own record that /cancel asked for it, kept
across re-reads. A cycle-end cancel made in the Razorpay dashboard shows only
when the period ends.

Configuration (FastAPI env; unset = Razorpay off, its routes answer 503
`billing_not_configured`):
  RAZORPAY_KEY_ID                 rzp_test_… / rzp_live_… (public; sent to the browser)
  RAZORPAY_KEY_SECRET             the matching secret (never leaves this service)
  RAZORPAY_PLAN_MONTHLY           plan_… (period monthly, interval 1)
  RAZORPAY_PLAN_YEARLY            plan_… (period yearly, interval 1; optional)
  RAZORPAY_WEBHOOK_SECRET         the secret typed when adding the webhook
  RAZORPAY_TOTAL_COUNT_MONTHLY    billing cycles a subscription may run (default 120)
  RAZORPAY_TOTAL_COUNT_YEARLY     (default 10)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from routers import billing as core

log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/api/billing/razorpay", tags=["billing"])

TABLE = "billing_razorpay_subscriptions"
EVENTS_TABLE = "razorpay_webhook_events"
# Statuses that give Premium (keep in step with has_premium() in migration
# 20261105090000). `pending` = a renewal charge failed and Razorpay is retrying.
PREMIUM_STATUSES = {"active", "pending"}
# Statuses that hold a mandate and may still charge — cancelled before an
# account is deleted (and the DB trigger refuses the delete while one exists).
LIVE_STATUSES = {"authenticated", "active", "pending", "halted", "paused"}
KNOWN_STATUSES = LIVE_STATUSES | {"created", "cancelled", "completed", "expired"}
# A status Razorpay adds later is stored as this: never Premium, still cancelled
# before a deletion.
UNKNOWN_STATUS_AS = "halted"
SUBSCRIPTION_ID = re.compile(r"^sub_[A-Za-z0-9]{6,40}$")
API_BASE = "https://api.razorpay.com/v1"
# An unpaid subscription this account opened a moment ago is reused, so closing
# Checkout and trying again doesn't leave a trail of new ones in the dashboard.
REUSE_CREATED_FOR = timedelta(minutes=30)


# ── Configuration ────────────────────────────────────────────────────────────

def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def razorpay_configured() -> bool:
    return bool(_env("RAZORPAY_KEY_ID") and _env("RAZORPAY_KEY_SECRET") and _env("RAZORPAY_PLAN_MONTHLY"))


def plan_id_for(plan: str) -> str | None:
    return {"monthly": _env("RAZORPAY_PLAN_MONTHLY"), "yearly": _env("RAZORPAY_PLAN_YEARLY")}.get(plan) or None


def plan_for_id(plan_id: str | None) -> str | None:
    if plan_id and plan_id == _env("RAZORPAY_PLAN_MONTHLY"):
        return "monthly"
    if plan_id and plan_id == _env("RAZORPAY_PLAN_YEARLY"):
        return "yearly"
    return None


def configured_plans() -> list[str]:
    return [p for p in ("monthly", "yearly") if plan_id_for(p)] if razorpay_configured() else []


def total_count_for(plan: str) -> int:
    name, default = (
        ("RAZORPAY_TOTAL_COUNT_YEARLY", 10) if plan == "yearly" else ("RAZORPAY_TOTAL_COUNT_MONTHLY", 120)
    )
    try:
        value = int(_env(name) or default)
    except ValueError:
        value = default
    return max(1, value)


# ── Razorpay gateway (the only code that talks to Razorpay; replaced in tests) ─

class RazorpayError(Exception):
    """A Razorpay API failure. Carries the HTTP status and Razorpay's error code
    only — never the response body or the request."""

    def __init__(self, status: int, code: str | None = None):
        super().__init__(f"razorpay {status} {code or ''}".strip())
        self.status = status
        self.code = code


class RazorpayGateway:
    """Razorpay's REST API over httpx (basic auth: key id + secret)."""

    def __init__(self, key_id: str, key_secret: str):
        self._auth = (key_id, key_secret)

    def _request(self, method: str, path: str, json: dict | None = None) -> dict:
        try:
            with httpx.Client(timeout=15.0, auth=self._auth) as client:
                res = client.request(method, f"{API_BASE}{path}", json=json)
        except httpx.HTTPError as exc:
            raise RazorpayError(0, type(exc).__name__) from exc
        if res.status_code >= 400:
            code = None
            try:
                code = (res.json().get("error") or {}).get("code")
            except Exception:  # noqa: BLE001 — the code is only a log hint
                pass
            raise RazorpayError(res.status_code, code)
        return res.json()

    def create_subscription(self, params: dict) -> dict:
        return self._request("POST", "/subscriptions", params)

    def retrieve_subscription(self, subscription_id: str) -> dict:
        return self._request("GET", f"/subscriptions/{subscription_id}")

    def cancel_subscription(self, subscription_id: str, *, at_cycle_end: bool) -> dict:
        return self._request(
            "POST", f"/subscriptions/{subscription_id}/cancel", {"cancel_at_cycle_end": 1 if at_cycle_end else 0}
        )

    def retrieve_plan(self, plan_id: str) -> dict:
        return self._request("GET", f"/plans/{plan_id}")


def _gateway_factory() -> RazorpayGateway:
    return RazorpayGateway(_env("RAZORPAY_KEY_ID"), _env("RAZORPAY_KEY_SECRET"))


def _gateway() -> RazorpayGateway:
    if not razorpay_configured():
        raise core._not_configured()
    return _gateway_factory()


def verify_webhook_signature(body: bytes, signature: str | None, secret: str) -> bool:
    """Razorpay's webhook signature: hex HMAC-SHA256 of the raw body."""
    if not signature or not secret:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


def _failure(stage: str, exc: Exception) -> HTTPException:
    """A 502 raised `from exc` (Sentry reports it once, with the cause)."""
    log.error("billing: Razorpay %s failed: %r", stage, exc)
    return HTTPException(status_code=502, detail={"error": "payment_provider_error"})


# ── Rows (pure) ──────────────────────────────────────────────────────────────

def subscription_row(sub: dict, user_id: str, *, cancel_at_period_end: bool = False) -> dict:
    """The billing_razorpay_subscriptions row for a Razorpay subscription."""
    status = sub.get("status")
    if status not in KNOWN_STATUSES:
        log.warning("billing: unknown Razorpay status %r — stored as %s", status, UNKNOWN_STATUS_AS)
        status = UNKNOWN_STATUS_AS
    return {
        "razorpay_subscription_id": sub["id"],
        "user_id": user_id,
        "status": status,
        "plan_id": sub.get("plan_id"),
        "plan": plan_for_id(sub.get("plan_id")),
        "current_period_end": core._ts(sub.get("current_end")),
        "cancel_at_period_end": bool(cancel_at_period_end) and status in LIVE_STATUSES,
        "ended_at": core._ts(sub.get("ended_at")),
        "created_at": core._ts(sub.get("created_at")) or datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def public_subscription(row: dict | None) -> dict | None:
    if not row:
        return None
    return {
        "provider": "razorpay",
        "status": row.get("status"),
        "plan": row.get("plan"),
        "currentPeriodEnd": row.get("current_period_end"),
        "cancelAtPeriodEnd": bool(row.get("cancel_at_period_end")),
    }


def subscription_id_from_event(event: dict) -> str | None:
    """Which subscription a webhook event is about (pure)."""
    if not str(event.get("event", "")).startswith("subscription."):
        return None
    entity = (((event.get("payload") or {}).get("subscription") or {}).get("entity")) or {}
    sub_id = entity.get("id")
    return sub_id if isinstance(sub_id, str) and SUBSCRIPTION_ID.match(sub_id) else None


# ── DB helpers (service role) ────────────────────────────────────────────────

async def rows_for(db, user_id: str) -> list[dict]:
    res = await core._run(lambda: db.table(TABLE).select("*").eq("user_id", user_id).execute())
    return list(res.data or [])


async def _row(db, subscription_id: str) -> dict | None:
    res = await core._run(
        lambda: db.table(TABLE).select("*").eq("razorpay_subscription_id", subscription_id).execute()
    )
    rows = res.data or []
    return rows[0] if rows else None


async def _upsert(db, row: dict) -> None:
    await core._run(lambda: db.table(TABLE).upsert(row, on_conflict="razorpay_subscription_id").execute())


async def sync_subscription(db, gateway: RazorpayGateway, subscription_id: str) -> dict | None:
    """Re-read one subscription from Razorpay and upsert it. None when it
    belongs to no account this app knows."""
    sub = await core._run(lambda: gateway.retrieve_subscription(subscription_id))
    existing = await _row(db, subscription_id)
    user_id = (existing or {}).get("user_id") or (sub.get("notes") or {}).get("user_id")
    if not user_id:
        log.warning("billing: Razorpay subscription %s has no linked account — ignored", subscription_id)
        return None
    row = subscription_row(sub, user_id, cancel_at_period_end=bool((existing or {}).get("cancel_at_period_end")))
    await _upsert(db, row)
    return row


async def cancel_all_now(db, user_id: str) -> int:
    """Cancels, immediately, every subscription of this account that may still
    charge (account deletion). Returns how many."""
    live = [r for r in await rows_for(db, user_id) if r.get("status") in LIVE_STATUSES]
    if not live:
        return 0
    gateway = _gateway()
    for existing in live:
        sub_id = existing["razorpay_subscription_id"]
        try:
            sub = await core._run(lambda: gateway.cancel_subscription(sub_id, at_cycle_end=False))
        except Exception as exc:
            raise _failure("cancel", exc) from exc
        await _upsert(db, subscription_row(sub, user_id))
    return len(live)


# Plans change rarely; the Premium page should not cost a Razorpay call per view.
_PRICE_TTL_S = 600.0
_price_cache: dict[str, tuple[float, dict]] = {}
_PERIOD_TO_INTERVAL = {"monthly": "month", "yearly": "year", "weekly": "week", "daily": "day"}


async def prices() -> dict[str, dict]:
    """{plan: {amount (paise), currency, interval}} from Razorpay's plans."""
    if not razorpay_configured():
        return {}
    out: dict[str, dict] = {}
    gateway = None
    for plan in ("monthly", "yearly"):
        plan_id = plan_id_for(plan)
        if not plan_id:
            continue
        cached = _price_cache.get(plan_id)
        if cached and time.monotonic() - cached[0] < _PRICE_TTL_S:
            out[plan] = cached[1]
            continue
        try:
            gateway = gateway or _gateway_factory()
            remote = await core._run(lambda: gateway.retrieve_plan(plan_id))
        except Exception as exc:  # noqa: BLE001 — display-only
            log.warning("billing: Razorpay plan lookup failed for %s: %r", plan, exc)
            continue
        item = remote.get("item") or {}
        info = {
            "amount": item.get("amount"),
            "currency": item.get("currency"),
            "interval": _PERIOD_TO_INTERVAL.get(remote.get("period") or ""),
        }
        _price_cache[plan_id] = (time.monotonic(), info)
        out[plan] = info
    return out


def _recent(row: dict) -> bool:
    try:
        created = datetime.fromisoformat(str(row.get("created_at")))
    except ValueError:
        return False
    return datetime.now(timezone.utc) - created < REUSE_CREATED_FOR


# ── Routes ───────────────────────────────────────────────────────────────────

class SubscribeRequest(BaseModel):
    plan: Literal["monthly", "yearly"] = "monthly"


@router.post("/subscribe")
async def subscribe(body: SubscribeRequest, authorization: str | None = Header(None)) -> dict[str, Any]:
    user_id, _ = core._caller(authorization)
    gateway = _gateway()
    plan_id = plan_id_for(body.plan)
    if not plan_id:
        raise HTTPException(status_code=422, detail={"error": "plan_not_available", "plan": body.plan})
    db = core._db()
    if await core.has_premium(db, user_id):
        raise HTTPException(status_code=409, detail={"error": "already_premium"})

    # Reuse an unpaid subscription for the same plan opened in the last 30 min.
    for row in await rows_for(db, user_id):
        if row.get("status") == "created" and row.get("plan_id") == plan_id and _recent(row):
            return {"keyId": _env("RAZORPAY_KEY_ID"), "subscriptionId": row["razorpay_subscription_id"]}

    params = {
        "plan_id": plan_id,
        "total_count": total_count_for(body.plan),
        "quantity": 1,
        # Razorpay emails/texts receipts and failed-charge notices itself.
        "customer_notify": 1,
        "notes": {"user_id": user_id},
    }
    try:
        sub = await core._run(lambda: gateway.create_subscription(params))
    except Exception as exc:
        raise _failure("subscribe", exc) from exc
    # The account link: written by this service, before the browser sees the id.
    await _upsert(db, subscription_row(sub, user_id))
    return {"keyId": _env("RAZORPAY_KEY_ID"), "subscriptionId": sub["id"]}


class SyncRequest(BaseModel):
    subscriptionId: str


@router.post("/sync")
async def sync_after_checkout(body: SyncRequest, authorization: str | None = Header(None)) -> dict[str, Any]:
    user_id, _ = core._caller(authorization)
    gateway = _gateway()
    db = core._db()
    if not SUBSCRIPTION_ID.match(body.subscriptionId):
        raise HTTPException(status_code=422, detail={"error": "invalid_subscription"})
    existing = await _row(db, body.subscriptionId)
    if not existing or existing.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail={"error": "not_your_subscription"})
    try:
        row = await sync_subscription(db, gateway, body.subscriptionId)
    except Exception as exc:
        raise _failure("sync", exc) from exc
    return {"premium": await core.has_premium(db, user_id), "subscription": public_subscription(row)}


@router.post("/cancel")
async def cancel_at_period_end(authorization: str | None = Header(None)) -> dict[str, Any]:
    """Stops renewal: Premium stays until the end of the paid period."""
    user_id, _ = core._caller(authorization)
    gateway = _gateway()
    db = core._db()
    rows = [
        r for r in await rows_for(db, user_id)
        if r.get("status") in LIVE_STATUSES and not r.get("cancel_at_period_end")
    ]
    if not rows:
        raise HTTPException(status_code=404, detail={"error": "no_billing_account"})
    last: dict | None = None
    for existing in rows:
        sub_id = existing["razorpay_subscription_id"]
        # A subscription that is not paying (halted, authenticated) has no paid
        # period to keep, so it stops now; a paying one at its cycle end.
        at_cycle_end = existing.get("status") in PREMIUM_STATUSES
        try:
            sub = await core._run(lambda: gateway.cancel_subscription(sub_id, at_cycle_end=at_cycle_end))
        except Exception as exc:
            raise _failure("cancel", exc) from exc
        last = subscription_row(sub, user_id, cancel_at_period_end=at_cycle_end)
        await _upsert(db, last)
    return {"premium": await core.has_premium(db, user_id), "subscription": public_subscription(last)}


@router.post("/webhook")
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: str | None = Header(None),
    x_razorpay_event_id: str | None = Header(None),
) -> dict[str, Any]:
    secret = _env("RAZORPAY_WEBHOOK_SECRET")
    if not secret or not razorpay_configured():
        raise core._not_configured()
    body = await request.body()
    if not x_razorpay_signature:
        raise HTTPException(status_code=400, detail={"error": "missing_signature"})
    if not verify_webhook_signature(body, x_razorpay_signature, secret):
        raise HTTPException(status_code=400, detail={"error": "bad_signature"})
    try:
        event = json.loads(body)
    except ValueError:
        raise HTTPException(status_code=400, detail={"error": "bad_payload"})

    etype = str(event.get("event", ""))
    sub_id = subscription_id_from_event(event)
    if not sub_id:
        return {"received": True, "handled": False}

    # Razorpay sends the same x-razorpay-event-id on every retry of one event.
    event_id = (x_razorpay_event_id or "").strip()[:128] or "sha256:" + hashlib.sha256(body).hexdigest()
    db = core._db()
    seen = await core._run(lambda: db.table(EVENTS_TABLE).select("event_id").eq("event_id", event_id).execute())
    if seen.data:
        return {"received": True, "duplicate": True}

    gateway = _gateway()
    try:
        await sync_subscription(db, gateway, sub_id)
    except Exception as exc:
        # Non-2xx → Razorpay retries the event (for up to 24 hours).
        log.error("billing: Razorpay webhook %s (%s) failed: %r", event_id, etype, exc)
        raise HTTPException(status_code=500, detail={"error": "webhook_processing_failed"}) from exc

    await core._run(
        lambda: db.table(EVENTS_TABLE)
        .upsert({"event_id": event_id, "type": etype[:64]}, on_conflict="event_id", ignore_duplicates=True)
        .execute()
    )
    return {"received": True, "handled": True}
