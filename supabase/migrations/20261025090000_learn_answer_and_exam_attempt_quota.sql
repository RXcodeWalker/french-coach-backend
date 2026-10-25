/*
  Launch limits (owner decision, 2026-10-24): 3 AI-marked Learn answers per
  account per day, and one sample exam per rolling 7 days.

  - learn_answer (3/day): charged by /api/feedback (/v3 and /stream) instead of
    `feedback` when the request comes from Learn (it names a `marking`
    scheme), and by the examiner route for the Learn profile. A replay of the
    same answer (same idempotency key) costs nothing; an answer every provider
    failed to mark is released. Other callers keep `feedback`.
  - exam_attempt: charged by the scoring service (server/index.ts, POST /score)
    with the exam's sessionId. consume_ai_quota is per UTC day, so the row's
    daily_limit is 1 and the rolling 7 days are checked by the scoring service
    itself before it charges (ai_usage_grants rows of the last 7 days). A
    failed scoring releases the grant.

  ai_usage_grants.feature is an FK to ai_quota_limits, so these rows must exist
  before the backend and scoring service that name them deploy.

  Rollback: DELETE the rows only after the code that names them is rolled back,
  or every Learn answer and exam submission 503s on the FK.
*/

INSERT INTO public.ai_quota_limits (feature, daily_limit) VALUES
  ('learn_answer', 3),
  ('exam_attempt', 1)
ON CONFLICT (feature) DO NOTHING;
