/*
  # French Coach - Gamified Learning Platform Schema

  1. New Tables
    - `profiles` - User profile with XP, level, streak data
    - `sessions` - Individual practice/exam sessions
    - `achievements` - Achievement unlock tracking
    - `skill_snapshots` - Skill mastery over time
    - `daily_challenges` - Daily challenge completions

  2. Security
    - RLS enabled on all tables
    - Users can only access their own data
*/

-- Profiles table: gamification state per user
CREATE TABLE IF NOT EXISTS profiles (
  id uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  username text,
  total_xp integer DEFAULT 0,
  current_level text DEFAULT 'Beginner',
  streak_days integer DEFAULT 0,
  longest_streak integer DEFAULT 0,
  last_session_date date,
  sessions_count integer DEFAULT 0,
  total_words_spoken integer DEFAULT 0,
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);

ALTER TABLE profiles ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own profile"
  ON profiles FOR SELECT
  TO authenticated
  USING (auth.uid() = id);

CREATE POLICY "Users can insert own profile"
  ON profiles FOR INSERT
  TO authenticated
  WITH CHECK (auth.uid() = id);

CREATE POLICY "Users can update own profile"
  ON profiles FOR UPDATE
  TO authenticated
  USING (auth.uid() = id)
  WITH CHECK (auth.uid() = id);

-- Sessions table: individual practice/exam records
CREATE TABLE IF NOT EXISTS sessions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid REFERENCES profiles(id) ON DELETE CASCADE NOT NULL,
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

ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own sessions"
  ON sessions FOR SELECT
  TO authenticated
  USING (auth.uid() = user_id);

CREATE POLICY "Users can insert own sessions"
  ON sessions FOR INSERT
  TO authenticated
  WITH CHECK (auth.uid() = user_id);

-- Achievements table
CREATE TABLE IF NOT EXISTS achievements (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid REFERENCES profiles(id) ON DELETE CASCADE NOT NULL,
  achievement_id text NOT NULL,
  achievement_name text NOT NULL,
  achieved_at timestamptz DEFAULT now(),
  UNIQUE(user_id, achievement_id)
);

ALTER TABLE achievements ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own achievements"
  ON achievements FOR SELECT
  TO authenticated
  USING (auth.uid() = user_id);

CREATE POLICY "Users can insert own achievements"
  ON achievements FOR INSERT
  TO authenticated
  WITH CHECK (auth.uid() = user_id);

-- Skill snapshots for progress tracking
CREATE TABLE IF NOT EXISTS skill_snapshots (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid REFERENCES profiles(id) ON DELETE CASCADE NOT NULL,
  grammar_score numeric(4,2) DEFAULT 0,
  vocabulary_score numeric(4,2) DEFAULT 0,
  fluency_score numeric(4,2) DEFAULT 0,
  communication_score numeric(4,2) DEFAULT 0,
  overall_score numeric(4,2) DEFAULT 0,
  recorded_at timestamptz DEFAULT now()
);

ALTER TABLE skill_snapshots ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own skill snapshots"
  ON skill_snapshots FOR SELECT
  TO authenticated
  USING (auth.uid() = user_id);

CREATE POLICY "Users can insert own skill snapshots"
  ON skill_snapshots FOR INSERT
  TO authenticated
  WITH CHECK (auth.uid() = user_id);

-- Daily challenges
CREATE TABLE IF NOT EXISTS daily_challenges (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid REFERENCES profiles(id) ON DELETE CASCADE NOT NULL,
  challenge_date date NOT NULL,
  question_text text,
  completed boolean DEFAULT false,
  xp_earned integer DEFAULT 0,
  completed_at timestamptz,
  UNIQUE(user_id, challenge_date)
);

ALTER TABLE daily_challenges ENABLE ROW LEVEL SECURITY;

-- Only create user-scoped policies when the table has a user_id column.
-- The remote DB may have a different daily_challenges schema (question-pool table
-- without user_id), in which case these policies are inapplicable and skipped.
DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name   = 'daily_challenges'
      AND column_name  = 'user_id'
  ) THEN
    IF NOT EXISTS (
      SELECT 1 FROM pg_policies
      WHERE schemaname = 'public' AND tablename = 'daily_challenges'
        AND policyname = 'Users can view own challenges'
    ) THEN
      EXECUTE $p$
        CREATE POLICY "Users can view own challenges"
          ON daily_challenges FOR SELECT TO authenticated
          USING (auth.uid() = user_id)
      $p$;
    END IF;

    IF NOT EXISTS (
      SELECT 1 FROM pg_policies
      WHERE schemaname = 'public' AND tablename = 'daily_challenges'
        AND policyname = 'Users can insert own challenges'
    ) THEN
      EXECUTE $p$
        CREATE POLICY "Users can insert own challenges"
          ON daily_challenges FOR INSERT TO authenticated
          WITH CHECK (auth.uid() = user_id)
      $p$;
    END IF;

    IF NOT EXISTS (
      SELECT 1 FROM pg_policies
      WHERE schemaname = 'public' AND tablename = 'daily_challenges'
        AND policyname = 'Users can update own challenges'
    ) THEN
      EXECUTE $p$
        CREATE POLICY "Users can update own challenges"
          ON daily_challenges FOR UPDATE TO authenticated
          USING (auth.uid() = user_id)
          WITH CHECK (auth.uid() = user_id)
      $p$;
    END IF;

    CREATE INDEX IF NOT EXISTS daily_challenges_user_id_date_idx
      ON daily_challenges(user_id, challenge_date);
  END IF;
END $$;

-- Indexes for performance
CREATE INDEX IF NOT EXISTS sessions_user_id_idx ON sessions(user_id);
CREATE INDEX IF NOT EXISTS sessions_created_at_idx ON sessions(created_at);
CREATE INDEX IF NOT EXISTS achievements_user_id_idx ON achievements(user_id);
CREATE INDEX IF NOT EXISTS skill_snapshots_user_id_idx ON skill_snapshots(user_id);
