/*
  # revoke_guardian_consent(p_token): the guardian proves who they are

  Replaces the closed child-id form. The caller presents the same email-link
  token that granted consent (matched by its sha256, exactly like
  grant_guardian_consent). Still anon-callable (a guardian has no account),
  still erases on revoke; the under-13 / age self-declaration flow is
  unchanged. No UI calls revoke yet.

  NOT YET APPLIED TO PRODUCTION as of 2026-10-03: the Supabase MCP holds any
  statement containing a DELETE for a confirmation that times out. Apply it in
  the Supabase SQL editor. Until then revocation is unavailable in production,
  which nothing in the app uses.
*/

CREATE OR REPLACE FUNCTION public.revoke_guardian_consent(p_token text)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_hash text;
  v_child uuid;
BEGIN
  IF p_token IS NULL OR length(p_token) < 32 THEN
    RAISE EXCEPTION 'invalid_token' USING ERRCODE = '22023';
  END IF;

  v_hash := encode(extensions.digest(p_token, 'sha256'), 'hex');

  SELECT child_user_id INTO v_child FROM public.guardian_consents
  WHERE token_hash = v_hash AND granted_at IS NOT NULL AND revoked_at IS NULL
  ORDER BY granted_at DESC
  LIMIT 1;

  IF v_child IS NULL THEN
    RAISE EXCEPTION 'no_active_consent' USING ERRCODE = '22023';
  END IF;

  UPDATE public.guardian_consents
  SET revoked_at = now()
  WHERE child_user_id = v_child AND granted_at IS NOT NULL AND revoked_at IS NULL;

  UPDATE public.profiles SET consent_status = 'revoked' WHERE id = v_child;

  -- Revocation means stop processing AND erase (unchanged behaviour).
  DELETE FROM public.profiles WHERE id = v_child;

  RETURN jsonb_build_object('ok', true);
END;
$$;

REVOKE EXECUTE ON FUNCTION public.revoke_guardian_consent(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.revoke_guardian_consent(text) TO authenticated, anon;
