-- In-app "Send feedback" (main repo: src/features/contact/, src/services/contact/sendFeedback.ts).
--
-- Learners (signed in or guests) send the owner a short message: a bug, a marking
-- complaint, an idea. Rows are written ONLY through submit_feedback() (SECURITY
-- DEFINER), which stamps user_id from the JWT and applies two rate limits, so the
-- anon key can't be used to flood the table. Nobody but the service key reads it.
-- The hourly GitHub Actions job (main repo: scripts/scheduledJobs/sendFeedbackDigest.ts)
-- emails each new row to the owner and stamps notified_at.

CREATE TABLE IF NOT EXISTS public.user_feedback (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at     timestamptz NOT NULL DEFAULT now(),
  user_id        uuid REFERENCES profiles(id) ON DELETE SET NULL,
  kind           text NOT NULL CHECK (kind IN ('bug', 'marking', 'idea', 'other')),
  message        text NOT NULL CHECK (char_length(btrim(message)) BETWEEN 3 AND 4000),
  contact_email  text CHECK (contact_email IS NULL OR (char_length(contact_email) <= 254 AND contact_email LIKE '%_@_%')),
  route          text CHECK (char_length(route) <= 200),
  user_agent     text CHECK (char_length(user_agent) <= 400),
  notified_at    timestamptz
);

CREATE INDEX IF NOT EXISTS user_feedback_unnotified_idx
  ON public.user_feedback (created_at) WHERE notified_at IS NULL;
CREATE INDEX IF NOT EXISTS user_feedback_user_created_idx
  ON public.user_feedback (user_id, created_at DESC);

ALTER TABLE public.user_feedback ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.user_feedback FROM anon, authenticated;
-- No policies: only the service key (bypasses RLS) and submit_feedback() touch it.

CREATE OR REPLACE FUNCTION public.submit_feedback(
  p_kind text,
  p_message text,
  p_contact_email text DEFAULT NULL,
  p_route text DEFAULT NULL,
  p_user_agent text DEFAULT NULL
) RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_uid uuid := auth.uid();
  v_recent int;
  v_id uuid;
BEGIN
  IF v_uid IS NOT NULL THEN
    -- 10 messages per account per day.
    SELECT count(*) INTO v_recent FROM user_feedback
      WHERE user_id = v_uid AND created_at > now() - interval '1 day';
    IF v_recent >= 10 THEN
      RAISE EXCEPTION 'feedback_rate_limited' USING ERRCODE = 'P0001';
    END IF;
  ELSE
    -- Guests share one pool: 100 anonymous messages per hour across the whole app.
    SELECT count(*) INTO v_recent FROM user_feedback
      WHERE user_id IS NULL AND created_at > now() - interval '1 hour';
    IF v_recent >= 100 THEN
      RAISE EXCEPTION 'feedback_rate_limited' USING ERRCODE = 'P0001';
    END IF;
  END IF;

  INSERT INTO user_feedback (user_id, kind, message, contact_email, route, user_agent)
  VALUES (
    -- A signed-in user without a profile row yet is stored as a guest, not refused.
    (SELECT id FROM profiles WHERE id = v_uid),
    p_kind,
    btrim(p_message),
    NULLIF(btrim(p_contact_email), ''),
    left(p_route, 200),
    left(p_user_agent, 400)
  )
  RETURNING id INTO v_id;
  RETURN v_id;
END;
$$;

REVOKE ALL ON FUNCTION public.submit_feedback(text, text, text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.submit_feedback(text, text, text, text, text) TO anon, authenticated;
