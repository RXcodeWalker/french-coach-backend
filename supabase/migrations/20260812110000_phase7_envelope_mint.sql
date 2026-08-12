/*
  # Shop Phase 7 — server-observed mint from verified scoring_envelopes
  (plan §15 Phase 7: "Server-observed work (signed transcripts from
  /api/transcribe, or server-side mint from verified scoring_envelopes)")

  Status (must be read before relying on this in any way): scoring_envelopes
  and session_transcripts are both CHECK-gated to content_provenance =
  'original-practice' (20260710055745, 20260710103213), and both tables'
  own migration headers say verbatim "For the whole of Phase A, every
  session is confidential-internal, so this table stays empty." Original-
  provenance content does not exist yet — it is the Assessment Engine
  project's S11 (question-bank content authoring), which is itself gated
  behind that project's Phase B (S9), neither of which has run. This
  migration is infrastructure only: it is additive alongside the existing
  client-asserted mint_gems (Phase 1) and does not replace it, does not
  change any client call site, and mints nothing until real
  original-practice envelopes exist. Do not treat a clean run of this
  migration's tests (which use fabricated fixture envelope rows) as proof
  that end-to-end integrity holds against real data — it proves the
  mechanism is correct against the documented shape, nothing more. The
  final production verification gate is deferred to when the Assessment
  Engine actually populates scoring_envelopes with original-practice rows.

  Design: one gem_events row of kind='earn' per attempt_id, minted once,
  amount derived server-side from the envelope's own `total` field (never
  client-supplied) via a fixed, documented conversion so the mint amount
  can't be gamed by forging the request payload — only by forging the
  envelope row itself, which is exactly what A5's REVOKE (already in
  place, 20260811102000) prevents for anon/authenticated. Idempotency is
  the attempt_id itself, not a client-supplied key — a given attempt can
  only ever mint once, by construction, regardless of how many times this
  is called. This also means it needs no cap check: attempts are finite
  and each mints once, so there is no repeatable-action cap to bound.

  Conversion: plan §5's earning table gives ~5 gems per answer at score
  ~7/10. An envelope's `total` is out of 40 (rolePlay 0-6 + communication
  0-17 + qualityOfLanguage 0-17, per 01-cambridge-rubric-source.md). To
  keep this on the same order of magnitude as the existing per-answer
  mint (1-20 bounded in mint_gems) without inventing a new unaudited
  constant, the amount is total/40*20 rounded, i.e. the same 1..20 bound
  mint_gems already uses, scaled linearly by the envelope's own score
  fraction. A perfect envelope mints the same ceiling a client-asserted
  perfect answer already could.
*/

CREATE OR REPLACE FUNCTION public.mint_gems_from_envelope(p_attempt_id text)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid := auth.uid();
  v_key text;
  v_envelope public.scoring_envelopes%ROWTYPE;
  v_total numeric;
  v_amount int;
  v_balance bigint;
BEGIN
  IF me IS NULL THEN
    RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000';
  END IF;

  SELECT * INTO v_envelope
  FROM public.scoring_envelopes
  WHERE attempt_id = p_attempt_id AND user_id = me;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'unknown_envelope' USING ERRCODE = '22023';
  END IF;

  IF v_envelope.content_provenance <> 'original-practice' THEN
    RAISE EXCEPTION 'not_original_practice' USING ERRCODE = '22023';
  END IF;

  v_key := me::text || ':envelope:' || p_attempt_id;

  IF EXISTS (SELECT 1 FROM public.gem_events WHERE id = v_key) THEN
    SELECT COALESCE(SUM(delta), 0) INTO v_balance FROM public.gem_events WHERE user_id = me;
    RETURN jsonb_build_object('ok', true, 'replayed', true, 'balance', v_balance);
  END IF;

  v_total := (v_envelope.envelope->>'total')::numeric;
  IF v_total IS NULL OR v_total < 0 OR v_total > 40 THEN
    RAISE EXCEPTION 'invalid_envelope_total' USING ERRCODE = '22023';
  END IF;

  v_amount := GREATEST(1, LEAST(20, ROUND(v_total / 40 * 20)));

  PERFORM 1 FROM public.profiles WHERE id = me FOR UPDATE;

  -- gem_events_occurred_at_bounds requires occurred_at within 30 days of
  -- this row's own created_at (defaults to now()). An old envelope (e.g. a
  -- late mint call, or a future backfill) would otherwise fail the CHECK
  -- outright — clamp to now() rather than raising, since the envelope's
  -- own created_at is already the authoritative "when was this scored"
  -- fact recorded in metadata below.
  INSERT INTO public.gem_events (id, user_id, delta, kind, metadata, occurred_at)
  VALUES (
    v_key, me, v_amount, 'earn',
    jsonb_build_object('source', 'scoring_envelope', 'attempt_id', p_attempt_id, 'session_id', v_envelope.session_id),
    GREATEST(v_envelope.created_at, now() - interval '30 days')
  );

  SELECT COALESCE(SUM(delta), 0) INTO v_balance FROM public.gem_events WHERE user_id = me;
  RETURN jsonb_build_object('ok', true, 'balance', v_balance, 'amount', v_amount);
END;
$$;

REVOKE EXECUTE ON FUNCTION public.mint_gems_from_envelope(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.mint_gems_from_envelope(text) TO authenticated;
