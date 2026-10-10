-- get_notification_candidates has failed on every call since 20260913100000 with
-- "structure of query does not match function result type": it declares
-- `email text` but selects auth.users.email, which is varchar(255), and
-- PL/pgSQL's RETURN QUERY does not implicitly cast between them. It raised
-- even with zero candidates, so the hourly notifications job never sent
-- anything. The only change is the u.email::text cast; CREATE OR REPLACE keeps
-- the existing grants (service_role only, per 20261003101130).
CREATE OR REPLACE FUNCTION public.get_notification_candidates(p_notif_type text)
RETURNS TABLE (user_id uuid, email text, timezone text, daily_goal integer, subscriptions jsonb)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  RETURN QUERY
  SELECT p.id, u.email::text, ps_agg.tz, p.daily_goal, ps_agg.subs
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
