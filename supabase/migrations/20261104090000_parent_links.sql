-- Parent view (main repo: src/features/parentView/, src/screens/ParentView.tsx).
--
-- A learner shares a read-only progress page with a parent by creating a
-- private link. The parent opens it on any device with no account: the page
-- calls get_parent_report(token), which is anon-callable and returns that
-- learner's last 120 days of practice as compact rows. The page builds the
-- dashboard from them in the browser (features/parentView/buildReport.ts), so
-- the SQL stays a plain, bounded read.
--
-- What a parent can see: when the learner practised, for how long, on which
-- topic, the scores and coach "next step" lines the learner saw, sample-exam
-- marks, and speaking-flow numbers (pace, pauses). What they cannot see:
-- recordings (never stored), transcripts, question text, notebook, account
-- details. Those columns are deliberately not selected below.
--
-- Security model (same as guardian_consents): the raw token (32 random bytes,
-- hex) is returned once to the learner who created it and lives only in the
-- link; the table stores sha256(token). The token travels in the URL hash
-- (/parent#<token>), so it is never sent to a server log or analytics. The
-- learner can list (with "last viewed") and revoke links at any time; a
-- revoked link stops working on the next load. Max 5 active links per learner.
--
-- No client can read or write parent_links directly: every path is an RPC
-- below (SECURITY DEFINER, pinned search_path, explicit revoke/grant).

CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA extensions;

CREATE TABLE IF NOT EXISTS public.parent_links (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id        uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  token_hash     text NOT NULL UNIQUE,
  label          text NOT NULL DEFAULT '' CHECK (char_length(label) <= 40),
  created_at     timestamptz NOT NULL DEFAULT now(),
  revoked_at     timestamptz,
  last_viewed_at timestamptz,
  view_count     integer NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS parent_links_user_idx ON public.parent_links (user_id);

ALTER TABLE public.parent_links ENABLE ROW LEVEL SECURITY;
-- No policies: RLS on with no policy denies every direct read/write; the RPCs
-- run as the table owner.
REVOKE ALL ON public.parent_links FROM PUBLIC;
REVOKE ALL ON public.parent_links FROM anon, authenticated;

-- ── create_parent_link ──────────────────────────────────────────────────────

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

  SELECT count(*) INTO v_active FROM public.parent_links WHERE user_id = me AND revoked_at IS NULL;
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

-- ── list_parent_links ───────────────────────────────────────────────────────

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

-- ── revoke_parent_link ──────────────────────────────────────────────────────

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
  UPDATE public.parent_links SET revoked_at = now()
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

-- ── get_parent_report ───────────────────────────────────────────────────────
-- anon + authenticated: the parent usually has no account. One unknown or
-- revoked token gives the same 'invalid_link' error, so nothing is leaked.

CREATE OR REPLACE FUNCTION public.get_parent_report(p_token text)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_link public.parent_links%ROWTYPE;
  v_since timestamptz := now() - interval '120 days';
  v_name text;
BEGIN
  IF p_token IS NULL OR p_token !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'invalid_link' USING ERRCODE = '22023';
  END IF;

  SELECT * INTO v_link FROM public.parent_links
  WHERE token_hash = encode(extensions.digest(p_token, 'sha256'), 'hex') AND revoked_at IS NULL;
  IF NOT FOUND THEN
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
        'exam', s.feedback->'exam'
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
