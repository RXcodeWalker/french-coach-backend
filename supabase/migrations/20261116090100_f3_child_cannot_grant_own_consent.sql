/*
  # F3 (security review 2026-11): a child's own session cannot grant guardian consent

  request_guardian_consent hands the raw token to the child's session (for the email step
  and the copy-link fallback), and grant_guardian_consent accepted it back from that same
  session. grant_guardian_consent now refuses ('own_consent', 42501) when the caller is
  the child the consent row is for.

  This closes the one-session path only. The token still reaches the browser, so a child
  can still sign out (or use a second account) and present it; closing that needs the
  token minted and emailed by the backend, which removes the copy-link fallback and is
  an owner decision (security review hand-off).

  correct_age_band (under-13 -> 13+ straight to 13_plus_not_required) is unchanged: it is
  the documented product decision in docs/systems/child-safety-consent.md.
*/

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

  IF auth.uid() IS NOT NULL AND auth.uid() = v_row.child_user_id THEN
    RAISE EXCEPTION 'own_consent' USING ERRCODE = '42501';
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
