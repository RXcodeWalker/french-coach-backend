/*
  # Explicitly revoke client EXECUTE on server-only RPCs

  These functions are meant for the service role only (scoring service,
  FastAPI with the service key, cron jobs). Their migrations granted
  EXECUTE to service_role after `REVOKE ... FROM PUBLIC`, but never revoked
  from anon/authenticated directly. On a database whose default privileges
  grant EXECUTE on new functions to anon/authenticated (a fresh local
  `supabase start`, checked 2026-10-03), that left every one of them
  callable from the browser — e.g. award_xp for arbitrary XP, or
  release_ai_quota_grant to refund one's own AI quota.

  Production (checked read-only 2026-10-03) already denies anon/authenticated
  on all of these, so this migration is a no-op there; it makes the
  migrations themselves correct instead of relying on environment defaults.
  service_role grants are untouched (except resolve_expired_duel, which
  production grants to no role). Client-facing RPCs are untouched.
*/

REVOKE EXECUTE ON FUNCTION public.award_xp(uuid, text, integer, text, jsonb) FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.consume_ai_quota(uuid, text, text) FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.release_ai_quota_grant(uuid, text, text) FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.consume_shadowing_coaching_quota(uuid, text) FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.release_shadowing_coaching_grant(uuid, text) FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.assign_weekly_league_cohorts() FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.assign_weekly_league_cohorts_as_of(timestamp with time zone) FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.reset_league_week(text) FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.seed_daily_challenge(date) FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.seed_daily_challenges_batch() FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.get_notification_candidates(text) FROM anon, authenticated;
-- resolve_expired_duel is internal (called only from other SECURITY DEFINER duel
-- RPCs); production grants it to no role at all, so service_role is revoked too.
REVOKE EXECUTE ON FUNCTION public.resolve_expired_duel(uuid) FROM anon, authenticated, service_role;
REVOKE EXECUTE ON FUNCTION public._league_week_key(timestamp with time zone) FROM anon, authenticated;
