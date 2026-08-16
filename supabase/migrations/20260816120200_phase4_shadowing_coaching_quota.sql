/*
  # Phase 4 — Shadowing Mode: detailed-coaching quota (implementation plan
  i-am-implementing-phase-sunny-lagoon.md §3)

  Detailed (Groq-backed) coaching is opt-in and metered: 3 *delivered*
  attempts per user per UTC day. Metering lives in Postgres, not FastAPI
  memory, so it survives restarts and multiple backend instances.

  shadowing_coaching_grants is a plain ledger of granted slots, one row per
  (user, coaching_request_id). Mutation happens only through the two
  service_role RPCs below -- `authenticated` gets SELECT only, so a client
  cannot grant itself quota by inserting rows directly (it could only ever
  reduce its own count by inserting garbage, which is not a privilege
  escalation).

  consume_shadowing_coaching_quota / release_shadowing_coaching_grant both
  take p_user_id explicitly because they are invoked by FastAPI using the
  service-role key, where auth.uid() is NULL -- the caller identity has
  already been established by verify_supabase_jwt() against
  SUPABASE_JWT_SECRET (lib/auth.py), the same secret and algorithm (HS256)
  that Supabase itself uses to mint auth.uid() for PostgREST requests, so the
  two identities cannot diverge. Neither RPC is reachable by `authenticated`
  or `anon` (REVOKE FROM PUBLIC + GRANT TO service_role only), so p_user_id
  can only ever be set by the trusted FastAPI process from a
  signature-verified JWT `sub` claim -- never from a request body field.

  release_shadowing_coaching_grant (revision-2 addition, review item 1): the
  original design consumed a quota slot before the Groq call with no refund
  path, which meant a Groq timeout/failure/malformed-JSON/failed-grounding
  response silently charged the user for feedback they never received. This
  RPC is a compensating delete on the already-atomic consume row, not a
  two-phase reservation protocol -- the consume path stays a single atomic
  RPC and its concurrency guarantee (the advisory lock) is unchanged. It is
  called by the router whenever the narrator's `grounded` flag comes back
  False, which happens for exactly these reasons: Groq unavailable, timed
  out, returned malformed JSON, or invented a claim that failed the
  per-claim grounding gate against Azure-authoritative words.

  House conventions verified in 20260814130200 / 20260815090000:
  SECURITY DEFINER + SET search_path = pg_catalog, public, pg_temp;
  not_authenticated / ERRCODE 28000; domain errors ERRCODE 22023;
  RETURNS jsonb with jsonb_build_object('ok', true, ...); and a matching
  REVOKE EXECUTE ... FROM PUBLIC before every GRANT.
*/

CREATE TABLE public.shadowing_coaching_grants (
  id         text PRIMARY KEY,               -- '<user_id>:<coaching_request_id>'
  user_id    uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  grant_date date NOT NULL,                  -- (now() AT TIME ZONE 'utc')::date
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX shadowing_coaching_grants_user_date_idx
  ON public.shadowing_coaching_grants (user_id, grant_date);

ALTER TABLE public.shadowing_coaching_grants ENABLE ROW LEVEL SECURITY;
CREATE POLICY "shadowing_coaching_grants owner read" ON public.shadowing_coaching_grants
  FOR SELECT TO authenticated USING (auth.uid() = user_id);
-- No INSERT/UPDATE/DELETE policy -- mutation only via the two service_role RPCs.

GRANT SELECT ON public.shadowing_coaching_grants TO authenticated, service_role;
GRANT INSERT, DELETE ON public.shadowing_coaching_grants TO service_role;

-- ── consume_shadowing_coaching_quota ─────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.consume_shadowing_coaching_quota(
  p_user_id uuid,
  p_idempotency_key text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_limit constant integer := 3;  -- the only place the daily limit is defined
  v_date date;
  v_id text;
  v_used integer;
BEGIN
  IF p_user_id IS NULL THEN
    RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000';
  END IF;
  IF p_idempotency_key IS NULL OR btrim(p_idempotency_key) = '' THEN
    RAISE EXCEPTION 'missing_idempotency_key' USING ERRCODE = '22023';
  END IF;

  -- Serializes this user for the rest of the transaction (submit_xp_event's
  -- pg_advisory_xact_lock precedent) -- makes the boundary check below
  -- race-safe under concurrent requests.
  PERFORM pg_advisory_xact_lock(hashtext('shadowing_coach:' || p_user_id::text));

  v_date := (now() AT TIME ZONE 'utc')::date;
  v_id := p_user_id::text || ':' || p_idempotency_key;

  -- Replay short-circuit: a retry of an already-granted key never consumes a
  -- second slot and never denies coaching already paid for.
  IF EXISTS (SELECT 1 FROM public.shadowing_coaching_grants WHERE id = v_id) THEN
    SELECT count(*) INTO v_used FROM public.shadowing_coaching_grants
      WHERE user_id = p_user_id AND grant_date = v_date;
    RETURN jsonb_build_object('ok', true, 'granted', true, 'replayed', true, 'used', v_used, 'limit', v_limit);
  END IF;

  SELECT count(*) INTO v_used FROM public.shadowing_coaching_grants
    WHERE user_id = p_user_id AND grant_date = v_date;

  IF v_used >= v_limit THEN
    RETURN jsonb_build_object('ok', true, 'granted', false, 'used', v_used, 'limit', v_limit, 'reason', 'daily_limit_reached');
  END IF;

  INSERT INTO public.shadowing_coaching_grants (id, user_id, grant_date)
  VALUES (v_id, p_user_id, v_date)
  ON CONFLICT (id) DO NOTHING;

  RETURN jsonb_build_object('ok', true, 'granted', true, 'replayed', false, 'used', v_used + 1, 'limit', v_limit);
END;
$$;

REVOKE EXECUTE ON FUNCTION public.consume_shadowing_coaching_quota(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.consume_shadowing_coaching_quota(uuid, text) TO service_role;

-- ── release_shadowing_coaching_grant ─────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.release_shadowing_coaching_grant(
  p_user_id uuid,
  p_idempotency_key text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_limit constant integer := 3;
  v_date date;
  v_id text;
  v_deleted integer;
  v_used integer;
BEGIN
  IF p_user_id IS NULL THEN
    RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000';
  END IF;
  IF p_idempotency_key IS NULL OR btrim(p_idempotency_key) = '' THEN
    RAISE EXCEPTION 'missing_idempotency_key' USING ERRCODE = '22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtext('shadowing_coach:' || p_user_id::text));

  v_date := (now() AT TIME ZONE 'utc')::date;
  v_id := p_user_id::text || ':' || p_idempotency_key;

  DELETE FROM public.shadowing_coaching_grants WHERE id = v_id;
  GET DIAGNOSTICS v_deleted = ROW_COUNT;

  SELECT count(*) INTO v_used FROM public.shadowing_coaching_grants
    WHERE user_id = p_user_id AND grant_date = v_date;

  RETURN jsonb_build_object('ok', true, 'released', v_deleted > 0, 'used', v_used, 'limit', v_limit);
END;
$$;

REVOKE EXECUTE ON FUNCTION public.release_shadowing_coaching_grant(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.release_shadowing_coaching_grant(uuid, text) TO service_role;

-- ── get_shadowing_coaching_quota ─────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.get_shadowing_coaching_quota()
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_limit constant integer := 3;
  me uuid;
  v_date date;
  v_used integer;
BEGIN
  me := auth.uid();
  IF me IS NULL THEN
    RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000';
  END IF;

  v_date := (now() AT TIME ZONE 'utc')::date;

  SELECT count(*) INTO v_used FROM public.shadowing_coaching_grants
    WHERE user_id = me AND grant_date = v_date;

  RETURN jsonb_build_object('ok', true, 'used', v_used, 'limit', v_limit, 'remaining', GREATEST(0, v_limit - v_used), 'date', v_date);
END;
$$;

REVOKE EXECUTE ON FUNCTION public.get_shadowing_coaching_quota() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.get_shadowing_coaching_quota() TO authenticated;
