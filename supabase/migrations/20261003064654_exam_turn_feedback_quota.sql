/*
  # exam_turn_feedback quota row (Phase 3 Batch 0)

  The Coached-exam corrections rail's per-turn examiner-feedback call is now
  metered under its own feature, separate from Learn's `feedback` row (both
  reach the same /api/feedback handler, profile 'rail' vs 'learn'). Without
  this row, consume_ai_quota raises unknown_feature (ai_usage_grants.feature
  is an FK to ai_quota_limits) and every rail call 503s.

  daily_limit = 60: owner decision, two Coached exams per user per UTC day.
  A Coached exam has 25-30 candidate turns (7-8 role-play + 2 x 9-11 topic,
  including the two further questions Coached always asks and 0-2
  extensions), so 60 = 2 x the 30-turn maximum. A grounding retry costs one
  more unit; a replay of the same key costs nothing.
*/

INSERT INTO public.ai_quota_limits (feature, daily_limit) VALUES
  ('exam_turn_feedback', 60)
ON CONFLICT (feature) DO NOTHING;
