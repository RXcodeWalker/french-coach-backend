/*
  # exam_pronunciation_evidence (exam-pronunciation plan, Batch 4 — §3a)

  Feedback-only pronunciation evidence for exam turns. It never changes a mark:
  the marks stay in scoring_envelopes, which this table does not reference and
  the scorer never reads (main repo ADR 0009 boundary, extended by the plan's
  §3c data-isolation test).

  Why a sidecar table and not the envelope: the envelope is the immutable
  record of a scoring attempt and is written at submit time, while Coached
  analysis happens mid-exam (before any envelope exists) and Exam Sim analysis
  happens after. Calibration joins on (user_id, session_id) to the original
  envelope (unique index from 20260909120000), counting a row only when its
  exam_transcript matches a candidate utterance in that envelope.

  - One row per analysed candidate turn, unique on
    (user_id, session_id, turn_key, assessor_version). The row IS the cache: a
    stored row means no Azure call and no charge (POST /api/exam/pronunciation).
  - user_id comes only from the verified JWT `sub`, never from the request.
    session_id is client-generated; isolation comes from user_id, not from the
    session id being unguessable.
  - result: word-level Azure results (incl. per-word accuracy, offsets), the
    aligned exam-transcript word and recogniser agreement per word, turn-level
    signal quality. No overall pronunciation score is stored.
  - suppressed: the per-word recognition-trust reasons the assessor computed
    (recogniser disagreement, chunk seam, short word, number). Threshold-based
    reasons (accuracy floors, SNR/confidence floors) are applied client-side
    from FAIRNESS_CONFIG and are reproducible from `result` + fairness_version.
  - fairness_version: the client's EXAM_PRONUNCIATION_VERSION at analysis time.
  - FK user_id -> profiles ON DELETE CASCADE, the same as session_transcripts
    and scoring_envelopes: delete_my_account() and
    revoke_guardian_consent(p_token) both delete the profiles row, so the
    evidence is erased on both paths. An insert racing a deletion fails on the
    FK instead of leaving an orphan.

  Access: RLS on, select-own policy for authenticated; no client INSERT,
  UPDATE or DELETE. The backend writes and reads with the service key and
  filters on the JWT subject in code.

  export_my_data(uuid) is re-created with an `exam_pronunciation_evidence` key;
  its body is otherwise identical to 20260911110000.
*/

CREATE TABLE public.exam_pronunciation_evidence (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id           uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  session_id        text NOT NULL CHECK (char_length(session_id) BETWEEN 1 AND 200),
  part              text NOT NULL CHECK (part IN ('rolePlay', 'topic1', 'topic2')),
  turn_key          text NOT NULL CHECK (char_length(turn_key) BETWEEN 1 AND 20),
  assessor_version  text NOT NULL,
  fairness_version  text NOT NULL,
  exam_transcript   text NOT NULL,
  reference_text    text NOT NULL DEFAULT '',
  result            jsonb NOT NULL,
  suppressed        jsonb NOT NULL DEFAULT '[]'::jsonb,
  raw_s             numeric(10, 3),
  trimmed_s         numeric(10, 3) NOT NULL CHECK (trimmed_s >= 0),
  created_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, session_id, turn_key, assessor_version)
);

CREATE INDEX exam_pronunciation_evidence_user_session_idx
  ON public.exam_pronunciation_evidence (user_id, session_id);

ALTER TABLE public.exam_pronunciation_evidence ENABLE ROW LEVEL SECURITY;

CREATE POLICY exam_pronunciation_evidence_select_own
  ON public.exam_pronunciation_evidence
  FOR SELECT TO authenticated
  USING (user_id = auth.uid());

REVOKE ALL ON public.exam_pronunciation_evidence FROM PUBLIC;
REVOKE ALL ON public.exam_pronunciation_evidence FROM anon, authenticated;
GRANT SELECT ON public.exam_pronunciation_evidence TO authenticated;
GRANT SELECT, INSERT ON public.exam_pronunciation_evidence TO service_role;

-- ── export_my_data: add exam_pronunciation_evidence ─────────────────────────

CREATE OR REPLACE FUNCTION public.export_my_data(p_subject_user_id uuid DEFAULT NULL)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid := auth.uid();
  target uuid;
  result jsonb;
BEGIN
  IF me IS NULL THEN
    RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000';
  END IF;

  IF p_subject_user_id IS NULL OR p_subject_user_id = me THEN
    target := me;
  ELSE
    IF NOT EXISTS (
      SELECT 1 FROM public.guardian_consents gc
      WHERE gc.child_user_id = p_subject_user_id
        AND gc.granted_at IS NOT NULL AND gc.revoked_at IS NULL
        AND EXISTS (SELECT 1 FROM auth.users u WHERE u.id = me AND lower(u.email) = gc.guardian_email)
    ) THEN
      RAISE EXCEPTION 'not_guardian_of_subject' USING ERRCODE = '42501';
    END IF;
    target := p_subject_user_id;
  END IF;

  SELECT jsonb_build_object(
    'exported_at', now(),
    'profile', (SELECT to_jsonb(p) FROM public.profiles p WHERE p.id = target),
    'sessions', COALESCE((SELECT jsonb_agg(to_jsonb(s)) FROM public.sessions s WHERE s.user_id = target), '[]'::jsonb),
    'xp_events', COALESCE((SELECT jsonb_agg(to_jsonb(e)) FROM public.xp_events e WHERE e.user_id = target), '[]'::jsonb),
    'coach_evidence', COALESCE((SELECT jsonb_agg(to_jsonb(c)) FROM public.coach_evidence c WHERE c.user_id = target), '[]'::jsonb),
    'session_transcripts', COALESCE((SELECT jsonb_agg(to_jsonb(t)) FROM public.session_transcripts t WHERE t.user_id = target), '[]'::jsonb),
    'scoring_envelopes', COALESCE((SELECT jsonb_agg(to_jsonb(v)) FROM public.scoring_envelopes v WHERE v.user_id = target), '[]'::jsonb),
    'pronunciation_attempts', COALESCE((SELECT jsonb_agg(to_jsonb(a)) FROM public.pronunciation_attempts a WHERE a.user_id = target), '[]'::jsonb),
    'pronunciation_phoneme_stats', COALESCE((SELECT jsonb_agg(to_jsonb(ps)) FROM public.pronunciation_phoneme_stats ps WHERE ps.user_id = target), '[]'::jsonb),
    'exam_pronunciation_evidence', COALESCE((SELECT jsonb_agg(to_jsonb(ep)) FROM public.exam_pronunciation_evidence ep WHERE ep.user_id = target), '[]'::jsonb)
  ) INTO result;

  RETURN result;
END;
$$;
REVOKE EXECUTE ON FUNCTION public.export_my_data(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.export_my_data(uuid) TO authenticated;
