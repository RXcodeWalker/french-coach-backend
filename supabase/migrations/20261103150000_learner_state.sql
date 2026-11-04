-- Learn state that used to live only on the device (main repo:
-- src/services/sync/learnerStateSync.ts). A learner who changed phone kept their
-- sessions but lost topic progress (sessions per topic, questions answered, gold
-- badges), the spaced-review (SM-2) schedule and their Learn settings.
--
-- One row per (user, key); value is a JSON object the client merges additively
-- on sign-in and upserts when it changes. Keys are a closed list — the notebook,
-- the Pronunciation Lab history, the Junior track and game bests stay local-only
-- by owner decision and must never be added here. Deleting the profile removes
-- the rows (FK cascade, delete_my_account()).
--
-- Size: the largest value (review_pool, ~150 bytes per question, 828 questions)
-- is under 130 KB; the cap is 256 KB per row.

CREATE TABLE IF NOT EXISTS public.learner_state (
  user_id     uuid NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  key         text NOT NULL CHECK (key IN ('topic_mastery', 'review_pool', 'learn_settings')),
  value       jsonb NOT NULL CHECK (jsonb_typeof(value) = 'object' AND octet_length(value::text) <= 262144),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, key)
);

ALTER TABLE public.learner_state ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON public.learner_state FROM anon;
REVOKE DELETE, TRUNCATE ON public.learner_state FROM authenticated;
GRANT SELECT, INSERT, UPDATE ON public.learner_state TO authenticated;

CREATE POLICY "Users can view own learner state"
  ON public.learner_state FOR SELECT TO authenticated USING ((select auth.uid()) = user_id);

CREATE POLICY "Users can insert own learner state"
  ON public.learner_state FOR INSERT TO authenticated WITH CHECK ((select auth.uid()) = user_id);

CREATE POLICY "Users can update own learner state"
  ON public.learner_state FOR UPDATE TO authenticated
  USING ((select auth.uid()) = user_id) WITH CHECK ((select auth.uid()) = user_id);
