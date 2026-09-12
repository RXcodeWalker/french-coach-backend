/*
  # Reliability plan §2.6 — Mystery Box server-side dedupe (Option B)

  MysteryBox.tsx currently has NO cooldown gate at all (confirmed: no
  storageGet/storageSet check anywhere in the component) and mints XP purely
  client-side via dispatchAddXP -> awardGemsForXP -> the local ledger,
  eventually synced through submit_xp_event with a fresh, per-call
  idempotency key (makeXpEventId — unique per call, not per source-per-day).
  submit_xp_event's only defense is a rolling 24h positive-XP cap (3000,
  20260815090000) with no per-source-per-day uniqueness for any source
  except daily_challenge — so today a client can claim Mystery Box's reward
  as many times as it likes, from one device or several.

  Follows daily_challenge's established pattern (20260814130000/…130200/…130300):
  a UNIQUE(user_id, claim_date) table plus a SECURITY DEFINER RPC that is the
  only path to a claim, calling the existing internal award_xp(...)
  (service_role-only, unchanged) rather than duplicating its ledger-insert
  logic. Unlike daily_challenge, there is no exam/scoring envelope to bind to
  — Mystery Box is a pure daily reward, so claim_mystery_box both determines
  eligibility (one claim per UTC calendar day, matching this codebase's only
  "once per day" convention — no local-calendar-day helper exists to deviate
  toward, see dateKey() in analyticsService.ts) and picks the reward amount
  itself. Randomizing server-side (not just trusting a client-submitted
  amount) closes the amount-tampering half of the exploit too: a client
  proving "I haven't claimed today" server-side would otherwise still be free
  to call submit_xp_event with any p_amount it likes for the 'mystery_box'
  source — so this migration also removes 'mystery_box' from submit_xp_event's
  client-submittable sources, same treatment as daily_challenge/friend_challenge.

  Reward table matches MysteryBox.tsx's existing REWARDS array (50/100/250,
  uniform random) — kept in the RPC as the single source of truth once this
  ships; MysteryBox.tsx's own REWARDS array becomes display-only for the
  post-claim reveal, no longer the value that gets minted.
*/

CREATE TABLE public.mystery_box_claims (
  user_id    uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  claim_date date NOT NULL,
  xp_awarded integer NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, claim_date)
);

ALTER TABLE public.mystery_box_claims ENABLE ROW LEVEL SECURITY;

CREATE POLICY "mystery_box_claims owner read" ON public.mystery_box_claims
  FOR SELECT USING (auth.uid() = user_id);
-- No INSERT/UPDATE/DELETE policy — mutation only via claim_mystery_box.

GRANT SELECT ON public.mystery_box_claims TO authenticated, service_role;

CREATE OR REPLACE FUNCTION public.claim_mystery_box()
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid;
  v_today date := (now() AT TIME ZONE 'UTC')::date;
  v_existing public.mystery_box_claims%ROWTYPE;
  v_amount integer;
  v_roll numeric;
BEGIN
  me := auth.uid();
  IF me IS NULL THEN RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000'; END IF;

  -- Idempotent short-circuit: a repeat call for a day already claimed
  -- returns the existing result rather than hitting the unique-violation.
  SELECT * INTO v_existing FROM public.mystery_box_claims
    WHERE user_id = me AND claim_date = v_today;
  IF FOUND THEN
    RETURN jsonb_build_object('ok', true, 'already_claimed', true, 'xp_awarded', v_existing.xp_awarded);
  END IF;

  -- Same three-tier reward table as MysteryBox.tsx's REWARDS array, chosen
  -- server-side so the amount can never be client-claimed.
  v_roll := random();
  v_amount := CASE
    WHEN v_roll < 1.0 / 3 THEN 50
    WHEN v_roll < 2.0 / 3 THEN 100
    ELSE 250
  END;

  INSERT INTO public.mystery_box_claims (user_id, claim_date, xp_awarded)
  VALUES (me, v_today, v_amount);

  PERFORM public.award_xp(
    me, 'mystery_box', v_amount,
    me::text || ':mystery_box:' || v_today::text,
    jsonb_build_object('claim_date', v_today)
  );

  RETURN jsonb_build_object('ok', true, 'already_claimed', false, 'xp_awarded', v_amount);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.claim_mystery_box() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.claim_mystery_box() TO authenticated;

-- Mystery Box XP now lands exclusively through claim_mystery_box's internal
-- award_xp(...) call — close the client-facing submit_xp_event path for this
-- source, same treatment as daily_challenge/friend_challenge (20260815090000).
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

  IF p_source IN ('daily_challenge', 'friend_challenge', 'mystery_box') THEN
    RAISE EXCEPTION 'source_not_client_submittable' USING ERRCODE = '22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtext('submit_xp_event:' || me::text));

  v_row_id := me::text || ':' || p_idempotency_key;

  IF EXISTS (SELECT 1 FROM public.xp_events WHERE id = v_row_id) THEN
    RETURN jsonb_build_object('ok', true, 'awarded', false);
  END IF;

  v_occurred_at := LEAST(GREATEST(p_occurred_at, now() - interval '48 hours'), now());

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
