-- Parent view x weekly parent email (main repo docs/systems/parent-view.md, retention-notifications.md §4).
--
-- The Sunday parent email (sendNotifications.ts weekly_report) now carries a private link to the
-- parent dashboard (/parent#<token>). The raw link token of a parent link is never stored, so the
-- sender cannot reuse it; instead each email gets its own token in parent_link_tokens, valid
-- 35 days (the week it covers plus a margin, so last week's email still opens), hashed like the
-- link's own token. Every email token hangs off ONE parent link per (learner, parent address),
-- source 'weekly_email', which the learner sees in Profile -> Parent view ("Weekly email") with
-- its last-opened date, and can turn off: a revoked link kills every email token at once, and the
-- sender then stops adding the link for that address (the learner's choice wins over the email).
-- Changing the parent address revokes the old address's link, so a past email stops working.
--
-- mint_parent_digest_token is service_role only (the sender, with p_user_id from its own
-- candidate query). get_parent_report keeps its signature and output (exam columns version,
-- 20261104100000) and now also accepts an unexpired email token.

ALTER TABLE public.parent_links
  ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'weekly_email')),
  ADD COLUMN IF NOT EXISTS parent_email text CHECK (parent_email IS NULL OR parent_email = lower(parent_email)),
  -- Whether a revocation was the learner's own (Profile "Turn off") or automatic (address changed).
  -- Only the learner's own choice stops the sender from re-creating the link for that address.
  ADD COLUMN IF NOT EXISTS revoked_by_learner boolean NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS parent_links_weekly_email_idx
  ON public.parent_links (user_id, parent_email) WHERE source = 'weekly_email';

CREATE TABLE IF NOT EXISTS public.parent_link_tokens (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  link_id    uuid NOT NULL REFERENCES public.parent_links(id) ON DELETE CASCADE,
  token_hash text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS parent_link_tokens_link_idx ON public.parent_link_tokens (link_id);

ALTER TABLE public.parent_link_tokens ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.parent_link_tokens FROM PUBLIC;
REVOKE ALL ON public.parent_link_tokens FROM anon, authenticated;

-- ── mint_parent_digest_token (service_role) ─────────────────────────────────
-- Returns a fresh raw token for this learner's weekly-email link to p_parent_email, or NULL when
-- the learner turned that link off (or the address is empty). Never raises for a missing link.

CREATE OR REPLACE FUNCTION public.mint_parent_digest_token(p_user_id uuid, p_parent_email text)
RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_email text := lower(trim(coalesce(p_parent_email, '')));
  v_link uuid;
  v_token text;
BEGIN
  IF p_user_id IS NULL OR v_email = '' THEN
    RETURN NULL;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM public.profiles WHERE id = p_user_id) THEN
    RETURN NULL;
  END IF;

  -- A changed parent address: the old address's link (and every email sent to it) stops working.
  UPDATE public.parent_links SET revoked_at = now()
  WHERE user_id = p_user_id AND source = 'weekly_email' AND revoked_at IS NULL
    AND parent_email IS DISTINCT FROM v_email;

  SELECT id INTO v_link FROM public.parent_links
  WHERE user_id = p_user_id AND source = 'weekly_email' AND parent_email = v_email AND revoked_at IS NULL
  ORDER BY created_at DESC LIMIT 1;

  IF v_link IS NULL THEN
    -- The learner turned this address's link off: respect it.
    IF EXISTS (SELECT 1 FROM public.parent_links
               WHERE user_id = p_user_id AND source = 'weekly_email' AND parent_email = v_email AND revoked_at IS NOT NULL
                 AND revoked_by_learner) THEN
      RETURN NULL;
    END IF;
    INSERT INTO public.parent_links (user_id, token_hash, label, source, parent_email)
    VALUES (p_user_id,
            -- The link's own token is never shown to anyone; only email tokens open it.
            encode(extensions.digest(encode(extensions.gen_random_bytes(32), 'hex'), 'sha256'), 'hex'),
            'Weekly email', 'weekly_email', v_email)
    RETURNING id INTO v_link;
  END IF;

  DELETE FROM public.parent_link_tokens WHERE link_id = v_link AND expires_at < now();

  v_token := encode(extensions.gen_random_bytes(32), 'hex');
  INSERT INTO public.parent_link_tokens (link_id, token_hash, expires_at)
  VALUES (v_link, encode(extensions.digest(v_token, 'sha256'), 'hex'), now() + interval '35 days');
  RETURN v_token;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.mint_parent_digest_token(uuid, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.mint_parent_digest_token(uuid, text) FROM anon, authenticated;
GRANT EXECUTE ON FUNCTION public.mint_parent_digest_token(uuid, text) TO service_role;

-- ── revoke_parent_link: mark the learner's own choice ───────────────────────

CREATE OR REPLACE FUNCTION public.revoke_parent_link(p_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid := auth.uid();
  v_count integer;
BEGIN
  IF me IS NULL THEN
    RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000';
  END IF;
  UPDATE public.parent_links SET revoked_at = now(), revoked_by_learner = true
  WHERE id = p_id AND user_id = me AND revoked_at IS NULL;
  GET DIAGNOSTICS v_count = ROW_COUNT;
  IF v_count = 0 THEN
    RAISE EXCEPTION 'link_not_found' USING ERRCODE = '22023';
  END IF;
  RETURN jsonb_build_object('ok', true);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.revoke_parent_link(uuid) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.revoke_parent_link(uuid) FROM anon;
GRANT EXECUTE ON FUNCTION public.revoke_parent_link(uuid) TO authenticated;

-- ── list_parent_links: say which link is the weekly email's ─────────────────

CREATE OR REPLACE FUNCTION public.list_parent_links()
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid := auth.uid();
BEGIN
  IF me IS NULL THEN
    RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000';
  END IF;
  RETURN coalesce((
    SELECT jsonb_agg(jsonb_build_object(
      'id', id,
      'label', label,
      'source', source,
      'createdAt', created_at,
      'lastViewedAt', last_viewed_at,
      'viewCount', view_count
    ) ORDER BY created_at DESC)
    FROM public.parent_links
    WHERE user_id = me AND revoked_at IS NULL
  ), '[]'::jsonb);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.list_parent_links() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.list_parent_links() FROM anon;
GRANT EXECUTE ON FUNCTION public.list_parent_links() TO authenticated;

-- create_parent_link's 5-link cap counts only links the learner made themselves.
CREATE OR REPLACE FUNCTION public.create_parent_link(p_label text DEFAULT '')
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid := auth.uid();
  v_label text := left(trim(coalesce(p_label, '')), 40);
  v_active integer;
  v_token text;
  v_id uuid;
  v_created timestamptz;
BEGIN
  IF me IS NULL THEN
    RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM public.profiles WHERE id = me) THEN
    RAISE EXCEPTION 'no_profile' USING ERRCODE = '22023';
  END IF;

  SELECT count(*) INTO v_active FROM public.parent_links
  WHERE user_id = me AND revoked_at IS NULL AND source = 'manual';
  IF v_active >= 5 THEN
    RAISE EXCEPTION 'too_many_links' USING ERRCODE = '22023';
  END IF;

  v_token := encode(extensions.gen_random_bytes(32), 'hex');

  INSERT INTO public.parent_links (user_id, token_hash, label)
  VALUES (me, encode(extensions.digest(v_token, 'sha256'), 'hex'), v_label)
  RETURNING id, created_at INTO v_id, v_created;

  RETURN jsonb_build_object('id', v_id, 'token', v_token, 'label', v_label, 'createdAt', v_created);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.create_parent_link(text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.create_parent_link(text) FROM anon;
GRANT EXECUTE ON FUNCTION public.create_parent_link(text) TO authenticated;

-- ── get_parent_report: also accept an unexpired email token ─────────────────

CREATE OR REPLACE FUNCTION public.get_parent_report(p_token text)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_hash text;
  v_link public.parent_links%ROWTYPE;
  v_since timestamptz := now() - interval '120 days';
  v_name text;
BEGIN
  IF p_token IS NULL OR p_token !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'invalid_link' USING ERRCODE = '22023';
  END IF;
  v_hash := encode(extensions.digest(p_token, 'sha256'), 'hex');

  SELECT * INTO v_link FROM public.parent_links
  WHERE token_hash = v_hash AND revoked_at IS NULL;
  IF NOT FOUND THEN
    SELECT l.* INTO v_link
    FROM public.parent_link_tokens t JOIN public.parent_links l ON l.id = t.link_id
    WHERE t.token_hash = v_hash AND t.expires_at > now() AND l.revoked_at IS NULL;
  END IF;
  IF v_link.id IS NULL THEN
    RAISE EXCEPTION 'invalid_link' USING ERRCODE = '22023';
  END IF;

  UPDATE public.parent_links
  SET last_viewed_at = now(), view_count = view_count + 1
  WHERE id = v_link.id;

  SELECT username INTO v_name FROM public.profiles WHERE id = v_link.user_id;

  RETURN jsonb_build_object(
    'learnerName', v_name,
    'linkLabel', v_link.label,
    'generatedAt', now(),
    'sessions', coalesce((
      SELECT jsonb_agg(jsonb_build_object(
        'at', s.created_at,
        'mode', s.mode,
        'topic', s.topic_key,
        'score', s.score,
        'durationSec', s.duration_sec,
        'words', s.word_count,
        'cefr', s.feedback->>'cefrLevel',
        'nextStep', s.feedback->>'biggest_opportunity',
        'highlight', s.feedback->>'best_moment',
        'exam', CASE
          WHEN s.mode <> 'exam' THEN NULL
          WHEN s.practice_only IS NOT NULL
            THEN jsonb_build_object('practiceOnly', s.practice_only, 'marks', s.exam_marks)
          ELSE s.feedback->'exam'
        END
      ) ORDER BY s.created_at)
      FROM (
        SELECT * FROM public.sessions
        WHERE user_id = v_link.user_id AND created_at >= v_since
        ORDER BY created_at DESC
        LIMIT 1000
      ) s
    ), '[]'::jsonb),
    'speaking', coalesce((
      SELECT jsonb_agg(jsonb_build_object(
        'at', sp.created_at,
        'topic', sp.topic_key,
        'wpm', sp.words_per_minute,
        'startDelayS', sp.start_delay_s,
        'pausesOver2s', sp.pauses_over_2s,
        'longestPauseS', sp.longest_pause_s,
        'words', sp.word_count,
        'speakingS', sp.speaking_span_s
      ) ORDER BY sp.created_at)
      FROM (
        SELECT * FROM public.speaking_samples
        WHERE user_id = v_link.user_id AND created_at >= v_since AND follow_up = false
        ORDER BY created_at DESC
        LIMIT 600
      ) sp
    ), '[]'::jsonb)
  );
END;
$$;
REVOKE EXECUTE ON FUNCTION public.get_parent_report(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.get_parent_report(text) TO anon, authenticated;
