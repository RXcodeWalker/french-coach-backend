/*
  # Backfill profiles base columns

  20260503093957_french_coach_schema.sql defined `profiles` via
  CREATE TABLE IF NOT EXISTS, but a legacy `profiles` table (full_name,
  streak, streak_last_date, total_words) already existed in production at
  that point, so the CREATE TABLE was a silent no-op. Every later migration
  used ADD COLUMN IF NOT EXISTS and landed fine on top of the legacy table
  (gems, achievements, inventory, active_boosters, migration_version all
  exist) — but the columns that were only ever declared in that skipped
  CREATE TABLE (username, total_xp, current_level, streak_days,
  longest_streak, last_session_date, sessions_count, total_words_spoken)
  were never added. This is what breaks claim_username/weekly_leaderboard
  (phase2/phase3 migrations reference profiles.username) and progressionSync
  (selects/upserts total_xp, username).

  Legacy columns (full_name, streak, streak_last_date, total_words) are left
  in place, unused — not renamed/dropped, per decision to avoid a data
  migration on live user rows.
*/

ALTER TABLE profiles
  ADD COLUMN IF NOT EXISTS username text,
  ADD COLUMN IF NOT EXISTS total_xp integer DEFAULT 0,
  ADD COLUMN IF NOT EXISTS current_level text DEFAULT 'Beginner',
  ADD COLUMN IF NOT EXISTS streak_days integer DEFAULT 0,
  ADD COLUMN IF NOT EXISTS longest_streak integer DEFAULT 0,
  ADD COLUMN IF NOT EXISTS last_session_date date,
  ADD COLUMN IF NOT EXISTS sessions_count integer DEFAULT 0,
  ADD COLUMN IF NOT EXISTS total_words_spoken integer DEFAULT 0;
