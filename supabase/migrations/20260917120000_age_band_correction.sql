/*
  # Age-band self-correction ("I mis-selected my age band")

  set_age_band() (20260911110000) is deliberately one-time — the initial
  onboarding step. This adds a second RPC, correct_age_band(), for a user
  who already has an age_band set and wants to change it later from Profile
  settings. Kept as a separate function (not "fixing" set_age_band's
  already_set check) so the one-time-onboarding contract stays intact for
  every existing caller.

  Same transition semantics as set_age_band(): -> 13_plus sets consent_status
  to '13_plus_not_required' (no gate); -> under_13 sets it to 'pending' (the
  client then walks the caller through request_guardian_consent same as
  first time — this RPC does not itself touch guardian_consents).

  This is an intentional, deliberate product decision to let the under_13 ->
  13_plus direction be instant and self-serve (no re-verification) — raised
  and confirmed with the project owner rather than assumed. It does mean an
  under-13 account can remove its own guardian gate with one click; nothing
  here should be read as asserting that's fine for every deployment context.
*/

CREATE OR REPLACE FUNCTION public.correct_age_band(p_band text)
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
  IF v_existing IS NULL THEN
    RAISE EXCEPTION 'age_band_not_set' USING ERRCODE = '22023';
  END IF;
  IF v_existing = p_band THEN
    RAISE EXCEPTION 'age_band_unchanged' USING ERRCODE = '22023';
  END IF;

  v_new_status := CASE WHEN p_band = 'under_13' THEN 'pending' ELSE '13_plus_not_required' END;

  UPDATE public.profiles SET age_band = p_band, consent_status = v_new_status WHERE id = me;

  RETURN jsonb_build_object('ok', true, 'age_band', p_band, 'consent_status', v_new_status);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.correct_age_band(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.correct_age_band(text) TO authenticated;
