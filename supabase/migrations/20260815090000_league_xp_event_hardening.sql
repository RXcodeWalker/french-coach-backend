/*
  # League Power prerequisite — XP event hardening (revised plan
  i-am-implementing-phase-hashed-karp.md, Part A1)

  Adds submit_xp_event, a client-callable SECURITY DEFINER RPC that becomes
  the sole write path for client-originated XP going forward, and revokes
  direct client INSERT on xp_events (20260808064748_add_xp_events_ledger.sql
  granted INSERT to anon/authenticated directly — that grant is closed here).

  This is bounded/trusted XP ingestion, not anti-cheat: p_amount is still
  fully client-claimed (unchanged from the existing direct-insert model).
  Only WHEN and HOW FAST events can be submitted is bounded here, via:

  1. Idempotency-before-cap ordering: the "does this event already exist?"
     check runs BEFORE the rolling-cap check, under a per-user advisory lock.
     A retried submission of an already-recorded event always returns
     success, even if the account is currently over the cap from other
     events synced since the original attempt.
  2. User-namespaced idempotency key: the stored xp_events.id is
     `auth.uid()::text || ':' || p_idempotency_key`, not the raw
     client-supplied key. Client-generated ids
     (`xp-${Date.now()}-${6 random base36 chars}`, see xpLedger.ts's
     makeXpEventId) are short pseudo-random strings, not cryptographically
     unguessable — namespacing by caller means two different auth.uid()
     values can never collide on the same underlying row id, regardless of
     what raw key either submits.
  3. Race-safe cap via advisory lock:
     pg_advisory_xact_lock(hashtext('submit_xp_event:' || me::text)),
     acquired immediately after the auth/source checks and held for the rest
     of the transaction, serializes all concurrent submit_xp_event calls from
     the SAME user (not a global lock — different users are not serialized
     against each other).
  4. Rolling 24-hour cap (not a UTC-calendar-day cap): created_at >= now() -
     interval '24 hours'. A sliding window is a better circuit breaker than a
     UTC-midnight reset, which would let a burst right after the boundary
     double up. Cap is 3000 (user's explicit choice — observed honest
     steady-state usage is ~300-600 XP/day, so this leaves 5-10x headroom;
     documented as a deliberate choice, not silently decided). Only
     WHERE amount > 0 counts toward the cap — negative XP (e.g.
     DailyNewsFlash.tsx's -5 "reveal transcript" penalty, confirmed at
     DailyNewsFlash.tsx:250) never counts against it and can never be
     blocked by it.
  5. daily_challenge/friend_challenge stay server-only: those sources are
     already written exclusively via the existing internal award_xp(...)
     (20260814130200_phase1_daily_challenge_xp_hardening.sql, service_role
     only, called function-to-function from submit_daily_challenge_attempt /
     duel RPCs) — submit_xp_event explicitly rejects them so a client can't
     mint those sources directly through this new client-facing entry point.

  occurred_at is still client-supplied (needed for correct offline-week
  attribution, matching the existing xp_events design) but clamped into a
  tight 48h window rather than trusting the table's own 30-day CHECK alone —
  a legitimate multi-day offline queue still lands close to the right week
  without allowing 30-day backdating through this RPC.
*/

CREATE OR REPLACE FUNCTION public.submit_xp_event(
  p_source text,
  p_amount integer,
  p_idempotency_key text,
  p_occurred_at timestamptz,
  p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid;
  v_row_id text;
  v_occurred_at timestamptz;
  v_recent_positive numeric;
  v_inserted boolean;
BEGIN
  me := auth.uid();
  IF me IS NULL THEN
    RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000';
  END IF;

  IF p_source IN ('daily_challenge', 'friend_challenge') THEN
    RAISE EXCEPTION 'source_not_client_submittable' USING ERRCODE = '22023';
  END IF;

  -- Serializes everything below per-caller for the rest of this transaction.
  PERFORM pg_advisory_xact_lock(hashtext('submit_xp_event:' || me::text));

  -- Namespaced so two different users can never collide on the same
  -- underlying row id, even on an identical raw p_idempotency_key.
  v_row_id := me::text || ':' || p_idempotency_key;

  -- Idempotency short-circuit BEFORE the cap check: a retry of an
  -- already-recorded event must always report success.
  IF EXISTS (SELECT 1 FROM public.xp_events WHERE id = v_row_id) THEN
    RETURN jsonb_build_object('ok', true, 'awarded', false);
  END IF;

  -- Trust the client's claimed occurred_at only within a tight recent
  -- window; clamp rather than reject so a legitimate multi-day offline
  -- queue still lands close to the right week without allowing 30-day
  -- backdating.
  v_occurred_at := LEAST(GREATEST(p_occurred_at, now() - interval '48 hours'), now());

  -- Rolling 24-hour circuit breaker (a sliding window, not a UTC-calendar-day
  -- reset). Bounds ingestion rate/volume only -- amount stays fully
  -- client-claimed. This is bounded/trusted XP ingestion, not anti-cheat.
  SELECT COALESCE(SUM(amount) FILTER (WHERE amount > 0), 0) INTO v_recent_positive
  FROM public.xp_events
  WHERE user_id = me AND created_at >= now() - interval '24 hours';

  IF v_recent_positive + GREATEST(p_amount, 0) > 3000 THEN
    RAISE EXCEPTION 'rolling_24h_xp_cap_exceeded' USING ERRCODE = '22023';
  END IF;

  INSERT INTO public.xp_events (id, user_id, amount, source, metadata, occurred_at)
  VALUES (v_row_id, me, p_amount, p_source, p_metadata, v_occurred_at)
  ON CONFLICT (id) DO NOTHING;

  v_inserted := FOUND;

  RETURN jsonb_build_object('ok', true, 'awarded', v_inserted, 'occurred_at', v_occurred_at);
END;
$$;

REVOKE EXECUTE ON FUNCTION public.submit_xp_event(text, integer, text, timestamptz, jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.submit_xp_event(text, integer, text, timestamptz, jsonb) TO authenticated;

-- Client can no longer write xp_events directly. service_role keeps INSERT
-- for fixture setup in tests; SECURITY DEFINER functions run as their owner
-- and are unaffected by this revoke regardless of the caller's grants.
REVOKE INSERT ON public.xp_events FROM authenticated, anon;
GRANT INSERT ON public.xp_events TO service_role;
