/*
  # F5 (security review 2026-11): daily rows for the AI routes that had no sign-in

  /api/drill, /api/grammar-lesson, /api/vocab-prep and /api/generate-scenario called Gemini
  or Groq with no sign-in and no limit. They now take a signed-in user (or a guest while guest
  access is on) and charge these rows. A cache hit is free and a failed call is given back.

    drill              20 a day  (a pronunciation drill for one word)
    grammar_lesson     20 a day
    vocab_prep         20 a day
    scenario_generate  10 a day  (Scenario Architect; each scenario then costs roleplay_turn
                                  per turn, which already has its own row)

  ai_usage_grants.feature is an FK to ai_quota_limits, so these rows must exist before the
  backend that names them deploys, or every call to those routes 503s.

  Depends on nothing newer than 20260914090000 (ai_quota_limits). Rollback: delete the rows
  only after the backend no longer names them.
*/

INSERT INTO public.ai_quota_limits (feature, daily_limit) VALUES
  ('drill', 20),
  ('grammar_lesson', 20),
  ('vocab_prep', 20),
  ('scenario_generate', 10)
ON CONFLICT (feature) DO NOTHING;
