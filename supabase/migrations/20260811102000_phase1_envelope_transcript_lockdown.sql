/*
  # A5 — close the forged-envelope hole (Shop plan §A5, §14.2)

  Not Shop scope on its own, but bundled into Phase 1 per the plan's
  explicit instruction ("not strictly Shop scope, but it is the only
  server-computed record of graded work and it's 4 lines").

  scoring_envelopes and session_transcripts both had owner-write INSERT
  policies with no `TO` clause (= TO PUBLIC) plus a blanket
  GRANT INSERT ON ... TO anon, authenticated (20260717120000) — so any
  client, authenticated or not, could forge an envelope. scoring_envelopes'
  partial unique index (one original per session_id, 20260808070000... see
  20260808064609) means a forged row can win the slot and get read back as
  authoritative. Reads stay; server/index.ts writes with
  SUPABASE_SERVICE_KEY and is unaffected by this REVOKE.
*/

DROP POLICY "scoring_envelopes owner write" ON public.scoring_envelopes;
DROP POLICY "session_transcripts owner write" ON public.session_transcripts;

REVOKE INSERT, UPDATE, DELETE ON public.scoring_envelopes, public.session_transcripts
  FROM anon, authenticated;
