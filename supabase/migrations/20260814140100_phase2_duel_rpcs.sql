/*
  # Friend Duels Phase 2 — RPCs (plan i-am-implementing-phase-shimmering-tide.md)

  resolve_expired_duel: internal-only helper, SECURITY DEFINER, no GRANT at
  all (unreachable via supabase.rpc(...) from any role, including
  service_role — only callable via PERFORM from the RPCs below, same trick
  award_xp uses one level further). Lazily resolves an accepted duel whose
  expires_at has passed: zero submissions -> expired; one submission -> that
  submitter wins by forfeit; two submissions while still 'accepted' is
  structurally unreachable (submit_duel_attempt's second-submit path flips
  status to 'completed' in the same statement sequence, under the same
  FOR UPDATE lock this helper also requires) — RAISEs invariant_violation
  rather than silently tolerating it, since that would mask a real locking bug.

  Transaction-semantics note: each RPC call is one Postgres transaction
  including every nested call. RAISE EXCEPTION anywhere rolls back the
  ENTIRE transaction, including whatever resolve_expired_duel already
  committed earlier in the same call. That's why start_duel_attempt/
  submit_duel_attempt/sync_duel_status RETURN a plain {ok:false,
  reason:'duel_expired'} jsonb — never RAISE — immediately after
  resolve_expired_duel reports it just mutated something. All other
  validation failures in these RPCs are pure reads with nothing to
  preserve, so they use the normal RAISE EXCEPTION + client mapError pattern.

  No migration needed against xp_events' CHECK constraint or grants —
  'friend_challenge' is already valid (20260814130200) and award_xp is
  already internally callable with zero new grants.
*/

CREATE OR REPLACE FUNCTION public.resolve_expired_duel(p_duel_id uuid)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_duel public.duel_challenges%ROWTYPE;
  v_count int;
  v_winner uuid;
  v_total numeric;
  v_amount int;
BEGIN
  SELECT * INTO v_duel FROM public.duel_challenges WHERE id = p_duel_id FOR UPDATE;
  IF NOT FOUND THEN RETURN false; END IF;

  IF v_duel.status <> 'accepted' OR now() <= v_duel.expires_at THEN
    RETURN false;
  END IF;

  SELECT count(*) INTO v_count FROM public.duel_attempts WHERE duel_id = p_duel_id;

  IF v_count = 0 THEN
    UPDATE public.duel_challenges SET status = 'expired', completed_at = now() WHERE id = p_duel_id;
  ELSIF v_count = 1 THEN
    SELECT user_id, score_total INTO v_winner, v_total FROM public.duel_attempts WHERE duel_id = p_duel_id;
    v_amount := GREATEST(1, ROUND(v_total / 40 * 50)) + 30;

    UPDATE public.duel_attempts SET outcome = 'forfeit_win', xp_awarded = v_amount
      WHERE duel_id = p_duel_id AND user_id = v_winner;
    UPDATE public.duel_challenges SET status = 'completed', completed_at = now(), winner_user_id = v_winner
      WHERE id = p_duel_id;

    PERFORM public.award_xp(v_winner, 'friend_challenge', v_amount,
                             v_winner::text || ':friend_challenge:' || p_duel_id::text,
                             jsonb_build_object('duel_id', p_duel_id, 'outcome', 'forfeit_win'));
  ELSE
    -- Structurally unreachable (see module header) — loud failure is
    -- cheaper to debug than a duel silently stuck in a wrong state.
    RAISE EXCEPTION 'invariant_violation: accepted duel % has 2 attempts', p_duel_id USING ERRCODE = 'XX000';
  END IF;

  RETURN true;
END;
$$;
-- No GRANT — unreachable via supabase.rpc(...) from any role, only via
-- internal PERFORM from the RPCs below.
REVOKE EXECUTE ON FUNCTION public.resolve_expired_duel(uuid) FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.create_duel_challenge(p_opponent_user_id uuid, p_question_set_id text)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid;
  v_lo uuid;
  v_hi uuid;
  v_friendship_status text;
  v_id uuid;
BEGIN
  me := auth.uid();
  IF me IS NULL THEN RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000'; END IF;

  IF p_opponent_user_id = me THEN
    RAISE EXCEPTION 'cannot_duel_self' USING ERRCODE = '22023';
  END IF;

  IF (SELECT count(*) FROM public.duel_challenges
      WHERE challenger_id = me AND created_at > now() - interval '1 day') >= 20 THEN
    RAISE EXCEPTION 'duel_rate_limited' USING ERRCODE = '22023';
  END IF;

  IF p_opponent_user_id < me THEN v_lo := p_opponent_user_id; v_hi := me;
  ELSE v_lo := me; v_hi := p_opponent_user_id; END IF;

  SELECT status INTO v_friendship_status FROM public.friendships WHERE user_low = v_lo AND user_high = v_hi;
  IF v_friendship_status IS DISTINCT FROM 'accepted' THEN
    RAISE EXCEPTION 'not_friends' USING ERRCODE = '22023';
  END IF;

  IF NOT EXISTS (SELECT 1 FROM public.igcse_question_sets WHERE id = p_question_set_id AND status = 'published') THEN
    RAISE EXCEPTION 'unknown_question_set' USING ERRCODE = '22023';
  END IF;

  INSERT INTO public.duel_challenges (challenger_id, opponent_id, question_set_id, status)
  VALUES (me, p_opponent_user_id, p_question_set_id, 'pending')
  RETURNING id INTO v_id;

  RETURN jsonb_build_object('ok', true, 'duel_id', v_id, 'status', 'pending');
END;
$$;
REVOKE EXECUTE ON FUNCTION public.create_duel_challenge(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.create_duel_challenge(uuid, text) TO authenticated;

CREATE OR REPLACE FUNCTION public.respond_duel_challenge(p_duel_id uuid, p_action text)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid;
  v_duel public.duel_challenges%ROWTYPE;
BEGIN
  me := auth.uid();
  IF me IS NULL THEN RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000'; END IF;

  IF p_action NOT IN ('accept', 'decline', 'cancel') THEN
    RAISE EXCEPTION 'invalid_action' USING ERRCODE = '22023';
  END IF;

  SELECT * INTO v_duel FROM public.duel_challenges WHERE id = p_duel_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'unknown_duel' USING ERRCODE = '22023'; END IF;

  IF me NOT IN (v_duel.challenger_id, v_duel.opponent_id) THEN
    -- Same error as "doesn't exist" — a non-participant cannot distinguish a
    -- real duel_id from a fake one via this RPC's response.
    RAISE EXCEPTION 'unknown_duel' USING ERRCODE = '22023';
  END IF;

  IF p_action IN ('accept', 'decline') AND me <> v_duel.opponent_id THEN
    RAISE EXCEPTION 'invalid_actor_for_action' USING ERRCODE = '22023';
  END IF;
  IF p_action = 'cancel' AND me <> v_duel.challenger_id THEN
    RAISE EXCEPTION 'invalid_actor_for_action' USING ERRCODE = '22023';
  END IF;

  IF v_duel.status <> 'pending' THEN
    -- Race/double-click no-op: correct actor+action, but someone already
    -- resolved this invite; safe, idempotent, matches friendships' idiom.
    RETURN jsonb_build_object('ok', true, 'status', v_duel.status);
  END IF;

  IF p_action = 'accept' THEN
    UPDATE public.duel_challenges SET status = 'accepted', responded_at = now(), expires_at = now() + interval '7 days'
      WHERE id = p_duel_id AND status = 'pending';
  ELSIF p_action = 'decline' THEN
    UPDATE public.duel_challenges SET status = 'declined', responded_at = now()
      WHERE id = p_duel_id AND status = 'pending';
  ELSIF p_action = 'cancel' THEN
    UPDATE public.duel_challenges SET status = 'cancelled', responded_at = now()
      WHERE id = p_duel_id AND status = 'pending';
  END IF;

  SELECT * INTO v_duel FROM public.duel_challenges WHERE id = p_duel_id;
  RETURN jsonb_build_object('ok', true, 'status', v_duel.status);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.respond_duel_challenge(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.respond_duel_challenge(uuid, text) TO authenticated;

CREATE OR REPLACE FUNCTION public.start_duel_attempt(p_duel_id uuid)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid;
  v_duel public.duel_challenges%ROWTYPE;
  v_resolved boolean;
  v_session_id text;
BEGIN
  me := auth.uid();
  IF me IS NULL THEN RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000'; END IF;

  SELECT * INTO v_duel FROM public.duel_challenges WHERE id = p_duel_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'unknown_duel' USING ERRCODE = '22023'; END IF;

  IF me NOT IN (v_duel.challenger_id, v_duel.opponent_id) THEN
    RAISE EXCEPTION 'unknown_duel' USING ERRCODE = '22023';
  END IF;

  v_resolved := public.resolve_expired_duel(p_duel_id);
  IF v_resolved THEN
    RETURN jsonb_build_object('ok', false, 'reason', 'duel_expired');
  END IF;

  SELECT * INTO v_duel FROM public.duel_challenges WHERE id = p_duel_id;
  IF v_duel.status <> 'accepted' THEN
    RAISE EXCEPTION 'duel_not_active' USING ERRCODE = '22023';
  END IF;

  IF EXISTS (SELECT 1 FROM public.duel_attempts WHERE duel_id = p_duel_id AND user_id = me) THEN
    RAISE EXCEPTION 'already_completed' USING ERRCODE = '22023';
  END IF;

  INSERT INTO public.duel_sessions (duel_id, user_id, session_id)
  VALUES (p_duel_id, me, 'duel-' || gen_random_uuid()::text)
  ON CONFLICT (duel_id, user_id) DO NOTHING;

  SELECT session_id INTO v_session_id FROM public.duel_sessions WHERE duel_id = p_duel_id AND user_id = me;

  RETURN jsonb_build_object('ok', true, 'session_id', v_session_id, 'question_set_id', v_duel.question_set_id, 'duel_id', p_duel_id);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.start_duel_attempt(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.start_duel_attempt(uuid) TO authenticated;

CREATE OR REPLACE FUNCTION public.submit_duel_attempt(p_duel_id uuid, p_attempt_id text)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid;
  v_duel public.duel_challenges%ROWTYPE;
  v_resolved boolean;
  v_existing_id uuid;
  v_envelope public.scoring_envelopes%ROWTYPE;
  v_reserved_session_id text;
  v_total numeric;
  v_count int;
  v_row_a public.duel_attempts%ROWTYPE;
  v_row_b public.duel_attempts%ROWTYPE;
  v_winner uuid;
  v_is_tie boolean;
  v_win_amount int;
BEGIN
  me := auth.uid();
  IF me IS NULL THEN RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000'; END IF;

  SELECT * INTO v_duel FROM public.duel_challenges WHERE id = p_duel_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'unknown_duel' USING ERRCODE = '22023'; END IF;

  IF me NOT IN (v_duel.challenger_id, v_duel.opponent_id) THEN
    RAISE EXCEPTION 'unknown_duel' USING ERRCODE = '22023';
  END IF;

  v_resolved := public.resolve_expired_duel(p_duel_id);
  IF v_resolved THEN
    RETURN jsonb_build_object('ok', false, 'reason', 'duel_expired');
  END IF;

  SELECT * INTO v_duel FROM public.duel_challenges WHERE id = p_duel_id;
  IF v_duel.status <> 'accepted' THEN
    RAISE EXCEPTION 'duel_not_active' USING ERRCODE = '22023';
  END IF;

  SELECT id INTO v_existing_id FROM public.duel_attempts WHERE duel_id = p_duel_id AND user_id = me;
  IF FOUND THEN
    RETURN jsonb_build_object('ok', true, 'already_claimed', true, 'status', v_duel.status);
  END IF;

  IF EXISTS (SELECT 1 FROM public.duel_attempts WHERE attempt_id = p_attempt_id) THEN
    RAISE EXCEPTION 'attempt_already_claimed' USING ERRCODE = '22023';
  END IF;

  SELECT * INTO v_envelope FROM public.scoring_envelopes WHERE attempt_id = p_attempt_id AND user_id = me;
  IF NOT FOUND THEN RAISE EXCEPTION 'unknown_envelope' USING ERRCODE = '22023'; END IF;

  IF v_envelope.content_provenance <> 'original-practice' THEN
    RAISE EXCEPTION 'not_original_practice' USING ERRCODE = '22023';
  END IF;

  IF (v_envelope.envelope->>'questionSetId') IS DISTINCT FROM v_duel.question_set_id THEN
    RAISE EXCEPTION 'question_set_mismatch' USING ERRCODE = '22023';
  END IF;

  SELECT session_id INTO v_reserved_session_id FROM public.duel_sessions WHERE duel_id = p_duel_id AND user_id = me;
  IF v_reserved_session_id IS NULL OR v_envelope.session_id <> v_reserved_session_id THEN
    RAISE EXCEPTION 'session_not_bound' USING ERRCODE = '22023';
  END IF;

  v_total := (v_envelope.envelope->>'total')::numeric;
  IF v_total IS NULL OR v_total < 0 OR v_total > 40 THEN
    RAISE EXCEPTION 'invalid_envelope_total' USING ERRCODE = '22023';
  END IF;

  PERFORM 1 FROM public.profiles WHERE id = me FOR UPDATE;

  INSERT INTO public.duel_attempts (duel_id, user_id, attempt_id, score_total, xp_awarded, outcome)
  VALUES (p_duel_id, me, p_attempt_id, v_total, 0, 'pending')
  ON CONFLICT (duel_id, user_id) DO NOTHING;

  SELECT count(*) INTO v_count FROM public.duel_attempts WHERE duel_id = p_duel_id;

  IF v_count = 1 THEN
    RETURN jsonb_build_object('ok', true, 'already_claimed', false, 'status', 'accepted', 'waiting_on_opponent', true, 'score_total', v_total);
  ELSE
    SELECT * INTO v_row_a FROM public.duel_attempts WHERE duel_id = p_duel_id AND user_id = v_duel.challenger_id;
    SELECT * INTO v_row_b FROM public.duel_attempts WHERE duel_id = p_duel_id AND user_id = v_duel.opponent_id;

    IF v_row_a.score_total = v_row_b.score_total THEN
      v_is_tie := true;
      v_winner := NULL;
      UPDATE public.duel_attempts SET outcome = 'tie', xp_awarded = 15 WHERE duel_id = p_duel_id;

      PERFORM public.award_xp(v_row_a.user_id, 'friend_challenge', 15,
                               v_row_a.user_id::text || ':friend_challenge:' || p_duel_id::text,
                               jsonb_build_object('duel_id', p_duel_id, 'outcome', 'tie'));
      PERFORM public.award_xp(v_row_b.user_id, 'friend_challenge', 15,
                               v_row_b.user_id::text || ':friend_challenge:' || p_duel_id::text,
                               jsonb_build_object('duel_id', p_duel_id, 'outcome', 'tie'));
    ELSE
      v_is_tie := false;
      IF v_row_a.score_total > v_row_b.score_total THEN
        v_winner := v_row_a.user_id;
      ELSE
        v_winner := v_row_b.user_id;
      END IF;

      v_win_amount := GREATEST(1, ROUND(GREATEST(v_row_a.score_total, v_row_b.score_total) / 40 * 50)) + 30;

      UPDATE public.duel_attempts SET outcome = 'win', xp_awarded = v_win_amount
        WHERE duel_id = p_duel_id AND user_id = v_winner;
      UPDATE public.duel_attempts SET outcome = 'loss', xp_awarded = 0
        WHERE duel_id = p_duel_id AND user_id <> v_winner;

      PERFORM public.award_xp(v_winner, 'friend_challenge', v_win_amount,
                               v_winner::text || ':friend_challenge:' || p_duel_id::text,
                               jsonb_build_object('duel_id', p_duel_id, 'outcome', 'win'));
      -- Loser: xp_awarded=0, no award_xp call — amount<>0 CHECK forbids it anyway.
    END IF;

    UPDATE public.duel_challenges SET status = 'completed', completed_at = now(), winner_user_id = v_winner, is_tie = v_is_tie
      WHERE id = p_duel_id;

    RETURN jsonb_build_object(
      'ok', true, 'already_claimed', false, 'status', 'completed',
      'is_tie', v_is_tie, 'winner_user_id', v_winner,
      'my_outcome', (SELECT outcome FROM public.duel_attempts WHERE duel_id = p_duel_id AND user_id = me),
      'my_xp_awarded', (SELECT xp_awarded FROM public.duel_attempts WHERE duel_id = p_duel_id AND user_id = me),
      'score_total', v_total
    );
  END IF;
END;
$$;
REVOKE EXECUTE ON FUNCTION public.submit_duel_attempt(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.submit_duel_attempt(uuid, text) TO authenticated;

CREATE OR REPLACE FUNCTION public.sync_duel_status(p_duel_id uuid)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid;
  v_duel public.duel_challenges%ROWTYPE;
BEGIN
  me := auth.uid();
  IF me IS NULL THEN RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000'; END IF;

  SELECT * INTO v_duel FROM public.duel_challenges WHERE id = p_duel_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'unknown_duel' USING ERRCODE = '22023'; END IF;

  IF me NOT IN (v_duel.challenger_id, v_duel.opponent_id) THEN
    RAISE EXCEPTION 'unknown_duel' USING ERRCODE = '22023';
  END IF;

  PERFORM public.resolve_expired_duel(p_duel_id);

  SELECT * INTO v_duel FROM public.duel_challenges WHERE id = p_duel_id;

  RETURN jsonb_build_object(
    'ok', true, 'status', v_duel.status, 'winner_user_id', v_duel.winner_user_id,
    'is_tie', v_duel.is_tie, 'expires_at', v_duel.expires_at
  );
END;
$$;
REVOKE EXECUTE ON FUNCTION public.sync_duel_status(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.sync_duel_status(uuid) TO authenticated;
