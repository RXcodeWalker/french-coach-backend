/*
  # Retention loop: specific daily nudges + a weekly progress email

  Main repo: scripts/scheduledJobs/sendNotifications.ts (the sender),
  scripts/scheduledJobs/retention/ (the pure copy/decision modules),
  src/services/notifications/learnerSnapshot*.ts (the client mirror),
  docs/systems/retention-notifications.md (the spec).

  Replaces the generic "Don't lose your streak" nudge, which existed because the
  server never knew the streak count. Four pieces:

    - learner_snapshots: one row per account, written by the client (push-only,
      never read back — the same one-directional precedent as profiles.daily_goal).
      Holds what the app itself shows: the streak count and its day key (the
      learner's local calendar day — the main repo's src/lib/localDay.ts), streak
      freezes owned, the learner's timezone, and the coach's current recurring
      mistake (the Learn intervention loop's active problem: skill id, its
      display label/hint, misses this week). Only ever used to word the
      learner's own nudges, so a client that lies only lies to itself. It is
      deliberately NOT profiles.streak_days, which stays out of the client
      UPDATE grant (Phase 1.3) because leaderboards read it.

    - email_report_settings: the weekly email opt-out and an optional parent /
      guardian copy address. Own-row RLS. Each row carries two random
      unsubscribe tokens (student, parent); the parent token rotates whenever
      parent_email changes, so an old email's link can never suppress a new
      address.

    - email_suppressions: addresses that clicked "stop" on a parent copy. Global
      and service-only: re-entering a suppressed address in Profile does not
      restart the emails.

    - notifications_log gains 'daily_nudge' (ONE per user per local day, across
      every nudge variant — the old design could send a streak AND a goal nudge
      the same day), 'weekly_report' and 'weekly_report_parent', plus `variant`
      and `channel` columns so the sender records which message went out and
      how (push / email / failed). Old rows and old types stay valid.

  notification_runs: one row per sender run, so the sender can see how far
  apart GitHub really runs the "hourly" schedule and send early when needed.

  RPCs (service_role only):
    - get_daily_nudge_candidates(): everyone with a push subscription and a
      notify_* preference on, who has not had today's nudge (local day), with
      the facts the sender needs to choose and word one nudge: streak, freezes,
      today's answers (local day, the same day the app's goal ring counts),
      habit hour (most common local hour of their last 21 days of sessions),
      last activity, focus mistake. The decision itself lives in TypeScript
      (retention/dailyNudge.ts) where it is unit-tested.
    - get_weekly_report_candidates(): from Sunday 17:00 to Monday 09:59 local
      (claimed once per week, keyed on that Sunday), active in the last 35 days, weekly report on (student) and/or an unsuppressed parent
      address (parent). Ensures each candidate has an email_report_settings row
      (for the unsubscribe tokens) before returning this week's and last week's
      aggregates.

  Client RPC:
    - unsubscribe_email_report(p_token uuid): anon + authenticated. A student
      token turns the student's weekly email off; a parent token suppresses the
      current parent address. Returns 'student' | 'parent' | 'unknown'. Tokens
      are gen_random_uuid() (122 random bits) and reveal nothing.

  Rollback: drop the two RPCs and three tables; the sender's previous version
  used get_notification_candidates(text), which this migration leaves in place.
*/

-- ── learner_snapshots ─────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.learner_snapshots (
  user_id              uuid PRIMARY KEY REFERENCES public.profiles(id) ON DELETE CASCADE,
  timezone             text NOT NULL,
  streak_count         integer NOT NULL DEFAULT 0 CHECK (streak_count BETWEEN 0 AND 10000),
  streak_last_date     date,
  streak_freezes       integer NOT NULL DEFAULT 0 CHECK (streak_freezes BETWEEN 0 AND 100),
  focus_skill_id       text CHECK (focus_skill_id ~ '^[a-z_]{1,40}$'),
  focus_label          text CHECK (char_length(focus_label) <= 60),
  focus_hint           text CHECK (char_length(focus_hint) <= 80),
  focus_misses         integer CHECK (focus_misses BETWEEN 0 AND 1000),
  focus_last_missed_at timestamptz,
  focus_has_drill      boolean NOT NULL DEFAULT false,
  updated_at           timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.learner_snapshots ENABLE ROW LEVEL SECURITY;

CREATE POLICY "learner_snapshots: select own" ON public.learner_snapshots
  FOR SELECT TO authenticated USING (auth.uid() = user_id);
CREATE POLICY "learner_snapshots: insert own" ON public.learner_snapshots
  FOR INSERT TO authenticated WITH CHECK (auth.uid() = user_id);
CREATE POLICY "learner_snapshots: update own" ON public.learner_snapshots
  FOR UPDATE TO authenticated USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);

-- auto_expose_new_tables is off (20260811090000): grant explicitly.
GRANT SELECT, INSERT, UPDATE ON public.learner_snapshots TO authenticated;
GRANT SELECT, DELETE ON public.learner_snapshots TO service_role;

-- Same rule as push_subscriptions: an invalid zone would abort the candidate
-- query for every user. validate_push_subscription_timezone() only reads
-- NEW.timezone, so it is reused as-is.
CREATE TRIGGER learner_snapshots_validate_timezone
  BEFORE INSERT OR UPDATE ON public.learner_snapshots
  FOR EACH ROW EXECUTE FUNCTION public.validate_push_subscription_timezone();

-- ── email_report_settings ─────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.email_report_settings (
  user_id             uuid PRIMARY KEY REFERENCES public.profiles(id) ON DELETE CASCADE,
  weekly_report       boolean NOT NULL DEFAULT true,
  parent_email        text CHECK (
    parent_email IS NULL
    OR (char_length(parent_email) <= 254 AND parent_email ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$')
  ),
  student_unsub_token uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
  parent_unsub_token  uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
  updated_at          timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.email_report_settings ENABLE ROW LEVEL SECURITY;

CREATE POLICY "email_report_settings: select own" ON public.email_report_settings
  FOR SELECT TO authenticated USING (auth.uid() = user_id);
CREATE POLICY "email_report_settings: insert own" ON public.email_report_settings
  FOR INSERT TO authenticated WITH CHECK (auth.uid() = user_id);
CREATE POLICY "email_report_settings: update own" ON public.email_report_settings
  FOR UPDATE TO authenticated USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);

-- Column-level: a client can never set its own tokens. user_id is in the
-- UPDATE list because PostgREST's upsert re-sets every payload column on
-- conflict; RLS keeps it pinned to auth.uid().
GRANT SELECT (user_id, weekly_report, parent_email, updated_at) ON public.email_report_settings TO authenticated;
GRANT INSERT (user_id, weekly_report, parent_email) ON public.email_report_settings TO authenticated;
GRANT UPDATE (user_id, weekly_report, parent_email) ON public.email_report_settings TO authenticated;
GRANT SELECT, INSERT ON public.email_report_settings TO service_role;

CREATE OR REPLACE FUNCTION public.email_report_settings_touch()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at := now();
  IF NEW.parent_email IS NOT NULL THEN
    NEW.parent_email := lower(btrim(NEW.parent_email));
  END IF;
  IF TG_OP = 'UPDATE' AND NEW.parent_email IS DISTINCT FROM OLD.parent_email THEN
    NEW.parent_unsub_token := gen_random_uuid();
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER email_report_settings_touch
  BEFORE INSERT OR UPDATE ON public.email_report_settings
  FOR EACH ROW EXECUTE FUNCTION public.email_report_settings_touch();

-- ── email_suppressions ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.email_suppressions (
  email      text PRIMARY KEY CHECK (email = lower(email)),
  reason     text NOT NULL CHECK (reason IN ('parent_unsubscribe')),
  created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.email_suppressions ENABLE ROW LEVEL SECURITY;
-- No client policies or grants: written only by unsubscribe_email_report().
GRANT SELECT ON public.email_suppressions TO service_role;

-- ── notifications_log: new types + what was sent ──────────────────────────────

ALTER TABLE public.notifications_log DROP CONSTRAINT IF EXISTS notifications_log_notif_type_check;
ALTER TABLE public.notifications_log ADD CONSTRAINT notifications_log_notif_type_check
  CHECK (notif_type IN ('streak_at_risk', 'daily_goal', 'daily_nudge', 'weekly_report', 'weekly_report_parent'));

ALTER TABLE public.notifications_log
  ADD COLUMN IF NOT EXISTS variant text CHECK (char_length(variant) <= 40),
  ADD COLUMN IF NOT EXISTS channel text CHECK (channel IN ('push', 'email', 'failed')),
  ADD COLUMN IF NOT EXISTS created_at timestamptz DEFAULT now();

GRANT UPDATE (variant, channel) ON public.notifications_log TO service_role;

-- ── notification_runs ─────────────────────────────────────────────────────────
-- One row per sender run. GitHub's "hourly" schedule actually fires every few
-- hours (observed 2026-11-03: ~8 h apart), so the sender reads the recent gap
-- between runs and sends early when the next run would land too late
-- (retention/dailyNudge.ts runGapHours). Service-only.

CREATE TABLE IF NOT EXISTS public.notification_runs (
  id     bigserial PRIMARY KEY,
  job    text NOT NULL CHECK (job IN ('daily_nudge', 'weekly_report')),
  ran_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS notification_runs_job_ran_at_idx ON public.notification_runs (job, ran_at DESC);
ALTER TABLE public.notification_runs ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT ON public.notification_runs TO service_role;
GRANT USAGE ON SEQUENCE public.notification_runs_id_seq TO service_role;

-- ── get_daily_nudge_candidates ────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.get_daily_nudge_candidates()
RETURNS TABLE (
  user_id              uuid,
  email                text,
  timezone             text,
  local_date           date,
  local_hour           integer,
  notify_streak        boolean,
  notify_daily_goal    boolean,
  daily_goal           integer,
  today_count          integer,
  last_active          timestamptz,
  active_days_14       integer,
  habit_hour           integer,
  streak_count         integer,
  streak_last_date     date,
  streak_freezes       integer,
  focus_skill_id       text,
  focus_label          text,
  focus_hint           text,
  focus_misses         integer,
  focus_last_missed_at timestamptz,
  focus_has_drill      boolean,
  subscriptions        jsonb
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  RETURN QUERY
  WITH base AS (
    SELECT
      p.id AS uid,
      u.email::text AS email,
      COALESCE(ls.timezone, ps_agg.tz) AS tz,
      p.notify_streak, p.notify_daily_goal, p.daily_goal,
      ls.streak_count, ls.streak_last_date, ls.streak_freezes,
      ls.focus_skill_id, ls.focus_label, ls.focus_hint, ls.focus_misses,
      ls.focus_last_missed_at, ls.focus_has_drill,
      ps_agg.subs
    FROM public.profiles p
    JOIN auth.users u ON u.id = p.id
    JOIN LATERAL (
      SELECT min(ps.timezone) AS tz,
             jsonb_agg(jsonb_build_object('endpoint', ps.endpoint, 'p256dh', ps.p256dh, 'auth_key', ps.auth_key)) AS subs
      FROM public.push_subscriptions ps WHERE ps.user_id = p.id
    ) ps_agg ON ps_agg.subs IS NOT NULL
    LEFT JOIN public.learner_snapshots ls ON ls.user_id = p.id
    WHERE (p.notify_streak OR p.notify_daily_goal)
  )
  SELECT
    b.uid,
    b.email,
    b.tz,
    (now() AT TIME ZONE b.tz)::date,
    extract(hour FROM now() AT TIME ZONE b.tz)::integer,
    b.notify_streak,
    b.notify_daily_goal,
    COALESCE(b.daily_goal, 3),
    COALESCE(sess.today_count, 0)::integer,
    sess.last_active,
    COALESCE(sess.active_days_14, 0)::integer,
    habit.hour,
    COALESCE(b.streak_count, 0),
    b.streak_last_date,
    COALESCE(b.streak_freezes, 0),
    b.focus_skill_id,
    b.focus_label,
    b.focus_hint,
    b.focus_misses,
    b.focus_last_missed_at,
    COALESCE(b.focus_has_drill, false),
    b.subs
  FROM base b
  JOIN LATERAL (
    SELECT
      max(s.created_at) AS last_active,
      -- The app's goal ring and streak count the learner's local day (src/lib/localDay.ts).
      count(*) FILTER (WHERE (s.created_at AT TIME ZONE b.tz)::date = (now() AT TIME ZONE b.tz)::date) AS today_count,
      count(DISTINCT (s.created_at AT TIME ZONE b.tz)::date) FILTER (WHERE s.created_at > now() - interval '14 days') AS active_days_14
    FROM public.sessions s WHERE s.user_id = b.uid
  ) sess ON true
  LEFT JOIN LATERAL (
    SELECT extract(hour FROM s.created_at AT TIME ZONE b.tz)::integer AS hour
    FROM public.sessions s
    WHERE s.user_id = b.uid AND s.created_at > now() - interval '21 days'
    GROUP BY 1
    ORDER BY count(*) DESC, 1 DESC
    LIMIT 1
  ) habit ON true
  WHERE b.tz IS NOT NULL
    -- Daily nudges stop after two quiet weeks; the weekly email carries on.
    AND sess.last_active > now() - interval '14 days'
    AND NOT EXISTS (
      SELECT 1 FROM public.notifications_log nl
      WHERE nl.user_id = b.uid
        AND nl.notif_type IN ('daily_nudge', 'streak_at_risk', 'daily_goal')
        AND nl.sent_on = (now() AT TIME ZONE b.tz)::date
    );
END;
$$;
REVOKE ALL ON FUNCTION public.get_daily_nudge_candidates() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.get_daily_nudge_candidates() FROM anon, authenticated;
GRANT EXECUTE ON FUNCTION public.get_daily_nudge_candidates() TO service_role;

-- ── get_weekly_report_candidates ──────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.get_weekly_report_candidates()
RETURNS TABLE (
  user_id               uuid,
  email                 text,
  username              text,
  timezone              text,
  local_date            date,
  student_due           boolean,
  parent_email          text,
  parent_due            boolean,
  student_unsub_token   uuid,
  parent_unsub_token    uuid,
  days_active           jsonb,
  sessions_week         integer,
  sessions_prev_week    integer,
  minutes_week          integer,
  words_week            integer,
  xp_week               integer,
  scored_week           integer,
  avg_score_week        numeric,
  scored_prev_week      integer,
  avg_score_prev_week   numeric,
  best_score_week       numeric,
  best_question_week    text,
  exams_week            integer,
  top_topic_week        text,
  exam_available        boolean,
  last_active           timestamptz,
  streak_count          integer,
  streak_last_date      date,
  focus_skill_id        text,
  focus_label           text,
  focus_hint            text,
  focus_misses          integer,
  focus_has_drill       boolean
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  -- Who is due this hour: Sunday 17:00 to Monday 09:59 in their own timezone
  -- (wide on purpose: GitHub runs the "hourly" job only every few hours, and the
  -- first run inside the window sends), active in the last 35 days, student copy
  -- on and/or an unsuppressed parent address, and not already sent for this week.
  -- ldate is the week's Sunday: the claim key and the last day of the report.
  CREATE TEMP TABLE IF NOT EXISTS _weekly_due (
    uid uuid PRIMARY KEY, tz text, ldate date, student_due boolean, parent_due boolean
  ) ON COMMIT DROP;
  TRUNCATE _weekly_due;

  INSERT INTO _weekly_due (uid, tz, ldate, student_due, parent_due)
  SELECT x.uid, x.tz, x.ldate, x.student_due, x.parent_due
  FROM (
    SELECT
      p.id AS uid,
      tzs.tz,
      ((now() AT TIME ZONE tzs.tz)::date - extract(dow FROM now() AT TIME ZONE tzs.tz)::integer) AS ldate,
      (COALESCE(ers.weekly_report, true) AND u.email IS NOT NULL
        AND NOT EXISTS (
          SELECT 1 FROM public.notifications_log nl
          WHERE nl.user_id = p.id AND nl.notif_type = 'weekly_report'
            AND nl.sent_on = ((now() AT TIME ZONE tzs.tz)::date - extract(dow FROM now() AT TIME ZONE tzs.tz)::integer)
        )) AS student_due,
      (ers.parent_email IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM public.email_suppressions es WHERE es.email = ers.parent_email)
        AND NOT EXISTS (
          SELECT 1 FROM public.notifications_log nl
          WHERE nl.user_id = p.id AND nl.notif_type = 'weekly_report_parent'
            AND nl.sent_on = ((now() AT TIME ZONE tzs.tz)::date - extract(dow FROM now() AT TIME ZONE tzs.tz)::integer)
        )) AS parent_due
    FROM public.profiles p
    JOIN auth.users u ON u.id = p.id
    LEFT JOIN public.email_report_settings ers ON ers.user_id = p.id
    JOIN LATERAL (
      SELECT COALESCE(
        (SELECT ls.timezone FROM public.learner_snapshots ls WHERE ls.user_id = p.id),
        (SELECT min(ps.timezone) FROM public.push_subscriptions ps WHERE ps.user_id = p.id)
      ) AS tz
    ) tzs ON tzs.tz IS NOT NULL
    WHERE (
        (extract(dow FROM now() AT TIME ZONE tzs.tz) = 0 AND extract(hour FROM now() AT TIME ZONE tzs.tz) >= 17)
        OR (extract(dow FROM now() AT TIME ZONE tzs.tz) = 1 AND extract(hour FROM now() AT TIME ZONE tzs.tz) < 10)
      )
      AND EXISTS (
        SELECT 1 FROM public.sessions s
        WHERE s.user_id = p.id AND s.created_at > now() - interval '35 days'
      )
  ) x
  WHERE x.student_due OR x.parent_due;

  -- Unsubscribe tokens must exist before the email that carries them.
  INSERT INTO public.email_report_settings (user_id)
  SELECT d.uid FROM _weekly_due d
  ON CONFLICT ON CONSTRAINT email_report_settings_pkey DO NOTHING;

  RETURN QUERY
  SELECT
    d.uid,
    u.email::text,
    p.username,
    d.tz,
    d.ldate,
    d.student_due,
    ers.parent_email,
    d.parent_due,
    ers.student_unsub_token,
    ers.parent_unsub_token,
    days.arr,
    COALESCE(w.sessions, 0)::integer,
    COALESCE(pw.sessions, 0)::integer,
    COALESCE(w.minutes, 0)::integer,
    COALESCE(w.words, 0)::integer,
    COALESCE(w.xp, 0)::integer,
    COALESCE(w.scored, 0)::integer,
    w.avg_score,
    COALESCE(pw.scored, 0)::integer,
    pw.avg_score,
    best.score,
    best.question,
    COALESCE(w.exams, 0)::integer,
    topic.topic_key,
    NOT EXISTS (
      SELECT 1 FROM public.ai_usage_grants g
      WHERE g.user_id = d.uid AND g.feature = 'exam_attempt'
        AND g.grant_date > (now() AT TIME ZONE 'UTC')::date - 7
    ),
    lastact.at,
    COALESCE(ls.streak_count, 0),
    ls.streak_last_date,
    ls.focus_skill_id,
    ls.focus_label,
    ls.focus_hint,
    ls.focus_misses,
    COALESCE(ls.focus_has_drill, false)
  FROM _weekly_due d
  JOIN public.profiles p ON p.id = d.uid
  JOIN auth.users u ON u.id = d.uid
  JOIN public.email_report_settings ers ON ers.user_id = d.uid
  LEFT JOIN public.learner_snapshots ls ON ls.user_id = d.uid
  -- This week = the 7 local days ending today (oldest first).
  JOIN LATERAL (
    SELECT jsonb_agg(
      EXISTS (
        SELECT 1 FROM public.sessions s
        WHERE s.user_id = d.uid AND (s.created_at AT TIME ZONE d.tz)::date = day::date
      ) ORDER BY day
    ) AS arr
    FROM generate_series(d.ldate - 6, d.ldate, interval '1 day') AS day
  ) days ON true
  JOIN LATERAL (
    SELECT
      count(*) AS sessions,
      round(sum(COALESCE(s.duration_sec, 0)) / 60.0) AS minutes,
      sum(COALESCE(s.word_count, 0)) AS words,
      sum(COALESCE(s.xp_earned, 0)) AS xp,
      count(s.score) FILTER (WHERE s.mode <> 'exam') AS scored,
      round(avg(s.score) FILTER (WHERE s.mode <> 'exam'), 1) AS avg_score,
      count(*) FILTER (WHERE s.mode = 'exam') AS exams
    FROM public.sessions s
    WHERE s.user_id = d.uid
      AND (s.created_at AT TIME ZONE d.tz)::date BETWEEN d.ldate - 6 AND d.ldate
  ) w ON true
  JOIN LATERAL (
    SELECT
      count(*) AS sessions,
      count(s.score) FILTER (WHERE s.mode <> 'exam') AS scored,
      round(avg(s.score) FILTER (WHERE s.mode <> 'exam'), 1) AS avg_score
    FROM public.sessions s
    WHERE s.user_id = d.uid
      AND (s.created_at AT TIME ZONE d.tz)::date BETWEEN d.ldate - 13 AND d.ldate - 7
  ) pw ON true
  LEFT JOIN LATERAL (
    SELECT s.score, left(s.question_text, 140) AS question
    FROM public.sessions s
    WHERE s.user_id = d.uid AND s.mode <> 'exam' AND s.score IS NOT NULL
      AND (s.created_at AT TIME ZONE d.tz)::date BETWEEN d.ldate - 6 AND d.ldate
    ORDER BY s.score DESC, s.created_at DESC
    LIMIT 1
  ) best ON true
  LEFT JOIN LATERAL (
    SELECT s.topic_key
    FROM public.sessions s
    WHERE s.user_id = d.uid AND s.topic_key IS NOT NULL
      AND (s.created_at AT TIME ZONE d.tz)::date BETWEEN d.ldate - 6 AND d.ldate
    GROUP BY s.topic_key
    ORDER BY count(*) DESC, s.topic_key
    LIMIT 1
  ) topic ON true
  JOIN LATERAL (
    SELECT max(s.created_at) AS at FROM public.sessions s WHERE s.user_id = d.uid
  ) lastact ON true;
END;
$$;
REVOKE ALL ON FUNCTION public.get_weekly_report_candidates() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.get_weekly_report_candidates() FROM anon, authenticated;
GRANT EXECUTE ON FUNCTION public.get_weekly_report_candidates() TO service_role;

-- ── unsubscribe_email_report ──────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.unsubscribe_email_report(p_token uuid)
RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_parent text;
BEGIN
  IF p_token IS NULL THEN
    RETURN 'unknown';
  END IF;

  UPDATE public.email_report_settings SET weekly_report = false
  WHERE student_unsub_token = p_token;
  IF FOUND THEN
    RETURN 'student';
  END IF;

  SELECT ers.parent_email INTO v_parent
  FROM public.email_report_settings ers WHERE ers.parent_unsub_token = p_token;
  IF v_parent IS NOT NULL THEN
    INSERT INTO public.email_suppressions (email, reason)
    VALUES (lower(v_parent), 'parent_unsubscribe')
    ON CONFLICT (email) DO NOTHING;
    RETURN 'parent';
  END IF;

  RETURN 'unknown';
END;
$$;
REVOKE ALL ON FUNCTION public.unsubscribe_email_report(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.unsubscribe_email_report(uuid) TO anon, authenticated;
