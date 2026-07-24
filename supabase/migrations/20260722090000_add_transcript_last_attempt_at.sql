/*
  # Exam scoring feedback reliability — session_transcripts.last_attempt_at

  GET /score's existence-only check (does an envelope row exist?) can't tell
  "an attempt is still running" apart from "the process that was scoring it
  is gone" — a session_transcripts row is written before scoring starts and
  looks identical in both cases. This column adds the missing signal:
  supabaseTranscriptStore.ts's save() now stamps it on every call (including
  a legitimate resubmission), so GET /score can compare it against a
  staleness threshold to distinguish "plausibly still scoring" (202) from
  "no envelope and no recent attempt" (404, safe to resubmit).

  Additive, default now() — no impact on existing rows or scoreAttempt/the
  scoring pipeline itself.
*/

alter table public.session_transcripts
  add column if not exists last_attempt_at timestamptz not null default now();
