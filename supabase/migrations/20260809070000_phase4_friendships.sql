/*
  # Phase 4 — Friends (social layer plan §3.3, §3.4, §4.2, §4.3, §4.4)

  One row per pair, canonical (user_low, user_high) with user_low < user_high
  — this is what eliminates duplicate-request logic and makes the concurrency
  guarantees in plan §3.4 hold:

  1. Every transition is a conditional UPDATE guarded on the expected current
     status. A 0-row UPDATE is a benign no-op, never an error — this makes
     accept-vs-cancel and accept-vs-decline safe in either commit order.
  2. Mutual simultaneous requests auto-accept: both users compute the same
     (low, high) pair, one INSERT wins the PK, the loser's INSERT hits the
     unique-violation branch below and promotes the existing pending row to
     accepted instead.
  3. block_user (Phase 5) takes a row lock on the pair before deleting it —
     out of scope here, but the canonical ordering below is what makes that
     safe against a concurrent accept.
  4. Canonical ordering prevents deadlock: every RPC touches (user_low,
     user_high) in the same sorted order, so two concurrent RPCs involving
     the same two users never acquire locks in opposite orders.

  Declining a request retains the row (status='declined') rather than
  deleting it — this is the anti-spam mechanism (plan §3.3): a re-request
  within 7 days of a decline is refused by send_friend_request below, with
  no separate table.

  friendships is written ONLY via these RPCs (plan §4.4) — direct table
  writes would bypass the transition guards, the 7-day decline cooldown, and
  the 20/day rate limit (plan §3.7). RLS therefore has no INSERT/UPDATE/DELETE
  policy for authenticated users, only SELECT for either party; all three
  RPCs are SECURITY DEFINER specifically so they can act despite that.
*/

-- ── friendships ───────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS friendships (
  user_low     uuid REFERENCES profiles(id) ON DELETE CASCADE NOT NULL,
  user_high    uuid REFERENCES profiles(id) ON DELETE CASCADE NOT NULL,
  status       text NOT NULL CHECK (status = ANY (ARRAY['pending', 'accepted', 'declined'])),
  requested_by uuid REFERENCES profiles(id) ON DELETE CASCADE NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_low, user_high),
  CONSTRAINT friendships_ordered CHECK (user_low < user_high),
  CONSTRAINT friendships_requester_is_party CHECK (requested_by = user_low OR requested_by = user_high)
);

CREATE INDEX IF NOT EXISTS friendships_user_low_status_idx ON friendships (user_low, status);
CREATE INDEX IF NOT EXISTS friendships_user_high_status_idx ON friendships (user_high, status);

ALTER TABLE friendships ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own friendships"
  ON friendships FOR SELECT
  TO authenticated
  USING (auth.uid() = user_low OR auth.uid() = user_high);

-- No INSERT/UPDATE/DELETE policy for authenticated — mutation is RPC-only
-- (see module header). SECURITY DEFINER functions below bypass RLS entirely
-- for their own writes, which is why no policy is needed for them.

GRANT SELECT ON public.friendships TO authenticated, service_role;

-- ── discoverable_profiles ────────────────────────────────────────────────
-- Prefix-searchable (plan §3.1: "Prefix only ... min 2 chars"), filtered by
-- discoverable and by blocks in both directions via auth.uid(). The blocks
-- table doesn't exist until Phase 5, so this view is created there instead
-- of here with a stub — see that migration for the final definition. Not
-- created in this migration to avoid a view that has to be dropped and
-- recreated one phase later.

-- ── send_friend_request ──────────────────────────────────────────────────
-- Rate-limited to 20/day (plan §3.7) and refuses a re-request within 7 days
-- of a retained decline (plan §3.3's anti-spam mechanism). SECURITY DEFINER
-- so it can write friendships despite the table having no direct-write
-- policy for authenticated users.

CREATE OR REPLACE FUNCTION send_friend_request(target_user_id uuid)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  me uuid := auth.uid();
  lo uuid;
  hi uuid;
  existing friendships%ROWTYPE;
BEGIN
  IF me IS NULL THEN
    RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000';
  END IF;

  IF target_user_id = me THEN
    RAISE EXCEPTION 'cannot_friend_self' USING ERRCODE = '22023';
  END IF;

  IF (SELECT count(*) FROM friendships WHERE requested_by = me AND created_at > now() - interval '1 day') >= 20 THEN
    RAISE EXCEPTION 'friend_request_rate_limited' USING ERRCODE = '22023';
  END IF;

  IF target_user_id < me THEN
    lo := target_user_id; hi := me;
  ELSE
    lo := me; hi := target_user_id;
  END IF;

  SELECT * INTO existing FROM friendships WHERE user_low = lo AND user_high = hi FOR UPDATE;

  IF existing.user_low IS NOT NULL THEN
    IF existing.status = 'accepted' THEN
      RAISE EXCEPTION 'already_friends' USING ERRCODE = '22023';
    ELSIF existing.status = 'pending' THEN
      IF existing.requested_by = me THEN
        RAISE EXCEPTION 'request_already_pending' USING ERRCODE = '22023';
      ELSE
        -- Mutual simultaneous request (plan §3.4 rule 2): the other party
        -- already has a pending row aimed at me — promote to accepted.
        UPDATE friendships SET status = 'accepted', updated_at = now()
        WHERE user_low = lo AND user_high = hi;
        RETURN;
      END IF;
    ELSIF existing.status = 'declined' THEN
      IF existing.updated_at > now() - interval '7 days' THEN
        RAISE EXCEPTION 'decline_cooldown' USING ERRCODE = '22023';
      END IF;
      UPDATE friendships SET status = 'pending', requested_by = me, updated_at = now()
      WHERE user_low = lo AND user_high = hi;
      RETURN;
    END IF;
  END IF;

  INSERT INTO friendships (user_low, user_high, status, requested_by)
  VALUES (lo, hi, 'pending', me);
END;
$$;

-- ── respond_friend_request ───────────────────────────────────────────────
-- Single entry point for accept/decline/cancel (plan §3.3), each a
-- conditional UPDATE guarded on expected current status (plan §3.4 rule 1):
-- a 0-row match is a benign no-op, not an error, so accept-vs-cancel and
-- accept-vs-decline are safe in either commit order.

CREATE OR REPLACE FUNCTION respond_friend_request(target_user_id uuid, action text)
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

  IF action NOT IN ('accept', 'decline', 'cancel') THEN
    RAISE EXCEPTION 'invalid_action' USING ERRCODE = '22023';
  END IF;

  IF target_user_id < me THEN lo := target_user_id; hi := me; ELSE lo := me; hi := target_user_id; END IF;

  IF action = 'accept' THEN
    -- Only the non-requester may accept.
    UPDATE friendships SET status = 'accepted', updated_at = now()
    WHERE user_low = lo AND user_high = hi AND status = 'pending' AND requested_by <> me;
  ELSIF action = 'decline' THEN
    -- Only the non-requester may decline; row retained (anti-spam, plan §3.3).
    UPDATE friendships SET status = 'declined', updated_at = now()
    WHERE user_low = lo AND user_high = hi AND status = 'pending' AND requested_by <> me;
  ELSIF action = 'cancel' THEN
    -- Only the requester may cancel; row deleted, not retained.
    DELETE FROM friendships
    WHERE user_low = lo AND user_high = hi AND status = 'pending' AND requested_by = me;
  END IF;
END;
$$;

-- ── remove_friend ─────────────────────────────────────────────────────────
-- Either party may remove an accepted friendship.

CREATE OR REPLACE FUNCTION remove_friend(target_user_id uuid)
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

  IF target_user_id < me THEN lo := target_user_id; hi := me; ELSE lo := me; hi := target_user_id; END IF;

  DELETE FROM friendships
  WHERE user_low = lo AND user_high = hi AND status = 'accepted';
END;
$$;

GRANT EXECUTE ON FUNCTION send_friend_request(uuid) TO authenticated;
GRANT EXECUTE ON FUNCTION respond_friend_request(uuid, text) TO authenticated;
GRANT EXECUTE ON FUNCTION remove_friend(uuid) TO authenticated;
