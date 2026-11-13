-- Feedback Email (main repo: scripts/scheduledJobs/sendFeedbackDigest.ts) reads and
-- claims public.user_feedback with the service key. 20261104090000 created the
-- table, revoked anon/authenticated, and never granted service_role. This
-- project does not auto-expose new tables (supabase/config.toml,
-- auto_expose_new_tables left unset), and service_role bypasses RLS but still
-- needs the GRANT — the same gap as 20260812111500. Production failed every
-- hourly run with "permission denied for table user_feedback".
--
-- SELECT + UPDATE only: the job lists unnotified rows and stamps notified_at
-- (and clears it if Resend fails). Inserts stay inside submit_feedback().
-- anon and authenticated stay revoked.

GRANT SELECT, UPDATE ON public.user_feedback TO service_role;
