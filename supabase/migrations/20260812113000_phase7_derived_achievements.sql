/*
  # Shop Phase 7 — server-validated achievements (structure only)
  (plan §15 Phase 7: "server-validated achievements; then revoke client
  UPDATE on total_xp/achievements")

  Status (must be read before relying on this in any way): checked every
  predicate in src/data/achievements.ts (19 achievements) against what is
  actually server-observed today. All 19 read client-only/localStorage
  state — session counts (Session = one answer, client-computed, A3),
  streak (localStorage only, never written to profiles, A1), skill mastery
  (coach belief snapshot, client IndexedDB/localStorage), XP (client
  ledger), grammar-coach/roleplay counters (client counters), exam
  completion (client flag; exam_results per plan B4 has no migration in
  this repo). The only genuinely server-observed facts anywhere today are
  gem_events (mint/spend history, Phase 1) and user_inventory (ownership,
  Phase 1) — neither maps to any achievement in the current catalogue.

  This migration is therefore infrastructure only, matching the same
  documented status as 20260812110000_phase7_envelope_mint.sql: real
  table, real RPC, real per-achievement rule dispatch, but the rule set
  derives ZERO of the 19 achievements today by design. Each rule moves
  from "not yet derivable" to a real predicate only as its underlying
  signal becomes server-observed — e.g. session count once envelopes/
  transcripts carry real attempts (S11/Phase B, Assessment Engine
  project), exam completion once exam_results (B4) gets a migration and a
  confirmed write path. Do not wire recompute_achievements as a
  replacement for the client-asserted profiles.achievements array (that is
  Phase 7's third clause, "revoke client UPDATE on total_xp/achievements")
  until at least one real predicate exists — doing so today would replace
  every user's unlocked achievements with an empty set and lock every
  achievement-gated shop item.

  achievements_derived is deliberately a separate table from profiles.
  achievements, not a replacement column: this phase does not touch the
  live, client-written array at all. It exists so the derived (currently
  empty) view of "what the server itself would say you've earned" can be
  computed, tested, and compared against the client array once real
  predicates exist, without touching the live column that
  progressionSync/every purchase-requirement check currently depends on.
*/

-- ── achievements_derived ─────────────────────────────────────────────────
-- One row per (user, achievement) the server has independently confirmed.
-- Append-only from the RPC's perspective — recompute_achievements upserts,
-- never deletes (an achievement, once server-confirmed, stays confirmed;
-- matches the client rule engine's own append-only unlock semantics).

CREATE TABLE IF NOT EXISTS achievements_derived (
  user_id        uuid REFERENCES profiles(id) ON DELETE CASCADE NOT NULL,
  achievement_id text NOT NULL,
  derived_at     timestamptz NOT NULL DEFAULT now(),
  source         text NOT NULL,
  PRIMARY KEY (user_id, achievement_id)
);

ALTER TABLE achievements_derived ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own derived achievements"
  ON achievements_derived FOR SELECT
  TO authenticated
  USING (auth.uid() = user_id);

-- No INSERT/UPDATE/DELETE policy — writable only by recompute_achievements
-- (SECURITY DEFINER, owned by postgres), same pattern as gem_events/
-- user_inventory in Phase 1.
GRANT SELECT ON public.achievements_derived TO authenticated, service_role;

-- ── recompute_achievements ───────────────────────────────────────────────
-- Re-evaluates every server-derivable achievement rule for the calling
-- user and upserts any newly-confirmed ones into achievements_derived.
-- Idempotent by construction (ON CONFLICT DO NOTHING — a re-run changes
-- nothing for an already-derived achievement). Returns the full set of
-- achievement ids now confirmed for this user, so a caller never needs a
-- second round-trip to know the result.
--
-- The rule set below is intentionally empty of any firing predicate today
-- — see the header. It is structured as a sequence of independent
-- "IF <server-observed fact> THEN confirm '<id>'" blocks, one per future
-- rule, so that adding a real predicate later (e.g. once envelope-backed
-- session counts exist) is a one-block addition here, not a redesign of
-- this function's shape.

CREATE OR REPLACE FUNCTION public.recompute_achievements()
RETURNS text[]
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid := auth.uid();
  v_result text[];
BEGIN
  IF me IS NULL THEN
    RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000';
  END IF;

  -- No rule currently fires: every achievement predicate in
  -- src/data/achievements.ts depends on a fact this schema does not yet
  -- observe server-side. This function still runs to completion and
  -- returns whatever has been previously confirmed (empty on a fresh
  -- user), rather than erroring, so callers can wire it in ahead of any
  -- real rule existing without special-casing "no rules yet."

  SELECT COALESCE(array_agg(achievement_id), ARRAY[]::text[])
  INTO v_result
  FROM public.achievements_derived
  WHERE user_id = me;

  RETURN v_result;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.recompute_achievements() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.recompute_achievements() TO authenticated;
