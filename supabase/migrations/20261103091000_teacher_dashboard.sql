/*
  Teacher dashboard (main repo docs/plans/teacher-dashboard.md; ADR 0014
  "Teacher access to student data is granted by class membership").

  Owner decisions taken as recommended in the plan: D1 teachers are invite-only
  (teacher_invites, created by the admin), D3 homework is free during the pilot
  but capped (assignment_exam / assignment_learn_answer rows + per-assignment
  caps in assignment_allowance), D4 a teacher mark reaches the student only
  after "Share", D7 a teacher sees activity from the join date onward only.

  Security model (not deferred):
    * A teacher reads a student's data ONLY through the SECURITY DEFINER
      teacher_* functions below, ONLY for an `active` member of a class the
      teacher owns, and ONLY rows created at or after that member's joined_at.
      No RLS policy on sessions / scoring_envelopes / session_transcripts /
      exam_feedback_reports is widened for teachers.
    * Access starts when the student joins with the class code and ends the
      moment they leave or are removed.
    * Every function revokes EXECUTE from PUBLIC, anon and authenticated before
      granting it to exactly the role that needs it (data-model.md §1), and
      every table gets explicit grants (auto_expose_new_tables is off).

  House conventions (20260914090000): SECURITY DEFINER + SET search_path =
  pg_catalog, public, pg_temp; not_authenticated / 28000; domain errors 22023;
  forbidden 42501; jsonb results with 'ok'.
*/

-- ── sessions: the exam fields that used to be local-only ─────────────────────
-- Written by the client's sessionSync (same posture as every other column:
-- client-claimed, own-row RLS). practice_only = "does not count toward
-- progress" (ADR 0007), exam_marks = the envelope's three area marks,
-- scoring_session_id = the transcript's sessionId (joins to scoring_envelopes),
-- assignment_id = the homework this session answered (no FK: client-written).

ALTER TABLE public.sessions
  ADD COLUMN IF NOT EXISTS practice_only boolean,
  ADD COLUMN IF NOT EXISTS exam_marks jsonb,
  ADD COLUMN IF NOT EXISTS scoring_session_id text,
  ADD COLUMN IF NOT EXISTS assignment_id text;

CREATE INDEX IF NOT EXISTS sessions_user_created_idx ON public.sessions (user_id, created_at);

-- ── teacher_invites (D1) ─────────────────────────────────────────────────────

CREATE TABLE public.teacher_invites (
  code        text PRIMARY KEY CHECK (code = upper(code) AND length(code) BETWEEN 4 AND 40),
  max_uses    integer NOT NULL DEFAULT 1 CHECK (max_uses > 0),
  use_count   integer NOT NULL DEFAULT 0 CHECK (use_count >= 0),
  expires_at  timestamptz,
  note        text,
  created_by  uuid REFERENCES auth.users(id),
  created_at  timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.teacher_invites ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.teacher_invites FROM anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.teacher_invites TO authenticated;
GRANT ALL ON public.teacher_invites TO service_role;
CREATE POLICY "Admins manage teacher invites" ON public.teacher_invites
  FOR ALL TO authenticated USING (is_admin()) WITH CHECK (is_admin());

-- ── teacher_profiles ─────────────────────────────────────────────────────────

CREATE TABLE public.teacher_profiles (
  user_id       uuid PRIMARY KEY REFERENCES public.profiles(id) ON DELETE CASCADE,
  display_name  text NOT NULL CHECK (length(btrim(display_name)) BETWEEN 1 AND 60),
  school_name   text CHECK (school_name IS NULL OR length(school_name) <= 100),
  invite_code   text,
  digest_opt_out boolean NOT NULL DEFAULT false,
  created_at    timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.teacher_profiles ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.teacher_profiles FROM anon, authenticated;
GRANT SELECT ON public.teacher_profiles TO authenticated;
GRANT ALL ON public.teacher_profiles TO service_role;
CREATE POLICY "Teachers read own teacher profile" ON public.teacher_profiles
  FOR SELECT TO authenticated USING (user_id = auth.uid());

-- ── classes / class_members ──────────────────────────────────────────────────

CREATE TABLE public.classes (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  teacher_id     uuid NOT NULL REFERENCES public.teacher_profiles(user_id) ON DELETE CASCADE,
  name           text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 60),
  exam_year      text CHECK (exam_year IS NULL OR length(exam_year) <= 20),
  join_code      text NOT NULL UNIQUE,
  code_active    boolean NOT NULL DEFAULT true,
  timezone       text NOT NULL DEFAULT 'Europe/London' CHECK (length(timezone) <= 64),
  digest_enabled boolean NOT NULL DEFAULT true,
  created_at     timestamptz NOT NULL DEFAULT now(),
  archived_at    timestamptz
);

CREATE INDEX classes_teacher_idx ON public.classes (teacher_id);

ALTER TABLE public.classes ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.classes FROM anon, authenticated;
GRANT SELECT ON public.classes TO authenticated;
GRANT ALL ON public.classes TO service_role;
CREATE POLICY "Teachers read own classes" ON public.classes
  FOR SELECT TO authenticated USING (teacher_id = auth.uid());

CREATE TABLE public.class_members (
  class_id    uuid NOT NULL REFERENCES public.classes(id) ON DELETE CASCADE,
  student_id  uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  status      text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'left', 'removed')),
  joined_at   timestamptz NOT NULL DEFAULT now(),
  ended_at    timestamptz,
  PRIMARY KEY (class_id, student_id)
);

-- One active class per student (v1).
CREATE UNIQUE INDEX class_members_one_active_per_student
  ON public.class_members (student_id) WHERE status = 'active';

ALTER TABLE public.class_members ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.class_members FROM anon, authenticated;
GRANT SELECT ON public.class_members TO authenticated;
GRANT ALL ON public.class_members TO service_role;
CREATE POLICY "Students read own memberships" ON public.class_members
  FOR SELECT TO authenticated USING (student_id = auth.uid());
CREATE POLICY "Teachers read their classes' memberships" ON public.class_members
  FOR SELECT TO authenticated
  USING (EXISTS (SELECT 1 FROM public.classes c WHERE c.id = class_id AND c.teacher_id = auth.uid()));

-- ── assignments (homework) ───────────────────────────────────────────────────

CREATE TABLE public.assignments (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  class_id           uuid NOT NULL REFERENCES public.classes(id) ON DELETE CASCADE,
  kind               text NOT NULL CHECK (kind IN ('learn_topic', 'role_play', 'mock_exam')),
  title              text NOT NULL CHECK (length(btrim(title)) BETWEEN 1 AND 80),
  topic_key          text CHECK (topic_key IS NULL OR length(topic_key) <= 64),
  role_play_card_id  text CHECK (role_play_card_id IS NULL OR length(role_play_card_id) <= 64),
  question_count     integer CHECK (question_count IS NULL OR question_count BETWEEN 1 AND 5),
  student_ids        uuid[],
  due_at             timestamptz NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  archived_at        timestamptz,
  CHECK (kind <> 'learn_topic' OR (topic_key IS NOT NULL AND question_count IS NOT NULL)),
  CHECK (kind <> 'role_play' OR role_play_card_id IS NOT NULL)
);

CREATE INDEX assignments_class_idx ON public.assignments (class_id, due_at);

ALTER TABLE public.assignments ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.assignments FROM anon, authenticated;
GRANT SELECT ON public.assignments TO authenticated;
GRANT ALL ON public.assignments TO service_role;
CREATE POLICY "Teachers read their classes' assignments" ON public.assignments
  FOR SELECT TO authenticated
  USING (EXISTS (SELECT 1 FROM public.classes c WHERE c.id = class_id AND c.teacher_id = auth.uid()));
-- Students read theirs through my_assignments() (it applies student_ids).

-- ── exam_attempt_meta (server-written, POST /score) ──────────────────────────
-- How an exam attempt was sat, recorded by the scoring service beside the
-- envelope (never inside it): Exam Sim or Coached, whether it counts toward
-- progress (ADR 0007; a typed answer always forces false server-side), and the
-- homework it answered.

CREATE TABLE public.exam_attempt_meta (
  user_id                 uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  session_id              text NOT NULL,
  conduct_mode            text NOT NULL CHECK (conduct_mode IN ('examSim', 'coached')),
  counts_toward_progress  boolean NOT NULL,
  assignment_id           uuid REFERENCES public.assignments(id) ON DELETE SET NULL,
  created_at              timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, session_id)
);

CREATE INDEX exam_attempt_meta_assignment_idx ON public.exam_attempt_meta (assignment_id) WHERE assignment_id IS NOT NULL;

ALTER TABLE public.exam_attempt_meta ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.exam_attempt_meta FROM anon, authenticated;
GRANT SELECT ON public.exam_attempt_meta TO authenticated;
GRANT ALL ON public.exam_attempt_meta TO service_role;
CREATE POLICY "Users read own exam attempt meta" ON public.exam_attempt_meta
  FOR SELECT TO authenticated USING (user_id = auth.uid());

-- ── teacher_marks (Stage 4) ──────────────────────────────────────────────────
-- A teacher's own marks for an exam attempt, beside the AI's — never in or
-- over scoring_envelopes (ADR 0009 parity). mark_set is a TeacherMarkSet
-- (main repo src/domain/igcse/comparison/teacherMark.ts). Never fed to the
-- judge or a calibration set without an explicit owner decision (plan D8).

CREATE TABLE public.teacher_marks (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  attempt_id          text NOT NULL REFERENCES public.scoring_envelopes(attempt_id) ON DELETE CASCADE,
  session_id          text NOT NULL,
  class_id            uuid NOT NULL REFERENCES public.classes(id) ON DELETE CASCADE,
  teacher_id          uuid NOT NULL REFERENCES public.teacher_profiles(user_id) ON DELETE CASCADE,
  student_id          uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  mark_set            jsonb NOT NULL CHECK (jsonb_typeof(mark_set) = 'object'),
  total               integer NOT NULL CHECK (total BETWEEN 0 AND 40),
  shared_with_student boolean NOT NULL DEFAULT false,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (attempt_id, teacher_id)
);

ALTER TABLE public.teacher_marks ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.teacher_marks FROM anon, authenticated;
GRANT SELECT ON public.teacher_marks TO authenticated;
GRANT ALL ON public.teacher_marks TO service_role;
CREATE POLICY "Teachers read own marks" ON public.teacher_marks
  FOR SELECT TO authenticated USING (teacher_id = auth.uid());
CREATE POLICY "Students read shared marks" ON public.teacher_marks
  FOR SELECT TO authenticated USING (student_id = auth.uid() AND shared_with_student);

-- ── teacher_digest_log (weekly email, claim-before-send) ─────────────────────

CREATE TABLE public.teacher_digest_log (
  class_id    uuid NOT NULL REFERENCES public.classes(id) ON DELETE CASCADE,
  week_start  date NOT NULL,
  claimed_at  timestamptz NOT NULL DEFAULT now(),
  sent_at     timestamptz,
  error       text,
  PRIMARY KEY (class_id, week_start)
);

ALTER TABLE public.teacher_digest_log ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.teacher_digest_log FROM anon, authenticated;
GRANT ALL ON public.teacher_digest_log TO service_role;

-- ── homework allowance quota rows (D3) ───────────────────────────────────────
-- FK targets for ai_usage_grants.feature: must exist before the code that
-- names them deploys. assignment_exam replaces exam_attempt for a homework
-- mock (so homework never uses up the learner's own weekly exam);
-- assignment_learn_answer replaces learn_answer for a homework Learn answer.
-- Per-assignment caps live in assignment_allowance() below.

INSERT INTO public.ai_quota_limits (feature, daily_limit) VALUES
  ('assignment_exam', 2),
  ('assignment_learn_answer', 20)
ON CONFLICT (feature) DO NOTHING;

-- ── helpers ──────────────────────────────────────────────────────────────────

-- 6 characters, no 0/O/1/I/L, from gen_random_uuid()'s random bytes.
CREATE OR REPLACE FUNCTION public._new_class_code()
RETURNS text
LANGUAGE plpgsql
VOLATILE
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  alphabet constant text := 'ABCDEFGHJKMNPQRSTUVWXYZ23456789';
  bytes bytea := decode(replace(gen_random_uuid()::text, '-', ''), 'hex');
  out text := '';
  i int;
BEGIN
  FOR i IN 0..5 LOOP
    out := out || substr(alphabet, (get_byte(bytes, i) % length(alphabet)) + 1, 1);
  END LOOP;
  RETURN out;
END;
$$;
REVOKE EXECUTE ON FUNCTION public._new_class_code() FROM PUBLIC, anon, authenticated;

CREATE OR REPLACE FUNCTION public._require_uid()
RETURNS uuid
LANGUAGE plpgsql
STABLE
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  IF auth.uid() IS NULL THEN
    RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000';
  END IF;
  RETURN auth.uid();
END;
$$;
REVOKE EXECUTE ON FUNCTION public._require_uid() FROM PUBLIC, anon, authenticated;

-- The class row if the caller owns it and it is not archived; else forbidden.
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
  SELECT * INTO v FROM public.classes
   WHERE id = p_class_id AND teacher_id = public._require_uid() AND archived_at IS NULL;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'forbidden' USING ERRCODE = '42501';
  END IF;
  RETURN v;
END;
$$;
REVOKE EXECUTE ON FUNCTION public._owned_class(uuid) FROM PUBLIC, anon, authenticated;

-- ── teacher setup ────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.redeem_teacher_invite(p_code text, p_display_name text, p_school_name text DEFAULT NULL)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid := public._require_uid();
  v_code text := upper(btrim(coalesce(p_code, '')));
  v_invite public.teacher_invites;
BEGIN
  IF length(btrim(coalesce(p_display_name, ''))) NOT BETWEEN 1 AND 60 THEN
    RAISE EXCEPTION 'invalid_display_name' USING ERRCODE = '22023';
  END IF;
  IF EXISTS (SELECT 1 FROM public.teacher_profiles WHERE user_id = me) THEN
    UPDATE public.teacher_profiles
       SET display_name = btrim(p_display_name), school_name = nullif(btrim(coalesce(p_school_name, '')), '')
     WHERE user_id = me;
    RETURN jsonb_build_object('ok', true, 'already_teacher', true);
  END IF;
  SELECT * INTO v_invite FROM public.teacher_invites WHERE code = v_code FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'invalid_code' USING ERRCODE = '22023';
  END IF;
  IF v_invite.expires_at IS NOT NULL AND v_invite.expires_at < now() THEN
    RAISE EXCEPTION 'code_expired' USING ERRCODE = '22023';
  END IF;
  IF v_invite.use_count >= v_invite.max_uses THEN
    RAISE EXCEPTION 'code_used_up' USING ERRCODE = '22023';
  END IF;
  UPDATE public.teacher_invites SET use_count = use_count + 1 WHERE code = v_code;
  INSERT INTO public.teacher_profiles (user_id, display_name, school_name, invite_code)
  VALUES (me, btrim(p_display_name), nullif(btrim(coalesce(p_school_name, '')), ''), v_code);
  RETURN jsonb_build_object('ok', true, 'already_teacher', false);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.redeem_teacher_invite(text, text, text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.redeem_teacher_invite(text, text, text) TO authenticated;

CREATE OR REPLACE FUNCTION public.set_teacher_digest_opt_out(p_opt_out boolean)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  UPDATE public.teacher_profiles SET digest_opt_out = coalesce(p_opt_out, false) WHERE user_id = public._require_uid();
  IF NOT FOUND THEN
    RAISE EXCEPTION 'not_a_teacher' USING ERRCODE = '42501';
  END IF;
  RETURN jsonb_build_object('ok', true);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.set_teacher_digest_opt_out(boolean) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.set_teacher_digest_opt_out(boolean) TO authenticated;

-- ── classes ──────────────────────────────────────────────────────────────────

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
  RETURN jsonb_build_object('ok', true, 'class_id', v_id, 'join_code', v_code);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.create_class(text, text, text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.create_class(text, text, text) TO authenticated;

CREATE OR REPLACE FUNCTION public.update_class(p_class_id uuid, p_name text, p_exam_year text, p_timezone text, p_digest_enabled boolean)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  PERFORM public._owned_class(p_class_id);
  IF length(btrim(coalesce(p_name, ''))) NOT BETWEEN 1 AND 60 THEN
    RAISE EXCEPTION 'invalid_name' USING ERRCODE = '22023';
  END IF;
  UPDATE public.classes
     SET name = btrim(p_name),
         exam_year = nullif(btrim(coalesce(p_exam_year, '')), ''),
         timezone = coalesce(nullif(btrim(coalesce(p_timezone, '')), ''), timezone),
         digest_enabled = coalesce(p_digest_enabled, digest_enabled)
   WHERE id = p_class_id;
  RETURN jsonb_build_object('ok', true);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.update_class(uuid, text, text, text, boolean) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.update_class(uuid, text, text, text, boolean) TO authenticated;

CREATE OR REPLACE FUNCTION public.rotate_class_code(p_class_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_code text;
  v_try int := 0;
BEGIN
  PERFORM public._owned_class(p_class_id);
  LOOP
    v_try := v_try + 1;
    v_code := public._new_class_code();
    BEGIN
      UPDATE public.classes SET join_code = v_code, code_active = true WHERE id = p_class_id;
      EXIT;
    EXCEPTION WHEN unique_violation THEN
      IF v_try >= 8 THEN RAISE; END IF;
    END;
  END LOOP;
  RETURN jsonb_build_object('ok', true, 'join_code', v_code);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.rotate_class_code(uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rotate_class_code(uuid) TO authenticated;

CREATE OR REPLACE FUNCTION public.set_class_code_active(p_class_id uuid, p_active boolean)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  PERFORM public._owned_class(p_class_id);
  UPDATE public.classes SET code_active = coalesce(p_active, false) WHERE id = p_class_id;
  RETURN jsonb_build_object('ok', true);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.set_class_code_active(uuid, boolean) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.set_class_code_active(uuid, boolean) TO authenticated;

-- Archiving ends every membership (so students can join another class) and
-- stops the code and the digest. Teacher marks stay (history).
CREATE OR REPLACE FUNCTION public.archive_class(p_class_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  PERFORM public._owned_class(p_class_id);
  UPDATE public.class_members SET status = 'removed', ended_at = now()
   WHERE class_id = p_class_id AND status = 'active';
  UPDATE public.classes SET archived_at = now(), code_active = false WHERE id = p_class_id;
  RETURN jsonb_build_object('ok', true);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.archive_class(uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.archive_class(uuid) TO authenticated;

-- ── joining (student side) ───────────────────────────────────────────────────

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
  v_current record;
BEGIN
  SELECT c.id, c.name, c.exam_year, c.teacher_id, t.display_name AS teacher_name
    INTO v
    FROM public.classes c JOIN public.teacher_profiles t ON t.user_id = c.teacher_id
   WHERE c.join_code = upper(btrim(coalesce(p_code, ''))) AND c.code_active AND c.archived_at IS NULL;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'invalid_code' USING ERRCODE = '22023';
  END IF;
  SELECT m.class_id, c.name INTO v_current
    FROM public.class_members m JOIN public.classes c ON c.id = m.class_id
   WHERE m.student_id = me AND m.status = 'active';
  RETURN jsonb_build_object(
    'ok', true,
    'class_id', v.id,
    'class_name', v.name,
    'exam_year', v.exam_year,
    'teacher_name', v.teacher_name,
    'is_own_class', v.teacher_id = me,
    'already_member', v_current.class_id IS NOT NULL AND v_current.class_id = v.id,
    'current_class_name', CASE WHEN v_current.class_id IS NOT NULL AND v_current.class_id <> v.id THEN v_current.name END
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
  v_current uuid;
BEGIN
  SELECT * INTO v FROM public.classes
   WHERE join_code = upper(btrim(coalesce(p_code, ''))) AND code_active AND archived_at IS NULL
   FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'invalid_code' USING ERRCODE = '22023';
  END IF;
  IF v.teacher_id = me THEN
    RAISE EXCEPTION 'own_class' USING ERRCODE = '22023';
  END IF;
  SELECT class_id INTO v_current FROM public.class_members WHERE student_id = me AND status = 'active';
  IF v_current = v.id THEN
    RETURN jsonb_build_object('ok', true, 'class_id', v.id, 'already_member', true);
  END IF;
  IF v_current IS NOT NULL THEN
    IF NOT coalesce(p_leave_current, false) THEN
      RAISE EXCEPTION 'already_in_class' USING ERRCODE = '22023';
    END IF;
    UPDATE public.class_members SET status = 'left', ended_at = now()
     WHERE class_id = v_current AND student_id = me AND status = 'active';
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

CREATE OR REPLACE FUNCTION public.leave_class()
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  UPDATE public.class_members SET status = 'left', ended_at = now()
   WHERE student_id = public._require_uid() AND status = 'active';
  RETURN jsonb_build_object('ok', true);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.leave_class() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.leave_class() TO authenticated;

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
    JOIN public.classes c ON c.id = m.class_id
    JOIN public.teacher_profiles t ON t.user_id = c.teacher_id
   WHERE m.student_id = public._require_uid() AND m.status = 'active';
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

CREATE OR REPLACE FUNCTION public.remove_class_member(p_class_id uuid, p_student_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  PERFORM public._owned_class(p_class_id);
  UPDATE public.class_members SET status = 'removed', ended_at = now()
   WHERE class_id = p_class_id AND student_id = p_student_id AND status = 'active';
  RETURN jsonb_build_object('ok', true);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.remove_class_member(uuid, uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.remove_class_member(uuid, uuid) TO authenticated;

-- ── teacher reads (class-scoped, from joined_at onward) ──────────────────────

CREATE OR REPLACE FUNCTION public.teacher_class_roster(p_class_id uuid)
RETURNS TABLE (student_id uuid, display_name text, status text, joined_at timestamptz, ended_at timestamptz)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
#variable_conflict use_column
BEGIN
  PERFORM public._owned_class(p_class_id);
  RETURN QUERY
    SELECT m.student_id, p.username, m.status, m.joined_at, m.ended_at
      FROM public.class_members m JOIN public.profiles p ON p.id = m.student_id
     WHERE m.class_id = p_class_id
     ORDER BY m.status = 'active' DESC, lower(coalesce(p.username, '')) ASC;
END;
$$;
REVOKE EXECUTE ON FUNCTION public.teacher_class_roster(uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.teacher_class_roster(uuid) TO authenticated;

-- Activity rows for every active member since max(joined_at, p_since). No
-- transcript here (class payload); teacher_student_sessions has it.
CREATE OR REPLACE FUNCTION public.teacher_class_sessions(p_class_id uuid, p_since timestamptz)
RETURNS TABLE (
  student_id uuid, id text, mode text, topic_key text, score numeric, word_count integer,
  duration_sec integer, created_at timestamptz, practice_only boolean, exam_marks jsonb,
  scoring_session_id text, assignment_id text, cefr_level text, critical_count integer
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
#variable_conflict use_column
BEGIN
  PERFORM public._owned_class(p_class_id);
  RETURN QUERY
    SELECT s.user_id, s.id, s.mode, s.topic_key, s.score::numeric, s.word_count, s.duration_sec,
           s.created_at, s.practice_only, s.exam_marks, s.scoring_session_id, s.assignment_id,
           s.feedback->>'cefrLevel',
           CASE WHEN jsonb_typeof(s.feedback->'critical_count') = 'number' THEN (s.feedback->>'critical_count')::int END
      FROM public.sessions s
      JOIN public.class_members m ON m.student_id = s.user_id AND m.class_id = p_class_id AND m.status = 'active'
     WHERE s.created_at >= greatest(m.joined_at, coalesce(p_since, m.joined_at))
     ORDER BY s.created_at DESC
     LIMIT 10000;
END;
$$;
REVOKE EXECUTE ON FUNCTION public.teacher_class_sessions(uuid, timestamptz) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.teacher_class_sessions(uuid, timestamptz) TO authenticated;

CREATE OR REPLACE FUNCTION public.teacher_student_sessions(p_class_id uuid, p_student_id uuid, p_since timestamptz)
RETURNS TABLE (
  id text, mode text, topic_key text, question_text text, transcript text, score numeric,
  word_count integer, duration_sec integer, created_at timestamptz, practice_only boolean,
  exam_marks jsonb, scoring_session_id text, assignment_id text, feedback jsonb
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
#variable_conflict use_column
BEGIN
  PERFORM public._owned_class(p_class_id);
  RETURN QUERY
    SELECT s.id, s.mode, s.topic_key, s.question_text, s.transcript, s.score::numeric, s.word_count,
           s.duration_sec, s.created_at, s.practice_only, s.exam_marks, s.scoring_session_id,
           s.assignment_id, s.feedback
      FROM public.sessions s
      JOIN public.class_members m ON m.student_id = s.user_id AND m.class_id = p_class_id AND m.status = 'active'
     WHERE s.user_id = p_student_id
       AND s.created_at >= greatest(m.joined_at, coalesce(p_since, m.joined_at))
     ORDER BY s.created_at DESC
     LIMIT 1000;
END;
$$;
REVOKE EXECUTE ON FUNCTION public.teacher_student_sessions(uuid, uuid, timestamptz) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.teacher_student_sessions(uuid, uuid, timestamptz) TO authenticated;

-- Marked exam attempts (original envelopes only) with the marks and the QoL
-- error lists the Insights tab needs — not the whole envelope.
CREATE OR REPLACE FUNCTION public.teacher_class_exams(p_class_id uuid, p_since timestamptz)
RETURNS TABLE (
  student_id uuid, attempt_id text, session_id text, created_at timestamptz,
  total integer, role_play integer, role_play_max integer, communication integer, quality_of_language integer,
  qol_errors jsonb, report_errors jsonb,
  conduct_mode text, counts_toward_progress boolean, assignment_id uuid,
  teacher_total integer, teacher_shared boolean
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
#variable_conflict use_column
DECLARE
  me uuid;
BEGIN
  PERFORM public._owned_class(p_class_id);
  me := auth.uid();
  RETURN QUERY
    SELECT e.user_id, e.attempt_id, e.session_id, e.created_at,
           (e.envelope->>'total')::int,
           (SELECT coalesce(sum((t->>'mark')::int), 0)::int FROM jsonb_array_elements(coalesce(e.envelope->'rolePlayTasks', '[]'::jsonb)) t),
           (jsonb_array_length(coalesce(e.envelope->'rolePlayTasks', '[]'::jsonb)) * 2)::int,
           (e.envelope->'communication'->>'mark')::int,
           (e.envelope->'qualityOfLanguage'->>'mark')::int,
           coalesce(e.envelope->'qualityOfLanguage'->'errors', '[]'::jsonb),
           r.report->'qualityOfLanguage'->'errors',
           am.conduct_mode, am.counts_toward_progress, am.assignment_id,
           tm.total, tm.shared_with_student
      FROM public.scoring_envelopes e
      JOIN public.class_members m ON m.student_id = e.user_id AND m.class_id = p_class_id AND m.status = 'active'
      LEFT JOIN public.exam_attempt_meta am ON am.user_id = e.user_id AND am.session_id = e.session_id
      LEFT JOIN public.exam_feedback_reports r ON r.envelope_id = e.attempt_id
      LEFT JOIN public.teacher_marks tm ON tm.attempt_id = e.attempt_id AND tm.teacher_id = me
     WHERE e.regraded_from IS NULL
       AND e.created_at >= greatest(m.joined_at, coalesce(p_since, m.joined_at))
     ORDER BY e.created_at DESC
     LIMIT 2000;
END;
$$;
REVOKE EXECUTE ON FUNCTION public.teacher_class_exams(uuid, timestamptz) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.teacher_class_exams(uuid, timestamptz) TO authenticated;

-- One attempt in full, for the review screen.
CREATE OR REPLACE FUNCTION public.teacher_exam_attempt(p_class_id uuid, p_attempt_id text)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid;
  v record;
BEGIN
  PERFORM public._owned_class(p_class_id);
  me := auth.uid();
  SELECT e.user_id, e.attempt_id, e.session_id, e.created_at, e.envelope,
         r.report, am.conduct_mode, am.counts_toward_progress, am.assignment_id,
         tm.mark_set, tm.shared_with_student, tm.updated_at AS marked_at, p.username
    INTO v
    FROM public.scoring_envelopes e
    JOIN public.class_members m ON m.student_id = e.user_id AND m.class_id = p_class_id AND m.status = 'active'
    JOIN public.profiles p ON p.id = e.user_id
    LEFT JOIN public.exam_attempt_meta am ON am.user_id = e.user_id AND am.session_id = e.session_id
    LEFT JOIN public.exam_feedback_reports r ON r.envelope_id = e.attempt_id
    LEFT JOIN public.teacher_marks tm ON tm.attempt_id = e.attempt_id AND tm.teacher_id = me
   WHERE e.attempt_id = p_attempt_id AND e.created_at >= m.joined_at;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'not_found' USING ERRCODE = '22023';
  END IF;
  RETURN jsonb_build_object(
    'ok', true,
    'student_id', v.user_id, 'student_name', v.username,
    'attempt_id', v.attempt_id, 'session_id', v.session_id, 'created_at', v.created_at,
    'envelope', v.envelope, 'report', v.report,
    'conduct_mode', v.conduct_mode, 'counts_toward_progress', v.counts_toward_progress,
    'assignment_id', v.assignment_id,
    'teacher_mark_set', v.mark_set, 'teacher_shared', v.shared_with_student, 'teacher_marked_at', v.marked_at);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.teacher_exam_attempt(uuid, text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.teacher_exam_attempt(uuid, text) TO authenticated;

-- ── teacher marks ────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.save_teacher_mark(p_class_id uuid, p_attempt_id text, p_mark_set jsonb, p_total integer, p_shared boolean)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid;
  v record;
BEGIN
  PERFORM public._owned_class(p_class_id);
  me := auth.uid();
  IF p_mark_set IS NULL OR jsonb_typeof(p_mark_set) <> 'object' OR jsonb_typeof(p_mark_set->'marks') <> 'array'
     OR octet_length(p_mark_set::text) > 20000 THEN
    RAISE EXCEPTION 'invalid_mark_set' USING ERRCODE = '22023';
  END IF;
  IF p_total IS NULL OR p_total NOT BETWEEN 0 AND 40 THEN
    RAISE EXCEPTION 'invalid_total' USING ERRCODE = '22023';
  END IF;
  SELECT e.user_id, e.session_id INTO v
    FROM public.scoring_envelopes e
    JOIN public.class_members m ON m.student_id = e.user_id AND m.class_id = p_class_id AND m.status = 'active'
   WHERE e.attempt_id = p_attempt_id AND e.regraded_from IS NULL AND e.created_at >= m.joined_at;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'not_found' USING ERRCODE = '22023';
  END IF;
  INSERT INTO public.teacher_marks (attempt_id, session_id, class_id, teacher_id, student_id, mark_set, total, shared_with_student)
  VALUES (p_attempt_id, v.session_id, p_class_id, me, v.user_id, p_mark_set, p_total, coalesce(p_shared, false))
  ON CONFLICT (attempt_id, teacher_id) DO UPDATE
    SET mark_set = excluded.mark_set, total = excluded.total,
        shared_with_student = excluded.shared_with_student, class_id = excluded.class_id, updated_at = now();
  RETURN jsonb_build_object('ok', true);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.save_teacher_mark(uuid, text, jsonb, integer, boolean) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.save_teacher_mark(uuid, text, jsonb, integer, boolean) TO authenticated;

CREATE OR REPLACE FUNCTION public.my_shared_teacher_marks()
RETURNS TABLE (attempt_id text, session_id text, total integer, mark_set jsonb, teacher_name text, updated_at timestamptz)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
#variable_conflict use_column
BEGIN
  RETURN QUERY
    SELECT tm.attempt_id, tm.session_id, tm.total, tm.mark_set, t.display_name, tm.updated_at
      FROM public.teacher_marks tm JOIN public.teacher_profiles t ON t.user_id = tm.teacher_id
     WHERE tm.student_id = public._require_uid() AND tm.shared_with_student
     ORDER BY tm.updated_at DESC
     LIMIT 50;
END;
$$;
REVOKE EXECUTE ON FUNCTION public.my_shared_teacher_marks() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.my_shared_teacher_marks() TO authenticated;

-- ── assignments ──────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.create_assignment(
  p_class_id uuid, p_kind text, p_title text, p_topic_key text, p_role_play_card_id text,
  p_question_count integer, p_due_at timestamptz, p_student_ids uuid[]
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_id uuid;
  v_students uuid[];
BEGIN
  PERFORM public._owned_class(p_class_id);
  IF p_due_at IS NULL OR p_due_at < now() - interval '1 hour' THEN
    RAISE EXCEPTION 'invalid_due_date' USING ERRCODE = '22023';
  END IF;
  IF (SELECT count(*) FROM public.assignments WHERE class_id = p_class_id AND archived_at IS NULL AND due_at > now()) >= 30 THEN
    RAISE EXCEPTION 'too_many_assignments' USING ERRCODE = '22023';
  END IF;
  -- Only current members may be targeted; an empty list means the whole class.
  IF p_student_ids IS NOT NULL AND cardinality(p_student_ids) > 0 THEN
    SELECT array_agg(student_id) INTO v_students
      FROM public.class_members
     WHERE class_id = p_class_id AND status = 'active' AND student_id = ANY (p_student_ids);
    IF v_students IS NULL THEN
      RAISE EXCEPTION 'invalid_students' USING ERRCODE = '22023';
    END IF;
  END IF;
  INSERT INTO public.assignments (class_id, kind, title, topic_key, role_play_card_id, question_count, due_at, student_ids)
  VALUES (p_class_id, p_kind, btrim(coalesce(p_title, '')),
          CASE WHEN p_kind = 'learn_topic' THEN p_topic_key END,
          CASE WHEN p_kind = 'role_play' THEN p_role_play_card_id END,
          CASE WHEN p_kind = 'learn_topic' THEN p_question_count END,
          p_due_at, v_students)
  RETURNING id INTO v_id;
  RETURN jsonb_build_object('ok', true, 'assignment_id', v_id);
EXCEPTION WHEN check_violation THEN
  RAISE EXCEPTION 'invalid_assignment' USING ERRCODE = '22023';
END;
$$;
REVOKE EXECUTE ON FUNCTION public.create_assignment(uuid, text, text, text, text, integer, timestamptz, uuid[]) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.create_assignment(uuid, text, text, text, text, integer, timestamptz, uuid[]) TO authenticated;

CREATE OR REPLACE FUNCTION public.archive_assignment(p_assignment_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_class uuid;
BEGIN
  SELECT class_id INTO v_class FROM public.assignments WHERE id = p_assignment_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'not_found' USING ERRCODE = '22023';
  END IF;
  PERFORM public._owned_class(v_class);
  UPDATE public.assignments SET archived_at = now() WHERE id = p_assignment_id;
  RETURN jsonb_build_object('ok', true);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.archive_assignment(uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.archive_assignment(uuid) TO authenticated;

-- The caller's open homework: not archived, set after they joined (a rejoin
-- doesn't resurrect old homework), due within the last 14 days or later.
CREATE OR REPLACE FUNCTION public.my_assignments()
RETURNS TABLE (
  id uuid, kind text, title text, topic_key text, role_play_card_id text, question_count integer,
  due_at timestamptz, created_at timestamptz, class_name text, teacher_name text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
#variable_conflict use_column
DECLARE
  me uuid := public._require_uid();
BEGIN
  RETURN QUERY
    SELECT a.id, a.kind, a.title, a.topic_key, a.role_play_card_id, a.question_count,
           a.due_at, a.created_at, c.name, t.display_name
      FROM public.class_members m
      JOIN public.classes c ON c.id = m.class_id AND c.archived_at IS NULL
      JOIN public.teacher_profiles t ON t.user_id = c.teacher_id
      JOIN public.assignments a ON a.class_id = m.class_id
     WHERE m.student_id = me AND m.status = 'active'
       AND a.archived_at IS NULL
       AND a.due_at > now() - interval '14 days'
       AND (a.student_ids IS NULL OR me = ANY (a.student_ids))
     ORDER BY a.due_at ASC
     LIMIT 50;
END;
$$;
REVOKE EXECUTE ON FUNCTION public.my_assignments() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.my_assignments() TO authenticated;

-- ── homework allowance (server-only: Node scoring service + FastAPI) ─────────
-- Whether p_user_id may charge homework allowance for this assignment instead
-- of their own limits. Grants for an assignment carry the idempotency key
-- prefix 'asg:<assignment_id>:' (ai_usage_grants.id = user:feature:key), so the
-- per-assignment cap is a count over those ids, excluding p_exclude_key's own
-- grant (a retry never counts against itself).
--   mock_exam   → feature assignment_exam,         cap 1 attempt
--   learn_topic → feature assignment_learn_answer, cap question_count + 2
--   role_play   → no AI marking allowance (the drill uses the normal limits)
CREATE OR REPLACE FUNCTION public.assignment_allowance(p_user_id uuid, p_assignment_id uuid, p_exclude_key text DEFAULT NULL)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v record;
  v_feature text;
  v_cap int;
  v_used int;
  v_prefix text;
BEGIN
  SELECT a.kind, a.topic_key, a.question_count, a.student_ids, a.archived_at, a.due_at
    INTO v
    FROM public.assignments a
    JOIN public.class_members m ON m.class_id = a.class_id AND m.student_id = p_user_id AND m.status = 'active'
    JOIN public.classes c ON c.id = a.class_id AND c.archived_at IS NULL
   WHERE a.id = p_assignment_id;
  IF NOT FOUND OR v.archived_at IS NOT NULL OR (v.student_ids IS NOT NULL AND NOT (p_user_id = ANY (v.student_ids))) THEN
    RETURN jsonb_build_object('ok', false, 'reason', 'not_assigned');
  END IF;
  -- Late work is allowed for 14 days after the due date, then the normal limits apply.
  IF v.due_at < now() - interval '14 days' THEN
    RETURN jsonb_build_object('ok', false, 'reason', 'closed');
  END IF;
  IF v.kind = 'mock_exam' THEN
    v_feature := 'assignment_exam'; v_cap := 1;
  ELSIF v.kind = 'learn_topic' THEN
    v_feature := 'assignment_learn_answer'; v_cap := v.question_count + 2;
  ELSE
    RETURN jsonb_build_object('ok', false, 'reason', 'no_allowance');
  END IF;
  v_prefix := p_user_id::text || ':' || v_feature || ':asg:' || p_assignment_id::text || ':';
  SELECT count(*) INTO v_used FROM public.ai_usage_grants g
   WHERE g.user_id = p_user_id AND g.feature = v_feature
     AND left(g.id, length(v_prefix)) = v_prefix
     AND (p_exclude_key IS NULL OR g.id <> p_user_id::text || ':' || v_feature || ':' || p_exclude_key);
  IF v_used >= v_cap THEN
    RETURN jsonb_build_object('ok', false, 'reason', 'used_up', 'feature', v_feature, 'used', v_used, 'cap', v_cap);
  END IF;
  RETURN jsonb_build_object('ok', true, 'kind', v.kind, 'feature', v_feature, 'topic_key', v.topic_key,
                            'used', v_used, 'cap', v_cap);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.assignment_allowance(uuid, uuid, text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.assignment_allowance(uuid, uuid, text) TO service_role;

-- Homework progress per (assignment, active member): counted from what the
-- server itself recorded — homework allowance grants (every AI-marked homework
-- answer, Examiner style included, which writes no Session) and exam attempt
-- meta — plus synced sessions that name the assignment (role-play drill turns,
-- and answers that fell back to the learner's own allowance). Completion is
-- derived here, never stored.
CREATE OR REPLACE FUNCTION public.teacher_assignment_progress(p_class_id uuid)
RETURNS TABLE (assignment_id uuid, student_id uuid, learn_answers integer, sessions integer, exam_attempts integer, last_at timestamptz)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
#variable_conflict use_column
BEGIN
  PERFORM public._owned_class(p_class_id);
  RETURN QUERY
    WITH a AS (
      SELECT id, kind FROM public.assignments WHERE class_id = p_class_id AND archived_at IS NULL
    ), m AS (
      SELECT student_id, joined_at FROM public.class_members WHERE class_id = p_class_id AND status = 'active'
    ), g AS (
      SELECT a.id AS aid, gr.user_id, count(*)::int AS n, max(gr.created_at) AS last_at
        FROM a JOIN public.ai_usage_grants gr
          ON gr.feature = 'assignment_learn_answer'
         AND left(gr.id, length(gr.user_id::text || ':assignment_learn_answer:asg:' || a.id::text || ':'))
             = gr.user_id::text || ':assignment_learn_answer:asg:' || a.id::text || ':'
       GROUP BY a.id, gr.user_id
    ), s AS (
      SELECT a.id AS aid, se.user_id, count(*)::int AS n, max(se.created_at) AS last_at
        FROM a JOIN public.sessions se ON se.assignment_id = a.id::text
       GROUP BY a.id, se.user_id
    ), e AS (
      SELECT a.id AS aid, em.user_id, count(*)::int AS n, max(em.created_at) AS last_at
        FROM a JOIN public.exam_attempt_meta em ON em.assignment_id = a.id
       GROUP BY a.id, em.user_id
    )
    SELECT a.id, m.student_id,
           coalesce(g.n, 0), coalesce(s.n, 0), coalesce(e.n, 0),
           greatest(g.last_at, s.last_at, e.last_at)
      FROM a CROSS JOIN m
      LEFT JOIN g ON g.aid = a.id AND g.user_id = m.student_id
      LEFT JOIN s ON s.aid = a.id AND s.user_id = m.student_id
      LEFT JOIN e ON e.aid = a.id AND e.user_id = m.student_id
     WHERE coalesce(g.n, 0) + coalesce(s.n, 0) + coalesce(e.n, 0) > 0;
END;
$$;
REVOKE EXECUTE ON FUNCTION public.teacher_assignment_progress(uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.teacher_assignment_progress(uuid) TO authenticated;
