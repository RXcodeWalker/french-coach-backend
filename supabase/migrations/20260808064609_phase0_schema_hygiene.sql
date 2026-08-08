/*
  # Phase 0 — Schema hygiene (social layer plan §1.4, Part 8 Phase 0)

  1. Backfill profiles.migration_version — written by migrationService.ts and
     read by progressionSync.ts today with no migration ever having created it
     (pre-existing defect, confirmed absent from every prior migration file).
  2. Add the missing UPDATE policy on sessions (achievements and
     skill_snapshots are append-only by design — see 05-deprecated-v1-removals
     framing on immutable logs — but sessions.xp_earned is later patched by
     sync code, so sessions specifically needs UPDATE).

  Column-type note (plan §1.4 defect 2): sessions.id is declared `uuid` in
  20260503093957_french_coach_schema.sql, but the client generates
  non-uuid strings (`sess-${Date.now()}-${random}`). This migration does not
  touch that column — it is out of scope for Phase 0, which only adds the
  missing pieces above. Left as-is per the plan; xp_events (Phase 1) uses a
  `text` PK deliberately, matching coach_evidence, to avoid the same trap.
*/

ALTER TABLE profiles
  ADD COLUMN IF NOT EXISTS migration_version integer NOT NULL DEFAULT 0;

CREATE POLICY "Users can update own sessions"
  ON sessions FOR UPDATE
  TO authenticated
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);
