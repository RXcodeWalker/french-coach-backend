/*
  # Fix: grant PostgREST roles on session_transcripts and scoring_envelopes

  Same fix as 20260716121500_grant_igcse_question_sets.sql, for the two
  tables the scoring service (server/index.ts) reads and writes.
  session_transcripts (20260710103213) and scoring_envelopes (20260710055745)
  were both created by CLI migration before that fix landed, so a plain
  `create table` never picked up the project's baseline anon/authenticated/
  service_role grants -- RLS policies only take effect once the role already
  has the underlying SQL privilege. Without this, service_role gets
  `permission denied for table session_transcripts` (42501, a GRANT-level
  error, not an RLS denial) the first time the scoring service tries to
  upsert a transcript or envelope.
*/

grant select, insert, update, delete
  on public.session_transcripts, public.scoring_envelopes
  to anon, authenticated, service_role;
