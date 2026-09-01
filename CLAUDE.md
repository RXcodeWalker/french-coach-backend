# CLAUDE.md — backend/

**This is a separate git repository** (own remote, own `.gitignore`) from the main app repo
that references it. The main repo's git history is no safety net here.

- Before editing: run `git -C backend status` from the main repo (or `git status` from here)
  and expect clean. After committing here, run `git push` — the main repo's commits don't
  cover this directory.
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
