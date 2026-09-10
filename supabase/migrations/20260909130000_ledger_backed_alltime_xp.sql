/*
  # Phase 1.3 — ledger-backed all-time leaderboard; total_xp no longer client-writable

  The forgery: profiles.total_xp has a column-level UPDATE grant to
  `authenticated` (20260811101000) plus the `update own profile` RLS policy,
  so a single `PATCH /rest/v1/profiles` sets any value; all_time_leaderboard
  reads `COALESCE(total_xp, 0)` straight off profiles
  (20260811103000). One console call moves the board.

  The fix (mirrors weekly_leaderboard, which already aggregates
  SUM(xp_events.amount)):

  1. profiles.xp_baseline — a per-user integer frozen at each existing user's
     current cloud total. Service-role write only: it is in NO `authenticated`
     grant. Forged pre-ledger totals are carried in the baseline (we can't
     tell which are real) but are no longer *growable* from the client — new
     XP only lands via submit_xp_event (20260815090000), which is
     rate-capped and idempotent.

  2. all_time_leaderboard.total_xp becomes
     `p.xp_baseline + COALESCE((SELECT SUM(amount) FROM xp_events WHERE
     user_id = p.id), 0)`. Everything else about the view is unchanged
     (same columns, same `username IS NOT NULL AND leaderboard_visibility =
     'global'` filter, still owner-run — not security_invoker — matching the
     existing definition).

  3. Re-issue the `GRANT UPDATE (...)` from 20260811101000 WITHOUT the
     XP/stat columns the client should never have been able to write:
     total_xp, current_level, streak_days, longest_streak,
     last_session_date, sessions_count, total_words_spoken.

     `achievements` is DELIBERATELY KEPT in the grant. Phase 1.3's security
     target is all_time_leaderboard forgery, and the board reads total_xp,
     not achievements — achievements feed no leaderboard. There is no
     server-side achievement derivation yet (recompute_achievements,
     20260812113000, has zero firing predicates), and that migration's
     header explicitly warns that revoking client `achievements` writes with
     no replacement "would replace every user's unlocked achievements with an
     empty set and lock every achievement-gated shop item." A forged
     achievement only unlocks cosmetic shop items; the gem ledger that gates
     real spending is already server-authoritative. Client-forgeable
     achievements is therefore a KNOWN ACCEPTED RISK, carried UNCHANGED from
     before this migration — to be closed when server-side achievement
     derivation lands (Phase 6/7), not here.

  gems / inventory are already correctly excluded from the grant
  (20260811101000) — untouched here.
*/

-- ── 1. xp_baseline ───────────────────────────────────────────────────────
ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS xp_baseline integer NOT NULL DEFAULT 0;

-- One-time backfill: freeze each existing user's current cloud total as
-- their pre-ledger baseline. Idempotent enough for re-run safety — on a
-- second apply every row's total_xp is unchanged so this is a no-op-ish
-- rewrite, but guard it anyway so a re-run after users have earned ledger
-- XP doesn't inflate the baseline by re-absorbing it.
UPDATE public.profiles
SET xp_baseline = COALESCE(total_xp, 0)
WHERE xp_baseline = 0 AND COALESCE(total_xp, 0) <> 0;

-- ── 2. ledger-backed view ────────────────────────────────────────────────
DROP VIEW IF EXISTS all_time_leaderboard;

CREATE VIEW all_time_leaderboard AS
SELECT
  p.id AS user_id,
  p.username,
  p.avatar_emoji,
  p.current_level,
  p.equipped_frame,
  p.equipped_nameplate,
  p.xp_baseline + COALESCE(
    (SELECT SUM(e.amount) FROM public.xp_events e WHERE e.user_id = p.id),
    0
  ) AS total_xp
FROM public.profiles p
WHERE p.username IS NOT NULL
  AND p.leaderboard_visibility = 'global';

GRANT SELECT ON public.all_time_leaderboard TO anon, authenticated, service_role;

-- ── 3. narrow the client UPDATE grant ───────────────────────────────────
-- Re-issue 20260811101000's grant without total_xp / current_level /
-- streak_days / longest_streak / last_session_date / sessions_count /
-- total_words_spoken. achievements stays (see header).
REVOKE UPDATE ON public.profiles FROM authenticated, anon;

GRANT UPDATE (
  id, username, achievements, migration_version, username_changed_at,
  leaderboard_visibility, discoverable, friend_requests_from,
  created_at, updated_at
) ON public.profiles TO authenticated;
