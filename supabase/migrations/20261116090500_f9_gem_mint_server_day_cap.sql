/*
  # F9 (security review 2026-11): the 450-a-day gem cap also follows the server's day

  mint_gems capped 'earn' at 450 per occurred_at date, which the client chooses up to 30
  days back, so one sitting could mint 30 x 450. It now also caps 'earn' rows created
  today (created_at, the server's day) at 450.

  The two caps answer differently on purpose (docs/systems/shop-economy.md §14.1, §14.7):
    - over the occurred_at-day cap: unchanged, {capped: true}, nothing inserted, no error;
    - over the server-day cap: RAISE 'daily_mint_limit' (22023), nothing inserted. The
      client's mint queue keeps a failed entry and retries on a later flush, so a returning
      guest's or offline backlog drains at 450 a day instead of being dropped.
  The gem_events_occurred_at_bounds CHECK and the 30-day / +1-day occurred_at bounds are
  unchanged. Purchases stay uncounted.
*/

CREATE OR REPLACE FUNCTION public.mint_gems(p_idempotency_key text, p_amount integer, p_occurred_at timestamptz)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid := auth.uid();
  v_key text;
  v_balance bigint;
  v_day_total bigint;
  v_server_day_total bigint;
BEGIN
  IF me IS NULL THEN
    RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000';
  END IF;

  IF p_amount < 1 OR p_amount > 20 THEN
    RAISE EXCEPTION 'invalid_amount' USING ERRCODE = '22023';
  END IF;

  IF p_occurred_at < now() - interval '30 days' OR p_occurred_at > now() + interval '1 day' THEN
    RAISE EXCEPTION 'invalid_occurred_at' USING ERRCODE = '22023';
  END IF;

  v_key := me::text || ':' || p_idempotency_key;

  IF EXISTS (SELECT 1 FROM public.gem_events WHERE id = v_key) THEN
    SELECT COALESCE(SUM(delta), 0) INTO v_balance FROM public.gem_events WHERE user_id = me;
    RETURN jsonb_build_object('ok', true, 'replayed', true, 'balance', v_balance);
  END IF;

  PERFORM 1 FROM public.profiles WHERE id = me FOR UPDATE;

  SELECT COALESCE(SUM(delta), 0) INTO v_day_total
  FROM public.gem_events
  WHERE user_id = me AND kind = 'earn' AND occurred_at::date = p_occurred_at::date;

  IF v_day_total + p_amount > 450 THEN
    SELECT COALESCE(SUM(delta), 0) INTO v_balance FROM public.gem_events WHERE user_id = me;
    RETURN jsonb_build_object('ok', true, 'capped', true, 'balance', v_balance);
  END IF;

  SELECT COALESCE(SUM(delta), 0) INTO v_server_day_total
  FROM public.gem_events
  WHERE user_id = me AND kind = 'earn' AND created_at >= date_trunc('day', now());

  IF v_server_day_total + p_amount > 450 THEN
    RAISE EXCEPTION 'daily_mint_limit' USING ERRCODE = '22023';
  END IF;

  INSERT INTO public.gem_events (id, user_id, delta, kind, occurred_at)
  VALUES (v_key, me, p_amount, 'earn', p_occurred_at);

  SELECT COALESCE(SUM(delta), 0) INTO v_balance FROM public.gem_events WHERE user_id = me;
  RETURN jsonb_build_object('ok', true, 'balance', v_balance);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.mint_gems(text, integer, timestamptz) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.mint_gems(text, integer, timestamptz) TO authenticated;
