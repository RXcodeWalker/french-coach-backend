/*
  # Phase 5 — Privacy, blocking, limits (social layer plan §3.5, §3.6, §3.7,
  # §4.1, §4.2, §4.3, §4.4)

  avatar_emoji and leaderboard_visibility were pulled forward into the Phase 3
  migration (see that file's header) because weekly_leaderboard's own
  definition depends on them. This migration adds the remaining two profiles
  columns from plan §4.1 (discoverable, friend_requests_from) plus the blocks
  table and its two RPCs.

  block_user must be atomic with deleting any existing friendship/pending
  request between the pair (plan §3.6: "deleted in the same transaction as
  the block insert") — hence SECURITY DEFINER, not a plain insert the client
  could do directly. It takes a row lock on the friendships pair before
  deleting it (plan §3.4 rule 3), which is what makes accept-vs-block
  serialise correctly regardless of commit order: block-first makes a
  concurrent accept match 0 rows (respond_friend_request's guard clause);
  accept-first is immediately undone by this delete. Both orders terminate
  at "blocked, not friends".

  Blocking is directional and never readable by the blocked party (plan
  §4.4) — RLS only grants SELECT to blocker_id = auth.uid(), so a user can't
  discover who has blocked them by querying the table.

  The global leaderboard does NOT filter blocked users (plan §3.6: per-viewer
  filtering would make ranks differ between viewers and break keyset
  pagination) — weekly_leaderboard from the Phase 3 migration is intentionally
  left unchanged here.
*/

-- ── profiles columns ─────────────────────────────────────────────────────────

ALTER TABLE profiles
  ADD COLUMN IF NOT EXISTS discoverable boolean NOT NULL DEFAULT true;

ALTER TABLE profiles
  ADD COLUMN IF NOT EXISTS friend_requests_from text NOT NULL DEFAULT 'anyone'
    CHECK (friend_requests_from = ANY (ARRAY['anyone', 'nobody']));

-- ── blocks ────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS blocks (
  blocker_id uuid REFERENCES profiles(id) ON DELETE CASCADE NOT NULL,
  blocked_id uuid REFERENCES profiles(id) ON DELETE CASCADE NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (blocker_id, blocked_id),
  CONSTRAINT blocks_not_self CHECK (blocker_id <> blocked_id)
);

CREATE INDEX IF NOT EXISTS blocks_blocked_id_idx ON blocks (blocked_id);

ALTER TABLE blocks ENABLE ROW LEVEL SECURITY;

-- Never readable by the blocked party (plan §4.4) — only the blocker sees
-- their own blocks list.
CREATE POLICY "Users can view own blocks"
  ON blocks FOR SELECT
  TO authenticated
  USING (auth.uid() = blocker_id);

CREATE POLICY "Users can insert own blocks"
  ON blocks FOR INSERT
  TO authenticated
  WITH CHECK (auth.uid() = blocker_id);

CREATE POLICY "Users can delete own blocks"
  ON blocks FOR DELETE
  TO authenticated
  USING (auth.uid() = blocker_id);

GRANT SELECT, INSERT, DELETE ON public.blocks TO authenticated, service_role;

-- ── discoverable_profiles ────────────────────────────────────────────────
-- Prefix-searchable (plan §3.1: "Prefix only, index-backed via
-- text_pattern_ops, min 2 chars"), filtered by discoverable and by blocks in
-- BOTH directions via auth.uid() inside the view (plan §3.6: symmetric
-- hiding — neither party sees the other, so a block isn't detectable by
-- absence). security_invoker so auth.uid() reflects the querying user, not
-- the view owner, and so the blocks subqueries are evaluated per-caller.

CREATE VIEW discoverable_profiles
WITH (security_invoker = true) AS
SELECT p.id, p.username, p.avatar_emoji, p.current_level
FROM profiles p
WHERE p.username IS NOT NULL
  AND p.discoverable = true
  AND NOT EXISTS (
    SELECT 1 FROM blocks b
    WHERE (b.blocker_id = auth.uid() AND b.blocked_id = p.id)
       OR (b.blocker_id = p.id AND b.blocked_id = auth.uid())
  );

GRANT SELECT ON public.discoverable_profiles TO authenticated;

-- ── block_user ────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION block_user(target_user_id uuid)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  me uuid := auth.uid();
  lo uuid;
  hi uuid;
BEGIN
  IF me IS NULL THEN
    RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000';
  END IF;

  IF target_user_id = me THEN
    RAISE EXCEPTION 'cannot_block_self' USING ERRCODE = '22023';
  END IF;

  IF target_user_id < me THEN lo := target_user_id; hi := me; ELSE lo := me; hi := target_user_id; END IF;

  -- Row lock on the pair before deleting (plan §3.4 rule 3) — serialises
  -- against a concurrent respond_friend_request('accept') on the same pair.
  PERFORM 1 FROM friendships WHERE user_low = lo AND user_high = hi FOR UPDATE;

  DELETE FROM friendships WHERE user_low = lo AND user_high = hi;

  INSERT INTO blocks (blocker_id, blocked_id)
  VALUES (me, target_user_id)
  ON CONFLICT (blocker_id, blocked_id) DO NOTHING;
END;
$$;

-- ── unblock_user ──────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION unblock_user(target_user_id uuid)
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
AS $$
BEGIN
  DELETE FROM blocks WHERE blocker_id = auth.uid() AND blocked_id = target_user_id;
END;
$$;

GRANT EXECUTE ON FUNCTION block_user(uuid) TO authenticated;
GRANT EXECUTE ON FUNCTION unblock_user(uuid) TO authenticated;
