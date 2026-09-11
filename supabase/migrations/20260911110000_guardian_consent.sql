/*
  # Phase 1.6 Part C — neutral age band + under-13 guardian consent

  Model (kept strictly separate per the plan, never conflated in code/docs/UI):
    - browser mic permission: a normal getUserMedia prompt, never legal consent;
    - FrenchCoach data-processing consent: the ToS/Privacy checkbox at signup,
      all users;
    - parental consent: email-plus, under-13 only, tracked here.

  age_band / consent_status live on profiles (service-role write only — no
  authenticated grant on either column, set only via the RPCs below, which
  run SECURITY DEFINER as the table owner). consent_status starts at
  '13_plus_not_required' by default so every pre-existing row (created
  before this migration) reads as "no gate" rather than NULL — a returning
  user is never silently blocked; AgeBandCheck (client) still routes them
  through the one-time age-band step because age_band itself is NULL until
  set_age_band runs.

  guardian_consents: one row per consent request. token_hash stores
  sha256(token), never the raw token — the raw token exists only in the
  URL Supabase mails the guardian and in this migration's RPC return value
  (transiently, to the caller who requested it). pgcrypto's digest() lives
  in the `extensions` schema (Supabase's standard location) which is
  intentionally outside every RPC's pinned search_path here, so it's called
  schema-qualified rather than added to search_path.

  request_guardian_consent's actual email delivery is intentionally NOT in
  this migration — SMTP is unconfigured in this project today (config.toml
  auth.email.smtp is commented out) and there is no email-sending library
  anywhere in backend/. The client wrapper (consentService.ts) calls a new
  backend/main.py endpoint to send the email over SMTP once configured;
  this RPC's job is only to mint the row + token and hand the link back to
  the caller so the client can pass it to that endpoint (or show a
  copy-link fallback if the send fails).

  grant_guardian_consent is callable by `anon` — a guardian confirming by
  emailed link is not expected to have (or need) an account of their own.
  It is the only RPC in this migration granted to anon; everything else
  requires the caller to already be the child's authenticated session.

  revoke_guardian_consent is guardian-invoked (also anon-callable, same
  token-based identification as grant) and, per the plan, immediately
  erases the child's account data (flips to 'revoked' AND calls
  delete_my_account's logic for that child) — revocation means "stop
  processing and erase," not just "stop processing."

  export_my_data / delete_my_account (20260911100000) are extended here
  with an optional p_subject_user_id so a confirmed guardian (a granted,
  non-revoked guardian_consents row naming that child) can export/delete
  the linked child's account — this couldn't be added at 20260911100000
  because guardian_consents didn't exist yet.

  Explicitly out of scope here (counsel items, recorded in ADR 0006 and the
  Privacy Policy DRAFT header, not re-litigated in this migration):
  DPDP under-18 handling, whether email-plus is legally sufficient VPC for
  COPPA/DPDP, reliance on the FTC transient-voice exception, a granular
  parental dashboard, consent-text localisation, age-estimation.
*/

CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA extensions;

-- ── profiles: age band + consent status ─────────────────────────────────────

ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS age_band text CHECK (age_band IN ('under_13', '13_plus')),
  ADD COLUMN IF NOT EXISTS consent_status text NOT NULL DEFAULT '13_plus_not_required'
    CHECK (consent_status IN ('13_plus_not_required', 'pending', 'granted', 'revoked'));

-- Neither column is added to the `authenticated` UPDATE grant re-issued by
-- 20260909130000 — service-role/SECURITY DEFINER RPC write only, same
-- posture as xp_baseline.

-- ── guardian_consents ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.guardian_consents (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  child_user_id  uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  guardian_email text NOT NULL,
  method         text NOT NULL DEFAULT 'email_plus',
  token_hash     text NOT NULL,
  requested_at   timestamptz NOT NULL DEFAULT now(),
  granted_at     timestamptz,
  revoked_at     timestamptz,
  evidence       jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS guardian_consents_child_idx ON public.guardian_consents(child_user_id);
-- RPCs look up a presented token by its hash; not unique (a re-requested
-- consent mints a new row rather than reusing/rotating the old one, so a
-- stale emailed link simply stops matching an active row's hash once a
-- fresh request supersedes it in application logic).
CREATE INDEX IF NOT EXISTS guardian_consents_token_hash_idx ON public.guardian_consents(token_hash);

ALTER TABLE public.guardian_consents ENABLE ROW LEVEL SECURITY;

-- Child reads own rows (e.g. to show "we sent it to guardian@example.com,
-- sent Nov 3"). All writes go through the SECURITY DEFINER RPCs below —
-- no INSERT/UPDATE/DELETE policy for any role.
CREATE POLICY "child reads own guardian_consents"
  ON public.guardian_consents FOR SELECT
  TO authenticated
  USING (auth.uid() = child_user_id);

REVOKE ALL ON public.guardian_consents FROM PUBLIC;
GRANT SELECT ON public.guardian_consents TO authenticated;

-- ── RPCs ─────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.set_age_band(p_band text)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid := auth.uid();
  v_existing text;
  v_new_status text;
BEGIN
  IF me IS NULL THEN
    RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000';
  END IF;
  IF p_band NOT IN ('under_13', '13_plus') THEN
    RAISE EXCEPTION 'invalid_age_band' USING ERRCODE = '22023';
  END IF;

  SELECT age_band INTO v_existing FROM public.profiles WHERE id = me;
  IF v_existing IS NOT NULL THEN
    RAISE EXCEPTION 'age_band_already_set' USING ERRCODE = '22023';
  END IF;

  v_new_status := CASE WHEN p_band = 'under_13' THEN 'pending' ELSE '13_plus_not_required' END;

  UPDATE public.profiles SET age_band = p_band, consent_status = v_new_status WHERE id = me;

  RETURN jsonb_build_object('ok', true, 'age_band', p_band, 'consent_status', v_new_status);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.set_age_band(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.set_age_band(text) TO authenticated;

CREATE OR REPLACE FUNCTION public.request_guardian_consent(p_guardian_email text)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid := auth.uid();
  v_age_band text;
  v_token text;
  v_consent_id uuid;
BEGIN
  IF me IS NULL THEN
    RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000';
  END IF;
  IF p_guardian_email IS NULL OR p_guardian_email !~ '^[^@\s]+@[^@\s]+\.[^@\s]+$' THEN
    RAISE EXCEPTION 'invalid_email' USING ERRCODE = '22023';
  END IF;

  SELECT age_band INTO v_age_band FROM public.profiles WHERE id = me;
  IF v_age_band IS DISTINCT FROM 'under_13' THEN
    RAISE EXCEPTION 'not_under_13' USING ERRCODE = '22023';
  END IF;

  v_token := encode(extensions.gen_random_bytes(32), 'hex');

  INSERT INTO public.guardian_consents (child_user_id, guardian_email, token_hash)
  VALUES (me, lower(p_guardian_email), encode(extensions.digest(v_token, 'sha256'), 'hex'))
  RETURNING id INTO v_consent_id;

  -- consent_status stays 'pending' (already set by set_age_band) — a
  -- request does not itself grant anything, it only mints the row/token.
  -- The raw token is returned once, here, to the child's own session; the
  -- client hands it to a backend endpoint that emails the guardian (or, if
  -- that send fails / isn't configured yet, shows a copy-link fallback).
  RETURN jsonb_build_object(
    'ok', true,
    'consent_id', v_consent_id,
    'token', v_token
  );
END;
$$;
REVOKE EXECUTE ON FUNCTION public.request_guardian_consent(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.request_guardian_consent(text) TO authenticated;

CREATE OR REPLACE FUNCTION public.grant_guardian_consent(p_token text, p_relationship text)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_hash text;
  v_row public.guardian_consents%ROWTYPE;
BEGIN
  IF p_token IS NULL OR length(p_token) < 32 THEN
    RAISE EXCEPTION 'invalid_token' USING ERRCODE = '22023';
  END IF;
  IF p_relationship IS NULL OR length(trim(p_relationship)) = 0 THEN
    RAISE EXCEPTION 'relationship_required' USING ERRCODE = '22023';
  END IF;

  v_hash := encode(extensions.digest(p_token, 'sha256'), 'hex');

  SELECT * INTO v_row FROM public.guardian_consents
  WHERE token_hash = v_hash AND granted_at IS NULL AND revoked_at IS NULL
  ORDER BY requested_at DESC
  LIMIT 1;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'invalid_or_used_token' USING ERRCODE = '22023';
  END IF;

  UPDATE public.guardian_consents
  SET granted_at = now(), evidence = evidence || jsonb_build_object('relationship', p_relationship)
  WHERE id = v_row.id;

  UPDATE public.profiles SET consent_status = 'granted' WHERE id = v_row.child_user_id;

  RETURN jsonb_build_object('ok', true, 'child_user_id', v_row.child_user_id);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.grant_guardian_consent(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.grant_guardian_consent(text, text) TO authenticated, anon;

CREATE OR REPLACE FUNCTION public.revoke_guardian_consent(p_child_user_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_had_granted_row boolean;
BEGIN
  SELECT EXISTS (
    SELECT 1 FROM public.guardian_consents
    WHERE child_user_id = p_child_user_id AND granted_at IS NOT NULL AND revoked_at IS NULL
  ) INTO v_had_granted_row;

  IF NOT v_had_granted_row THEN
    RAISE EXCEPTION 'no_active_consent' USING ERRCODE = '22023';
  END IF;

  UPDATE public.guardian_consents
  SET revoked_at = now()
  WHERE child_user_id = p_child_user_id AND granted_at IS NOT NULL AND revoked_at IS NULL;

  UPDATE public.profiles SET consent_status = 'revoked' WHERE id = p_child_user_id;

  -- Revocation means stop processing AND erase (plan Part C, step under
  -- "revoke_guardian_consent"). Deletes the profiles row directly rather
  -- than calling delete_my_account() (which reads auth.uid() = the
  -- guardian's own, absent, session) — cascades identically either way.
  DELETE FROM public.profiles WHERE id = p_child_user_id;

  RETURN jsonb_build_object('ok', true);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.revoke_guardian_consent(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.revoke_guardian_consent(uuid) TO authenticated, anon;

-- ── export_my_data / delete_my_account: guardian-of-linked-child variant ────
-- Re-created (not altered — Postgres has no ALTER FUNCTION to change a
-- body's logic in place) with an optional p_subject_user_id. Signature
-- change from 20260911100000's zero-arg versions is safe: both new params
-- default to NULL, so every existing call site (accountService.ts) is
-- unaffected and continues to export/delete the caller's own data.

CREATE OR REPLACE FUNCTION public.export_my_data(p_subject_user_id uuid DEFAULT NULL)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid := auth.uid();
  target uuid;
  result jsonb;
BEGIN
  IF me IS NULL THEN
    RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000';
  END IF;

  IF p_subject_user_id IS NULL OR p_subject_user_id = me THEN
    target := me;
  ELSE
    IF NOT EXISTS (
      SELECT 1 FROM public.guardian_consents gc
      WHERE gc.child_user_id = p_subject_user_id
        AND gc.granted_at IS NOT NULL AND gc.revoked_at IS NULL
        AND EXISTS (SELECT 1 FROM auth.users u WHERE u.id = me AND lower(u.email) = gc.guardian_email)
    ) THEN
      RAISE EXCEPTION 'not_guardian_of_subject' USING ERRCODE = '42501';
    END IF;
    target := p_subject_user_id;
  END IF;

  SELECT jsonb_build_object(
    'exported_at', now(),
    'profile', (SELECT to_jsonb(p) FROM public.profiles p WHERE p.id = target),
    'sessions', COALESCE((SELECT jsonb_agg(to_jsonb(s)) FROM public.sessions s WHERE s.user_id = target), '[]'::jsonb),
    'xp_events', COALESCE((SELECT jsonb_agg(to_jsonb(e)) FROM public.xp_events e WHERE e.user_id = target), '[]'::jsonb),
    'coach_evidence', COALESCE((SELECT jsonb_agg(to_jsonb(c)) FROM public.coach_evidence c WHERE c.user_id = target), '[]'::jsonb),
    'session_transcripts', COALESCE((SELECT jsonb_agg(to_jsonb(t)) FROM public.session_transcripts t WHERE t.user_id = target), '[]'::jsonb),
    'scoring_envelopes', COALESCE((SELECT jsonb_agg(to_jsonb(v)) FROM public.scoring_envelopes v WHERE v.user_id = target), '[]'::jsonb),
    'pronunciation_attempts', COALESCE((SELECT jsonb_agg(to_jsonb(a)) FROM public.pronunciation_attempts a WHERE a.user_id = target), '[]'::jsonb),
    'pronunciation_phoneme_stats', COALESCE((SELECT jsonb_agg(to_jsonb(ps)) FROM public.pronunciation_phoneme_stats ps WHERE ps.user_id = target), '[]'::jsonb)
  ) INTO result;

  RETURN result;
END;
$$;
REVOKE EXECUTE ON FUNCTION public.export_my_data(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.export_my_data(uuid) TO authenticated;

CREATE OR REPLACE FUNCTION public.delete_my_account(p_subject_user_id uuid DEFAULT NULL)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid := auth.uid();
  target uuid;
BEGIN
  IF me IS NULL THEN
    RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000';
  END IF;

  IF p_subject_user_id IS NULL OR p_subject_user_id = me THEN
    target := me;
  ELSE
    IF NOT EXISTS (
      SELECT 1 FROM public.guardian_consents gc
      WHERE gc.child_user_id = p_subject_user_id
        AND gc.granted_at IS NOT NULL AND gc.revoked_at IS NULL
        AND EXISTS (SELECT 1 FROM auth.users u WHERE u.id = me AND lower(u.email) = gc.guardian_email)
    ) THEN
      RAISE EXCEPTION 'not_guardian_of_subject' USING ERRCODE = '42501';
    END IF;
    target := p_subject_user_id;
  END IF;

  DELETE FROM public.profiles WHERE id = target;

  RETURN jsonb_build_object('ok', true);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.delete_my_account(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.delete_my_account(uuid) TO authenticated;
