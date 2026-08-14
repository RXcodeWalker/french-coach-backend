/*
  # Daily Challenge Phase 1 — RPCs (revised plan §Fix 2, §"Full corrected
  backend schema/RPC set" part 4)

  start_daily_challenge (Fix 2): reserves a server-minted session_id for
  (user, challenge_date) BEFORE the exam runs. ExamMode is wired to use this
  session_id verbatim instead of minting its own `exam-sim-${Date.now()}`
  (see ExamMode.tsx changes) — this is what lets
  submit_daily_challenge_attempt later prove "this exact run was entered
  through the Daily Challenge flow," not just "the question set happens to
  match." Idempotent reservation via ON CONFLICT DO NOTHING: re-entering the
  flow (back button, reload before scoring) returns the SAME session_id
  rather than minting a new one each time, so a stale in-flight transcript
  submitted under the first-issued session_id is still valid.

  submit_daily_challenge_attempt: the score-binding RPC. Never trusts a
  client-sent score — pulls total from scoring_envelopes.envelope->>'total'
  (server-computed by POST /score) after verifying every provenance check in
  order:
    1. auth
    2. challenge exists for p_challenge_date
    3. envelope exists AND is owned by the caller (unknown_envelope covers
       both "doesn't exist" and "wrong owner" — mirrors mint_gems_from_envelope's
       verified precedent of not leaking cross-user existence)
    4. content_provenance = 'original-practice' (envelope's own redistributability
       gate, same as mint_gems_from_envelope)
    5. envelope's questionSetId matches the day's assigned question_set_id
    6. envelope was created on the assigned UTC day (challenge_date)
    7. envelope's session_id matches the one reserved by start_daily_challenge
       for this user/date (Fix 2's core check — session_not_bound rejects a
       coincidental same-day ExamMode practice run on the same set, since its
       sessionId is `exam-sim-<timestamp>`, never `daily-<uuid>`)
    8. not already claimed today (UNIQUE(user_id, challenge_date) backs this;
       checked explicitly first so a repeat call returns a clean idempotent
       "already claimed" result rather than a raw unique-violation)
    9. row-lock profiles (mirrors mint_gems_from_envelope's FOR UPDATE, even
       though this RPC doesn't write profiles directly today — kept for
       consistency with the lock-before-any-side-effect convention the other
       mutating RPCs in this schema use)
   10. insert daily_challenge_attempts
   11. internal award_xp(...) call — works despite award_xp having no
       `authenticated` grant, because a function-to-function call inside
       another SECURITY DEFINER function runs with the callee's effective
       (owner) role, not PostgREST's caller-role grant check (see Fix 1's
       migration header for the full explanation).

  XP amount: mirrors mint_gems_from_envelope's total/40-fraction pattern,
  scaled to a flat, generous daily-challenge bonus (50) since this is a
  once-a-day event, not a per-answer mint — GREATEST(1, ...) floor so a
  zero-score attempt still awards something for completing the day's
  challenge, consistent with mint_gems_from_envelope's own zero-score floor
  behavior.
*/

CREATE OR REPLACE FUNCTION public.start_daily_challenge(p_challenge_date date)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid;
  v_challenge public.daily_challenge_assignments%ROWTYPE;
  v_session_id text;
BEGIN
  me := auth.uid();
  IF me IS NULL THEN RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000'; END IF;

  SELECT * INTO v_challenge FROM public.daily_challenge_assignments WHERE challenge_date = p_challenge_date;
  IF NOT FOUND THEN RAISE EXCEPTION 'unknown_challenge' USING ERRCODE = '22023'; END IF;

  IF EXISTS (SELECT 1 FROM public.daily_challenge_attempts
             WHERE user_id = me AND challenge_date = p_challenge_date) THEN
    RAISE EXCEPTION 'already_completed' USING ERRCODE = '22023';
  END IF;

  INSERT INTO public.daily_challenge_sessions (user_id, challenge_date, session_id)
  VALUES (me, p_challenge_date, 'daily-' || gen_random_uuid()::text)
  ON CONFLICT (user_id, challenge_date) DO NOTHING;

  SELECT session_id INTO v_session_id FROM public.daily_challenge_sessions
    WHERE user_id = me AND challenge_date = p_challenge_date;

  RETURN jsonb_build_object(
    'ok', true, 'session_id', v_session_id, 'question_set_id', v_challenge.question_set_id
  );
END;
$$;
REVOKE EXECUTE ON FUNCTION public.start_daily_challenge(date) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.start_daily_challenge(date) TO authenticated;

CREATE OR REPLACE FUNCTION public.submit_daily_challenge_attempt(p_challenge_date date, p_attempt_id text)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid;
  v_challenge public.daily_challenge_assignments%ROWTYPE;
  v_envelope public.scoring_envelopes%ROWTYPE;
  v_reserved_session_id text;
  v_envelope_question_set_id text;
  v_total numeric;
  v_amount int;
  v_row_id uuid;
BEGIN
  me := auth.uid();
  IF me IS NULL THEN RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000'; END IF;

  SELECT * INTO v_challenge FROM public.daily_challenge_assignments WHERE challenge_date = p_challenge_date;
  IF NOT FOUND THEN RAISE EXCEPTION 'unknown_challenge' USING ERRCODE = '22023'; END IF;

  -- Idempotent short-circuit: a repeat call (e.g. the claim-recovery retry
  -- path) for an already-claimed day returns the existing result rather than
  -- hitting the UNIQUE(user_id, challenge_date) violation.
  SELECT id INTO v_row_id FROM public.daily_challenge_attempts
    WHERE user_id = me AND challenge_date = p_challenge_date;
  IF FOUND THEN
    RETURN jsonb_build_object('ok', true, 'already_claimed', true);
  END IF;

  SELECT * INTO v_envelope FROM public.scoring_envelopes
    WHERE attempt_id = p_attempt_id AND user_id = me;
  IF NOT FOUND THEN RAISE EXCEPTION 'unknown_envelope' USING ERRCODE = '22023'; END IF;

  IF v_envelope.content_provenance <> 'original-practice' THEN
    RAISE EXCEPTION 'not_original_practice' USING ERRCODE = '22023';
  END IF;

  v_envelope_question_set_id := v_envelope.envelope->>'questionSetId';
  IF v_envelope_question_set_id IS DISTINCT FROM v_challenge.question_set_id THEN
    RAISE EXCEPTION 'question_set_mismatch' USING ERRCODE = '22023';
  END IF;

  IF (v_envelope.created_at AT TIME ZONE 'UTC')::date <> p_challenge_date THEN
    RAISE EXCEPTION 'wrong_day' USING ERRCODE = '22023';
  END IF;

  SELECT session_id INTO v_reserved_session_id FROM public.daily_challenge_sessions
    WHERE user_id = me AND challenge_date = p_challenge_date;
  IF v_reserved_session_id IS NULL OR v_envelope.session_id <> v_reserved_session_id THEN
    RAISE EXCEPTION 'session_not_bound' USING ERRCODE = '22023';
  END IF;

  v_total := (v_envelope.envelope->>'total')::numeric;
  IF v_total IS NULL OR v_total < 0 OR v_total > 40 THEN
    RAISE EXCEPTION 'invalid_envelope_total' USING ERRCODE = '22023';
  END IF;

  PERFORM 1 FROM public.profiles WHERE id = me FOR UPDATE;

  v_amount := GREATEST(1, ROUND(v_total / 40 * 50));

  INSERT INTO public.daily_challenge_attempts (user_id, challenge_date, attempt_id, score_total, xp_awarded)
  VALUES (me, p_challenge_date, p_attempt_id, v_total, v_amount)
  RETURNING id INTO v_row_id;

  PERFORM public.award_xp(
    me, 'daily_challenge', v_amount,
    me::text || ':daily_challenge:' || p_challenge_date::text,
    jsonb_build_object('challenge_date', p_challenge_date, 'attempt_id', p_attempt_id)
  );

  RETURN jsonb_build_object('ok', true, 'already_claimed', false, 'xp_awarded', v_amount, 'score_total', v_total);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.submit_daily_challenge_attempt(date, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.submit_daily_challenge_attempt(date, text) TO authenticated;
