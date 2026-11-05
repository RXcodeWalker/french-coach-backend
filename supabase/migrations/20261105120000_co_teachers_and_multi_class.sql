/*
  Teacher dashboard: co-teachers and several classes per student (owner, 2026-11-05).
  Main repo docs/systems/teacher-dashboard.md §2, ADR 0015 amendment.

  Co-teachers
    * class_teachers(class_id, teacher_id, role) — the owner (classes.teacher_id,
      backfilled here) plus up to 4 co-teachers.
    * A co-teacher joins with the class's co-teacher code (classes.co_teacher_code,
      'T-' + 6 characters, a separate namespace from the student join code). Only
      the owner can create, rotate or turn off that code, and only an existing
      teacher account (teacher_profiles row) can redeem it — so the invite-only
      teacher rule still holds.
    * Every teacher of a class sees and does what the owner does (roster, activity,
      insights, homework, marking, the join code), EXCEPT: archiving the class and
      managing co-teachers are owner-only. A co-teacher can leave; the owner can
      remove a co-teacher. Each teacher's marks stay their own (teacher_marks is
      per teacher). _owned_class() now means "a teacher of this class".

  Several classes per student
    * The one-active-class-per-student index is dropped; a student may be an
      active member of up to 5 classes. join_class no longer moves a student out
      of another class (p_leave_current is kept for old clients and ignored).
    * leave_class(p_class_id) leaves one class; the old no-argument leave_class()
      stays (leaves every class) for clients deployed before this.
    * my_classes() lists them; my_class() stays (the most recently joined) for
      old clients.
    * Each class's teachers still see only activity from that class's join date.

  Backward compatible with the frontend deployed before it: every existing
  function keeps its signature and result keys.
*/

-- ── class_teachers ───────────────────────────────────────────────────────────

CREATE TABLE public.class_teachers (
  class_id    uuid NOT NULL REFERENCES public.classes(id) ON DELETE CASCADE,
  teacher_id  uuid NOT NULL REFERENCES public.teacher_profiles(user_id) ON DELETE CASCADE,
  role        text NOT NULL CHECK (role IN ('owner', 'co_teacher')),
  added_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (class_id, teacher_id)
);

CREATE INDEX class_teachers_teacher_idx ON public.class_teachers (teacher_id);
CREATE UNIQUE INDEX class_teachers_one_owner ON public.class_teachers (class_id) WHERE role = 'owner';

INSERT INTO public.class_teachers (class_id, teacher_id, role, added_at)
SELECT id, teacher_id, 'owner', created_at FROM public.classes
ON CONFLICT DO NOTHING;

ALTER TABLE public.classes ADD COLUMN IF NOT EXISTS co_teacher_code text UNIQUE;

-- Policy helper: SECURITY DEFINER so the policies below don't recurse through
-- class_teachers' own RLS.
CREATE OR REPLACE FUNCTION public.is_class_teacher(p_class_id uuid)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
  SELECT EXISTS (SELECT 1 FROM public.class_teachers WHERE class_id = p_class_id AND teacher_id = auth.uid());
$$;
REVOKE EXECUTE ON FUNCTION public.is_class_teacher(uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.is_class_teacher(uuid) TO authenticated;

ALTER TABLE public.class_teachers ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.class_teachers FROM anon, authenticated;
GRANT SELECT ON public.class_teachers TO authenticated;
GRANT ALL ON public.class_teachers TO service_role;
CREATE POLICY "Teachers read their classes' teachers" ON public.class_teachers
  FOR SELECT TO authenticated USING (public.is_class_teacher(class_id));

-- Every teacher of a class (not only the owner) reads the class, its members and its homework.
DROP POLICY IF EXISTS "Teachers read own classes" ON public.classes;
CREATE POLICY "Teachers read their classes" ON public.classes
  FOR SELECT TO authenticated USING (public.is_class_teacher(id));

DROP POLICY IF EXISTS "Teachers read their classes' memberships" ON public.class_members;
CREATE POLICY "Teachers read their classes' memberships" ON public.class_members
  FOR SELECT TO authenticated USING (public.is_class_teacher(class_id));

DROP POLICY IF EXISTS "Teachers read their classes' assignments" ON public.assignments;
CREATE POLICY "Teachers read their classes' assignments" ON public.assignments
  FOR SELECT TO authenticated USING (public.is_class_teacher(class_id));

-- The co-teacher code is the owner's to share: hide it from co-teachers' table reads.
REVOKE SELECT ON public.classes FROM authenticated;
GRANT SELECT (id, teacher_id, name, exam_year, join_code, code_active, timezone, digest_enabled, created_at, archived_at)
  ON public.classes TO authenticated;

-- ── several classes per student ──────────────────────────────────────────────

DROP INDEX IF EXISTS public.class_members_one_active_per_student;
CREATE INDEX IF NOT EXISTS class_members_student_active_idx ON public.class_members (student_id) WHERE status = 'active';

-- ── access helpers ───────────────────────────────────────────────────────────

-- Now: any teacher of the (non-archived) class. Every teacher_* function uses it.
CREATE OR REPLACE FUNCTION public._owned_class(p_class_id uuid)
RETURNS public.classes
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v public.classes;
BEGIN
  SELECT c.* INTO v FROM public.classes c
    JOIN public.class_teachers t ON t.class_id = c.id AND t.teacher_id = public._require_uid()
   WHERE c.id = p_class_id AND c.archived_at IS NULL;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'forbidden' USING ERRCODE = '42501';
  END IF;
  RETURN v;
END;
$$;
REVOKE EXECUTE ON FUNCTION public._owned_class(uuid) FROM PUBLIC, anon, authenticated;

-- Owner only: archiving, and managing co-teachers.
CREATE OR REPLACE FUNCTION public._class_owner(p_class_id uuid)
RETURNS public.classes
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v public.classes;
BEGIN
  SELECT c.* INTO v FROM public.classes c
    JOIN public.class_teachers t ON t.class_id = c.id AND t.teacher_id = public._require_uid() AND t.role = 'owner'
   WHERE c.id = p_class_id AND c.archived_at IS NULL;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'owner_only' USING ERRCODE = '42501';
  END IF;
  RETURN v;
END;
$$;
REVOKE EXECUTE ON FUNCTION public._class_owner(uuid) FROM PUBLIC, anon, authenticated;

-- ── classes (owner row on create; archive is owner-only) ─────────────────────

CREATE OR REPLACE FUNCTION public.create_class(p_name text, p_exam_year text DEFAULT NULL, p_timezone text DEFAULT 'Europe/London')
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid := public._require_uid();
  v_id uuid;
  v_code text;
  v_try int := 0;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM public.teacher_profiles WHERE user_id = me) THEN
    RAISE EXCEPTION 'not_a_teacher' USING ERRCODE = '42501';
  END IF;
  IF length(btrim(coalesce(p_name, ''))) NOT BETWEEN 1 AND 60 THEN
    RAISE EXCEPTION 'invalid_name' USING ERRCODE = '22023';
  END IF;
  IF (SELECT count(*) FROM public.classes WHERE teacher_id = me AND archived_at IS NULL) >= 20 THEN
    RAISE EXCEPTION 'too_many_classes' USING ERRCODE = '22023';
  END IF;
  LOOP
    v_try := v_try + 1;
    v_code := public._new_class_code();
    BEGIN
      INSERT INTO public.classes (teacher_id, name, exam_year, join_code, timezone)
      VALUES (me, btrim(p_name), nullif(btrim(coalesce(p_exam_year, '')), ''), v_code,
              coalesce(nullif(btrim(coalesce(p_timezone, '')), ''), 'Europe/London'))
      RETURNING id INTO v_id;
      EXIT;
    EXCEPTION WHEN unique_violation THEN
      IF v_try >= 8 THEN RAISE; END IF;
    END;
  END LOOP;
  INSERT INTO public.class_teachers (class_id, teacher_id, role) VALUES (v_id, me, 'owner');
  RETURN jsonb_build_object('ok', true, 'class_id', v_id, 'join_code', v_code);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.create_class(text, text, text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.create_class(text, text, text) TO authenticated;

CREATE OR REPLACE FUNCTION public.archive_class(p_class_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  PERFORM public._class_owner(p_class_id);
  UPDATE public.class_members SET status = 'removed', ended_at = now()
   WHERE class_id = p_class_id AND status = 'active';
  UPDATE public.classes SET archived_at = now(), code_active = false, co_teacher_code = NULL WHERE id = p_class_id;
  RETURN jsonb_build_object('ok', true);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.archive_class(uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.archive_class(uuid) TO authenticated;

-- ── co-teachers ──────────────────────────────────────────────────────────────

-- 'T-' + 6 characters; a fresh code replaces the old one.
CREATE OR REPLACE FUNCTION public.rotate_co_teacher_code(p_class_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_code text;
  v_try int := 0;
BEGIN
  PERFORM public._class_owner(p_class_id);
  LOOP
    v_try := v_try + 1;
    v_code := 'T-' || public._new_class_code();
    BEGIN
      UPDATE public.classes SET co_teacher_code = v_code WHERE id = p_class_id;
      EXIT;
    EXCEPTION WHEN unique_violation THEN
      IF v_try >= 8 THEN RAISE; END IF;
    END;
  END LOOP;
  RETURN jsonb_build_object('ok', true, 'co_teacher_code', v_code);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.rotate_co_teacher_code(uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rotate_co_teacher_code(uuid) TO authenticated;

CREATE OR REPLACE FUNCTION public.clear_co_teacher_code(p_class_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  PERFORM public._class_owner(p_class_id);
  UPDATE public.classes SET co_teacher_code = NULL WHERE id = p_class_id;
  RETURN jsonb_build_object('ok', true);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.clear_co_teacher_code(uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.clear_co_teacher_code(uuid) TO authenticated;

-- The class's teachers, for any teacher of it; the co-teacher code only for the owner.
CREATE OR REPLACE FUNCTION public.teacher_class_teachers(p_class_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid;
  v public.classes;
  v_my_role text;
BEGIN
  v := public._owned_class(p_class_id);
  me := auth.uid();
  SELECT role INTO v_my_role FROM public.class_teachers WHERE class_id = p_class_id AND teacher_id = me;
  RETURN jsonb_build_object(
    'ok', true,
    'my_role', v_my_role,
    'co_teacher_code', CASE WHEN v_my_role = 'owner' THEN v.co_teacher_code END,
    'teachers', coalesce((
      SELECT jsonb_agg(jsonb_build_object('teacher_id', t.teacher_id, 'display_name', p.display_name,
                                          'role', t.role, 'added_at', t.added_at)
                       ORDER BY t.role = 'owner' DESC, t.added_at)
        FROM public.class_teachers t JOIN public.teacher_profiles p ON p.user_id = t.teacher_id
       WHERE t.class_id = p_class_id), '[]'::jsonb));
END;
$$;
REVOKE EXECUTE ON FUNCTION public.teacher_class_teachers(uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.teacher_class_teachers(uuid) TO authenticated;

CREATE OR REPLACE FUNCTION public.join_class_as_teacher(p_code text)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid := public._require_uid();
  v public.classes;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM public.teacher_profiles WHERE user_id = me) THEN
    RAISE EXCEPTION 'not_a_teacher' USING ERRCODE = '42501';
  END IF;
  SELECT * INTO v FROM public.classes
   WHERE co_teacher_code = upper(btrim(coalesce(p_code, ''))) AND archived_at IS NULL
   FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'invalid_code' USING ERRCODE = '22023';
  END IF;
  IF EXISTS (SELECT 1 FROM public.class_teachers WHERE class_id = v.id AND teacher_id = me) THEN
    RETURN jsonb_build_object('ok', true, 'class_id', v.id, 'already_teacher', true);
  END IF;
  IF (SELECT count(*) FROM public.class_teachers WHERE class_id = v.id) >= 5 THEN
    RAISE EXCEPTION 'too_many_teachers' USING ERRCODE = '22023';
  END IF;
  -- A teacher is never also a student of the same class.
  UPDATE public.class_members SET status = 'left', ended_at = now()
   WHERE class_id = v.id AND student_id = me AND status = 'active';
  INSERT INTO public.class_teachers (class_id, teacher_id, role) VALUES (v.id, me, 'co_teacher');
  RETURN jsonb_build_object('ok', true, 'class_id', v.id, 'already_teacher', false);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.join_class_as_teacher(text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.join_class_as_teacher(text) TO authenticated;

-- The owner removes a co-teacher, or a co-teacher removes themself. The owner never leaves.
CREATE OR REPLACE FUNCTION public.remove_class_teacher(p_class_id uuid, p_teacher_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid;
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
  RETURN jsonb_build_object('ok', true);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.remove_class_teacher(uuid, uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.remove_class_teacher(uuid, uuid) TO authenticated;

-- Classes the caller teaches, with their role (the classes table hides co_teacher_code).
CREATE OR REPLACE FUNCTION public.my_teaching_classes()
RETURNS TABLE (id uuid, name text, exam_year text, join_code text, code_active boolean, timezone text,
               digest_enabled boolean, created_at timestamptz, my_role text, owner_name text)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
#variable_conflict use_column
BEGIN
  RETURN QUERY
    SELECT c.id, c.name, c.exam_year, c.join_code, c.code_active, c.timezone, c.digest_enabled, c.created_at,
           t.role, op.display_name
      FROM public.class_teachers t
      JOIN public.classes c ON c.id = t.class_id AND c.archived_at IS NULL
      JOIN public.teacher_profiles op ON op.user_id = c.teacher_id
     WHERE t.teacher_id = public._require_uid()
     ORDER BY c.created_at ASC;
END;
$$;
REVOKE EXECUTE ON FUNCTION public.my_teaching_classes() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.my_teaching_classes() TO authenticated;

-- ── joining (student side): several classes ──────────────────────────────────

CREATE OR REPLACE FUNCTION public.preview_class(p_code text)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid := public._require_uid();
  v record;
  v_count int;
BEGIN
  SELECT c.id, c.name, c.exam_year, t.display_name AS teacher_name
    INTO v
    FROM public.classes c JOIN public.teacher_profiles t ON t.user_id = c.teacher_id
   WHERE c.join_code = upper(btrim(coalesce(p_code, ''))) AND c.code_active AND c.archived_at IS NULL;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'invalid_code' USING ERRCODE = '22023';
  END IF;
  SELECT count(*) INTO v_count FROM public.class_members WHERE student_id = me AND status = 'active';
  RETURN jsonb_build_object(
    'ok', true,
    'class_id', v.id,
    'class_name', v.name,
    'exam_year', v.exam_year,
    'teacher_name', v.teacher_name,
    'teacher_names', (SELECT jsonb_agg(p.display_name ORDER BY ct.role = 'owner' DESC, ct.added_at)
                        FROM public.class_teachers ct JOIN public.teacher_profiles p ON p.user_id = ct.teacher_id
                       WHERE ct.class_id = v.id),
    'is_own_class', EXISTS (SELECT 1 FROM public.class_teachers WHERE class_id = v.id AND teacher_id = me),
    'already_member', EXISTS (SELECT 1 FROM public.class_members WHERE class_id = v.id AND student_id = me AND status = 'active'),
    -- Kept for clients from before several classes: always null now (joining never moves a student out).
    'current_class_name', NULL,
    'other_class_count', v_count,
    'class_limit_reached', v_count >= 5 AND NOT EXISTS (
      SELECT 1 FROM public.class_members WHERE class_id = v.id AND student_id = me AND status = 'active')
  );
END;
$$;
REVOKE EXECUTE ON FUNCTION public.preview_class(text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.preview_class(text) TO authenticated;

CREATE OR REPLACE FUNCTION public.join_class(p_code text, p_leave_current boolean DEFAULT false)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid := public._require_uid();
  v public.classes;
BEGIN
  SELECT * INTO v FROM public.classes
   WHERE join_code = upper(btrim(coalesce(p_code, ''))) AND code_active AND archived_at IS NULL
   FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'invalid_code' USING ERRCODE = '22023';
  END IF;
  IF EXISTS (SELECT 1 FROM public.class_teachers WHERE class_id = v.id AND teacher_id = me) THEN
    RAISE EXCEPTION 'own_class' USING ERRCODE = '22023';
  END IF;
  IF EXISTS (SELECT 1 FROM public.class_members WHERE class_id = v.id AND student_id = me AND status = 'active') THEN
    RETURN jsonb_build_object('ok', true, 'class_id', v.id, 'already_member', true);
  END IF;
  IF (SELECT count(*) FROM public.class_members WHERE student_id = me AND status = 'active') >= 5 THEN
    RAISE EXCEPTION 'too_many_classes_joined' USING ERRCODE = '22023';
  END IF;
  IF (SELECT count(*) FROM public.class_members WHERE class_id = v.id AND status = 'active') >= 60 THEN
    RAISE EXCEPTION 'class_full' USING ERRCODE = '22023';
  END IF;
  -- A rejoin restarts the visible window (D7): joined_at is reset.
  INSERT INTO public.class_members (class_id, student_id, status, joined_at, ended_at)
  VALUES (v.id, me, 'active', now(), NULL)
  ON CONFLICT (class_id, student_id)
  DO UPDATE SET status = 'active', joined_at = now(), ended_at = NULL;
  RETURN jsonb_build_object('ok', true, 'class_id', v.id, 'already_member', false);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.join_class(text, boolean) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.join_class(text, boolean) TO authenticated;

-- Leave one class (the no-argument leave_class() — every class — stays for old clients).
CREATE OR REPLACE FUNCTION public.leave_class(p_class_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  UPDATE public.class_members SET status = 'left', ended_at = now()
   WHERE student_id = public._require_uid() AND class_id = p_class_id AND status = 'active';
  RETURN jsonb_build_object('ok', true);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.leave_class(uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.leave_class(uuid) TO authenticated;

CREATE OR REPLACE FUNCTION public.my_classes()
RETURNS TABLE (class_id uuid, class_name text, exam_year text, teacher_name text, teacher_names text[], joined_at timestamptz)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
#variable_conflict use_column
BEGIN
  RETURN QUERY
    SELECT c.id, c.name, c.exam_year, op.display_name,
           ARRAY(SELECT p.display_name FROM public.class_teachers ct JOIN public.teacher_profiles p ON p.user_id = ct.teacher_id
                  WHERE ct.class_id = c.id ORDER BY ct.role = 'owner' DESC, ct.added_at),
           m.joined_at
      FROM public.class_members m
      JOIN public.classes c ON c.id = m.class_id AND c.archived_at IS NULL
      JOIN public.teacher_profiles op ON op.user_id = c.teacher_id
     WHERE m.student_id = public._require_uid() AND m.status = 'active'
     ORDER BY m.joined_at ASC;
END;
$$;
REVOKE EXECUTE ON FUNCTION public.my_classes() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.my_classes() TO authenticated;

-- Old clients: the most recently joined class.
CREATE OR REPLACE FUNCTION public.my_class()
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v record;
BEGIN
  SELECT c.id, c.name, c.exam_year, t.display_name AS teacher_name, m.joined_at
    INTO v
    FROM public.class_members m
    JOIN public.classes c ON c.id = m.class_id AND c.archived_at IS NULL
    JOIN public.teacher_profiles t ON t.user_id = c.teacher_id
   WHERE m.student_id = public._require_uid() AND m.status = 'active'
   ORDER BY m.joined_at DESC
   LIMIT 1;
  IF NOT FOUND THEN
    RETURN jsonb_build_object('ok', true, 'class', NULL);
  END IF;
  RETURN jsonb_build_object('ok', true, 'class', jsonb_build_object(
    'class_id', v.id, 'class_name', v.name, 'exam_year', v.exam_year,
    'teacher_name', v.teacher_name, 'joined_at', v.joined_at));
END;
$$;
REVOKE EXECUTE ON FUNCTION public.my_class() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.my_class() TO authenticated;
