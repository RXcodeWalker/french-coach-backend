-- The owner's account has no AI usage limits (owner, 2026-11-13).
-- consume_ai_quota is rewritten in place from the installed version: before the invite
-- check it grants immediately, writing no grant row, when the user is the owner —
-- app_metadata.role = 'admin' AND the owner's email (same rule as is_admin(), migration
-- 20261102090000, and lib/auth.py). Everyone else is unchanged. release_ai_quota_grant
-- needs no change: with no grant row it deletes nothing.
DO $$
DECLARE
  def text;
  marker text := '  SELECT invite_status INTO v_invite_status';
  guard text := E'  IF EXISTS (\n'
    || E'    SELECT 1 FROM auth.users u\n'
    || E'    WHERE u.id = p_user_id\n'
    || E'      AND u.raw_app_meta_data->>''role'' = ''admin''\n'
    || E'      AND lower(coalesce(u.email, '''')) = ''omjhamvar29@gmail.com''\n'
    || E'  ) THEN\n'
    || E'    RETURN jsonb_build_object(''ok'', true, ''granted'', true, ''replayed'', false, ''used'', 0, ''limit'', 2147483647);\n'
    || E'  END IF;\n\n';
BEGIN
  def := pg_get_functiondef('public.consume_ai_quota(uuid,text,text)'::regprocedure);
  IF position(marker in def) = 0 THEN
    RAISE EXCEPTION 'consume_ai_quota body not recognised; owner exemption not applied';
  END IF;
  IF position('omjhamvar29@gmail.com' in def) > 0 THEN
    RETURN; -- already applied
  END IF;
  def := replace(def, marker, guard || marker);
  EXECUTE def;
END $$;
