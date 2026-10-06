/*
  # Azure Speech ledger: a `probe` source for calibration runs
  (exam-pronunciation plan, Batch 7 — main repo docs/systems/exam-pronunciation.md)

  backend/scripts/probe_exam_pronunciation.py records the Azure responses the
  calibration fixtures replay. Those calls bill the same Azure resource as
  production, so the plan logs them in the ledger as source `probe` — visible
  in azure_speech_usage_summary()'s by_source, and counted toward the monthly
  cap like any other source.

  Only the source list changes: the column CHECK is replaced and
  reserve_azure_seconds is re-created with the same body and the new value.
  Grants are re-stated because CREATE OR REPLACE keeps them, but the REVOKE
  before GRANT convention of 20261005090000 is kept.
*/

ALTER TABLE public.azure_speech_usage DROP CONSTRAINT IF EXISTS azure_speech_usage_source_check;
ALTER TABLE public.azure_speech_usage ADD CONSTRAINT azure_speech_usage_source_check
  CHECK (source IN ('learn', 'exam', 'repair', 'lab', 'shadowing', 'probe'));

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
  IF p_source IS NULL OR p_source NOT IN ('learn', 'exam', 'repair', 'lab', 'shadowing', 'probe') THEN
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
