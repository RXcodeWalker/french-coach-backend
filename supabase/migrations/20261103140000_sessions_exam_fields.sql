-- Exam-attempt fields on `sessions` (main repo: src/services/sync/sessionSync.ts).
--
-- practice_only (Coached / early-start / resumed attempts, ADR-0007), exam_marks
-- (the three 0520 area marks + total) and scoring_session_id used to be dropped
-- by sessionToRow, so an exam restored on a new device lost them and counted
-- toward progress again. The client now writes and reads them back.
--
-- These columns already exist in production: 20261103090000_teacher_dashboard
-- (applied 2026-11-03, on the teacher-dashboard branch) added them. This file
-- records them on main and is a no-op where they exist. assignment_id belongs to
-- that migration and is not repeated here. The existing "Users can update own
-- sessions" policy covers the client's exam-field repair update.

ALTER TABLE public.sessions
  ADD COLUMN IF NOT EXISTS practice_only boolean,
  ADD COLUMN IF NOT EXISTS exam_marks jsonb,
  ADD COLUMN IF NOT EXISTS scoring_session_id text;

-- Restore pulls the newest 500 rows per user (was a 90-day window).
CREATE INDEX IF NOT EXISTS sessions_user_created_idx ON public.sessions (user_id, created_at);
