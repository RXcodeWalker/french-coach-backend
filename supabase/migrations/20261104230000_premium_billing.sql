/*
  # Premium subscriptions (Stripe) — owner decision 2026-11-03

  What is sold: a monthly/yearly Premium subscription that raises the launch
  usage limits (main repo docs/systems/payments.md, ADR 0014). Gems are NOT sold
  (shop-economy.md §10: no real-money gems before Phase 7).

  Tables (all written only by the FastAPI service with the service-role key,
  from Stripe's signed webhooks — never by a browser):

  - billing_customers      one Stripe customer per account.
  - billing_subscriptions  the Stripe subscription mirror. Stripe is the source
                           of truth; the webhook re-reads the subscription from
                           Stripe and upserts it here, so event order never
                           matters.
  - stripe_webhook_events  the event ids already processed (idempotency: Stripe
                           delivers at least once and retries for 3 days).

  Premium limits are config, not code: ai_quota_limits.premium_daily_limit.
  NULL = Premium gets the same limit as everyone else. Change the numbers with
  an UPDATE; no deploy needed.

  has_premium(user) = a subscription whose status is active, trialing or
  past_due (Stripe is still retrying the card — access stays while it does),
  and whose current period has not ended more than 3 days ago (a safety net if
  the "deleted" webhook is ever lost; 3 days = Stripe's webhook retry window).

  consume_ai_quota / release_ai_quota_grant are redefined only to read the
  Premium limit; every other line is unchanged from 20260914090000.

  Deleting an account with a live subscription is refused (trigger on
  profiles): the app cancels the subscription first (POST
  /api/billing/cancel-for-deletion), so nobody is charged for a deleted
  account.

  Rollback: re-run 20260914090000's two function bodies, then DROP the
  trigger, the function has_premium, the three tables and the column.
*/

-- ── Tables ───────────────────────────────────────────────────────────────────

CREATE TABLE public.billing_customers (
  user_id            uuid PRIMARY KEY REFERENCES public.profiles(id) ON DELETE CASCADE,
  stripe_customer_id text NOT NULL UNIQUE,
  created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE public.billing_subscriptions (
  stripe_subscription_id text PRIMARY KEY,
  user_id                uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  stripe_customer_id     text NOT NULL,
  status                 text NOT NULL CHECK (status IN (
                           'incomplete', 'incomplete_expired', 'trialing', 'active',
                           'past_due', 'canceled', 'unpaid', 'paused')),
  price_id               text,
  plan                   text CHECK (plan IS NULL OR plan IN ('monthly', 'yearly')),
  current_period_end     timestamptz,
  cancel_at_period_end   boolean NOT NULL DEFAULT false,
  canceled_at            timestamptz,
  updated_at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX billing_subscriptions_user_idx ON public.billing_subscriptions (user_id);

CREATE TABLE public.stripe_webhook_events (
  event_id     text PRIMARY KEY,
  type         text NOT NULL,
  received_at  timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.billing_customers ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.billing_subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.stripe_webhook_events ENABLE ROW LEVEL SECURITY;

-- A signed-in user may read their own subscription; nobody but the service
-- role writes anything. No policy at all on the other two tables.
CREATE POLICY "billing_subscriptions owner read" ON public.billing_subscriptions
  FOR SELECT TO authenticated USING (auth.uid() = user_id);

REVOKE ALL ON public.billing_customers, public.billing_subscriptions, public.stripe_webhook_events
  FROM PUBLIC, anon, authenticated;
GRANT SELECT ON public.billing_subscriptions TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.billing_customers TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.billing_subscriptions TO service_role;
GRANT SELECT, INSERT, DELETE ON public.stripe_webhook_events TO service_role;

-- ── Premium limits (config) ──────────────────────────────────────────────────

ALTER TABLE public.ai_quota_limits ADD COLUMN premium_daily_limit integer
  CHECK (premium_daily_limit IS NULL OR premium_daily_limit >= 0);

-- Owner's starting numbers (2026-11-03). exam_attempt is per UTC day: Premium
-- skips the free 1-per-7-days window (server/examAllowance.ts), so 1/day.
UPDATE public.ai_quota_limits SET premium_daily_limit = 30 WHERE feature = 'learn_answer';
UPDATE public.ai_quota_limits SET premium_daily_limit = 1  WHERE feature = 'exam_attempt';
UPDATE public.ai_quota_limits SET premium_daily_limit = 60 WHERE feature = 'pronunciation_lab';
-- Transcription rows are already well above these (learn_transcribe 200,
-- exam_turn_transcribe 150), so they stay NULL (same limit for everyone).
-- Setting a premium value LOWER than daily_limit would cut Premium users back.

-- ── has_premium ──────────────────────────────────────────────────────────────

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
  );
$$;

-- Service role only: a client could otherwise ask about any user id.
REVOKE EXECUTE ON FUNCTION public.has_premium(uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.has_premium(uuid) TO service_role;

-- ── consume_ai_quota (Premium-aware) ─────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.consume_ai_quota(
  p_user_id uuid,
  p_feature text,
  p_idempotency_key text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_invite_status text;
  v_limit integer;
  v_premium_limit integer;
  v_date date;
  v_id text;
  v_used integer;
BEGIN
  IF p_user_id IS NULL THEN
    RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000';
  END IF;
  IF p_feature IS NULL OR btrim(p_feature) = '' THEN
    RAISE EXCEPTION 'missing_feature' USING ERRCODE = '22023';
  END IF;
  IF p_idempotency_key IS NULL OR btrim(p_idempotency_key) = '' THEN
    RAISE EXCEPTION 'missing_idempotency_key' USING ERRCODE = '22023';
  END IF;

  SELECT invite_status INTO v_invite_status FROM public.profiles WHERE id = p_user_id;
  IF v_invite_status IS DISTINCT FROM 'redeemed' THEN
    RETURN jsonb_build_object('ok', true, 'granted', false, 'reason', 'invite_not_redeemed');
  END IF;

  SELECT daily_limit, premium_daily_limit INTO v_limit, v_premium_limit
    FROM public.ai_quota_limits WHERE feature = p_feature;
  IF v_limit IS NULL THEN
    RAISE EXCEPTION 'unknown_feature' USING ERRCODE = '22023';
  END IF;
  IF v_premium_limit IS NOT NULL AND public.has_premium(p_user_id) THEN
    v_limit := v_premium_limit;
  END IF;

  v_id := p_user_id::text || ':' || p_feature || ':' || p_idempotency_key;

  IF EXISTS (SELECT 1 FROM public.ai_usage_grants WHERE id = v_id) THEN
    SELECT count(*) INTO v_used FROM public.ai_usage_grants
      WHERE user_id = p_user_id AND feature = p_feature AND grant_date = (now() AT TIME ZONE 'Asia/Kolkata')::date;
    RETURN jsonb_build_object('ok', true, 'granted', true, 'replayed', true, 'used', v_used, 'limit', v_limit);
  END IF;

  PERFORM pg_advisory_xact_lock(hashtext('ai_quota:' || p_feature || ':' || p_user_id::text));

  v_date := (now() AT TIME ZONE 'Asia/Kolkata')::date;

  SELECT count(*) INTO v_used FROM public.ai_usage_grants
    WHERE user_id = p_user_id AND feature = p_feature AND grant_date = v_date;

  IF v_used >= v_limit THEN
    RETURN jsonb_build_object('ok', true, 'granted', false, 'used', v_used, 'limit', v_limit, 'reason', 'daily_quota_reached');
  END IF;

  INSERT INTO public.ai_usage_grants (id, user_id, feature, grant_date)
  VALUES (v_id, p_user_id, p_feature, v_date)
  ON CONFLICT (id) DO NOTHING;

  RETURN jsonb_build_object('ok', true, 'granted', true, 'replayed', false, 'used', v_used + 1, 'limit', v_limit);
END;
$$;

REVOKE EXECUTE ON FUNCTION public.consume_ai_quota(uuid, text, text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.consume_ai_quota(uuid, text, text) TO service_role;

-- ── release_ai_quota_grant (Premium-aware limit in its reply) ────────────────

CREATE OR REPLACE FUNCTION public.release_ai_quota_grant(
  p_user_id uuid,
  p_feature text,
  p_idempotency_key text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_limit integer;
  v_premium_limit integer;
  v_date date;
  v_id text;
  v_deleted integer;
  v_used integer;
BEGIN
  IF p_user_id IS NULL THEN
    RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000';
  END IF;
  IF p_feature IS NULL OR btrim(p_feature) = '' THEN
    RAISE EXCEPTION 'missing_feature' USING ERRCODE = '22023';
  END IF;
  IF p_idempotency_key IS NULL OR btrim(p_idempotency_key) = '' THEN
    RAISE EXCEPTION 'missing_idempotency_key' USING ERRCODE = '22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtext('ai_quota:' || p_feature || ':' || p_user_id::text));

  v_date := (now() AT TIME ZONE 'Asia/Kolkata')::date;
  v_id := p_user_id::text || ':' || p_feature || ':' || p_idempotency_key;

  DELETE FROM public.ai_usage_grants WHERE id = v_id;
  GET DIAGNOSTICS v_deleted = ROW_COUNT;

  SELECT daily_limit, premium_daily_limit INTO v_limit, v_premium_limit
    FROM public.ai_quota_limits WHERE feature = p_feature;
  IF v_premium_limit IS NOT NULL AND public.has_premium(p_user_id) THEN
    v_limit := v_premium_limit;
  END IF;
  SELECT count(*) INTO v_used FROM public.ai_usage_grants
    WHERE user_id = p_user_id AND feature = p_feature AND grant_date = v_date;

  RETURN jsonb_build_object('ok', true, 'released', v_deleted > 0, 'used', v_used, 'limit', v_limit);
END;
$$;

REVOKE EXECUTE ON FUNCTION public.release_ai_quota_grant(uuid, text, text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.release_ai_quota_grant(uuid, text, text) TO service_role;

-- ── No deleting an account that is still being charged ───────────────────────

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
  ) THEN
    RAISE EXCEPTION 'active_subscription' USING
      ERRCODE = 'P0001',
      HINT = 'Cancel the Stripe subscription first (the app does this via POST /api/billing/cancel-for-deletion).';
  END IF;
  RETURN OLD;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.refuse_delete_with_live_subscription() FROM PUBLIC, anon, authenticated;

CREATE TRIGGER profiles_refuse_delete_with_live_subscription
  BEFORE DELETE ON public.profiles
  FOR EACH ROW EXECUTE FUNCTION public.refuse_delete_with_live_subscription();
