# CLAUDE.md — backend/

**This is a separate git repository** (own remote, own `.gitignore`) from the main app repo
that references it. The main repo's git history is no safety net here.

- Before editing: run `git -C backend status` from the main repo (or `git status` from here)
  and expect clean. **Every commit made here must be followed by `git push` in the same
  turn — no exceptions.** The main repo's commits (and its own push) don't cover this
  directory, so a commit left unpushed here is silently invisible everywhere else.
- Migrations live in `backend/supabase/migrations/`, read in date order as the only in-repo
  record of the deployed schema. `backend/supabase/tests/*.test.mjs` is the executable spec of
  the privileged RPC contracts — run against a **local** `npx supabase start` stack only, never
  the hosted project.
- `evaluator_service.py` is a legacy, unreached Cambridge scorer (zero callers from `src/` in
  the main repo) with its own unsourced rubric — see the main repo's
  `docs/decisions/0003-node-engine-is-the-authoritative-scorer.md`. Do not "fix" its rubric to
  match the main repo's `rubric.ts`; that's a separate decision, not a documentation or routine
  change.
- **Admin = the `admin` role AND an allow-listed email** (`lib/auth.py`'s `is_admin_payload`; env `ADMIN_EMAILS`,
  default the owner `omjhamvar29@gmail.com`; fails closed). `require_admin` and the exam-pronunciation `admin` mode both use
  it, and the SQL `is_admin()` (migration `20261102090000`) repeats the email — change all of them together. Never
  check `app_metadata.role` alone.
- Its own CI runs pytest; there is no frontend CI here.
- Every Groq/Gemini model ID, Whisper included, comes from `lib/model_config.py` (env var with a fallback
  default there, nowhere else): `GROQ_WHISPER_MODEL` (default `whisper-large-v3`) is what
  `main._groq_whisper` sends, so never put a literal model string in a call site. `/api/transcribe`
  charges a quota feature chosen by its `purpose` form field (`learn` → `learn_transcribe`,
  `exam_turn` → `exam_turn_transcribe`, `junior` → `junior_transcribe`, default `transcribe`); any new feature must be seeded in
  `ai_quota_limits` by a migration first (FK), and `lib/guest.py`'s `_AUDIO_FEATURES` must list it so
  guests keep the audio backstop.
- Groq's on_demand tier refuses (413 "Request too large") any call whose prompt + `max_tokens` is over its
  8,000 tokens-per-minute limit, before the model runs. Check that sum before raising a Groq token budget:
  the one-call Learn ladder prompt (`learn-prompt-v9`) reached ~8,150 on six-ladder questions, one reason the
  ladder became a separate small checklist call (`LADDER_CHECK_ANSWER_TOKENS`, ~3,500 per request).
- Sentry lives in `lib/observability.py` (no-op without `SENTRY_DSN`; initialised at the top of
  `main.py`). 5xx responses are reported by the FastAPI integration; a provider's final failure by
  `_log_provider_failure` (pass `report=False` for a retry or when raising a 5xx `HTTPException
  from exc`); a failure inside a stream by `capture_route_error`. `log.error` is a breadcrumb, not
  an issue. Expected 4xx never report. Request bodies and frame locals are never sent.
- `POST /api/native-voice` (`routers/native_voice.py`, Pronunciation Lab Stage 4b) is the only place Azure
  **text-to-speech** runs at request time: it writes an AI-corrected sentence once into the public `tts-audio`
  bucket at `lib/voice_key.py`'s path — a port of the main repo's `src/services/tts/voiceKey.ts`; change neither
  without the other (vectors in `data/tts/voice_key_vectors.json`, checked by both repos' tests). It charges
  `native_voice` (20/day) and meters characters in `azure_tts_usage` against `azure_tts_budget.cap_characters`
  (50k/month — F0's 500k is shared with the "Record voices" workflow); a file already in the bucket costs nothing.
- `POST /api/pronunciation` charges `pronunciation_lab` (30/day, migration `20261029090000`) instead of `pronunciation`
  when the client sends `source=lab` on a **scripted** take (Pronunciation Lab: sentence takes, focus-word drills, monthly
  check); freeform and every other source keep `pronunciation`. Cache-miss only, released on failure/no-verdict, and the
  feature names stay string literals at the call sites (`test_quota_features_seeded` collects them). Read-only
  `GET /api/pronunciation/allowance` (in `main.py`, beside `/api/learn/allowance`) → `{limit, used, remaining, resetsAt}`.
- `scripts/probe_lab_calibration.py` (+ pure helpers in `services/pronunciation/lab_calibration.py`) records the Pronunciation Lab's
  §7 calibration fixtures: the production **scripted** Azure request on real recordings, metered as ledger source `probe`, written to
  `tests/fixtures/pronunciation_lab_calibration/` (no audio). That folder is the evidence the main repo's `gate.test.ts` reads, so only
  real, honestly recorded sets belong in it — use `--out-dir` for practice or synthetic runs.
- **Premium billing is Stripe, in `routers/billing.py` (migration `20261104230000`).** Only the Stripe webhook (signature =
  `STRIPE_WEBHOOK_SECRET`, raw body) and the signed-in `/sync` / `/cancel-for-deletion` routes write `billing_*` rows, always from a
  subscription **re-read from Stripe** — never trust an event payload's status or a browser's user id; the account link is the
  `user_id` metadata written at Checkout, outranked by `billing_customers`. Premium only raises limits: `consume_ai_quota` uses
  `ai_quota_limits.premium_daily_limit` (NULL = same as free; never set it below `daily_limit`) when `has_premium()` is true, and
  `has_premium()`'s status list must match `PREMIUM_STATUSES`. A profile with a live subscription cannot be deleted (trigger) —
  the app cancels first. Billing is off (503 `billing_not_configured`) until `STRIPE_SECRET_KEY` + `STRIPE_PRICE_MONTHLY` are set.
  Never sell gems here (main repo `shop-economy.md` §10).
  **Razorpay is the second provider (`routers/billing_razorpay.py`, migration `20261105090000`, table
  `billing_razorpay_subscriptions`)** under the same rules: the account link is the row inserted when this service creates the
  subscription (then its `user_id` note), every write comes from a re-read, the webhook is `X-Razorpay-Signature` over the raw body.
  `has_premium()` and the deletion trigger read both tables — `billing_razorpay.PREMIUM_STATUSES`/`LIVE_STATUSES` change with
  them (`test_statuses_match_the_migration`). Razorpay has no cancel-at-period-end flag, so `cancel_at_period_end` there is
  this app's own record from `/cancel`, kept across re-reads. Off until `RAZORPAY_KEY_ID` + `RAZORPAY_KEY_SECRET` +
  `RAZORPAY_PLAN_MONTHLY` are set; its table is read only while it is configured.
- **Teacher dashboard** (migration `20261103091000`, main repo `docs/systems/teacher-dashboard.md`, ADR 0014): a teacher
  reads a student's data only through the class-scoped `SECURITY DEFINER` `teacher_*` functions, for active members,
  from their join date onward — never widen RLS on `sessions`/`scoring_envelopes`/`session_transcripts`/
  `exam_feedback_reports` for teachers. Learn feedback (JSON, stream, examiner routes) charges `assignment_learn_answer`
  instead of `learn_answer` when `X-Assignment-Id` names homework the service-role RPC `assignment_allowance` accepts
  (`lib/assignment_allowance.py`; key prefix `asg:<id>:` so the RPC can count an assignment's answers); anything else
  falls back to `learn_answer`. SQL behaviour spec: `supabase/tests/plain_pg/` (plain local Postgres).
