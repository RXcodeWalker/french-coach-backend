/*
  Pronunciation Lab allowance (plan docs/plans/pronunciation-lab.md, Stage 7;
  owner decision D3, 2026-10-28): 30 Lab takes per account per day.

  - pronunciation_lab (30/day): charged by POST /api/pronunciation instead of
    `pronunciation` when the client names source=lab on a scripted take (the
    Lab's sentence takes, focus-word drills and the monthly check). Charged
    only on a cache miss (a replay of the same audio is free); released when
    the take got no assessment. Other callers keep `pronunciation`.
  - Read-only: GET /api/pronunciation/allowance.

  ai_usage_grants.feature is an FK to ai_quota_limits, so this row must exist
  before the backend that names it deploys, or every Lab take 503s.

  Rollback: DELETE the row only after the code that names it is rolled back.
*/

INSERT INTO public.ai_quota_limits (feature, daily_limit) VALUES
  ('pronunciation_lab', 30)
ON CONFLICT (feature) DO NOTHING;
