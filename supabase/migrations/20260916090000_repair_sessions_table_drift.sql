/*
  # Repair `public.sessions` schema drift

  The live `sessions` table was created out-of-band before
  20260503093957_french_coach_schema.sql ever ran, so that file's
  `CREATE TABLE IF NOT EXISTS sessions (...)` silently did nothing and the
  declared shape has never actually existed in production. Observed live
  shape (via the PostgREST OpenAPI document, 2026-09-16):

      id uuid PK, user_id uuid NULL, mode, topic_key, question_id,
      question_text, transcript, word_count, score, duration_sec,
      feedback_json jsonb, is_past_paper boolean, created_at

  versus what `src/services/sync/sessionSync.ts` reads and writes:

      id, user_id, mode, topic_key, question_text, transcript, word_count,
      score, xp_earned, duration_sec, feedback, created_at

  Three consequences, all reproduced in the browser console:
    * SELECT 400s — `column sessions.xp_earned does not exist`
    * INSERT 400s — same, plus `feedback` vs `feedback_json`
    * INSERT would 400 anyway — the client generates `sess-<ts>-<rand>` ids,
      which are not valid uuids. 20260808064609 flagged this and deliberately
      left it; it is fixed here because the table is being rebuilt regardless.

  Rebuilt via DROP + CREATE rather than a column-by-column ALTER because the
  table is empty — `count=exact` over the service role (which bypasses RLS)
  reports 0 rows globally, so there is no data to preserve, and no foreign key
  anywhere in the schema references sessions.id. The legacy-only columns
  (question_id, is_past_paper, feedback_json) go with it; nothing in src/ or
  backend/ reads them.

  Policies and grants are recreated here because dropping the table takes them
  with it — including the UPDATE policy added by 20260808064609 and the grants
  from 20260811090000.
*/

DROP TABLE IF EXISTS public.sessions CASCADE;

CREATE TABLE public.sessions (
  id text PRIMARY KEY,
  user_id uuid REFERENCES public.profiles(id) ON DELETE CASCADE NOT NULL,
  mode text NOT NULL DEFAULT 'practice',
  topic_key text,
  question_text text,
  transcript text,
  word_count integer DEFAULT 0,
  score numeric(4,2) DEFAULT 0,
  xp_earned integer DEFAULT 0,
  duration_sec integer DEFAULT 0,
  feedback jsonb,
  created_at timestamptz DEFAULT now()
);

CREATE INDEX sessions_user_id_created_at_idx
  ON public.sessions (user_id, created_at DESC);

ALTER TABLE public.sessions ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own sessions"
  ON public.sessions FOR SELECT
  TO authenticated
  USING (auth.uid() = user_id);

CREATE POLICY "Users can insert own sessions"
  ON public.sessions FOR INSERT
  TO authenticated
  WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can update own sessions"
  ON public.sessions FOR UPDATE
  TO authenticated
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);

GRANT SELECT, INSERT, UPDATE         ON public.sessions TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.sessions TO service_role;
