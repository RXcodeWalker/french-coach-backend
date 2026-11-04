-- Parent view: read an exam row's condition and marks from the real
-- sessions.practice_only / exam_marks columns (20261103140000) instead of the
-- feedback->'exam' blob the first version expected; the blob stays a fallback.
-- Same signature, grants re-stated.

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
