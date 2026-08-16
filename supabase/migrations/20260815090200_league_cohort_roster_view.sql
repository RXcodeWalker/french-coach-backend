/*
  # League Power — cohort roster view (revised plan
  i-am-implementing-phase-hashed-karp.md, Part B2)

  "Current week" is the SYSTEM's most recent COMPLETED week (from
  league_assignment_runs.completed_at), not each user's own historical max
  league_memberships.week_key. Anchoring on the global table means "no row
  for the actual current week" correctly resolves to an empty result (the
  'unranked' state) for a user who was ranked in some past week but isn't in
  the current week's build (cleared their username, set
  leaderboard_visibility to non-'global', etc.) -- their own max(week_key)
  would otherwise still resolve to that stale row.

  This view is created WITHOUT security_invoker, so it runs against its
  underlying tables using the view owner's RLS-bypass status (same mechanism
  this project's existing weekly_leaderboard/duel_challenges_view already
  rely on) -- auth.uid() itself is unaffected by that and always reflects the
  real calling user. This means RLS provides ZERO defense-in-depth for this
  view; the view's own WHERE clause is the only thing scoping rows to the
  caller, which is why the "global current week" anchor above matters.

  For a user with no row for the current global week (brand-new,
  pre-first-cron-run, or currently ineligible), the nested cohort_id subquery
  returns NULL, the outer WHERE never matches, and the query returns an
  empty set -- the 'unranked' state, not an error and not someone else's
  cohort.
*/

CREATE VIEW public.league_cohort_roster AS
SELECT
  lc.id AS cohort_id,
  lm.week_key,
  lc.tier AS pool_tier,
  lm.user_id,
  p.username,
  p.avatar_emoji,
  COALESCE((
    SELECT SUM(e.amount) FROM public.xp_events e
    WHERE e.user_id = lm.user_id AND e.week_key = lm.week_key
  ), 0) AS live_weekly_xp,
  lm.final_weekly_xp,
  lm.rank_in_cohort,
  lm.promoted,
  lm.demoted
FROM public.league_memberships lm
JOIN public.league_cohorts lc ON lc.id = lm.cohort_id
JOIN public.profiles p ON p.id = lm.user_id
WHERE lm.week_key = (SELECT max(week_key) FROM public.league_assignment_runs WHERE completed_at IS NOT NULL)
  AND lm.cohort_id = (
    SELECT lm2.cohort_id FROM public.league_memberships lm2
    WHERE lm2.user_id = auth.uid()
      AND lm2.week_key = (SELECT max(week_key) FROM public.league_assignment_runs WHERE completed_at IS NOT NULL)
  );

GRANT SELECT ON public.league_cohort_roster TO authenticated;
