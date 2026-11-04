/*
  # Premium subscriptions — Razorpay as a second provider (owner, 2026-11-04)

  Razorpay takes UPI Autopay, RuPay and Indian cards (RBI e-mandates) in rupees.
  Premium itself is unchanged: the same limits (ai_quota_limits.
  premium_daily_limit) and the same has_premium(), which now also counts a
  Razorpay subscription. Main repo docs/systems/payments.md §6.

  Needs 20261104100000_premium_billing.sql first (it redefines has_premium() and
  refuse_delete_with_live_subscription(), both created there).

  Tables (written only by the FastAPI service, routers/billing_razorpay.py, with
  the service-role key, from subscriptions it re-read from Razorpay):

  - billing_razorpay_subscriptions  the Razorpay subscription mirror. A row is
                                    inserted when the service creates the
                                    subscription (status 'created'), which is
                                    the account link every later re-read trusts.
  - razorpay_webhook_events         event ids already processed (Razorpay
                                    delivers at least once and retries).

  Razorpay statuses: created → authenticated → active; pending (a renewal
  charge failed, Razorpay is retrying) → halted (retries exhausted); paused;
  cancelled / completed / expired (terminal).

  has_premium(user) = a Stripe subscription as before, OR a Razorpay one that is
  active or pending, whose current period ended no more than 3 days ago.

  Deleting an account is refused while a Razorpay subscription holds a mandate
  (authenticated, active, pending, halted, paused); the app cancels it first.

  Rollback: re-run 20261104100000's has_premium() and
  refuse_delete_with_live_subscription() bodies, then DROP the two tables.
*/

CREATE TABLE public.billing_razorpay_subscriptions (
  razorpay_subscription_id text PRIMARY KEY,
  user_id                  uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  status                   text NOT NULL CHECK (status IN (
                             'created', 'authenticated', 'active', 'pending', 'halted',
                             'paused', 'cancelled', 'completed', 'expired')),
  plan_id                  text,
  plan                     text CHECK (plan IS NULL OR plan IN ('monthly', 'yearly')),
  current_period_end       timestamptz,
  -- This app's own record that /cancel asked to stop at the period end
  -- (Razorpay's subscription has no such flag).
  cancel_at_period_end     boolean NOT NULL DEFAULT false,
  ended_at                 timestamptz,
  created_at               timestamptz NOT NULL DEFAULT now(),
  updated_at               timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX billing_razorpay_subscriptions_user_idx ON public.billing_razorpay_subscriptions (user_id);

CREATE TABLE public.razorpay_webhook_events (
  event_id     text PRIMARY KEY,
  type         text NOT NULL,
  received_at  timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.billing_razorpay_subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.razorpay_webhook_events ENABLE ROW LEVEL SECURITY;

CREATE POLICY "billing_razorpay_subscriptions owner read" ON public.billing_razorpay_subscriptions
  FOR SELECT TO authenticated USING (auth.uid() = user_id);

REVOKE ALL ON public.billing_razorpay_subscriptions, public.razorpay_webhook_events
  FROM PUBLIC, anon, authenticated;
GRANT SELECT ON public.billing_razorpay_subscriptions TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.billing_razorpay_subscriptions TO service_role;
GRANT SELECT, INSERT, DELETE ON public.razorpay_webhook_events TO service_role;

-- ── has_premium: either provider ─────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.has_premium(p_user_id uuid)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
  SELECT EXISTS (
    SELECT 1 FROM public.billing_subscriptions s
    WHERE s.user_id = p_user_id
      AND s.status IN ('active', 'trialing', 'past_due')
      AND (s.current_period_end IS NULL OR s.current_period_end > now() - interval '3 days')
  ) OR EXISTS (
    SELECT 1 FROM public.billing_razorpay_subscriptions r
    WHERE r.user_id = p_user_id
      AND r.status IN ('active', 'pending')
      AND (r.current_period_end IS NULL OR r.current_period_end > now() - interval '3 days')
  );
$$;

REVOKE EXECUTE ON FUNCTION public.has_premium(uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.has_premium(uuid) TO service_role;

-- ── No deleting an account that is still being charged (either provider) ─────

CREATE OR REPLACE FUNCTION public.refuse_delete_with_live_subscription()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM public.billing_subscriptions s
    WHERE s.user_id = OLD.id
      AND s.status IN ('active', 'trialing', 'past_due', 'unpaid', 'paused', 'incomplete')
  ) OR EXISTS (
    SELECT 1 FROM public.billing_razorpay_subscriptions r
    WHERE r.user_id = OLD.id
      AND r.status IN ('authenticated', 'active', 'pending', 'halted', 'paused')
  ) THEN
    RAISE EXCEPTION 'active_subscription' USING
      ERRCODE = 'P0001',
      HINT = 'Cancel the subscription first (the app does this via POST /api/billing/cancel-for-deletion).';
  END IF;
  RETURN OLD;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.refuse_delete_with_live_subscription() FROM PUBLIC, anon, authenticated;
