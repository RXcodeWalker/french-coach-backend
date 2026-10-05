/*
  # Azure Speech usage ledger, monthly budget and exam_pronunciation quota row
  (exam-pronunciation plan, Batch 1 — main repo docs/systems/exam-pronunciation.md
  once Batch 7 writes it)

  Every Azure Speech call this backend makes is metered in seconds of audio,
  so real usage (minutes per exam, Learn's share) can be read from data before
  any limit is chosen. The *mechanism* for a monthly cap ships now; the cap
  itself is left unset (NULL = unlimited) until the owner picks a number.

  1. azure_speech_usage — the ledger. One row per Azure HTTP request
     (a chunked request is one row per chunk). Append-only in the sense that
     rows are never deleted or re-attributed: a row is inserted as 'reserved'
     and moves exactly once, to 'settled' (Azure processed the audio — the
     seconds are what the server measured from the WAV header) or 'released'
     (no billable call happened — the seconds stay for the record but are
     excluded from every total). user_id is ON DELETE SET NULL so monthly
     totals survive account deletion; the table holds no content (no audio,
     no transcript), only seconds and the session/turn coordinates.

     A 'reserved' row whose request died before settling (e.g. the free-plan
     instance spun down mid-call) keeps counting toward the month. That
     over-counts by at most one request's seconds, which is the safe
     direction for a budget.

  2. azure_speech_budget — a single config row. cap_seconds NULL means
     unlimited. To set a cap later (no migration needed):
       UPDATE public.azure_speech_budget SET cap_seconds = 18000;  -- 5 h
     The month is the UTC calendar month.

  3. reserve_azure_seconds / settle_azure_seconds / release_azure_seconds —
     service-role RPCs. reserve takes a transaction-scoped advisory lock, so
     two concurrent reservations cannot both squeeze under the cap.
     azure_speech_usage_summary() feeds GET /api/admin/azure-usage.

  4. ('exam_pronunciation', 1000) in ai_quota_limits — plan Decision 3:
     effectively uncapped, but the row must exist before the exam route
     deploys (ai_usage_grants.feature is an FK; an unseeded feature 503s
     every call).

  Client grants: none. RLS on with no policies; anon/authenticated hold no
  privilege on either table or any function here.

  Conventions as in 20260914090000: SECURITY DEFINER + SET search_path =
  pg_catalog, public, pg_temp; domain errors ERRCODE 22023; jsonb returns;
  REVOKE ... FROM PUBLIC before every GRANT.
*/

-- ── azure_speech_usage ───────────────────────────────────────────────────────

CREATE TABLE public.azure_speech_usage (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     uuid REFERENCES public.profiles(id) ON DELETE SET NULL,
  source      text NOT NULL CHECK (source IN ('learn', 'exam', 'repair', 'lab', 'shadowing')),
  session_id  text,
  part        text,
  turn_key    text,
  seconds     numeric(10, 3) NOT NULL CHECK (seconds >= 0),
  status      text NOT NULL DEFAULT 'reserved' CHECK (status IN ('reserved', 'settled', 'released')),
  created_at  timestamptz NOT NULL DEFAULT now(),
  settled_at  timestamptz
);

CREATE INDEX azure_speech_usage_created_at_idx ON public.azure_speech_usage (created_at);
CREATE INDEX azure_speech_usage_user_idx ON public.azure_speech_usage (user_id);

ALTER TABLE public.azure_speech_usage ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.azure_speech_usage FROM anon, authenticated;
GRANT SELECT ON public.azure_speech_usage TO service_role;

-- ── azure_speech_budget ──────────────────────────────────────────────────────

CREATE TABLE public.azure_speech_budget (
  id           boolean PRIMARY KEY DEFAULT true CHECK (id),
  cap_seconds  integer CHECK (cap_seconds IS NULL OR cap_seconds >= 0),
  updated_at   timestamptz NOT NULL DEFAULT now()
);
INSERT INTO public.azure_speech_budget (id, cap_seconds) VALUES (true, NULL)
ON CONFLICT (id) DO NOTHING;

ALTER TABLE public.azure_speech_budget ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.azure_speech_budget FROM anon, authenticated;
GRANT SELECT ON public.azure_speech_budget TO service_role;

-- ── helpers ──────────────────────────────────────────────────────────────────

-- Start of the current UTC calendar month, as a timestamptz.
CREATE OR REPLACE FUNCTION public._azure_speech_month_start()
RETURNS timestamptz
LANGUAGE sql
STABLE
SET search_path = pg_catalog, public, pg_temp
AS $$
  SELECT date_trunc('month', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc';
$$;

REVOKE EXECUTE ON FUNCTION public._azure_speech_month_start() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public._azure_speech_month_start() FROM anon, authenticated;

-- ── reserve_azure_seconds ────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.reserve_azure_seconds(
  p_user_id    uuid,
  p_source     text,
  p_seconds    numeric,
  p_session_id text DEFAULT NULL,
  p_part       text DEFAULT NULL,
  p_turn_key   text DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_cap integer;
  v_used numeric;
  v_id uuid;
  v_user uuid;
BEGIN
  IF p_source IS NULL OR p_source NOT IN ('learn', 'exam', 'repair', 'lab', 'shadowing') THEN
    RAISE EXCEPTION 'unknown_source' USING ERRCODE = '22023';
  END IF;
  IF p_seconds IS NULL OR p_seconds < 0 THEN
    RAISE EXCEPTION 'invalid_seconds' USING ERRCODE = '22023';
  END IF;

  -- One global lock: the cap is project-wide, not per user.
  PERFORM pg_advisory_xact_lock(hashtext('azure_speech_budget'));

  SELECT cap_seconds INTO v_cap FROM public.azure_speech_budget WHERE id;

  SELECT coalesce(sum(seconds), 0) INTO v_used FROM public.azure_speech_usage
    WHERE status IN ('reserved', 'settled') AND created_at >= public._azure_speech_month_start();

  IF v_cap IS NOT NULL AND v_used + p_seconds > v_cap THEN
    RETURN jsonb_build_object(
      'ok', true, 'granted', false, 'reason', 'budget_exhausted',
      'used_seconds', v_used, 'cap_seconds', v_cap
    );
  END IF;

  -- A user id with no profile row (deleted mid-request) is recorded as NULL
  -- rather than failing the reservation on the FK: the seconds still count.
  SELECT id INTO v_user FROM public.profiles WHERE id = p_user_id;

  INSERT INTO public.azure_speech_usage (user_id, source, session_id, part, turn_key, seconds)
  VALUES (v_user, p_source, p_session_id, p_part, p_turn_key, p_seconds)
  RETURNING id INTO v_id;

  RETURN jsonb_build_object(
    'ok', true, 'granted', true, 'reservation_id', v_id,
    'used_seconds', v_used + p_seconds, 'cap_seconds', v_cap
  );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.reserve_azure_seconds(uuid, text, numeric, text, text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.reserve_azure_seconds(uuid, text, numeric, text, text, text) FROM anon, authenticated;
GRANT EXECUTE ON FUNCTION public.reserve_azure_seconds(uuid, text, numeric, text, text, text) TO service_role;

-- ── settle_azure_seconds ─────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.settle_azure_seconds(
  p_reservation_id uuid,
  p_seconds        numeric
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_updated integer;
BEGIN
  IF p_seconds IS NULL OR p_seconds < 0 THEN
    RAISE EXCEPTION 'invalid_seconds' USING ERRCODE = '22023';
  END IF;

  UPDATE public.azure_speech_usage
    SET status = 'settled', seconds = p_seconds, settled_at = now()
    WHERE id = p_reservation_id AND status = 'reserved';
  GET DIAGNOSTICS v_updated = ROW_COUNT;

  RETURN jsonb_build_object('ok', true, 'settled', v_updated > 0);
END;
$$;

REVOKE EXECUTE ON FUNCTION public.settle_azure_seconds(uuid, numeric) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.settle_azure_seconds(uuid, numeric) FROM anon, authenticated;
GRANT EXECUTE ON FUNCTION public.settle_azure_seconds(uuid, numeric) TO service_role;

-- ── release_azure_seconds ────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.release_azure_seconds(p_reservation_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_updated integer;
BEGIN
  UPDATE public.azure_speech_usage
    SET status = 'released', settled_at = now()
    WHERE id = p_reservation_id AND status = 'reserved';
  GET DIAGNOSTICS v_updated = ROW_COUNT;

  RETURN jsonb_build_object('ok', true, 'released', v_updated > 0);
END;
$$;

REVOKE EXECUTE ON FUNCTION public.release_azure_seconds(uuid) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.release_azure_seconds(uuid) FROM anon, authenticated;
GRANT EXECUTE ON FUNCTION public.release_azure_seconds(uuid) TO service_role;

-- ── azure_speech_usage_summary ───────────────────────────────────────────────
-- Month-to-date seconds by source and per exam session, for
-- GET /api/admin/azure-usage. Aggregated in SQL so the answer is never cut
-- off by PostgREST's row limit.

CREATE OR REPLACE FUNCTION public.azure_speech_usage_summary()
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_start timestamptz := public._azure_speech_month_start();
  v_cap integer;
  v_total numeric;
  v_by_source jsonb;
  v_exams jsonb;
BEGIN
  SELECT cap_seconds INTO v_cap FROM public.azure_speech_budget WHERE id;

  SELECT coalesce(sum(seconds), 0) INTO v_total FROM public.azure_speech_usage
    WHERE status IN ('reserved', 'settled') AND created_at >= v_start;

  SELECT coalesce(jsonb_object_agg(source, total), '{}'::jsonb) INTO v_by_source FROM (
    SELECT source, sum(seconds) AS total FROM public.azure_speech_usage
      WHERE status IN ('reserved', 'settled') AND created_at >= v_start
      GROUP BY source
  ) s;

  SELECT coalesce(jsonb_agg(jsonb_build_object('session_id', session_id, 'seconds', total) ORDER BY total DESC), '[]'::jsonb)
    INTO v_exams FROM (
      SELECT session_id, sum(seconds) AS total FROM public.azure_speech_usage
        WHERE status IN ('reserved', 'settled') AND created_at >= v_start
          AND source = 'exam' AND session_id IS NOT NULL
        GROUP BY session_id
    ) e;

  RETURN jsonb_build_object(
    'month_start', v_start,
    'cap_seconds', v_cap,
    'total_seconds', v_total,
    'by_source', v_by_source,
    'exams', v_exams
  );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.azure_speech_usage_summary() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.azure_speech_usage_summary() FROM anon, authenticated;
GRANT EXECUTE ON FUNCTION public.azure_speech_usage_summary() TO service_role;

-- ── exam_pronunciation quota row ─────────────────────────────────────────────

INSERT INTO public.ai_quota_limits (feature, daily_limit) VALUES
  ('exam_pronunciation', 1000)
ON CONFLICT (feature) DO NOTHING;
