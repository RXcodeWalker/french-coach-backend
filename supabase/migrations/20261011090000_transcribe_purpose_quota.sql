/*
  Per-purpose quota rows for POST /api/transcribe.

  The single `transcribe` row (30/day) cannot carry per-answer Learn
  transcription plus 20-25 exam turns per exam. /api/transcribe now takes a
  `purpose` form field and charges:

    learn      -> learn_transcribe       200/day (one call per Learn answer)
    exam_turn  -> exam_turn_transcribe   150/day (about 6 exams of 25 turns)
    (default)  -> transcribe              30/day (unchanged)

  ai_usage_grants.feature is an FK to ai_quota_limits, so these rows must
  exist before the route that names them deploys. A replay of the same
  idempotency key (the audio hash) costs nothing, as for every feature.

  Rollback: DELETE the two rows only after the backend that names them is
  rolled back, or every call with that purpose 503s on the FK.
*/

INSERT INTO public.ai_quota_limits (feature, daily_limit) VALUES
  ('learn_transcribe', 200),
  ('exam_turn_transcribe', 150)
ON CONFLICT (feature) DO NOTHING;
