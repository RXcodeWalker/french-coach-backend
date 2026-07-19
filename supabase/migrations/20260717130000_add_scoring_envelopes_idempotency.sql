/*
  # Phase B — enforce one-original-envelope-per-session at the database

  scoring_envelopes is keyed by attempt_id (not session_id) so a session can
  be scored more than once via regrades (buildEnvelope.ts's regradedFrom).
  That means nothing today stops two concurrent POST /score calls for the
  same sessionId from both passing the app-level "does an envelope already
  exist?" check and both scoring — a check-then-act race. This migration
  makes the invariant a DB constraint instead of an application promise:
  exactly one *original* envelope (regraded_from is null) per session_id;
  regrades remain unlimited, each carrying the attempt_id it superseded.

  regraded_from is a text FK-shaped reference to another row's attempt_id,
  not a real foreign key — mirrors regradedFrom's optional-string shape in
  ScoringEnvelope (envelope/types.ts) and avoids self-referential FK
  complexity for a column that's audit metadata, not a join target.
*/

alter table public.scoring_envelopes
  add column if not exists regraded_from text;

create unique index if not exists scoring_envelopes_one_original_per_session
  on public.scoring_envelopes (session_id)
  where regraded_from is null;
