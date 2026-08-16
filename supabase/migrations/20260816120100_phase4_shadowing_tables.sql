/*
  # Phase 4 — Shadowing Mode: shadowing_attempts (implementation plan
  i-am-implementing-phase-sunny-lagoon.md §3)

  Append-only telemetry table for shadowing attempts. Owner-scoped RLS with
  SELECT + INSERT only -- no UPDATE, no DELETE. These rows feed phrase-level
  trend and (later) mastery analytics; a rewritable history is a corrupt
  history. Idempotent retry is achieved with
  `upsert(row, { onConflict: 'id', ignoreDuplicates: true })`, which
  PostgREST compiles to INSERT ... ON CONFLICT DO NOTHING and needs only the
  INSERT privilege -- exactly the mechanism and reasoning documented in
  src/services/social/xpLedger.ts's header. Do NOT "fix" this to a plain
  upsert; that would require UPDATE, which is deliberately not granted here.

  No FK to pronunciation_attempts -- the two pushes (pronunciationSync +
  the new shadowing push) fail independently. A FK would turn a partial sync
  failure into a hard error and permanently strand the detail row. `id` is a
  soft join key (== pronunciation_attempts.id for the same attempt).

  No `anon` grant, unlike 20260805090000_add_pronunciation_history.sql. RLS
  already blocks anon (auth.uid() is null), and there is no read-before-auth
  use case here the way there might have been for that older table.

  `phrase_id` is intentionally not FK'd -- the corpus is a frontend TS
  constant (src/data/shadowingPhrases.ts), exactly as
  pronunciation_attempts.reference_text already is a free-text field with no
  backing table.
*/

CREATE TABLE public.shadowing_attempts (
  id                 text PRIMARY KEY,       -- client-generated; == pronunciation_attempts.id
  user_id            uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  phrase_id          text NOT NULL,
  provider           text NOT NULL,
  assessor_version   text NOT NULL,
  score              numeric,                -- null iff could_not_assess
  could_not_assess   boolean NOT NULL DEFAULT false,
  sub_scores         jsonb,
  rhythm_metrics     jsonb,
  coaching_delivered boolean NOT NULL DEFAULT false,
  schema_version     integer NOT NULL DEFAULT 1,
  created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX shadowing_attempts_user_phrase_idx
  ON public.shadowing_attempts (user_id, phrase_id, created_at DESC);

ALTER TABLE public.shadowing_attempts ENABLE ROW LEVEL SECURITY;

CREATE POLICY "shadowing_attempts owner read"   ON public.shadowing_attempts
  FOR SELECT TO authenticated USING (auth.uid() = user_id);
CREATE POLICY "shadowing_attempts owner insert" ON public.shadowing_attempts
  FOR INSERT TO authenticated WITH CHECK (auth.uid() = user_id);
-- Append-only: no UPDATE or DELETE policy. See header above.

GRANT SELECT, INSERT ON public.shadowing_attempts TO authenticated, service_role;
