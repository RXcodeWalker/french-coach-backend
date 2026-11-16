/*
  # F12 (security review 2026-11): a removed co-teacher cannot rejoin with the old code

  remove_class_teacher left classes.co_teacher_code as it was, and join_class_as_teacher
  accepts it from any teacher account, so a co-teacher the owner removed could walk back
  in. When the owner removes another teacher, the class's co-teacher code is now cleared
  (the owner makes a new one with rotate_co_teacher_code). A co-teacher leaving on their
  own does not clear it. The result says whether it was cleared.
*/

CREATE OR REPLACE FUNCTION public.remove_class_teacher(p_class_id uuid, p_teacher_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid;
  v_removed boolean;
  v_cleared boolean := false;
BEGIN
  PERFORM public._owned_class(p_class_id);
  me := auth.uid();
  IF p_teacher_id <> me THEN
    PERFORM public._class_owner(p_class_id);
  END IF;
  IF EXISTS (SELECT 1 FROM public.class_teachers WHERE class_id = p_class_id AND teacher_id = p_teacher_id AND role = 'owner') THEN
    RAISE EXCEPTION 'owner_cannot_leave' USING ERRCODE = '22023';
  END IF;
  DELETE FROM public.class_teachers WHERE class_id = p_class_id AND teacher_id = p_teacher_id AND role = 'co_teacher';
  v_removed := FOUND;
  IF v_removed AND p_teacher_id <> me THEN
    UPDATE public.classes SET co_teacher_code = NULL WHERE id = p_class_id AND co_teacher_code IS NOT NULL;
    v_cleared := FOUND;
  END IF;
  RETURN jsonb_build_object('ok', true, 'co_teacher_code_cleared', v_cleared);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.remove_class_teacher(uuid, uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.remove_class_teacher(uuid, uuid) TO authenticated;
