/*
  # Shop Phase 3 — expose equipped cosmetics on discoverable_profiles

  Plan §14.2 named only public_profile and weekly_leaderboard for the
  equipped_frame/equipped_nameplate rebuild; 20260811103000 additionally
  covered all_time_leaderboard for consistency. discoverable_profiles (the
  view backing username search, defined in 20260809080000) was missed —
  a genuine gap in the plan, confirmed during Phase 3 frontend work, since
  Phase 3 explicitly lists search as a cosmetic render surface.

  Unlike the other three views, discoverable_profiles is WITH
  (security_invoker = true) deliberately, so auth.uid() inside it reflects
  the calling user for the NOT EXISTS block-filter predicate. That must be
  preserved exactly — dropping it would leak blocked users into search
  results. The other three views run as owner on purpose (to read across
  users despite profiles' self-scoped SELECT policy) and are NOT touched
  by this migration.

  Recreated via DROP + CREATE (not CREATE OR REPLACE) because Postgres
  cannot add columns to an existing view via REPLACE.
*/

DROP VIEW IF EXISTS discoverable_profiles;

CREATE VIEW discoverable_profiles
WITH (security_invoker = true) AS
SELECT p.id, p.username, p.avatar_emoji, p.current_level,
       p.equipped_frame, p.equipped_nameplate
FROM profiles p
WHERE p.username IS NOT NULL
  AND p.discoverable = true
  AND NOT EXISTS (
    SELECT 1 FROM blocks b
    WHERE (b.blocker_id = auth.uid() AND b.blocked_id = p.id)
       OR (b.blocker_id = p.id AND b.blocked_id = auth.uid())
  );

GRANT SELECT ON public.discoverable_profiles TO authenticated;
