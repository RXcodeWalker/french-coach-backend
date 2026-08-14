/*
  # Daily Challenge Phase 1 — XP hardening (revised plan §Fix 1, §"Full
  corrected backend schema/RPC set" part 3)

  award_xp centralizes server-side XP writes into xp_events, setting
  occurred_at := now() itself (never a client-supplied value) so a caller
  cannot backdate an award into a different ISO week — the same
  clock-authority principle xp_events' own occurred_at bounds CHECK already
  applies to client-direct inserts (20260808064748), just enforced at the
  single server-side write path this phase introduces instead of trusted to
  the CHECK alone.

  Fix 1 (the security-review finding this migration exists to close): the
  first-pass design granted EXECUTE on award_xp to `authenticated`, which
  would let any logged-in user mint arbitrary XP by calling it directly with
  an invented source/amount/idempotency key. This version REVOKEs from
  PUBLIC and grants ONLY to service_role — no `GRANT ... TO authenticated`
  at all. submit_daily_challenge_attempt (next migration) still calls
  award_xp(...) internally as a plain SQL function call, which works
  regardless of `authenticated`'s grants: a call from inside another
  SECURITY DEFINER function executes with that function's effective (owner)
  role, not the original caller's role, and PostgREST's grant-checking only
  applies to a direct RPC invocation over the API — never to an internal
  function-to-function call. This preserves award_xp as a reusable internal
  abstraction (for Phase 2/3 to call the same way) without exposing a
  mintable RPC to the client.

  Idempotency: p_idempotency_key becomes xp_events.id directly (mirrors
  mint_gems_from_envelope's v_key / gem_events.id pattern) — a duplicate call
  with the same key is a no-op (ON CONFLICT DO NOTHING), never a double
  award, and the function tells the caller whether it actually inserted.

  Only inserts into xp_events (the weekly_leaderboard source of truth) — does
  NOT touch profiles.total_xp. total_xp is a separate client-synced counter
  (progressionSync.ts) that already diverges from xp_events for every
  existing XP source (practice, exam, etc.); unifying that is out of this
  phase's scope (Shop Phase 7's deferred "revoke client UPDATE on
  total_xp/achievements" clause is the eventual fix, not this one).
*/

ALTER TABLE public.xp_events DROP CONSTRAINT xp_events_source_check;
ALTER TABLE public.xp_events ADD CONSTRAINT xp_events_source_check CHECK (source = ANY (ARRAY[
  'practice', 'exam', 'roleplay', 'word_drop', 'daily_news',
  'story', 'listening', 'sentence_rebuilder', 'accent_analyzer',
  'emoji_master', 'micro_drill', 'mystery_box', 'challenge',
  'minigame', 'friend_challenge', 'daily_challenge'
]));

CREATE OR REPLACE FUNCTION public.award_xp(
  p_user_id uuid,
  p_source text,
  p_amount integer,
  p_idempotency_key text,
  p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_inserted boolean;
BEGIN
  INSERT INTO public.xp_events (id, user_id, amount, source, metadata, occurred_at)
  VALUES (p_idempotency_key, p_user_id, p_amount, p_source, p_metadata, now())
  ON CONFLICT (id) DO NOTHING;

  v_inserted := FOUND;

  RETURN jsonb_build_object('ok', true, 'awarded', v_inserted);
END;
$$;

REVOKE EXECUTE ON FUNCTION public.award_xp(uuid, text, integer, text, jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.award_xp(uuid, text, integer, text, jsonb) TO service_role;
