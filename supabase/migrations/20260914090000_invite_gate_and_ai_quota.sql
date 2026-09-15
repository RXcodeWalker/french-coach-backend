/*
  # Phase 3 — Cost & Abuse Controls: invite-code gate + per-user AI-cost quota
  (phase-3-plan-tidy-widget.md)

  Two independent-but-linked mechanisms, both server-enforced (never trusted
  from the client):

  1. Invite-code gate. `profiles.invite_status` defaults to 'unredeemed' for
     every row created after this migration; a two-step ALTER (add as
     'redeemed' default, backfill, then flip the default) grandfathers every
     pre-existing row to 'redeemed' without a bulk UPDATE touching every
     profile. redeem_invite_code() is the only way to flip a row to
     'redeemed'; check_invite_code() is a read-only anon-callable pre-check
     so the gate UI can show a specific reason before the real redeem call.
     Enforcement itself does not live in the frontend gate screen — it lives
     inside consume_ai_quota() below, which denies any caller whose
     invite_status is not 'redeemed' before touching quota state at all.

  2. Per-user daily AI-cost quota, modeled directly on
     consume_shadowing_coaching_quota / release_shadowing_coaching_grant
     (20260816120200) — same advisory-lock-then-count shape, same
     replay-before-cap-check ordering, same jsonb return convention. The one
     structural difference: this RPC is called by two different backend
     processes (server/, Node; backend/, Python), neither of which runs in a
     per-request client-JWT context — both hold only the service-role key
     and an already-verified user_id from their own JWT verification. So
     auth.uid() cannot be used here (unlike get_shadowing_coaching_quota,
     which is called by `authenticated` directly); p_user_id is passed
     explicitly and only ever reachable via GRANT ... TO service_role.

  ai_quota_limits is a plain lookup table, not hardcoded into the RPC body,
  because Phase 3's own plan text says the starting numbers are "tunable
  later via UPDATE, no migration needed" — consume_shadowing_coaching_quota's
  v_limit constant was fine as a literal because it's one feature; six
  features with independently-tunable caps belong in a table.

  House conventions verified in 20260816120200 / 20260911110000:
  SECURITY DEFINER + SET search_path = pg_catalog, public, pg_temp;
  not_authenticated / ERRCODE 28000; domain errors ERRCODE 22023;
  RETURNS jsonb with jsonb_build_object('ok', true, ...); and a matching
  REVOKE EXECUTE ... FROM PUBLIC before every GRANT.
*/

-- ── profiles: invite_status ──────────────────────────────────────────────────

ALTER TABLE public.profiles
  ADD COLUMN invite_status text NOT NULL DEFAULT 'redeemed'
    CHECK (invite_status IN ('unredeemed', 'redeemed'));
ALTER TABLE public.profiles ALTER COLUMN invite_status SET DEFAULT 'unredeemed';

-- Not added to the `authenticated` UPDATE grant (20260909130000) —
-- service-role/SECURITY DEFINER RPC write only, same posture as
-- xp_baseline / age_band / consent_status.

-- ── invite_codes / invite_code_redemptions ───────────────────────────────────

CREATE TABLE public.invite_codes (
  code        text PRIMARY KEY,
  max_uses    integer NOT NULL DEFAULT 1,
  use_count   integer NOT NULL DEFAULT 0,
  created_by  uuid REFERENCES auth.users(id),
  created_at  timestamptz NOT NULL DEFAULT now(),
  expires_at  timestamptz,
  note        text
);

CREATE TABLE public.invite_code_redemptions (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code        text NOT NULL REFERENCES public.invite_codes(code),
  user_id     uuid NOT NULL UNIQUE REFERENCES auth.users(id) ON DELETE CASCADE,
  redeemed_at timestamptz NOT NULL DEFAULT now()
);

-- No client-facing RLS INSERT/UPDATE/DELETE policy on either table — all
-- mutation goes through the RPCs below, matching shadowing_coaching_grants.
ALTER TABLE public.invite_codes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.invite_code_redemptions ENABLE ROW LEVEL SECURITY;

GRANT SELECT, INSERT, UPDATE ON public.invite_codes TO service_role;
GRANT SELECT, INSERT ON public.invite_code_redemptions TO service_role;

-- ── redeem_invite_code ───────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.redeem_invite_code(p_code text)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid;
  v_code text;
  v_updated integer;
BEGIN
  me := auth.uid();
  IF me IS NULL THEN
    RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000';
  END IF;
  IF p_code IS NULL OR btrim(p_code) = '' THEN
    RAISE EXCEPTION 'missing_code' USING ERRCODE = '22023';
  END IF;

  -- Idempotency-first (checked before any invite_codes mutation): a retry
  -- of an already-redeemed user is a clean no-op success, not a raw
  -- unique-violation surfaced from a second insert attempt.
  IF EXISTS (SELECT 1 FROM public.invite_code_redemptions WHERE user_id = me) THEN
    RETURN jsonb_build_object('ok', true, 'granted', true, 'already_redeemed', true);
  END IF;

  v_code := upper(btrim(p_code));

  PERFORM pg_advisory_xact_lock(hashtext('invite_code:' || v_code));

  UPDATE public.invite_codes
    SET use_count = use_count + 1
    WHERE code = v_code AND use_count < max_uses AND (expires_at IS NULL OR expires_at > now());
  GET DIAGNOSTICS v_updated = ROW_COUNT;

  IF v_updated = 0 THEN
    RETURN jsonb_build_object('ok', true, 'granted', false, 'reason', 'invalid_or_exhausted');
  END IF;

  INSERT INTO public.invite_code_redemptions (code, user_id) VALUES (v_code, me);
  UPDATE public.profiles SET invite_status = 'redeemed' WHERE id = me;

  RETURN jsonb_build_object('ok', true, 'granted', true, 'already_redeemed', false);
END;
$$;

REVOKE EXECUTE ON FUNCTION public.redeem_invite_code(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.redeem_invite_code(text) TO authenticated;

-- ── check_invite_code ────────────────────────────────────────────────────────
-- Read-only pre-check so the gate screen can show a specific reason before
-- the real redeem call. Never mutates. anon-callable: the gate screen may
-- run before the user has signed in.

CREATE OR REPLACE FUNCTION public.check_invite_code(p_code text)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_code text;
  v_row public.invite_codes%ROWTYPE;
BEGIN
  IF p_code IS NULL OR btrim(p_code) = '' THEN
    RETURN jsonb_build_object('ok', true, 'valid', false, 'reason', 'missing_code');
  END IF;

  v_code := upper(btrim(p_code));
  SELECT * INTO v_row FROM public.invite_codes WHERE code = v_code;

  IF NOT FOUND THEN
    RETURN jsonb_build_object('ok', true, 'valid', false, 'reason', 'not_found');
  END IF;
  IF v_row.expires_at IS NOT NULL AND v_row.expires_at <= now() THEN
    RETURN jsonb_build_object('ok', true, 'valid', false, 'reason', 'expired');
  END IF;
  IF v_row.use_count >= v_row.max_uses THEN
    RETURN jsonb_build_object('ok', true, 'valid', false, 'reason', 'exhausted');
  END IF;

  RETURN jsonb_build_object('ok', true, 'valid', true);
END;
$$;

REVOKE EXECUTE ON FUNCTION public.check_invite_code(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.check_invite_code(text) TO anon, authenticated, service_role;

-- ── ai_quota_limits / ai_usage_grants ────────────────────────────────────────

CREATE TABLE public.ai_quota_limits (
  feature      text PRIMARY KEY,
  daily_limit  integer NOT NULL
);
INSERT INTO public.ai_quota_limits (feature, daily_limit) VALUES
  ('score', 20), ('feedback', 20), ('roleplay_turn', 30),
  ('transcribe', 30), ('pronunciation', 30), ('exam', 10);

CREATE TABLE public.ai_usage_grants (
  id         text PRIMARY KEY,   -- '<user_id>:<feature>:<idempotency_key>'
  user_id    uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  feature    text NOT NULL REFERENCES public.ai_quota_limits(feature),
  grant_date date NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ai_usage_grants_user_feature_date_idx
  ON public.ai_usage_grants (user_id, feature, grant_date);

ALTER TABLE public.ai_quota_limits ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ai_usage_grants ENABLE ROW LEVEL SECURITY;

CREATE POLICY "ai_quota_limits anyone read" ON public.ai_quota_limits
  FOR SELECT TO authenticated, anon USING (true);
CREATE POLICY "ai_usage_grants owner read" ON public.ai_usage_grants
  FOR SELECT TO authenticated USING (auth.uid() = user_id);
-- No INSERT/UPDATE/DELETE policy on ai_usage_grants — mutation only via the
-- service_role RPCs below, same posture as shadowing_coaching_grants.

GRANT SELECT ON public.ai_quota_limits TO authenticated, anon, service_role;
GRANT SELECT ON public.ai_usage_grants TO authenticated, service_role;
GRANT INSERT, DELETE ON public.ai_usage_grants TO service_role;

-- ── consume_ai_quota ─────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.consume_ai_quota(
  p_user_id uuid,
  p_feature text,
  p_idempotency_key text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_invite_status text;
  v_limit integer;
  v_date date;
  v_id text;
  v_used integer;
BEGIN
  IF p_user_id IS NULL THEN
    RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000';
  END IF;
  IF p_feature IS NULL OR btrim(p_feature) = '' THEN
    RAISE EXCEPTION 'missing_feature' USING ERRCODE = '22023';
  END IF;
  IF p_idempotency_key IS NULL OR btrim(p_idempotency_key) = '' THEN
    RAISE EXCEPTION 'missing_idempotency_key' USING ERRCODE = '22023';
  END IF;

  -- "No profile row" and an explicit NULL/'unredeemed' column value are
  -- treated identically as "not redeemed" — IS DISTINCT FROM does this
  -- without a separate zero-rows branch, since a SELECT ... INTO that
  -- matches no row leaves v_invite_status NULL exactly as a NULL column
  -- value would.
  SELECT invite_status INTO v_invite_status FROM public.profiles WHERE id = p_user_id;
  IF v_invite_status IS DISTINCT FROM 'redeemed' THEN
    RETURN jsonb_build_object('ok', true, 'granted', false, 'reason', 'invite_not_redeemed');
  END IF;

  SELECT daily_limit INTO v_limit FROM public.ai_quota_limits WHERE feature = p_feature;
  IF v_limit IS NULL THEN
    RAISE EXCEPTION 'unknown_feature' USING ERRCODE = '22023';
  END IF;

  v_id := p_user_id::text || ':' || p_feature || ':' || p_idempotency_key;

  -- Replay short-circuit checked before the cap, per shadowing_coaching's
  -- ordering: a replay is never charged twice and can't retroactively
  -- widen the cap.
  IF EXISTS (SELECT 1 FROM public.ai_usage_grants WHERE id = v_id) THEN
    SELECT count(*) INTO v_used FROM public.ai_usage_grants
      WHERE user_id = p_user_id AND feature = p_feature AND grant_date = (now() AT TIME ZONE 'utc')::date;
    RETURN jsonb_build_object('ok', true, 'granted', true, 'replayed', true, 'used', v_used, 'limit', v_limit);
  END IF;

  PERFORM pg_advisory_xact_lock(hashtext('ai_quota:' || p_feature || ':' || p_user_id::text));

  v_date := (now() AT TIME ZONE 'utc')::date;

  SELECT count(*) INTO v_used FROM public.ai_usage_grants
    WHERE user_id = p_user_id AND feature = p_feature AND grant_date = v_date;

  IF v_used >= v_limit THEN
    RETURN jsonb_build_object('ok', true, 'granted', false, 'used', v_used, 'limit', v_limit, 'reason', 'daily_quota_reached');
  END IF;

  INSERT INTO public.ai_usage_grants (id, user_id, feature, grant_date)
  VALUES (v_id, p_user_id, p_feature, v_date)
  ON CONFLICT (id) DO NOTHING;

  RETURN jsonb_build_object('ok', true, 'granted', true, 'replayed', false, 'used', v_used + 1, 'limit', v_limit);
END;
$$;

REVOKE EXECUTE ON FUNCTION public.consume_ai_quota(uuid, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.consume_ai_quota(uuid, text, text) TO service_role;

-- ── release_ai_quota_grant ───────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.release_ai_quota_grant(
  p_user_id uuid,
  p_feature text,
  p_idempotency_key text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_limit integer;
  v_date date;
  v_id text;
  v_deleted integer;
  v_used integer;
BEGIN
  IF p_user_id IS NULL THEN
    RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000';
  END IF;
  IF p_feature IS NULL OR btrim(p_feature) = '' THEN
    RAISE EXCEPTION 'missing_feature' USING ERRCODE = '22023';
  END IF;
  IF p_idempotency_key IS NULL OR btrim(p_idempotency_key) = '' THEN
    RAISE EXCEPTION 'missing_idempotency_key' USING ERRCODE = '22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtext('ai_quota:' || p_feature || ':' || p_user_id::text));

  v_date := (now() AT TIME ZONE 'utc')::date;
  v_id := p_user_id::text || ':' || p_feature || ':' || p_idempotency_key;

  DELETE FROM public.ai_usage_grants WHERE id = v_id;
  GET DIAGNOSTICS v_deleted = ROW_COUNT;

  SELECT daily_limit INTO v_limit FROM public.ai_quota_limits WHERE feature = p_feature;
  SELECT count(*) INTO v_used FROM public.ai_usage_grants
    WHERE user_id = p_user_id AND feature = p_feature AND grant_date = v_date;

  RETURN jsonb_build_object('ok', true, 'released', v_deleted > 0, 'used', v_used, 'limit', v_limit);
END;
$$;

REVOKE EXECUTE ON FUNCTION public.release_ai_quota_grant(uuid, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.release_ai_quota_grant(uuid, text, text) TO service_role;
