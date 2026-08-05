-- Pronunciation history cloud sync (accent-analyzer plan §13, D3)
--
-- Two tables, per the plan:
--   pronunciation_attempts    — per-attempt scores, mode, locale, provider,
--                               assessorVersion, confidence; retained 90 days
--                               (plan §12, same SYNC_WINDOW_DAYS precedent as
--                               sessions).
--   pronunciation_phoneme_stats — per-(user, locale, phoneme) rolling accuracy
--                               aggregate, kept indefinitely (plan §12: "what
--                               progress tracking actually needs is the
--                               per-phoneme aggregate, not the attempts").
--                               Only written by authoritative-tier (Azure)
--                               results — plan §13.
--
-- client_request_id is unique per user for idempotency (plan §13) — retrying
-- a failed push must not create a duplicate attempt row.

CREATE TABLE IF NOT EXISTS pronunciation_attempts (
  id                  text PRIMARY KEY,               -- client-generated id
  user_id             uuid REFERENCES profiles(id) ON DELETE CASCADE NOT NULL,
  client_request_id   text NOT NULL,
  mode                text NOT NULL,                  -- 'scripted' | 'freeform'
  locale              text NOT NULL,
  provider            text NOT NULL,                  -- 'azure' | 'whisper-heuristic'
  assessor_version    text NOT NULL,
  score               numeric,                        -- null iff couldNotAssess
  could_not_assess    boolean NOT NULL DEFAULT false,
  confidence_overall  numeric,
  reference_text      text,
  transcript          text,
  schema_version      integer NOT NULL DEFAULT 1,      -- PRONUNCIATION_SYNC_SCHEMA_VERSION at write time
  created_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, client_request_id)
);

ALTER TABLE pronunciation_attempts ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own pronunciation attempts"
  ON pronunciation_attempts FOR SELECT TO authenticated USING (auth.uid() = user_id);

CREATE POLICY "Users can insert own pronunciation attempts"
  ON pronunciation_attempts FOR INSERT TO authenticated WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can update own pronunciation attempts"
  ON pronunciation_attempts FOR UPDATE TO authenticated
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can delete own pronunciation attempts"
  ON pronunciation_attempts FOR DELETE TO authenticated USING (auth.uid() = user_id);

CREATE INDEX IF NOT EXISTS pronunciation_attempts_user_created_idx
  ON pronunciation_attempts(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS pronunciation_phoneme_stats (
  user_id       uuid REFERENCES profiles(id) ON DELETE CASCADE NOT NULL,
  locale        text NOT NULL,
  phoneme       text NOT NULL,
  accuracy_ewma numeric NOT NULL,   -- decay-weighted rolling accuracy, decayWeight() half-life
  sample_count  integer NOT NULL DEFAULT 0,
  updated_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, locale, phoneme)
);

ALTER TABLE pronunciation_phoneme_stats ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own phoneme stats"
  ON pronunciation_phoneme_stats FOR SELECT TO authenticated USING (auth.uid() = user_id);

CREATE POLICY "Users can insert own phoneme stats"
  ON pronunciation_phoneme_stats FOR INSERT TO authenticated WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can update own phoneme stats"
  ON pronunciation_phoneme_stats FOR UPDATE TO authenticated
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can delete own phoneme stats"
  ON pronunciation_phoneme_stats FOR DELETE TO authenticated USING (auth.uid() = user_id);

-- Grants: a plain `create table` does not inherit the project's baseline
-- role grants (see 20260717120000_grant_scoring_service_tables.sql — this
-- has already caused one silent 42501 outage). Ship the grant in the same
-- migration as the table this time.
grant select, insert, update, delete
  on public.pronunciation_attempts, public.pronunciation_phoneme_stats
  to anon, authenticated, service_role;
