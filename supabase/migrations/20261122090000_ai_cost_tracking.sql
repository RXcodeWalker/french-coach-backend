/*
  # AI cost per active user and gross margin

  Premium is only a business if cost-per-active-user is known and below what a
  subscriber pays. This migration is the measuring instrument; nothing in it
  changes what any user can do.

  1. ai_usage_events — one row per *measured* Groq / Gemini / Whisper call, in
     raw units (tokens, audio seconds), never dollars. lib/ai_cost.py writes it
     from two SDK-level hooks, so a new call site is metered without editing
     it. user_id is ON DELETE SET NULL (totals survive account deletion);
     guests have user_id NULL + is_guest. No prompt, transcript or audio is
     stored — only counts.

  2. ai_model_pricing — list prices per (provider, model, kind). Dollars are
     applied at READ time, so correcting a rate re-prices all history. Rows
     seeded here are `verified = false`: the owner must confirm each against the
     provider's price page. A model with no row is reported as `unpriced`,
     never silently costed at 0.

  3. ai_cost_settings — one config row: the Premium price (NULL until the owner
     sets it), payment-processor fees, and Azure rates (0 while on the free F0
     tier). Azure seconds/characters are read from the existing ledgers
     (azure_speech_usage, azure_tts_usage), not duplicated here.

  4. ai_cost_summary(from, to) — service-role only; feeds GET /api/admin/ai-cost.
     Returns active users, cost per active user (mean, p50, p90, top spenders),
     free vs Premium cohorts, and gross margin.

  Definitions (the numbers are only comparable if these stay fixed):
    * active user  = a signed-in user with >= 1 measured AI call in the window.
    * AI cost      = events priced at the table's rates + Azure ledgers at the
                     settings' rates. It is LIST price: while a provider's free
                     tier covers the calls the real invoice is lower, but the
                     margin you'd have at paid-tier prices is what matters.
    * premium      = has_premium() right now (status active/trialing/past_due),
                     applied to the window's users — an approximation for a
                     window in which someone upgraded or cancelled.
    * revenue      = paying subscribers (Stripe or Razorpay status 'active')
                     x premium_price_usd_monthly x window days / 30. Trials earn
                     nothing but their AI cost still counts, which is the point
                     of running a trial. Convert INR to USD yourself when you
                     set the price.
    * gross margin = (revenue - payment fees - premium cohort AI cost) / revenue.
                     `blended` also subtracts the free cohort's AI cost, i.e.
                     what it costs to run the free tier that feeds Premium.

  Set the numbers (no migration needed):
    UPDATE public.ai_cost_settings SET premium_price_usd_monthly = 4.99;
    UPDATE public.ai_cost_settings SET payment_fee_percent = 2.9, payment_fee_fixed_usd = 0.30;
    UPDATE public.ai_model_pricing SET usd_per_m_input = ..., verified = true WHERE ...;
    INSERT INTO public.ai_model_pricing (provider, model, kind, usd_per_m_input, usd_per_m_output)
      VALUES ('gemini', 'gemini-3.6-flash', 'chat', <in>, <out>);

  Client grants: none. RLS on, no policies; service_role only.
  Rollback: DROP FUNCTION public.ai_cost_summary(timestamptz, timestamptz);
            DROP TABLE public.ai_usage_events, public.ai_model_pricing, public.ai_cost_settings;
*/

-- ── ai_usage_events ──────────────────────────────────────────────────────────

CREATE TABLE public.ai_usage_events (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  created_at    timestamptz NOT NULL DEFAULT now(),
  user_id       uuid REFERENCES public.profiles(id) ON DELETE SET NULL,
  is_guest      boolean NOT NULL DEFAULT false,
  -- The ai_quota_limits feature in force when the call was made; 'unattributed'
  -- for work no user asked for (health probes, startup).
  feature       text NOT NULL DEFAULT 'unattributed',
  provider      text NOT NULL CHECK (provider IN ('groq', 'gemini')),
  model         text NOT NULL,
  kind          text NOT NULL CHECK (kind IN ('chat', 'audio')),
  input_tokens  integer NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
  -- Includes reasoning/thinking tokens: providers bill them as output.
  output_tokens integer NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
  audio_seconds numeric(10, 3) NOT NULL DEFAULT 0 CHECK (audio_seconds >= 0),
  -- false = the provider's response carried no usage figure, so this call's
  -- cost is unknown (counted, never guessed).
  measured      boolean NOT NULL DEFAULT true
);

CREATE INDEX ai_usage_events_created_at_idx ON public.ai_usage_events (created_at);
CREATE INDEX ai_usage_events_user_idx ON public.ai_usage_events (user_id, created_at);

ALTER TABLE public.ai_usage_events ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.ai_usage_events FROM anon, authenticated;
GRANT SELECT, INSERT ON public.ai_usage_events TO service_role;

-- ── ai_model_pricing ─────────────────────────────────────────────────────────

CREATE TABLE public.ai_model_pricing (
  provider            text NOT NULL CHECK (provider IN ('groq', 'gemini')),
  model               text NOT NULL,
  kind                text NOT NULL CHECK (kind IN ('chat', 'audio')),
  usd_per_m_input     numeric(12, 4) CHECK (usd_per_m_input >= 0),
  usd_per_m_output    numeric(12, 4) CHECK (usd_per_m_output >= 0),
  usd_per_audio_hour  numeric(12, 4) CHECK (usd_per_audio_hour >= 0),
  -- Providers that round each request up to a floor (Groq Whisper: 10 s).
  min_billed_seconds  numeric(8, 3) NOT NULL DEFAULT 0 CHECK (min_billed_seconds >= 0),
  verified            boolean NOT NULL DEFAULT false,
  note                text,
  updated_at          timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (provider, model, kind),
  CHECK (
    (kind = 'chat'  AND usd_per_m_input IS NOT NULL AND usd_per_m_output IS NOT NULL)
    OR
    (kind = 'audio' AND usd_per_audio_hour IS NOT NULL)
  )
);

ALTER TABLE public.ai_model_pricing ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.ai_model_pricing FROM anon, authenticated;
GRANT SELECT ON public.ai_model_pricing TO service_role;

-- Seeded from the owner-side defaults in lib/model_config.py. These are the
-- developer's recollection of list prices, NOT checked against the price page:
-- verified = false until the owner flips it. Gemini has no row on purpose (the
-- model ID is env-driven and I'd be guessing): its calls show up as `unpriced`
-- until a row is inserted.
INSERT INTO public.ai_model_pricing
  (provider, model, kind, usd_per_m_input, usd_per_m_output, usd_per_audio_hour, min_billed_seconds, verified, note)
VALUES
  ('groq', 'openai/gpt-oss-120b', 'chat',  0.15, 0.60, NULL, 0,  false,
   'UNVERIFIED list price - confirm on the Groq pricing page'),
  ('groq', 'whisper-large-v3',    'audio', NULL, NULL, 0.111, 10, false,
   'UNVERIFIED list price and 10 s per-request minimum - confirm on the Groq pricing page')
ON CONFLICT (provider, model, kind) DO NOTHING;

-- ── ai_cost_settings ─────────────────────────────────────────────────────────

CREATE TABLE public.ai_cost_settings (
  id                         boolean PRIMARY KEY DEFAULT true CHECK (id),
  -- NULL until Premium has a price: revenue and margin are then reported as null.
  premium_price_usd_monthly  numeric(10, 2) CHECK (premium_price_usd_monthly IS NULL OR premium_price_usd_monthly >= 0),
  payment_fee_percent        numeric(5, 2) NOT NULL DEFAULT 0 CHECK (payment_fee_percent >= 0),
  payment_fee_fixed_usd      numeric(6, 2) NOT NULL DEFAULT 0 CHECK (payment_fee_fixed_usd >= 0),
  -- Azure F0 is free, so these are 0 until the resource moves to a paid tier.
  azure_speech_usd_per_hour  numeric(10, 4) NOT NULL DEFAULT 0 CHECK (azure_speech_usd_per_hour >= 0),
  azure_tts_usd_per_m_chars  numeric(10, 4) NOT NULL DEFAULT 0 CHECK (azure_tts_usd_per_m_chars >= 0),
  updated_at                 timestamptz NOT NULL DEFAULT now()
);
INSERT INTO public.ai_cost_settings (id) VALUES (true) ON CONFLICT (id) DO NOTHING;

ALTER TABLE public.ai_cost_settings ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.ai_cost_settings FROM anon, authenticated;
GRANT SELECT ON public.ai_cost_settings TO service_role;

-- ── ai_cost_summary ──────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.ai_cost_summary(
  p_from timestamptz DEFAULT now() - interval '30 days',
  p_to   timestamptz DEFAULT now()
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  s               public.ai_cost_settings%ROWTYPE;
  v_days          numeric := greatest(extract(epoch FROM (p_to - p_from)) / 86400.0, 0.0001);
  v_result        jsonb;
BEGIN
  IF p_to <= p_from THEN
    RAISE EXCEPTION 'p_to must be after p_from' USING ERRCODE = '22023';
  END IF;

  SELECT * INTO s FROM public.ai_cost_settings WHERE id;

  WITH
  -- Each event priced at READ time. unpriced = no pricing row for its model.
  priced AS (
    SELECT
      e.user_id, e.is_guest, e.feature, e.provider, e.model, e.measured,
      (p.model IS NULL) AS unpriced,
      CASE
        WHEN p.model IS NULL OR NOT e.measured THEN 0
        WHEN e.kind = 'chat' THEN
          e.input_tokens  / 1000000.0 * p.usd_per_m_input
          + e.output_tokens / 1000000.0 * p.usd_per_m_output
        ELSE greatest(e.audio_seconds, p.min_billed_seconds) / 3600.0 * p.usd_per_audio_hour
      END AS usd
    FROM public.ai_usage_events e
    LEFT JOIN public.ai_model_pricing p
      ON p.provider = e.provider AND p.model = e.model AND p.kind = e.kind
    WHERE e.created_at >= p_from AND e.created_at < p_to
  ),
  -- Azure is attributed to a user from its own ledgers (settled/reserved only).
  azure AS (
    SELECT user_id, sum(seconds) / 3600.0 * s.azure_speech_usd_per_hour AS usd
      FROM public.azure_speech_usage
      WHERE status IN ('reserved', 'settled') AND created_at >= p_from AND created_at < p_to
      GROUP BY user_id
    UNION ALL
    SELECT user_id, sum(characters) / 1000000.0 * s.azure_tts_usd_per_m_chars
      FROM public.azure_tts_usage
      WHERE status = 'reserved' AND created_at >= p_from AND created_at < p_to
      GROUP BY user_id
  ),
  -- Everything that cost something, per signed-in user.
  per_user AS (
    SELECT u.user_id,
           sum(u.usd) AS usd,
           public.has_premium(u.user_id) AS premium
    FROM (
      SELECT user_id, usd FROM priced WHERE user_id IS NOT NULL
      UNION ALL
      SELECT user_id, usd FROM azure WHERE user_id IS NOT NULL
    ) u
    GROUP BY u.user_id
  ),
  -- Active = at least one measured AI call (Azure alone does not make a user active).
  active AS (
    SELECT pu.* FROM per_user pu
    WHERE EXISTS (SELECT 1 FROM priced p WHERE p.user_id = pu.user_id)
  ),
  paying AS (
    SELECT count(DISTINCT user_id) AS n FROM (
      SELECT user_id FROM public.billing_subscriptions WHERE status = 'active'
      UNION
      SELECT user_id FROM public.billing_razorpay_subscriptions WHERE status = 'active'
    ) x
  ),
  trialing AS (
    SELECT count(DISTINCT user_id) AS n FROM public.billing_subscriptions WHERE status = 'trialing'
  ),
  totals AS (
    SELECT
      (SELECT coalesce(sum(usd), 0) FROM priced)                               AS provider_usd,
      (SELECT coalesce(sum(usd), 0) FROM azure)                                AS azure_usd,
      (SELECT coalesce(sum(usd), 0) FROM priced WHERE is_guest)                AS guest_usd,
      (SELECT coalesce(sum(usd), 0) FROM priced
         WHERE NOT is_guest AND user_id IS NULL)                               AS unattributed_usd,
      (SELECT count(*) FROM priced)                                            AS calls,
      (SELECT count(*) FROM priced WHERE unpriced)                             AS unpriced_calls,
      (SELECT count(*) FROM priced WHERE NOT measured)                         AS unmeasured_calls,
      (SELECT count(*) FROM priced WHERE is_guest)                             AS guest_calls,
      (SELECT count(*) FROM active)                                            AS active_users,
      (SELECT coalesce(sum(usd), 0) FROM active)                               AS active_usd,
      (SELECT count(*) FROM active WHERE premium)                              AS premium_active,
      (SELECT coalesce(sum(usd), 0) FROM active WHERE premium)                 AS premium_usd,
      (SELECT count(*) FROM active WHERE NOT premium)                          AS free_active,
      (SELECT coalesce(sum(usd), 0) FROM active WHERE NOT premium)             AS free_usd,
      (SELECT coalesce(percentile_cont(0.5) WITHIN GROUP (ORDER BY usd), 0)::numeric FROM active) AS p50,
      (SELECT coalesce(percentile_cont(0.9) WITHIN GROUP (ORDER BY usd), 0)::numeric FROM active) AS p90,
      (SELECT n FROM paying)                                                   AS paying_users,
      (SELECT n FROM trialing)                                                 AS trialing_users
  ),
  by_feature AS (
    SELECT coalesce(jsonb_agg(jsonb_build_object('feature', feature, 'calls', calls, 'usd', round(usd, 6))
                              ORDER BY usd DESC), '[]'::jsonb) AS j
    FROM (SELECT feature, count(*) AS calls, sum(usd) AS usd FROM priced GROUP BY feature) f
  ),
  by_model AS (
    SELECT coalesce(jsonb_agg(jsonb_build_object('provider', provider, 'model', model, 'calls', calls,
                                                 'usd', round(usd, 6), 'unpriced', unpriced)
                              ORDER BY usd DESC), '[]'::jsonb) AS j
    FROM (SELECT provider, model, bool_or(unpriced) AS unpriced, count(*) AS calls, sum(usd) AS usd
          FROM priced GROUP BY provider, model) m
  ),
  -- User ids only, never names or emails: enough to find a heavy user in Supabase.
  top_users AS (
    SELECT coalesce(jsonb_agg(jsonb_build_object('user_id', user_id, 'usd', round(usd, 6), 'premium', premium)
                              ORDER BY usd DESC), '[]'::jsonb) AS j
    FROM (SELECT user_id, usd, premium FROM active ORDER BY usd DESC LIMIT 10) t
  )
  SELECT jsonb_build_object(
    'from', p_from,
    'to', p_to,
    'days', round(v_days, 2),
    'complete', (t.unpriced_calls = 0 AND t.unmeasured_calls = 0),
    'calls', t.calls,
    'unpriced_calls', t.unpriced_calls,
    'unmeasured_calls', t.unmeasured_calls,
    'total_usd', round(t.provider_usd + t.azure_usd, 6),
    'provider_usd', round(t.provider_usd, 6),
    'azure_usd', round(t.azure_usd, 6),
    'guest_usd', round(t.guest_usd, 6),
    'guest_calls', t.guest_calls,
    'unattributed_usd', round(t.unattributed_usd, 6),
    'active_users', t.active_users,
    'cost_per_active_user_usd',
      CASE WHEN t.active_users > 0 THEN round(t.active_usd / t.active_users, 6) END,
    'cost_per_active_user_p50_usd', CASE WHEN t.active_users > 0 THEN round(t.p50, 6) END,
    'cost_per_active_user_p90_usd', CASE WHEN t.active_users > 0 THEN round(t.p90, 6) END,
    -- Per 30 days, so windows of different length compare.
    'cost_per_active_user_30d_usd',
      CASE WHEN t.active_users > 0 THEN round(t.active_usd / t.active_users / v_days * 30, 6) END,
    'free', jsonb_build_object(
      'active_users', t.free_active,
      'usd', round(t.free_usd, 6),
      'cost_per_user_usd', CASE WHEN t.free_active > 0 THEN round(t.free_usd / t.free_active, 6) END),
    'premium', jsonb_build_object(
      'active_users', t.premium_active,
      'usd', round(t.premium_usd, 6),
      'cost_per_user_usd', CASE WHEN t.premium_active > 0 THEN round(t.premium_usd / t.premium_active, 6) END,
      'paying_subscribers', t.paying_users,
      'trialing_subscribers', t.trialing_users),
    'margin', CASE WHEN s.premium_price_usd_monthly IS NULL THEN
        jsonb_build_object('price_set', false)
      ELSE
        (WITH r AS (
           SELECT t.paying_users * s.premium_price_usd_monthly * v_days / 30.0 AS revenue,
                  (t.paying_users * s.premium_price_usd_monthly * s.payment_fee_percent / 100.0
                     + t.paying_users * s.payment_fee_fixed_usd) * v_days / 30.0 AS fees
         )
         SELECT jsonb_build_object(
           'price_set', true,
           'premium_price_usd_monthly', s.premium_price_usd_monthly,
           'revenue_usd', round(r.revenue, 4),
           'payment_fees_usd', round(r.fees, 4),
           'premium_ai_cost_usd', round(t.premium_usd, 4),
           -- Null while nobody pays: a margin on zero revenue is meaningless.
           'gross_margin_pct',
             CASE WHEN r.revenue > 0
                  THEN round((r.revenue - r.fees - t.premium_usd) / r.revenue * 100, 2) END,
           'blended_gross_margin_pct',
             CASE WHEN r.revenue > 0
                  THEN round((r.revenue - r.fees - t.premium_usd - t.free_usd) / r.revenue * 100, 2) END,
           -- What one subscriber can cost per month before margin hits 0.
           'break_even_cost_per_subscriber_usd',
             round(s.premium_price_usd_monthly * (1 - s.payment_fee_percent / 100.0) - s.payment_fee_fixed_usd, 4))
         FROM r)
      END,
    'by_feature', (SELECT j FROM by_feature),
    'by_model', (SELECT j FROM by_model),
    'top_users', (SELECT j FROM top_users)
  ) INTO v_result
  FROM totals t;

  RETURN v_result;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.ai_cost_summary(timestamptz, timestamptz) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.ai_cost_summary(timestamptz, timestamptz) FROM anon, authenticated;
GRANT EXECUTE ON FUNCTION public.ai_cost_summary(timestamptz, timestamptz) TO service_role;
