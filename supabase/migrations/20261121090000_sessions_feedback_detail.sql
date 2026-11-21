-- The full feedback a learner was shown for one answer (main repo:
-- src/services/sync/feedbackDetail.ts, written by sessionSync.ts's sessionToRow).
--
-- `sessions.feedback` holds only a short summary (scores, CEFR level, one-line
-- next step and highlight, critical-issue count). That is what restore and the
-- teacher/parent readers use. It is not enough to answer a complaint like "the
-- app said my sentence was wrong but it's fine": the corrections, quoted errors
-- and improved answer the learner saw were never stored anywhere.
--
-- This is a SEPARATE column on purpose. teacher_student_sessions returns the
-- whole `feedback` blob and get_parent_report reads keys out of it; neither
-- selects feedback_detail, so adding it widens no teacher or parent view. Keep
-- it that way: no RPC, view or scheduled job may select feedback_detail (the
-- owner reads it with the service role, e.g. the SQL editor). The client writes
-- it and never reads it back (restore does not select it).
--
-- Access is the existing sessions RLS (owner-only SELECT/INSERT/UPDATE) and table
-- grants; export_my_data's to_jsonb(s) includes it in the learner's own export,
-- and delete_my_account's cascade removes it.
--
-- Size: the client caps the blob at 16 KB of JSON (real replies are ~5 KB
-- median, ~10 KB max); the CHECK refuses anything over 32 KB so a modified
-- client cannot bloat the table.

ALTER TABLE public.sessions
  ADD COLUMN IF NOT EXISTS feedback_detail jsonb;

ALTER TABLE public.sessions
  DROP CONSTRAINT IF EXISTS sessions_feedback_detail_check;

ALTER TABLE public.sessions
  ADD CONSTRAINT sessions_feedback_detail_check CHECK (
    feedback_detail IS NULL
    OR (jsonb_typeof(feedback_detail) = 'object' AND octet_length(feedback_detail::text) <= 32768)
  );

COMMENT ON COLUMN public.sessions.feedback_detail IS
  'Full feedback shown for this answer (corrections, quoted errors, improved answer). Debugging only: no RPC or view may select it.';
