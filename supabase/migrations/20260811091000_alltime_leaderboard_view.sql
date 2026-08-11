/*
  # all_time_leaderboard view

  getAllTimeLeaderboard() in src/services/social/leaderboardService.ts selects
  `id, username, avatar_emoji, total_xp` straight off `profiles` with
  cross-user filters (leaderboard_visibility = 'global', username not null).
  That can never work: profiles' SELECT policy is strictly self-scoped
  (auth.uid() = id) and 20260809060000_phase3_leaderboard_views.sql is explicit
  that it must stay that way — loosening it would leak gems, inventory,
  active_boosters, streak_days to every user. Before the grant fix that query
  returned "permission denied"; after it, it silently returns exactly one row
  (your own), which reads as an empty/one-entry all-time board.

  So the all-time board gets the same treatment weekly_leaderboard already has:
  a curated cross-user projection that deliberately omits `security_invoker`,
  exposing only the four columns a leaderboard row needs. Same visibility
  filter as weekly_leaderboard, same "no rank column" rule — ranking is
  computed client-side from page position (plan §3.2).
*/

CREATE VIEW all_time_leaderboard AS
SELECT
  id AS user_id,
  username,
  avatar_emoji,
  current_level,
  COALESCE(total_xp, 0) AS total_xp
FROM profiles
WHERE username IS NOT NULL
  AND leaderboard_visibility = 'global';

GRANT SELECT ON public.all_time_leaderboard TO anon, authenticated, service_role;
