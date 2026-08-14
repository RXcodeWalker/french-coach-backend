/*
  # Daily Challenge Phase 1 — leaderboard view (revised plan §"Full corrected
  backend schema/RPC set", part 2)

  Identical pattern to weekly_leaderboard (20260809060000): a plain view runs
  with its OWNER's privileges and bypasses daily_challenge_attempts' owner-only
  RLS, so no second "leaderboard read" policy is needed on the base table (see
  that migration's header for the full rationale, and this phase's "Optional
  cleanup — accepted" note).

  Scoped to today only — a daily challenge view has no "which day" parameter
  from the caller's side beyond filtering challenge_date, matching how
  weekly_leaderboard is filtered by week_key by the caller rather than baking
  "current week" into the view itself.
*/

CREATE VIEW public.daily_challenge_leaderboard AS
SELECT
  a.challenge_date,
  a.user_id,
  p.username,
  p.avatar_emoji,
  a.score_total,
  a.xp_awarded,
  a.created_at
FROM public.daily_challenge_attempts a
JOIN public.profiles p ON p.id = a.user_id
WHERE p.username IS NOT NULL;

GRANT SELECT ON public.daily_challenge_leaderboard TO anon, authenticated, service_role;
