/*
  # Fix missing table GRANTs on the core user tables

  RLS policies and table GRANTs are two independent layers: a policy can only
  narrow access that a GRANT already allows. `profiles`, `sessions`,
  `achievements`, `skill_snapshots` and `coach_evidence` all have correct
  per-user RLS policies from 20260503093957_french_coach_schema.sql, but none
  of them ever received a GRANT for `anon`/`authenticated` — the schema
  migration relied on Supabase's default privileges, which this project does
  not have enabled for new entities (see the auto_expose_new_tables note in
  supabase/config.toml). Every later migration that added a table
  (xp_events, reserved_usernames, friendships, blocks) carried its own
  explicit GRANT and works; these five predate that convention and do not.

  Symptom: claim_username() is SECURITY INVOKER, so its UPDATE on profiles
  runs as `authenticated` and fails with "permission denied for table
  profiles" (surfaced client-side as a 401). progressionSync's
  select/upsert on profiles, and every other sync write, fail the same way
  — silently, since those call sites only console.warn.

  Grants are scoped to exactly the commands each table has a policy for, so
  RLS remains the only thing deciding *which* rows are visible:
    profiles         SELECT/INSERT/UPDATE  (self-scoped: auth.uid() = id)
    sessions         SELECT/INSERT/UPDATE
    achievements     SELECT/INSERT         (append-only by design)
    skill_snapshots  SELECT/INSERT         (append-only by design)
    coach_evidence   SELECT/INSERT/UPDATE
  No DELETE anywhere — no table has a DELETE policy, and adding the grant
  without one would be dead privilege. Nothing is granted to `anon`: all five
  policies are `TO authenticated`.
*/

GRANT SELECT, INSERT, UPDATE ON public.profiles        TO authenticated;
GRANT SELECT, INSERT, UPDATE ON public.sessions        TO authenticated;
GRANT SELECT, INSERT         ON public.achievements    TO authenticated;
GRANT SELECT, INSERT         ON public.skill_snapshots TO authenticated;
GRANT SELECT, INSERT, UPDATE ON public.coach_evidence  TO authenticated;

-- service_role bypasses RLS but still needs the grant; it already holds full
-- privileges on profiles, so these are the remaining four for parity with the
-- newer tables' migrations.
GRANT SELECT, INSERT, UPDATE, DELETE ON public.sessions        TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.achievements    TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.skill_snapshots TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.coach_evidence  TO service_role;
