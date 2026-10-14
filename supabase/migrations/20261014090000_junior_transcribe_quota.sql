/*
  Quota row for the Junior track's Whisper path (junior-track §2, Batch 4).

  POST /api/transcribe with purpose=junior charges `junior_transcribe`. A
  Junior mission is about 6-7 turns and a turn can be tried twice, so a few
  missions a day fit inside 150 (the same size as exam_turn_transcribe; a
  starting point, revisited after the playtest). It is only called when the
  warm-up picked Whisper, Web Speech is missing (iPad Safari), or Web Speech
  heard nothing for a turn — never with the expected sentence as a prompt.

  ai_usage_grants.feature is an FK to ai_quota_limits, so this row must exist
  before the route that names it deploys. A replay of the same idempotency
  key (the audio hash) costs nothing, as for every feature.

  Rollback: DELETE the row only after the backend that names it is rolled
  back, or every purpose=junior call 503s on the FK.
*/

INSERT INTO public.ai_quota_limits (feature, daily_limit) VALUES
  ('junior_transcribe', 150)
ON CONFLICT (feature) DO NOTHING;
