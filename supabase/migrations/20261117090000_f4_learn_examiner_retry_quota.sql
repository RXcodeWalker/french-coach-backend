/*
  # F4 (security review 2026-11): the examiner grounding retry has its own daily row

  A Learn answer in the Examiner style charges `learn_answer` on its first attempt. The
  grounding retry (attempt 2, the same answer asked again when attempt 1's quotes did not
  hold) charged the 20-a-day `feedback` row, so a client could send every Learn answer as
  "attempt 2" and skip the 3-a-day Learn limit. It now charges `learn_examiner_retry`.

  10 a day (Premium 30, matching learn_answer's Premium limit): at most one retry per answer,
  with room for homework answers (assignment_learn_answer, 20 a day), whose retries land
  here too. The owner's account is exempt as for every feature (consume_ai_quota).

  ai_usage_grants.feature is an FK to ai_quota_limits, so this row must exist before the
  backend that names it deploys, or every examiner-style retry 503s.

  Works whether or not 20261104230000_premium_billing (premium_daily_limit) is applied.
  Rollback: delete the row only after the backend no longer names it.
*/

INSERT INTO public.ai_quota_limits (feature, daily_limit) VALUES
  ('learn_examiner_retry', 10)
ON CONFLICT (feature) DO NOTHING;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'ai_quota_limits' AND column_name = 'premium_daily_limit'
  ) THEN
    UPDATE public.ai_quota_limits SET premium_daily_limit = 30
    WHERE feature = 'learn_examiner_retry' AND premium_daily_limit IS NULL;
  END IF;
END $$;
