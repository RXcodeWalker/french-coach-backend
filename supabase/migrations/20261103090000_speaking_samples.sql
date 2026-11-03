-- Learn speaking log, cloud copy (main repo: src/services/fluency/speakingLog.ts,
-- src/services/sync/speakingLogSync.ts).
--
-- One row per spoken Learn answer: flow measurements of the learner's own
-- recording (start delay, pauses, speech/articulation rate, words per run) taken
-- in the browser by domain/fluency/speechMetrics.ts, plus ids and two scores.
-- No audio and no transcript text. Rows are written once by the client and never
-- changed, so users get SELECT + INSERT only; the client pushes with
-- ON CONFLICT DO NOTHING, so a retried push is a no-op. Deleting the profile
-- removes every row (FK cascade, delete_my_account()).
--
-- Size: ~250 bytes a row incl. index; 3 answers/day/account (the launch Learn
-- limit) is ~0.3 MB per active account per year.

CREATE TABLE IF NOT EXISTS public.speaking_samples (
  user_id            uuid NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  id                 text NOT NULL CHECK (char_length(id) <= 64),   -- client-generated
  created_at         timestamptz NOT NULL,                          -- when the answer was evaluated
  metrics_version    smallint NOT NULL CHECK (metrics_version BETWEEN 1 AND 1000),
  surface            text NOT NULL CHECK (surface IN ('learn')),
  style              text NOT NULL CHECK (style IN ('coach', 'examiner')),
  session_id         text CHECK (char_length(session_id) <= 64),
  topic_key          text CHECK (char_length(topic_key) <= 64),
  question_id        text NOT NULL CHECK (char_length(question_id) <= 128),
  follow_up          boolean NOT NULL,
  transcript_source  text NOT NULL CHECK (transcript_source IN ('whisper', 'captions')),
  score              real CHECK (score BETWEEN 0 AND 10),
  ai_fluency         real CHECK (ai_fluency BETWEEN 0 AND 10),
  duration_sec       real NOT NULL CHECK (duration_sec BETWEEN 0 AND 3600),
  recorded_s         real NOT NULL CHECK (recorded_s BETWEEN 0 AND 3600),
  start_delay_s      real NOT NULL CHECK (start_delay_s BETWEEN 0 AND 3600),
  speaking_span_s    real NOT NULL CHECK (speaking_span_s BETWEEN 0 AND 3600),
  voiced_s           real NOT NULL CHECK (voiced_s BETWEEN 0 AND 3600),
  pause_count        smallint NOT NULL CHECK (pause_count >= 0),
  pauses_over_1s     smallint NOT NULL CHECK (pauses_over_1s >= 0),
  pauses_over_2s     smallint NOT NULL CHECK (pauses_over_2s >= 0),
  longest_pause_s    real NOT NULL CHECK (longest_pause_s BETWEEN 0 AND 3600),
  total_pause_s      real NOT NULL CHECK (total_pause_s BETWEEN 0 AND 3600),
  word_count         smallint NOT NULL CHECK (word_count >= 0),
  words_per_minute   real CHECK (words_per_minute BETWEEN 0 AND 1000),
  articulation_wpm   real CHECK (articulation_wpm BETWEEN 0 AND 1000),
  words_per_run      real CHECK (words_per_run BETWEEN 0 AND 10000),
  PRIMARY KEY (user_id, id)
);

CREATE INDEX IF NOT EXISTS speaking_samples_user_created_idx
  ON public.speaking_samples (user_id, created_at DESC);

ALTER TABLE public.speaking_samples ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON public.speaking_samples FROM anon;
REVOKE UPDATE, DELETE, TRUNCATE ON public.speaking_samples FROM authenticated;
GRANT SELECT, INSERT ON public.speaking_samples TO authenticated;

CREATE POLICY "Users can view own speaking samples"
  ON public.speaking_samples FOR SELECT TO authenticated USING ((select auth.uid()) = user_id);

CREATE POLICY "Users can insert own speaking samples"
  ON public.speaking_samples FOR INSERT TO authenticated WITH CHECK ((select auth.uid()) = user_id);
