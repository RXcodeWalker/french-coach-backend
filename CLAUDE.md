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
  the Learn ladder call (`LADDER_EXTRA_ANSWER_TOKENS`) already sits at ~8,150 on six-ladder questions.
