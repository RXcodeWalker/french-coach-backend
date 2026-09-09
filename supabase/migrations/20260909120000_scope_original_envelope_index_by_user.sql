/*
  # Exam IDOR (Phase 1.1) — scope the one-original-per-session invariant by user

  The scoring service (server/index.ts) reads scoring_envelopes and
  session_transcripts with the Supabase SERVICE key, which bypasses RLS. The
  `owner read` SELECT policies therefore do nothing for the server path; the
  only owner-enforcement point there is the store code, which now filters
  `user_id = <authenticated caller>` on every read (see
  scripts/scoring/supabaseEnvelopeStore.ts,
  scripts/stt/supabaseTranscriptStore.ts in the main repo).

  This migration realigns the DB objects with that scoped read model:

  1. `scoring_envelopes_one_original_per_session` was `unique (session_id)
     where regraded_from is null` — a global constraint across all users. With
     random free-play sessionIds a cross-user collision is astronomically
     unlikely, but the invariant should still be "one original per (user,
     session)", not "one original per session globally": otherwise a foreign
     user who guessed a sessionId could pre-empt the real owner's slot with a
     forged row (client INSERT is already revoked — 20260811102000 — so this
     is defence-in-depth, not the live hole). Recreate it scoped to
     (user_id, session_id).

  2. Add a covering `(user_id, session_id)` index for the newly-scoped
     `.eq('user_id', ...).eq('session_id', ...)` reads
     (load-by-session / listBySession / the saveOriginal 23505-recovery
     select). The existing `scoring_envelopes_session_id_idx` (session_id
     only) stays — it still serves any session_id-first lookup.

  session_transcripts needs no schema change: its PK is `session_id` and the
  store now does an explicit ownership pre-check before upserting (throws
  TranscriptOwnershipError rather than clobbering a foreign row). Re-keying
  the table was rejected as higher-risk and unnecessary given existing data
  is untouched.

  Precedent for the (…, user_id) filter pattern: mint_gems_from_envelope
  (20260812110000) filters `WHERE attempt_id = … AND user_id = me`.
*/

drop index if exists public.scoring_envelopes_one_original_per_session;

create unique index if not exists scoring_envelopes_one_original_per_session
  on public.scoring_envelopes (user_id, session_id)
  where regraded_from is null;

create index if not exists scoring_envelopes_user_session_idx
  on public.scoring_envelopes (user_id, session_id);
