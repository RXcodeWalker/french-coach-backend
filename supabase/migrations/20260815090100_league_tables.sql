/*
  # League Power — schema (revised plan i-am-implementing-phase-hashed-karp.md,
  Part B1)

  profiles.league_tier: the user's REAL standing tier, independent of
  whichever cohort they get pooled into in a given week.

  league_cohorts: one row per (tier, week_key, split-index) cohort actually
  built for a week. tier here is the pool/effective tier (post-merge), not
  necessarily every member's real tier.

  league_memberships: pool_tier vs standing_tier split (the significant fix
  from the prior draft) --

    pool_tier     = which tier's cohort this user actually competed in this
                    week (after any whole-tier merge-down). Matches
                    league_cohorts.tier for their cohort. This is what the
                    roster/UI shows as "your league this week".
    standing_tier = this user's REAL tier (profiles.league_tier) at the
                    moment they were placed, captured BEFORE any merge-down
                    was applied. Next week's promotion/demotion moves
                    relative to standing_tier, not pool_tier.

  Concretely: if Diamond's weekly pool has only 3 users, they get merged into
  a Platinum-labeled cohort for matchmaking purposes (pool_tier='platinum'),
  but their standing_tier stays 'diamond' -- so a mediocre-performing Diamond
  user who ranks bottom-15% of that merged cohort demotes to Platinum (one
  step down from Diamond), never silently skipping to Gold (one step down
  from the merged cohort's Platinum label). Ranking WITHIN the cohort still
  correctly uses the pooled/merged membership -- that's a real competition
  against real opponents this week -- only the tier-transition math keys off
  standing_tier.

  league_assignment_runs: internal bookkeeping for the weekly RPC (B3/B4) --
  tracks which week has been built/finalized, and is the anchor B2's roster
  view uses for "the current week" (a global anchor, not each user's own
  history -- see B2's header for why that matters).
*/

ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS league_tier text
    CHECK (league_tier IS NULL OR league_tier = ANY (ARRAY['bronze','silver','gold','platinum','diamond']));
-- No column-level GRANT UPDATE added for authenticated/anon -- the table-level
-- UPDATE grant was already revoked (20260811101000) and league_tier is
-- deliberately left off the writable-column allowlist.

CREATE TABLE public.league_cohorts (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tier         text NOT NULL CHECK (tier = ANY (ARRAY['bronze','silver','gold','platinum','diamond'])),
  week_key     text NOT NULL,
  member_count integer NOT NULL DEFAULT 0,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX league_cohorts_week_tier_idx ON public.league_cohorts(week_key, tier);

CREATE TABLE public.league_memberships (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  cohort_id       uuid NOT NULL REFERENCES public.league_cohorts(id) ON DELETE CASCADE,
  user_id         uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  week_key        text NOT NULL,
  -- Which tier's cohort this user actually competed in this week (after any
  -- whole-tier merge-down). Matches league_cohorts.tier for their cohort.
  -- This is what the roster/UI shows as "your league this week".
  pool_tier       text NOT NULL CHECK (pool_tier = ANY (ARRAY['bronze','silver','gold','platinum','diamond'])),
  -- This user's REAL tier (profiles.league_tier) at the moment they were
  -- placed, captured BEFORE any merge-down was applied. Next week's
  -- promotion/demotion moves relative to standing_tier, not pool_tier.
  standing_tier   text NOT NULL CHECK (standing_tier = ANY (ARRAY['bronze','silver','gold','platinum','diamond'])),
  final_weekly_xp numeric,
  rank_in_cohort  integer,
  promoted        boolean NOT NULL DEFAULT false,
  demoted         boolean NOT NULL DEFAULT false,
  created_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT league_memberships_unique_week_user UNIQUE (week_key, user_id),
  CONSTRAINT league_memberships_not_both_promoted_demoted CHECK (NOT (promoted AND demoted))
);
CREATE INDEX league_memberships_week_cohort_idx ON public.league_memberships(week_key, cohort_id);
CREATE INDEX league_memberships_week_user_idx ON public.league_memberships(week_key, user_id);

-- Internal bookkeeping/lock-state table for the weekly RPC. No user data --
-- RLS enabled with zero authenticated/anon policies is sufficient (service_role
-- bypasses RLS by default in Supabase).
CREATE TABLE public.league_assignment_runs (
  week_key     text PRIMARY KEY,
  started_at   timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz
);

ALTER TABLE public.league_cohorts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.league_memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.league_assignment_runs ENABLE ROW LEVEL SECURITY;

-- league_cohorts: no authenticated/anon policy. Real exposure is only
-- through league_cohort_roster (B2).
-- league_memberships: own-row-only SELECT. Cross-user "cohort mates"
-- visibility is handled entirely by league_cohort_roster, not RLS here.
CREATE POLICY "Users can view own league memberships"
  ON public.league_memberships FOR SELECT
  TO authenticated
  USING (auth.uid() = user_id);

GRANT SELECT ON public.league_memberships TO authenticated;
GRANT SELECT, INSERT, UPDATE ON public.league_cohorts TO service_role;
GRANT SELECT, INSERT, UPDATE ON public.league_memberships TO service_role;
GRANT SELECT, INSERT, UPDATE ON public.league_assignment_runs TO service_role;
