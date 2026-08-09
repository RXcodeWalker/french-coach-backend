/*
  # Phase 3 — Leaderboard read path (social layer plan §2.1, §3.2, §3.5, §4.1,
  # §4.3, §4.4)

  Plan §4.1 groups avatar_emoji and the three privacy columns
  (leaderboard_visibility, discoverable, friend_requests_from) together under
  "profiles columns", assigning all four to Phase 5. But weekly_leaderboard's
  own definition (§3.2, §4.3) needs to filter on leaderboard_visibility, and
  a leaderboard row needs an avatar — so those two columns are pulled forward
  into this migration; only discoverable and friend_requests_from (needed by
  Phase 4/5's discoverable_profiles view and search) stay with Phase 5's
  privacy migration.

  leaderboard_visibility is NOT NULL DEFAULT 'global' specifically so the
  ALTER backfills every existing profiles row to 'global' rather than NULL —
  a nullable column would make `= 'global'` silently false for every
  pre-existing user and empty the leaderboard.

  profiles.SELECT stays strictly self-scoped (`auth.uid() = id`) — it must
  never be loosened, since it would leak total_xp, gems, inventory,
  active_boosters, streak_days, migration_version to every other user (plan
  §4.4, the highest-severity item in the whole plan). Cross-user reads go
  through these two views instead.

  A Postgres view runs with its OWNER's privileges and bypasses the
  underlying tables' RLS unless declared `security_invoker` (plan §4.3) —
  that is exactly what's wanted here: a narrow, curated cross-user read
  surface without loosening the base tables. Do not add `security_invoker`
  to these two views; that would just reintroduce the RLS wall they exist
  to work around.

  No RPC, no rollup table — the leaderboard is a live aggregate over
  xp_events (plan §2.1). Ranking itself is computed client-side from page
  position + a `count: 'exact', head: true` "my rank" query (plan §3.2);
  this view intentionally does not expose a rank column.
*/

-- ── profiles columns (pulled forward from Phase 5, see note above) ──────────

ALTER TABLE profiles
  ADD COLUMN IF NOT EXISTS avatar_emoji text;

ALTER TABLE profiles
  ADD COLUMN IF NOT EXISTS leaderboard_visibility text NOT NULL DEFAULT 'global'
    CHECK (leaderboard_visibility = ANY (ARRAY['global', 'friends', 'hidden']));

-- ── public_profile ───────────────────────────────────────────────────────────
-- Minimal cross-user-safe projection. No email, no XP, no gems, no streak —
-- those come from weekly_leaderboard (weekly/total XP) or stay private.

CREATE VIEW public_profile AS
SELECT id, username, avatar_emoji, current_level
FROM profiles
WHERE username IS NOT NULL;

GRANT SELECT ON public.public_profile TO anon, authenticated, service_role;

-- ── weekly_leaderboard ──────────────────────────────────────────────────────
-- Aggregate over xp_events, not a stored rollup (plan §2.1) — index-only
-- GROUP BY on the generated week_key column (see xp_events_week_user_amount_idx
-- from the Phase 1 migration), fast enough at this scale (§2.1 scale check).
-- Honors leaderboard_visibility: only 'global' profiles appear here. 'friends'
-- visibility is enforced client-side by the friends tab query (needs the
-- friend graph from Phase 4), not by this view — a global view has no
-- per-viewer concept. Blocked users are NOT filtered from the global board;
-- ranks stay objective (plan §3.6).

CREATE VIEW weekly_leaderboard AS
SELECT
  p.id AS user_id,
  p.username,
  p.avatar_emoji,
  p.current_level,
  e.week_key,
  SUM(e.amount) AS weekly_xp
FROM xp_events e
JOIN profiles p ON p.id = e.user_id
WHERE p.username IS NOT NULL
  AND p.leaderboard_visibility = 'global'
GROUP BY p.id, p.username, p.avatar_emoji, p.current_level, e.week_key;

GRANT SELECT ON public.weekly_leaderboard TO anon, authenticated, service_role;
