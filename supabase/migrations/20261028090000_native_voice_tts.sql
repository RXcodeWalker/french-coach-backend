/*
  Native voice for AI-corrected sentences (Pronunciation Lab Stage 4b, owner
  decision D2 — main repo docs/plans/pronunciation-lab.md §9).

  POST /api/native-voice (routers/native_voice.py) synthesises a sentence once
  with Azure text-to-speech and stores it in the public `tts-audio` bucket at
  the path the app plays it from (main repo src/services/tts/voiceKey.ts). A
  sentence already in the bucket costs nothing; only a new one is charged.

  1. ('native_voice', 20) in ai_quota_limits — 20 new sentences per account per
     UTC day. Learn marks 3 answers a day, so this leaves room for Lab practice
     on other corrected sentences. ai_usage_grants.feature is an FK: this row
     must exist before the backend that charges it deploys, or every request
     503s.

  2. azure_tts_usage — the character ledger. Azure bills neural text-to-speech
     in characters, a different meter from azure_speech_usage (seconds of
     audio assessed), so it gets its own table. One row per synthesis request,
     'reserved' when the request starts, 'released' when no billable call
     happened (the characters are kept for the record, excluded from totals).
     A reserved row whose request died keeps counting: over-counting is the
     safe direction for a budget. No content: no text, no audio — only the
     character count and the object path's hash.

  3. azure_tts_budget — one config row. cap_characters is the month's ceiling
     for THIS endpoint (UTC calendar month). It ships at 50,000: Azure F0
     gives 500,000 neural characters a month for the whole key, and the
     "Record voices" workflow spends from the same allowance (its catalogue is
     ≈ 393k characters, recorded once). Change it without a migration:
       UPDATE public.azure_tts_budget SET cap_characters = 100000;
     NULL means unlimited — only on a paid tier.

  4. reserve_azure_tts_characters / release_azure_tts_characters — service-role
     RPCs. reserve takes a transaction-scoped advisory lock so two concurrent
     reservations cannot both squeeze under the cap.

  Client grants: none. RLS on with no policies; anon/authenticated hold no
  privilege on either table or any function here.

  Rollback: drop the functions and tables, and DELETE the quota row only after
  the backend that names it is rolled back.
*/

INSERT INTO public.ai_quota_limits (feature, daily_limit) VALUES
  ('native_voice', 20)
ON CONFLICT (feature) DO NOTHING;

-- ── azure_tts_usage ──────────────────────────────────────────────────────────

CREATE TABLE public.azure_tts_usage (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     uuid REFERENCES public.profiles(id) ON DELETE SET NULL,
  object_path text NOT NULL,
  characters  integer NOT NULL CHECK (characters >= 0),
  status      text NOT NULL DEFAULT 'reserved' CHECK (status IN ('reserved', 'released')),
  created_at  timestamptz NOT NULL DEFAULT now(),
  released_at timestamptz
);

CREATE INDEX azure_tts_usage_created_at_idx ON public.azure_tts_usage (created_at);

ALTER TABLE public.azure_tts_usage ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.azure_tts_usage FROM anon, authenticated;
GRANT SELECT ON public.azure_tts_usage TO service_role;

-- ── azure_tts_budget ─────────────────────────────────────────────────────────

CREATE TABLE public.azure_tts_budget (
  id              boolean PRIMARY KEY DEFAULT true CHECK (id),
  cap_characters  integer CHECK (cap_characters IS NULL OR cap_characters >= 0),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
INSERT INTO public.azure_tts_budget (id, cap_characters) VALUES (true, 50000)
ON CONFLICT (id) DO NOTHING;

ALTER TABLE public.azure_tts_budget ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.azure_tts_budget FROM anon, authenticated;
GRANT SELECT ON public.azure_tts_budget TO service_role;

-- ── reserve_azure_tts_characters ─────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.reserve_azure_tts_characters(
  p_user_id     uuid,
  p_object_path text,
  p_characters  integer
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_cap integer;
  v_used bigint;
  v_id uuid;
  v_user uuid;
BEGIN
  IF p_characters IS NULL OR p_characters < 0 THEN
    RAISE EXCEPTION 'invalid_characters' USING ERRCODE = '22023';
  END IF;
  IF p_object_path IS NULL OR length(p_object_path) = 0 OR length(p_object_path) > 200 THEN
    RAISE EXCEPTION 'invalid_object_path' USING ERRCODE = '22023';
  END IF;

  -- One global lock: the cap is project-wide, not per user.
  PERFORM pg_advisory_xact_lock(hashtext('azure_tts_budget'));

  SELECT cap_characters INTO v_cap FROM public.azure_tts_budget WHERE id;

  SELECT coalesce(sum(characters), 0) INTO v_used FROM public.azure_tts_usage
    WHERE status = 'reserved' AND created_at >= public._azure_speech_month_start();

  IF v_cap IS NOT NULL AND v_used + p_characters > v_cap THEN
    RETURN jsonb_build_object(
      'ok', true, 'granted', false, 'reason', 'budget_exhausted',
      'used_characters', v_used, 'cap_characters', v_cap
    );
  END IF;

  -- A user id with no profile row (a guest, or deleted mid-request) is
  -- recorded as NULL: the characters still count.
  SELECT id INTO v_user FROM public.profiles WHERE id = p_user_id;

  INSERT INTO public.azure_tts_usage (user_id, object_path, characters)
  VALUES (v_user, p_object_path, p_characters)
  RETURNING id INTO v_id;

  RETURN jsonb_build_object(
    'ok', true, 'granted', true, 'reservation_id', v_id,
    'used_characters', v_used + p_characters, 'cap_characters', v_cap
  );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.reserve_azure_tts_characters(uuid, text, integer) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.reserve_azure_tts_characters(uuid, text, integer) FROM anon, authenticated;
GRANT EXECUTE ON FUNCTION public.reserve_azure_tts_characters(uuid, text, integer) TO service_role;

-- ── release_azure_tts_characters ─────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.release_azure_tts_characters(p_reservation_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_updated integer;
BEGIN
  UPDATE public.azure_tts_usage
    SET status = 'released', released_at = now()
    WHERE id = p_reservation_id AND status = 'reserved';
  GET DIAGNOSTICS v_updated = ROW_COUNT;

  RETURN jsonb_build_object('ok', true, 'released', v_updated > 0);
END;
$$;

REVOKE EXECUTE ON FUNCTION public.release_azure_tts_characters(uuid) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.release_azure_tts_characters(uuid) FROM anon, authenticated;
GRANT EXECUTE ON FUNCTION public.release_azure_tts_characters(uuid) TO service_role;
