/*
  # F1 (security review 2026-11): clients can no longer insert profiles rows

  A signed-in user could call delete_my_account() and then INSERT a new profiles row for
  their own id with any consent_status, xp_baseline, username, ... and, because
  ai_usage_grants cascaded from profiles, with every usage limit reset.

  1. The profile row is created only by the signup trigger. production already has
     on_auth_user_created / handle_new_user, but no migration defined them; they are
     defined here so the repo matches (id only, ON CONFLICT DO NOTHING). The trigger is
     created only if it is missing, so production's existing trigger is not dropped.
  2. INSERT on profiles is revoked from authenticated and anon, and the
     "Users can insert own profile" policy is dropped. The app's profile writes are UPDATEs
     (an upsert needs INSERT privilege even when the row exists, so the main repo switched
     its three upserts to updates first: deploy that frontend before applying this).
  3. ai_usage_grants.user_id now references auth.users instead of profiles, so a deleted
     profile no longer takes the day's usage with it (deleting the login still cascades).
     The two erasure paths a guardian controls, revoke_guardian_consent and a guardian's
     delete_my_account(child), delete the child's grants explicitly so they still erase
     everything; a user deleting their own profile keeps their grants until the login goes.

  Works whether or not 20261005100000 / 20261006090000 / 20261014090000 are applied.
*/

-- 1. Signup creates the profile row.
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  INSERT INTO public.profiles (id) VALUES (NEW.id) ON CONFLICT (id) DO NOTHING;
  RETURN NEW;
END;
$$;
REVOKE EXECUTE ON FUNCTION public.handle_new_user() FROM PUBLIC, anon, authenticated;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'on_auth_user_created' AND tgrelid = 'auth.users'::regclass AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER on_auth_user_created
      AFTER INSERT ON auth.users
      FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();
  END IF;
END $$;

-- 2. No client inserts.
REVOKE INSERT ON public.profiles FROM authenticated, anon;
DROP POLICY IF EXISTS "Users can insert own profile" ON public.profiles;

-- 3. Usage outlives the profile row.
ALTER TABLE public.ai_usage_grants DROP CONSTRAINT IF EXISTS ai_usage_grants_user_id_fkey;
ALTER TABLE public.ai_usage_grants
  ADD CONSTRAINT ai_usage_grants_user_id_fkey
  FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;

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

  -- Revocation means stop processing AND erase. Usage grants no longer cascade from
  -- profiles (F1), so they are erased explicitly.
  DELETE FROM public.ai_usage_grants WHERE user_id = v_child;
  DELETE FROM public.profiles WHERE id = v_child;

  RETURN jsonb_build_object('ok', true);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.revoke_guardian_consent(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.revoke_guardian_consent(text) TO authenticated, anon;

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
    -- A guardian's delete is an erasure of the child's account, usage included (F1).
    DELETE FROM public.ai_usage_grants WHERE user_id = target;
  END IF;

  DELETE FROM public.profiles WHERE id = target;

  RETURN jsonb_build_object('ok', true);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.delete_my_account(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.delete_my_account(uuid) TO authenticated;
