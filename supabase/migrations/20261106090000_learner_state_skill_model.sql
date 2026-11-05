-- learner_state gains the key `skill_model`: the main repo's per-skill learner model
-- (docs/systems/learner-model.md §4.3, ADR 0016; src/services/sync/learnerStateSync.ts).
--
-- The value is a per-device conflict-free map of decayed per-skill numbers:
--   { "version": "learner-model-v1", "devices": { <deviceId>: { updatedAt, skills } }, "legacy": … }
-- Each device writes only its own bucket; the client merges (latest bucket per device, earliest
-- legacy bucket) and sums the buckets on read. It holds counts and weights only — no text,
-- transcripts or event bodies — and stays under the existing 256 KB row cap (≤ 8 device buckets
-- × 30 skills × ~120 bytes).
--
-- Only the key CHECK changes. RLS, grants and the size/shape checks are untouched. The client
-- pushes this key only while its flag `learnerModelSync` is live; switch that flag on only after
-- this migration is applied in production (a push before then is refused by the old CHECK).

ALTER TABLE public.learner_state DROP CONSTRAINT IF EXISTS learner_state_key_check;
ALTER TABLE public.learner_state
  ADD CONSTRAINT learner_state_key_check
  CHECK (key IN ('topic_mastery', 'review_pool', 'learn_settings', 'skill_model'));
