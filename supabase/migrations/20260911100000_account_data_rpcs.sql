/*
  # Phase 1.6 Part B — data export & account deletion RPCs

  export_my_data(): aggregates the caller's own rows across every table that
  carries `user_id` and holds data the plan's Part B names — profile,
  sessions, xp_events, coach_evidence, session_transcripts,
  scoring_envelopes, pronunciation_attempts, pronunciation_phoneme_stats.
  (The plan's "pronunciation_history" is these two actual tables —
  20260805090000_add_pronunciation_history.sql — there is no single table
  by that name.) Read-only, SECURITY DEFINER only so it can read
  session_transcripts/scoring_envelopes rows even though this project's RLS
  select policies on those tables are already owner-scoped and would work
  without it — DEFINER keeps this RPC's shape consistent with every other
  mutating/aggregating RPC in this schema and insulates it from a future RLS
  policy change on any one of these tables.

  delete_my_account(): `DELETE FROM profiles WHERE id = me` and lets the
  FK graph cascade (every table above is `user_id -> profiles(id) ON DELETE
  CASCADE`, verified against each table's own CREATE TABLE). Known
  limitation, called out in the plan and here: this does not remove the
  `auth.users` row itself — PostgREST/SQL has no access to the Auth admin
  API, so fully deleting the identity requires a privileged backend call
  (Auth admin API, e.g. via backend/lib/auth.py's existing service-role
  client pattern). Flagged as a fast-follow, not a Phase 1 blocker, per the
  plan's own "Open assumptions" section. A user whose profile is deleted
  this way can still technically log back in and will get a fresh blank
  profile row on next load — acceptable for the soft-launch scope this
  migration targets.

  Guardian-authorized export/delete of a linked child account (the
  `p_subject_user_id` parameter described in the plan) is added by the
  Part C migration once `guardian_consents` exists — see
  20260911110000_guardian_consent.sql — rather than forward-referencing a
  table that doesn't exist yet at this migration.
*/

CREATE OR REPLACE FUNCTION public.export_my_data()
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid := auth.uid();
  result jsonb;
BEGIN
  IF me IS NULL THEN
    RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000';
  END IF;

  SELECT jsonb_build_object(
    'exported_at', now(),
    'profile', (SELECT to_jsonb(p) FROM public.profiles p WHERE p.id = me),
    'sessions', COALESCE((SELECT jsonb_agg(to_jsonb(s)) FROM public.sessions s WHERE s.user_id = me), '[]'::jsonb),
    'xp_events', COALESCE((SELECT jsonb_agg(to_jsonb(e)) FROM public.xp_events e WHERE e.user_id = me), '[]'::jsonb),
    'coach_evidence', COALESCE((SELECT jsonb_agg(to_jsonb(c)) FROM public.coach_evidence c WHERE c.user_id = me), '[]'::jsonb),
    'session_transcripts', COALESCE((SELECT jsonb_agg(to_jsonb(t)) FROM public.session_transcripts t WHERE t.user_id = me), '[]'::jsonb),
    'scoring_envelopes', COALESCE((SELECT jsonb_agg(to_jsonb(v)) FROM public.scoring_envelopes v WHERE v.user_id = me), '[]'::jsonb),
    'pronunciation_attempts', COALESCE((SELECT jsonb_agg(to_jsonb(a)) FROM public.pronunciation_attempts a WHERE a.user_id = me), '[]'::jsonb),
    'pronunciation_phoneme_stats', COALESCE((SELECT jsonb_agg(to_jsonb(ps)) FROM public.pronunciation_phoneme_stats ps WHERE ps.user_id = me), '[]'::jsonb)
  ) INTO result;

  RETURN result;
END;
$$;
REVOKE EXECUTE ON FUNCTION public.export_my_data() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.export_my_data() TO authenticated;

CREATE OR REPLACE FUNCTION public.delete_my_account()
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid := auth.uid();
BEGIN
  IF me IS NULL THEN
    RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000';
  END IF;

  -- Cascades to every child table (sessions, xp_events, coach_evidence,
  -- session_transcripts, scoring_envelopes, pronunciation_attempts,
  -- pronunciation_phoneme_stats, gem_events, user_inventory, achievements,
  -- friendships, blocks, league_memberships, ... — every user_id FK in this
  -- schema is ON DELETE CASCADE off profiles(id)).
  DELETE FROM public.profiles WHERE id = me;

  RETURN jsonb_build_object('ok', true);
END;
$$;
REVOKE EXECUTE ON FUNCTION public.delete_my_account() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.delete_my_account() TO authenticated;
