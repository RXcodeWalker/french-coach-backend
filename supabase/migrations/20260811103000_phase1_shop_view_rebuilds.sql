/*
  # Shop Phase 1 — view rebuilds for equipped cosmetics (plan §14.2)

  public_profile, weekly_leaderboard, and all_time_leaderboard are the three
  cross-user read surfaces (20260809060000, 20260811091000) that render a
  user's row to other users — friends/search, the weekly board, and the
  all-time board respectively. Plan §14.2 names public_profile and
  weekly_leaderboard explicitly ("must be DROPped and recreated to add
  equipped_frame/equipped_nameplate"); all_time_leaderboard is the same kind
  of leaderboard row and postdates the plan's last schema read, so it gets
  the same treatment for consistency with Phase 3's stated success
  criterion ("equipping via RPC shows on the live leaderboard for a second
  account") — every surface a leaderboard row renders on should show it.

  Deliberately NOT security_invoker (plan §14.2 note carried over from
  20260809060000's header) — these run as owner specifically to read across
  users without loosening profiles' own strictly self-scoped SELECT policy.
*/

DROP VIEW IF EXISTS public_profile;

CREATE VIEW public_profile AS
SELECT id, username, avatar_emoji, current_level, equipped_frame, equipped_nameplate
FROM profiles
WHERE username IS NOT NULL;

GRANT SELECT ON public.public_profile TO anon, authenticated, service_role;

DROP VIEW IF EXISTS weekly_leaderboard;

CREATE VIEW weekly_leaderboard AS
SELECT
  p.id AS user_id,
  p.username,
  p.avatar_emoji,
  p.current_level,
  p.equipped_frame,
  p.equipped_nameplate,
  e.week_key,
  SUM(e.amount) AS weekly_xp
FROM xp_events e
JOIN profiles p ON p.id = e.user_id
WHERE p.username IS NOT NULL
  AND p.leaderboard_visibility = 'global'
GROUP BY p.id, p.username, p.avatar_emoji, p.current_level, p.equipped_frame, p.equipped_nameplate, e.week_key;

GRANT SELECT ON public.weekly_leaderboard TO anon, authenticated, service_role;

DROP VIEW IF EXISTS all_time_leaderboard;

CREATE VIEW all_time_leaderboard AS
SELECT
  id AS user_id,
  username,
  avatar_emoji,
  current_level,
  equipped_frame,
  equipped_nameplate,
  COALESCE(total_xp, 0) AS total_xp
FROM profiles
WHERE username IS NOT NULL
  AND leaderboard_visibility = 'global';

GRANT SELECT ON public.all_time_leaderboard TO anon, authenticated, service_role;
