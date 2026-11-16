/*
  # F10 (security review 2026-11): guardian export/delete need a confirmed email

  export_my_data(uuid) and delete_my_account(uuid) let a caller act on a child's account
  when their auth.users.email matched the consent row's guardian_email, confirmed or not.
  Both now also require u.email_confirmed_at IS NOT NULL.

  export_my_data includes exam_pronunciation_evidence only when that table exists:
  20261005100000 (which adds it) is not applied in production, and a static reference to
  a missing table would make every export fail at run time.
  delete_my_account keeps F1's explicit erasure of a child's usage grants.
*/

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
  v_evidence jsonb;
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
        AND EXISTS (
          SELECT 1 FROM auth.users u
          WHERE u.id = me AND lower(u.email) = gc.guardian_email AND u.email_confirmed_at IS NOT NULL
        )
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

  IF to_regclass('public.exam_pronunciation_evidence') IS NOT NULL THEN
    EXECUTE 'SELECT COALESCE(jsonb_agg(to_jsonb(ep)), ''[]''::jsonb) FROM public.exam_pronunciation_evidence ep WHERE ep.user_id = $1'
      INTO v_evidence USING target;
    result := result || jsonb_build_object('exam_pronunciation_evidence', v_evidence);
  END IF;

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
        AND EXISTS (
          SELECT 1 FROM auth.users u
          WHERE u.id = me AND lower(u.email) = gc.guardian_email AND u.email_confirmed_at IS NOT NULL
        )
    ) THEN
      RAISE EXCEPTION 'not_guardian_of_subject' USING ERRCODE = '42501';
    END IF;
    target := p_subject_user_id;
    -- A guardian's delete is an erasure of the child's account, usage included (F1).
    DELETE FROM public.ai_usage_grants WHERE user_id = target;
  END IF;

  DELETE FROM public.profiles WHERE id = target;

  RETURN jsonb_build_object('ok', true);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.delete_my_account(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.delete_my_account(uuid) TO authenticated;
