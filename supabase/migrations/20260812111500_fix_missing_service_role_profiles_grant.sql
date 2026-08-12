/*
  # Fix missing service_role GRANT on profiles

  Found while verifying Shop Phase 7 against a clean `supabase db reset`:
  the existing backend/supabase/tests/phase1_shop_economy.test.mjs harness
  fails immediately with "permission denied for table profiles" on its very
  first setup step (admin.from('profiles').upsert(...) using
  SERVICE_ROLE_KEY), because service_role has no GRANT at all on profiles —
  not SELECT, INSERT, nor UPDATE.

  20260811090000_fix_missing_table_grants.sql granted service_role full
  privileges on sessions/achievements/skill_snapshots/coach_evidence but
  deliberately skipped profiles, on the stated assumption (that migration's
  own comment, line 40) that "it already holds full privileges on profiles."
  That assumption does not hold against a clean db reset — service_role
  bypasses RLS but, like any non-superuser role, still needs an explicit
  GRANT to touch a table at all, and profiles never received one for
  service_role in any migration. This is a pre-existing gap, unrelated to
  Phase 7's own content; it is fixed here only because it blocks running
  any DB integration test (Phase 1's suite or this phase's) from a clean
  reset.

  Scoped identically to 20260811090000's pattern for its four sibling
  tables: full privileges for service_role, since it bypasses RLS entirely
  and is only ever used by trusted server-side code (Phase 1's test harness,
  and any future service-role backend job).
*/

GRANT SELECT, INSERT, UPDATE, DELETE ON public.profiles TO service_role;
