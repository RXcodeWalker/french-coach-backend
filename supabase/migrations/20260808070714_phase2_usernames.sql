/*
  # Phase 2 — Usernames (social layer plan §3.1, §4.1, §4.2, §4.3, §4.4)

  profiles.username already exists (20260503093957_french_coach_schema.sql)
  but has never been written to, has no uniqueness constraint, and no
  rename throttle. This migration adds those, plus a reserved_usernames
  table so admins can extend the reserved list via the existing is_admin()
  path without redeploying a function body.

  Validation (charset, length, starts-with-letter) is enforced client-side by
  a pure isValidUsername (src/services/social/usernameService.ts) so it can
  be unit-tested; the RPCs below re-validate server-side since the DB is the
  actual arbiter of uniqueness and the client check is only advisory (plan
  §3.1: "client availability check is advisory, the insert is the arbiter").

  Both RPCs are SECURITY INVOKER (the default) — they only ever touch the
  calling user's own profiles row, which the existing "Users can update own
  profile" policy already permits, so no privilege escalation is needed
  (unlike is_admin(), which must read JWT claims regardless of RLS).
*/

-- ── profiles columns ─────────────────────────────────────────────────────────

ALTER TABLE profiles
  ADD COLUMN IF NOT EXISTS username_changed_at timestamptz;

-- Case-preserving storage, case-insensitive uniqueness (plan §3.1).
CREATE UNIQUE INDEX IF NOT EXISTS profiles_username_lower_idx
  ON profiles (lower(username))
  WHERE username IS NOT NULL;

-- Prefix search support (plan §3.1: "Prefix only, index-backed via
-- text_pattern_ops, min 2 chars. No fuzzy/trigram."). Used by the
-- discoverable_profiles view in a later phase; created now alongside the
-- column it indexes.
CREATE INDEX IF NOT EXISTS profiles_username_prefix_idx
  ON profiles (lower(username) text_pattern_ops)
  WHERE username IS NOT NULL;

-- ── reserved_usernames ───────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS reserved_usernames (
  username text PRIMARY KEY
);

ALTER TABLE reserved_usernames ENABLE ROW LEVEL SECURITY;

CREATE POLICY "reserved usernames public read"
  ON reserved_usernames FOR SELECT
  USING (true);

CREATE POLICY "reserved usernames admin write"
  ON reserved_usernames FOR ALL
  USING (is_admin())
  WITH CHECK (is_admin());

GRANT SELECT ON public.reserved_usernames TO anon, authenticated, service_role;
GRANT INSERT, UPDATE, DELETE ON public.reserved_usernames TO authenticated, service_role;

-- Seed a small starter list (all lowercase — checked via lower() at claim time).
INSERT INTO reserved_usernames (username) VALUES
  ('admin'), ('administrator'), ('root'), ('support'), ('help'),
  ('moderator'), ('mod'), ('staff'), ('official'), ('system'),
  ('frenchcoach'), ('null'), ('undefined'), ('anonymous'), ('guest')
ON CONFLICT (username) DO NOTHING;

-- ── claim_username ───────────────────────────────────────────────────────────
-- First claim only (profiles.username IS NULL); use rename_username to change
-- an existing one. Re-validates charset/length/reserved server-side — the
-- client check is advisory only (plan §3.1).

CREATE OR REPLACE FUNCTION claim_username(new_username text)
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
AS $$
BEGIN
  IF new_username !~ '^[A-Za-z][A-Za-z0-9_]{2,19}$' THEN
    RAISE EXCEPTION 'invalid_username' USING ERRCODE = '22023';
  END IF;

  IF EXISTS (SELECT 1 FROM reserved_usernames WHERE username = lower(new_username)) THEN
    RAISE EXCEPTION 'username_reserved' USING ERRCODE = '22023';
  END IF;

  UPDATE profiles
  SET username = new_username,
      username_changed_at = now()
  WHERE id = auth.uid()
    AND username IS NULL;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'username_already_set' USING ERRCODE = '22023';
  END IF;
END;
$$;

-- ── rename_username ──────────────────────────────────────────────────────────
-- Throttled to once per 30 days via username_changed_at (plan §3.1 — "needs
-- no rate-limit table"). The WHERE clause's age check is the throttle; a
-- caller inside the window matches 0 rows and gets the same error as an
-- invalid/taken name would via the unique index, so it fails closed.

CREATE OR REPLACE FUNCTION rename_username(new_username text)
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
AS $$
BEGIN
  IF new_username !~ '^[A-Za-z][A-Za-z0-9_]{2,19}$' THEN
    RAISE EXCEPTION 'invalid_username' USING ERRCODE = '22023';
  END IF;

  IF EXISTS (SELECT 1 FROM reserved_usernames WHERE username = lower(new_username)) THEN
    RAISE EXCEPTION 'username_reserved' USING ERRCODE = '22023';
  END IF;

  UPDATE profiles
  SET username = new_username,
      username_changed_at = now()
  WHERE id = auth.uid()
    AND (username_changed_at IS NULL OR username_changed_at <= now() - interval '30 days');

  IF NOT FOUND THEN
    RAISE EXCEPTION 'rename_throttled' USING ERRCODE = '22023';
  END IF;
END;
$$;

GRANT EXECUTE ON FUNCTION claim_username(text) TO authenticated;
GRANT EXECUTE ON FUNCTION rename_username(text) TO authenticated;
