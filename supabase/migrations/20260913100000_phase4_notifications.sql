/*
  # Phase 4.3 — streak-at-risk / daily-goal notifications (Web Push + email)

  New surfaces:
    - push_subscriptions: user-owned, client writes its own row directly via
      the standard four-policy RLS shape (pronunciation_attempts precedent).
      timezone is validated at INSERT/UPDATE time by a trigger against
      pg_timezone_names — a CHECK constraint can't reference another table,
      and an invalid zone reaching get_notification_candidates' per-row
      LATERAL evaluation would abort the whole candidate query for every
      user, not just the offender.
    - profiles: three new columns (daily_goal, notify_streak,
      notify_daily_goal), client-writable. Mirrors 20260909130000's
      column-level-grant precedent — the existing narrowed GRANT UPDATE
      list is re-issued WITH these three columns ADDED, never replaced
      wholesale (that would silently re-grant total_xp/streak_days/etc,
      undoing Phase 1.3's fix).
    - notifications_log: service-role only, no client policies. Backs
      at-most-once-per-user/type/day send attempts via a claim-before-send
      upsert (see sendNotifications.ts) — not exactly-once delivery, an
      accepted trade-off (a false "already sent" beats a duplicate send).
    - get_notification_candidates(p_notif_type): one row per user (push
      subscriptions aggregated into a jsonb array), service_role only.
      "Today"/streak-plausibility windows are computed in each user's own
      captured local timezone, not UTC. The idempotency NOT EXISTS check is
      grouped around the whole (streak-cond OR goal-cond) disjunction, not
      applied to only one branch — an ungrouped `WHERE a OR b AND NOT EXISTS`
      would let AND's tighter precedence exempt the `a` branch from the
      idempotency check entirely, causing repeated hourly re-sends for the
      whole streak-at-risk window.
*/

-- ── push_subscriptions ───────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.push_subscriptions (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  endpoint   text NOT NULL,
  p256dh     text NOT NULL,
  auth_key   text NOT NULL,
  timezone   text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, endpoint)
);

ALTER TABLE public.push_subscriptions ENABLE ROW LEVEL SECURITY;

CREATE POLICY "push_subscriptions: select own" ON public.push_subscriptions
  FOR SELECT TO authenticated USING (auth.uid() = user_id);
CREATE POLICY "push_subscriptions: insert own" ON public.push_subscriptions
  FOR INSERT TO authenticated WITH CHECK (auth.uid() = user_id);
CREATE POLICY "push_subscriptions: update own" ON public.push_subscriptions
  FOR UPDATE TO authenticated USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);
CREATE POLICY "push_subscriptions: delete own" ON public.push_subscriptions
  FOR DELETE TO authenticated USING (auth.uid() = user_id);

-- This project has auto_expose_new_tables disabled (20260811090000) — a
-- plain CREATE TABLE grants nothing to anon/authenticated/service_role by
-- default (see 20260805090000's header for the prior outage this caused).
GRANT SELECT, INSERT, UPDATE, DELETE ON public.push_subscriptions TO authenticated;
GRANT SELECT, DELETE ON public.push_subscriptions TO service_role;

-- Reject invalid IANA zone names at the source. A CHECK constraint cannot
-- reference pg_timezone_names (a view, not a value list), so this must be a
-- trigger.
CREATE OR REPLACE FUNCTION public.validate_push_subscription_timezone()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_timezone_names WHERE name = NEW.timezone) THEN
    RAISE EXCEPTION 'invalid_timezone: %', NEW.timezone USING ERRCODE = '22023';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER push_subscriptions_validate_timezone
  BEFORE INSERT OR UPDATE ON public.push_subscriptions
  FOR EACH ROW EXECUTE FUNCTION public.validate_push_subscription_timezone();

-- ── profiles: notification preferences + push-only daily_goal mirror ───────────

ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS daily_goal integer,
  ADD COLUMN IF NOT EXISTS notify_streak boolean NOT NULL DEFAULT true,
  ADD COLUMN IF NOT EXISTS notify_daily_goal boolean NOT NULL DEFAULT true;

-- Additive: re-issue 20260909130000's narrowed authenticated UPDATE grant
-- list WITH these three columns added, never a wholesale replacement (which
-- would silently re-grant the stat columns Phase 1.3 revoked).
GRANT UPDATE (
  id, username, achievements, migration_version, username_changed_at,
  leaderboard_visibility, discoverable, friend_requests_from,
  created_at, updated_at,
  daily_goal, notify_streak, notify_daily_goal
) ON public.profiles TO authenticated;

-- ── notifications_log ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.notifications_log (
  user_id    uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  notif_type text NOT NULL CHECK (notif_type IN ('streak_at_risk', 'daily_goal')),
  sent_on    date NOT NULL,
  PRIMARY KEY (user_id, notif_type, sent_on)
);

ALTER TABLE public.notifications_log ENABLE ROW LEVEL SECURITY;
-- No policies for anon/authenticated — service_role bypasses RLS by design
-- and is the only writer/reader.
GRANT SELECT, INSERT ON public.notifications_log TO service_role;

-- ── get_notification_candidates ──────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.get_notification_candidates(p_notif_type text)
RETURNS TABLE (user_id uuid, email text, timezone text, daily_goal integer, subscriptions jsonb)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  RETURN QUERY
  SELECT p.id, u.email, ps_agg.tz, p.daily_goal, ps_agg.subs
  FROM public.profiles p
  JOIN auth.users u ON u.id = p.id
  JOIN LATERAL (
    SELECT min(ps.timezone) AS tz,
           jsonb_agg(jsonb_build_object('endpoint', ps.endpoint, 'p256dh', ps.p256dh, 'auth_key', ps.auth_key)) AS subs
    FROM public.push_subscriptions ps WHERE ps.user_id = p.id
  ) ps_agg ON true
  JOIN LATERAL (
    SELECT
      max(s.created_at) AS last_active,
      count(*) FILTER (WHERE (s.created_at AT TIME ZONE ps_agg.tz)::date = (now() AT TIME ZONE ps_agg.tz)::date) AS today_count,
      count(DISTINCT (s.created_at AT TIME ZONE ps_agg.tz)::date) FILTER (WHERE s.created_at > now() - interval '3 days') AS recent_active_days
    FROM public.sessions s WHERE s.user_id = p.id
  ) sess ON true
  WHERE
    (
      (p_notif_type = 'streak_at_risk' AND p.notify_streak
        AND sess.last_active BETWEEN now() - interval '32 hours' AND now() - interval '20 hours'
        AND sess.recent_active_days >= 2)
      OR
      (p_notif_type = 'daily_goal' AND p.notify_daily_goal
        AND COALESCE(sess.today_count, 0) < COALESCE(p.daily_goal, 3)
        AND extract(hour FROM now() AT TIME ZONE ps_agg.tz) BETWEEN 18 AND 21)
    )
    AND NOT EXISTS (
      SELECT 1 FROM public.notifications_log nl
      WHERE nl.user_id = p.id AND nl.notif_type = p_notif_type
        AND nl.sent_on = (now() AT TIME ZONE ps_agg.tz)::date
    );
END;
$$;
REVOKE ALL ON FUNCTION public.get_notification_candidates(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.get_notification_candidates(text) TO service_role;
