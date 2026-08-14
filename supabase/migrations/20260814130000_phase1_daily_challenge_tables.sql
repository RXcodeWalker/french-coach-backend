/*
  # Daily Challenge Phase 1 — tables (revised plan §"Full corrected backend
  schema/RPC set", part 1)

  daily_challenge_assignments: one row per UTC day, seeded by seed_daily_challenge
  (see _seed_rpc.sql). question_set_id references the closed 10-set corpus
  (igcse_question_sets), not a single question — the only server-authoritative
  scorer (POST /score) grades a whole question set as one attempt (see plan's
  "Roadmap correction" note).

  Named `daily_challenge_assignments`, not `daily_challenges` as the plan
  document literally names it: backend/supabase/migrations/20260503093957_french_coach_schema.sql
  (the original base schema) already defines a table called `daily_challenges`
  with a completely different, per-user shape (id uuid PK, user_id,
  challenge_date, question_text, completed boolean) backing a live
  `GET /api/questions/daily` FastAPI endpoint (backend/main.py) and seeded by
  backend/seed_questions.py — a single-question-per-day design that predates
  this repo's IGCSE-only assessment-engine rewrite. It is unreachable from the
  current frontend (Home.tsx's "Daily Challenge" card is fully static; no
  apiClient.ts call ever hits that endpoint), so it is dead in practice but
  NOT dead in code — it is real, seeded, self-consistent functionality, not
  orphaned scaffolding, and dropping/renaming it is a product decision outside
  this migration's scope. Renaming the new table sidesteps the collision
  without touching that legacy feature. Flagged separately for whoever owns
  product direction to decide: formally deprecate/remove the legacy
  single-question daily challenge, or reconcile it with this one.

  daily_challenge_sessions: Fix 2's session-reservation mechanism. Binds a
  server-minted session_id to (user, challenge_date) BEFORE the exam runs, so
  submit_daily_challenge_attempt can prove "this envelope was produced as
  today's Daily Challenge attempt" rather than just "this envelope happens to
  match today's question set" (which a coincidental same-day ExamMode practice
  run could also satisfy). Mutation only via start_daily_challenge (see
  _rpcs.sql) — no INSERT/UPDATE/DELETE policy.

  daily_challenge_attempts: one row per completed, claimed attempt.
  UNIQUE(user_id, challenge_date) is the one-attempt-per-day guarantee;
  UNIQUE(attempt_id) additionally prevents the same scoring_envelopes row from
  ever backing two different claims (cross-user or same-user replay). RLS
  simplified to owner-only read (see plan's "Optional cleanup — accepted"):
  daily_challenge_leaderboard (a view, added in the next migration) runs with
  its owner's privileges and bypasses this RLS entirely, matching the existing
  weekly_leaderboard/xp_events pattern — no second "leaderboard read" policy
  needed here.
*/

CREATE TABLE public.daily_challenge_assignments (
  challenge_date  date PRIMARY KEY,
  question_set_id text NOT NULL REFERENCES public.igcse_question_sets(id),
  seeded_at       timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT ON public.daily_challenge_assignments TO anon, authenticated, service_role;
-- No RLS: challenge assignment is public information (every user gets the
-- same question set for a given day), mirroring igcse_question_sets' public
-- read policy for published rows.

CREATE TABLE public.daily_challenge_sessions (
  user_id        uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  challenge_date date NOT NULL REFERENCES public.daily_challenge_assignments(challenge_date),
  session_id     text NOT NULL UNIQUE,
  created_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, challenge_date)
);

ALTER TABLE public.daily_challenge_sessions ENABLE ROW LEVEL SECURITY;

CREATE POLICY "daily_challenge_sessions owner read" ON public.daily_challenge_sessions
  FOR SELECT USING (auth.uid() = user_id);
-- No INSERT/UPDATE/DELETE policy — mutation only via start_daily_challenge.

GRANT SELECT ON public.daily_challenge_sessions TO authenticated, service_role;

CREATE TABLE public.daily_challenge_attempts (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id        uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  challenge_date date NOT NULL REFERENCES public.daily_challenge_assignments(challenge_date),
  attempt_id     text NOT NULL UNIQUE REFERENCES public.scoring_envelopes(attempt_id),
  score_total    numeric NOT NULL,
  xp_awarded     integer NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, challenge_date)
);

CREATE INDEX daily_challenge_attempts_date_idx ON public.daily_challenge_attempts (challenge_date);

ALTER TABLE public.daily_challenge_attempts ENABLE ROW LEVEL SECURITY;

CREATE POLICY "daily_challenge_attempts owner read" ON public.daily_challenge_attempts
  FOR SELECT USING (auth.uid() = user_id);
-- No INSERT/UPDATE/DELETE policy — mutation only via submit_daily_challenge_attempt.

GRANT SELECT ON public.daily_challenge_attempts TO authenticated, service_role;
