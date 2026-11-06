-- The AI usage-limit day ends at midnight India time, not UTC midnight
-- (owner, 2026-11-06; most learners are in India, where UTC midnight is 05:30).
-- consume_ai_quota / release_ai_quota_grant stamp and count grant_date in
-- Asia/Kolkata. Rewritten in place from whatever version is installed, so this
-- works before or after 20261104230000_premium_billing (whose file now also uses
-- Asia/Kolkata). Must match backend main.py QUOTA_TZ and the scoring server's
-- server/examAllowance.ts. Today's grants keep their UTC date; the switch can
-- give at most one extra Learn answer window on the day it is applied.
DO $$
DECLARE
  f text;
  def text;
BEGIN
  FOREACH f IN ARRAY ARRAY['public.consume_ai_quota(uuid,text,text)', 'public.release_ai_quota_grant(uuid,text,text)'] LOOP
    def := pg_get_functiondef(f::regprocedure);
    def := replace(def, '(now() AT TIME ZONE ''utc'')::date', '(now() AT TIME ZONE ''Asia/Kolkata'')::date');
    EXECUTE def;
  END LOOP;
END $$;
