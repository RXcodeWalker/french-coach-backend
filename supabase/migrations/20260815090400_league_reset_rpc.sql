/*
  # League Power — admin reset RPC (revised plan
  i-am-implementing-phase-hashed-karp.md, Part B4)

  Takes the same global advisory lock as assign_weekly_league_cohorts_as_of
  (B3) before its own max(week_key) check, closing a TOCTOU race: without a
  shared lock, a newer week's assignment could complete between this
  function's own max() check and its DELETE, letting a stale "N is still the
  most recent week" check through even though N+1 now exists on top of it.

  Deliberate limitation (unchanged from the prior draft, still correct):
  this cannot undo a prior week's finalization/profiles.league_tier changes,
  only the target week's own cohort build. Re-running the assignment RPC
  afterward is still safe because Phase A only touches league_memberships
  rows where final_weekly_xp IS NULL (idempotent per-user, not just
  per-week).
*/

CREATE OR REPLACE FUNCTION public.reset_league_week(p_week_key text)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_max_week text;
BEGIN
  -- Same lock assign_weekly_league_cohorts_as_of takes -- makes reset and
  -- assignment fully mutually exclusive, closing the TOCTOU race where a
  -- newer week's assignment could complete between this function's own
  -- max() check and its DELETE.
  PERFORM pg_advisory_xact_lock(hashtext('league_assignment_global'));

  SELECT max(week_key) INTO v_max_week FROM public.league_assignment_runs;
  IF v_max_week IS NULL OR p_week_key IS DISTINCT FROM v_max_week THEN
    RAISE EXCEPTION 'can_only_reset_most_recent_week' USING ERRCODE = '22023';
  END IF;

  DELETE FROM public.league_memberships WHERE week_key = p_week_key;
  DELETE FROM public.league_cohorts WHERE week_key = p_week_key;
  DELETE FROM public.league_assignment_runs WHERE week_key = p_week_key;

  RETURN jsonb_build_object('ok', true, 'reset_week', p_week_key);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.reset_league_week(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.reset_league_week(text) TO service_role;
