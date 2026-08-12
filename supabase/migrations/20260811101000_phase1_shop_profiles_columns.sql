/*
  # Shop Phase 1 — profiles columns + column-level lockdown (plan A4, §14.2)

  equipped_frame/equipped_nameplate are id references into shop_items,
  rendered client-side by looking up presentation from shopCatalogue.ts
  (Phase 3, out of scope here). The equipped avatar continues to use the
  existing avatar_emoji column (see the schema migration's header for why).

  A4 — the REVOKE is the actual fix for the finding that started this phase:
  profiles' UPDATE policy (20260503093957) has no column scope, so any
  authenticated user could set their own gems/inventory/avatar_emoji/
  equipped_frame/equipped_nameplate directly from the browser console,
  bypassing every RPC below. This narrows UPDATE at the grant level, which
  RLS policies cannot do on their own.

  The plan's own §14.2 snippet (`REVOKE UPDATE (gems, inventory, ...) ...`)
  does not work as written — verified locally: it inserts a *column-level*
  revoke, but 20260811090000_fix_missing_table_grants.sql already gave
  authenticated a *table-level* `GRANT UPDATE ON profiles` (whole-row, no
  column list). In Postgres those are independent grant entries — a
  column-level REVOKE does not narrow a pre-existing table-level GRANT, so
  gems/inventory/etc. stayed fully updatable (confirmed by the §16 test
  suite: test 6 failed against the plan's literal statement). The actual
  fix has to revoke the table-level grant entirely and re-grant UPDATE at
  column level for exactly the columns that should remain writable.

  Deliberately kept writable (verified against actual client write paths,
  plan's B1 concern): username, username_changed_at (claim_username/
  rename_username are SECURITY INVOKER and rely on the caller's own UPDATE
  grant — src/services/social/usernameService.ts, backend migration
  20260808070714), leaderboard_visibility/discoverable/friend_requests_from
  (src/services/social/privacyService.ts direct writes), migration_version
  (src/services/sync/migrationService.ts), total_xp and achievements
  (accepted risk until Phase 7 per plan §10/§B — revoking these now would
  break progressionSync's XP sync for every live user with no replacement
  path yet). id/created_at/updated_at/current_level/streak_days/
  longest_streak/last_session_date/sessions_count/total_words_spoken were
  already covered by the old table-level grant and are carried forward
  unchanged — none of this phase's findings concern them.
*/

ALTER TABLE profiles
  ADD COLUMN IF NOT EXISTS equipped_frame text REFERENCES shop_items(id);

ALTER TABLE profiles
  ADD COLUMN IF NOT EXISTS equipped_nameplate text REFERENCES shop_items(id);

REVOKE UPDATE ON public.profiles FROM authenticated, anon;

GRANT UPDATE (
  id, username, total_xp, current_level, streak_days, longest_streak,
  last_session_date, sessions_count, total_words_spoken, created_at, updated_at,
  achievements, migration_version, username_changed_at,
  leaderboard_visibility, discoverable, friend_requests_from
) ON public.profiles TO authenticated;
