/*
  # Daily Challenge Phase 1 — seed RPCs (revised plan §Fix 3, §"Full
  corrected backend schema/RPC set" part 5)

  seed_daily_challenge: atomic insert, no pre-check — the first-pass design's
  check-then-insert (IF EXISTS ... RETURN, then a separate INSERT) raced
  under concurrent job runs (e.g. a manual workflow_dispatch overlapping the
  scheduled run): both could pass the check and one would crash on the
  challenge_date primary-key violation. INSERT ... ON CONFLICT DO NOTHING
  makes a duplicate run a clean no-op instead.

  Picks a published question set from the closed corpus, excluding
  yesterday's assignment so the same set never repeats two days running.
  md5(id || date) ordering is a deterministic-per-day, effectively-random
  selection — no external randomness source needed, and re-running
  seed_daily_challenge for the same date (before it's committed) would pick
  the same candidate, which is irrelevant here since the INSERT is
  conflict-safe regardless.

  seed_daily_challenges_batch seeds BOTH today and tomorrow every run: today
  as a self-healing safety net (catches up if an earlier run was missed),
  tomorrow as the usual one-day buffer. Both are no-ops if already seeded, so
  this is safe to call as often as the cron fires (every 6 hours — see the
  GitHub Actions workflow). No new scheduler: reuses the existing
  scheduled-rpc.yml reusable workflow (Phase 0 scaffold).

  Both RPCs are service_role-only (REVOKE FROM PUBLIC, no `authenticated`
  grant at all) — unreachable from any client, matching the workflow
  scaffold's own stated requirement that service_role-only RPCs are what it
  expects to call.
*/

CREATE OR REPLACE FUNCTION public.seed_daily_challenge(p_challenge_date date)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_set_id text;
  v_inserted boolean;
BEGIN
  SELECT id INTO v_set_id
  FROM public.igcse_question_sets
  WHERE status = 'published'
    AND id <> COALESCE(
      (SELECT question_set_id FROM public.daily_challenge_assignments WHERE challenge_date = p_challenge_date - 1),
      ''
    )
  ORDER BY md5(id || p_challenge_date::text)
  LIMIT 1;

  IF v_set_id IS NULL THEN
    RAISE EXCEPTION 'no_published_question_sets' USING ERRCODE = '22023';
  END IF;

  INSERT INTO public.daily_challenge_assignments (challenge_date, question_set_id)
  VALUES (p_challenge_date, v_set_id)
  ON CONFLICT (challenge_date) DO NOTHING;

  v_inserted := FOUND;

  RETURN jsonb_build_object(
    'ok', true, 'already_seeded', NOT v_inserted,
    'question_set_id', COALESCE(
      (SELECT question_set_id FROM public.daily_challenge_assignments WHERE challenge_date = p_challenge_date),
      v_set_id
    )
  );
END;
$$;
REVOKE EXECUTE ON FUNCTION public.seed_daily_challenge(date) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.seed_daily_challenge(date) TO service_role;

CREATE OR REPLACE FUNCTION public.seed_daily_challenges_batch()
RETURNS jsonb
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp
AS $$
  SELECT jsonb_build_object(
    'today', public.seed_daily_challenge((now() AT TIME ZONE 'UTC')::date),
    'tomorrow', public.seed_daily_challenge((now() AT TIME ZONE 'UTC')::date + 1)
  );
$$;
REVOKE EXECUTE ON FUNCTION public.seed_daily_challenges_batch() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.seed_daily_challenges_batch() TO service_role;
