/*
  # Phase 1 — XP ledger (social layer plan §2.1, §2.2, §2.3, §4.2, §4.4)

  Append-only event log; the sole source for weekly XP. Deliberately NOT a
  trigger-maintained rollup (plan §2.1) — the weekly leaderboard (added in a
  later phase) reads an aggregate view over this table.

  Idempotency (plan §2.2): id is a client-generated text PK (matching
  coach_evidence, not sessions' drifted uuid column — see Phase 0 migration's
  note on that defect). Writers must use INSERT with duplicate tolerance
  (`.upsert(row, { onConflict: 'id', ignoreDuplicates: true })` or a plain
  insert swallowing the unique-violation), NEVER a plain `.upsert()` that lets
  ON CONFLICT DO UPDATE re-fire — this table has no UPDATE policy specifically
  to make that impossible to do accidentally (append-only enforced by policy
  absence, not convention).

  week_key (plan §2.1, §3.2): generated stored column, ISO 8601 week computed
  in UTC regardless of session timezone. to_char() is STABLE (session-
  timezone-dependent) and cannot appear in a STORED generated expression, so
  this uses extract(isoyear/week ...) at a fixed 'UTC' offset instead, which
  is IMMUTABLE. Mirror this logic in src/domain/weekKey.ts — the two must
  agree or client-side weekly grouping will disagree with the DB aggregate.

  Clock-skew bound (plan §2.2): occurred_at is client-stamped (required for
  correct offline week attribution) but constrained against created_at
  (server-evaluated default now()), so a client cannot forward-date past the
  server's clock and backdating is bounded to the offline-flush window.

  Amount bound (plan §3.7): per-row CHECK; max honest single award is 39
  (computeXPGain's ceiling) but dispatchAddXP reaches 400 (Challenges.tsx
  claim reward), so the bound is set well above both with room for the
  documented negative case (DailyNewsFlash's -5 transcript-reveal penalty).
*/

CREATE TABLE IF NOT EXISTS xp_events (
  id             text PRIMARY KEY,
  user_id        uuid REFERENCES profiles(id) ON DELETE CASCADE NOT NULL,
  amount         integer NOT NULL CHECK (amount BETWEEN -100 AND 500 AND amount <> 0),
  source         text NOT NULL CHECK (source = ANY (ARRAY[
                   'practice', 'exam', 'roleplay', 'word_drop', 'daily_news',
                   'story', 'listening', 'sentence_rebuilder', 'accent_analyzer',
                   'emoji_master', 'micro_drill', 'mystery_box', 'challenge',
                   'minigame', 'friend_challenge'
                 ])),
  metadata       jsonb NOT NULL DEFAULT '{}',
  occurred_at    timestamptz NOT NULL,
  schema_version integer NOT NULL DEFAULT 1,
  created_at     timestamptz NOT NULL DEFAULT now(),
  week_key       text GENERATED ALWAYS AS (
                   extract(isoyear FROM (occurred_at AT TIME ZONE 'UTC'))::int::text
                   || '-W' ||
                   lpad(extract(week FROM (occurred_at AT TIME ZONE 'UTC'))::int::text, 2, '0')
                 ) STORED,
  CONSTRAINT xp_events_occurred_at_bounds CHECK (
    occurred_at >= created_at - interval '30 days'
    AND occurred_at <= created_at + interval '1 day'
  )
);

CREATE INDEX IF NOT EXISTS xp_events_week_user_amount_idx
  ON xp_events(week_key, user_id, amount);

CREATE INDEX IF NOT EXISTS xp_events_user_occurred_idx
  ON xp_events(user_id, occurred_at DESC);

ALTER TABLE xp_events ENABLE ROW LEVEL SECURITY;

-- SELECT + INSERT own only. No UPDATE, no DELETE policy — append-only
-- enforced by the absence of those policies (plan §2.2, §4.4).
CREATE POLICY "Users can view own xp events"
  ON xp_events FOR SELECT
  TO authenticated
  USING (auth.uid() = user_id);

CREATE POLICY "Users can insert own xp events"
  ON xp_events FOR INSERT
  TO authenticated
  WITH CHECK (auth.uid() = user_id);

-- A plain `create table` does not inherit baseline PostgREST grants (plan
-- §1.4 defect 4 / §4.4) — this has silently 42501'd twice before in this
-- project (see 20260716121500_grant_igcse_question_sets.sql,
-- 20260717120000_grant_scoring_service_tables.sql). Grant inline, in the same
-- migration that creates the table, per plan §4.4's explicit instruction.
GRANT SELECT, INSERT ON public.xp_events TO anon, authenticated, service_role;
