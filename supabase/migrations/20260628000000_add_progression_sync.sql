-- Add gamification columns missing from profiles for cloud sync
ALTER TABLE profiles
  ADD COLUMN IF NOT EXISTS gems            INTEGER   NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS achievements    TEXT[]    NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS inventory       JSONB     NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS active_boosters JSONB     NOT NULL DEFAULT '[]';

-- Auto-update updated_at on every row change
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
$$;

CREATE TRIGGER profiles_updated_at
  BEFORE UPDATE ON profiles
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();
