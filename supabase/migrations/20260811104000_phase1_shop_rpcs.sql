/*
  # Shop Phase 1 — hardened SECURITY DEFINER RPCs (plan §14.3)

  Every point in §14.3's hardening checklist applies to all functions below:
  SET search_path = pg_catalog, public, pg_temp with schema-qualified object
  references; owned by postgres (default — migrations run as postgres via
  the CLI, same as every other SECURITY DEFINER function in this schema;
  verify with \df+ before relying on it, per plan B2); auth guard raising
  not_authenticated/28000; every client identifier validated against its
  table with no dynamic SQL; one transaction per call with no
  EXCEPTION WHEN OTHERS; FOR UPDATE row lock on profiles before reading a
  balance.

  A11's specific finding — Rev 1's SECURITY DEFINER sketch omitted
  REVOKE EXECUTE FROM PUBLIC (Postgres grants EXECUTE to PUBLIC by default)
  — is the one hardening step none of this schema's existing SECURITY
  DEFINER functions do (block_user, send_friend_request,
  respond_friend_request, remove_friend, all in 20260809070000/80000).
  Each function below is REVOKEd from PUBLIC and re-GRANTed to authenticated
  explicitly; the pre-existing functions are unrelated to Shop and are left
  alone.

  Idempotency-key namespacing (A11): gem_events.id is a global text PK, so a
  raw client-supplied key could let one user pre-claim another user's key.
  Every key is stored as `auth.uid() || ':' || p_idempotency_key`, and the
  replay lookup is scoped the same way — collision across users is
  impossible by construction, and the lookup can never read another user's
  row.
*/

-- ── mint_gems ─────────────────────────────────────────────────────────────
-- Amount bounded 1..20 (real range is 1-8; headroom for future award types).
-- occurred_at bounded to [now() - 30 days, now() + 1 day], same shape as
-- xp_events. Rolling cap: SUM(delta) for kind='earn' on that occurred_at
-- date < 450 gems/day; purchases (kind='purchase', not used until Phase 7)
-- are never counted toward it. Over cap never raises — a legitimate user
-- must never see a failure for practicing — it just mints nothing and
-- reports capped:true.

CREATE OR REPLACE FUNCTION public.mint_gems(p_idempotency_key text, p_amount int, p_occurred_at timestamptz)
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

  INSERT INTO public.gem_events (id, user_id, delta, kind, occurred_at)
  VALUES (v_key, me, p_amount, 'earn', p_occurred_at);

  SELECT COALESCE(SUM(delta), 0) INTO v_balance FROM public.gem_events WHERE user_id = me;
  RETURN jsonb_build_object('ok', true, 'balance', v_balance);
END;
$$;

REVOKE EXECUTE ON FUNCTION public.mint_gems(text, int, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.mint_gems(text, int, timestamptz) TO authenticated;

-- ── purchase_shop_item ───────────────────────────────────────────────────
-- Step order matches plan §14.3 exactly: auth guard, replay check, row
-- lock, item+price read (client never sends a price), requirement check,
-- balance check, max_owned check, then the payment + grant insert as one
-- atomic pair.

CREATE OR REPLACE FUNCTION public.purchase_shop_item(p_item_id text, p_idempotency_key text)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid := auth.uid();
  v_key text;
  v_item public.shop_items%ROWTYPE;
  v_balance bigint;
  v_owned_qty integer;
  v_new_qty integer;
BEGIN
  IF me IS NULL THEN
    RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000';
  END IF;

  v_key := me::text || ':' || p_idempotency_key;

  IF EXISTS (SELECT 1 FROM public.gem_events WHERE id = v_key) THEN
    SELECT COALESCE(SUM(delta), 0) INTO v_balance FROM public.gem_events WHERE user_id = me;
    RETURN jsonb_build_object('ok', true, 'replayed', true, 'balance', v_balance);
  END IF;

  PERFORM 1 FROM public.profiles WHERE id = me FOR UPDATE;

  SELECT * INTO v_item FROM public.shop_items WHERE id = p_item_id AND active;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'unknown_item' USING ERRCODE = '22023';
  END IF;

  IF v_item.requirement ? 'achievement' THEN
    IF NOT EXISTS (
      SELECT 1 FROM public.profiles
      WHERE id = me AND (v_item.requirement->>'achievement') = ANY(achievements)
    ) THEN
      RAISE EXCEPTION 'requirement_not_met' USING ERRCODE = '22023';
    END IF;
  END IF;

  SELECT COALESCE(SUM(delta), 0) INTO v_balance FROM public.gem_events WHERE user_id = me;
  IF v_balance < v_item.price_gems THEN
    RAISE EXCEPTION 'insufficient_gems' USING ERRCODE = '22023';
  END IF;

  SELECT COALESCE(qty, 0) INTO v_owned_qty
  FROM public.user_inventory WHERE user_id = me AND item_id = p_item_id;
  IF v_item.max_owned IS NOT NULL AND COALESCE(v_owned_qty, 0) >= v_item.max_owned THEN
    RAISE EXCEPTION 'already_owned' USING ERRCODE = '22023';
  END IF;

  INSERT INTO public.gem_events (id, user_id, delta, kind, item_id)
  VALUES (v_key, me, -v_item.price_gems, 'spend', p_item_id);

  INSERT INTO public.user_inventory (user_id, item_id, qty)
  VALUES (me, p_item_id, 1)
  ON CONFLICT (user_id, item_id) DO UPDATE SET qty = public.user_inventory.qty + 1
  RETURNING qty INTO v_new_qty;

  SELECT COALESCE(SUM(delta), 0) INTO v_balance FROM public.gem_events WHERE user_id = me;
  RETURN jsonb_build_object('ok', true, 'balance', v_balance, 'qty', v_new_qty);
END;
$$;

REVOKE EXECUTE ON FUNCTION public.purchase_shop_item(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.purchase_shop_item(text, text) TO authenticated;

-- ── equip_cosmetic ────────────────────────────────────────────────────────
-- p_item_id = NULL unequips the slot. The avatar slot continues to write
-- the literal glyph into profiles.avatar_emoji (see the schema migration's
-- header for why); frame/nameplate slots store the item id directly, since
-- their presentation is resolved client-side by id in Phase 3. The item's
-- own `kind` must match p_slot — ownership alone doesn't stop a client from
-- passing a frame's id into the nameplate slot, so both are checked in one
-- lookup.

CREATE OR REPLACE FUNCTION public.equip_cosmetic(p_slot text, p_item_id text)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid := auth.uid();
  v_item public.shop_items%ROWTYPE;
  v_owned_qty integer;
BEGIN
  IF me IS NULL THEN
    RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000';
  END IF;

  IF p_slot NOT IN ('avatar', 'frame', 'nameplate') THEN
    RAISE EXCEPTION 'invalid_slot' USING ERRCODE = '22023';
  END IF;

  IF p_item_id IS NULL THEN
    IF p_slot = 'avatar' THEN
      UPDATE public.profiles SET avatar_emoji = NULL WHERE id = me;
    ELSIF p_slot = 'frame' THEN
      UPDATE public.profiles SET equipped_frame = NULL WHERE id = me;
    ELSE
      UPDATE public.profiles SET equipped_nameplate = NULL WHERE id = me;
    END IF;
    RETURN;
  END IF;

  SELECT * INTO v_item FROM public.shop_items WHERE id = p_item_id AND active AND kind = p_slot;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'unknown_item' USING ERRCODE = '22023';
  END IF;

  SELECT COALESCE(qty, 0) INTO v_owned_qty
  FROM public.user_inventory WHERE user_id = me AND item_id = p_item_id;
  IF COALESCE(v_owned_qty, 0) <= 0 THEN
    RAISE EXCEPTION 'not_owned' USING ERRCODE = '22023';
  END IF;

  IF p_slot = 'avatar' THEN
    UPDATE public.profiles SET avatar_emoji = v_item.emoji WHERE id = me;
  ELSIF p_slot = 'frame' THEN
    UPDATE public.profiles SET equipped_frame = p_item_id WHERE id = me;
  ELSE
    UPDATE public.profiles SET equipped_nameplate = p_item_id WHERE id = me;
  END IF;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.equip_cosmetic(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.equip_cosmetic(text, text) TO authenticated;

-- ── consume_item ──────────────────────────────────────────────────────────
-- p_idempotency_key is a FRESH uuid per consumption (scoped to the USE, not
-- the item — plan §14.3), replayed via item_consumptions rather than
-- gem_events (see the schema migration's header on that table for why).
-- One transaction: decrement is a conditional UPDATE guarded on qty > 0
-- (the friendships-RPC idiom already used elsewhere in this schema — a
-- 0-row match means not owned, not a silent success), and the consumption
-- log insert commits or rolls back with it.

CREATE OR REPLACE FUNCTION public.consume_item(p_item_id text, p_idempotency_key text)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid := auth.uid();
  v_key text;
  v_new_qty integer;
BEGIN
  IF me IS NULL THEN
    RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000';
  END IF;

  v_key := me::text || ':' || p_idempotency_key;

  IF EXISTS (SELECT 1 FROM public.item_consumptions WHERE id = v_key) THEN
    SELECT COALESCE(qty, 0) INTO v_new_qty
    FROM public.user_inventory WHERE user_id = me AND item_id = p_item_id;
    RETURN jsonb_build_object('ok', true, 'replayed', true, 'qty', COALESCE(v_new_qty, 0));
  END IF;

  PERFORM 1 FROM public.profiles WHERE id = me FOR UPDATE;

  IF NOT EXISTS (SELECT 1 FROM public.shop_items WHERE id = p_item_id AND active) THEN
    RAISE EXCEPTION 'unknown_item' USING ERRCODE = '22023';
  END IF;

  UPDATE public.user_inventory
  SET qty = qty - 1
  WHERE user_id = me AND item_id = p_item_id AND qty > 0
  RETURNING qty INTO v_new_qty;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'not_owned' USING ERRCODE = '22023';
  END IF;

  INSERT INTO public.item_consumptions (id, user_id, item_id)
  VALUES (v_key, me, p_item_id);

  RETURN jsonb_build_object('ok', true, 'qty', v_new_qty);
END;
$$;

REVOKE EXECUTE ON FUNCTION public.consume_item(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.consume_item(text, text) TO authenticated;
