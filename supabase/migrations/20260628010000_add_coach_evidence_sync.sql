-- Coach evidence cloud sync
-- Stores the append-only EvidenceEvent log for cross-device coach continuity.
-- Beliefs and problems are derived locally from this log; only events are synced.

CREATE TABLE IF NOT EXISTS coach_evidence (
  id                text PRIMARY KEY,                  -- client makeId('ev'|'av') strings
  user_id           uuid REFERENCES profiles(id) ON DELETE CASCADE NOT NULL,
  occurred_at       timestamptz NOT NULL,
  source_session_id text,
  evidence_type     text NOT NULL,                     -- 'language' | 'behavior' | ...
  target_node_ids   text[] NOT NULL DEFAULT '{}',
  observation       jsonb  NOT NULL DEFAULT '{}',
  result            jsonb  NOT NULL DEFAULT '{}',
  reliability       jsonb  NOT NULL DEFAULT '{}',
  context           jsonb  NOT NULL DEFAULT '{}',
  schema_version    integer NOT NULL DEFAULT 1,         -- COACH_SYNC_SCHEMA_VERSION at write time
  created_at        timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE coach_evidence ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own coach evidence"
  ON coach_evidence FOR SELECT TO authenticated USING (auth.uid() = user_id);

CREATE POLICY "Users can insert own coach evidence"
  ON coach_evidence FOR INSERT TO authenticated WITH CHECK (auth.uid() = user_id);

-- UPDATE included for idempotent upserts (onConflict: 'id') even though rows are immutable
CREATE POLICY "Users can update own coach evidence"
  ON coach_evidence FOR UPDATE TO authenticated
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);

CREATE INDEX IF NOT EXISTS coach_evidence_user_occurred_idx
  ON coach_evidence(user_id, occurred_at DESC);
