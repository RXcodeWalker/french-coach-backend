/*
  D11 (0520 conduct plan): expire open duels at deploy time.

  NOT A MIGRATION — deliberately outside supabase/migrations/ so it can never
  be applied by a migration run. Run it BY HAND, once, in the Supabase SQL
  editor, at the moment the rewritten 0520 question sets go live (i.e. right
  after `python seed_igcse_questions.py`, timed with the frontend + scoring
  server deploy). It has NOT been run.

  Why: the 10 sets (original-practice-001..010) keep their ids but every
  content hash changes. A duel that started before the deploy and finishes
  after it would compare two attempts scored on different question content, and
  submit_duel_attempt's questionSetId check (which compares ids, not hashes)
  would not notice.

  What it does: moves every OPEN duel on those sets ('pending' or 'accepted')
  to 'expired', with completed_at = now(), winner_user_id NULL, is_tie false —
  the shape duel_challenges_expired_shape / _terminal_has_completed_at require.

  Deliberately NOT resolve_expired_duel(): that helper turns a duel with one
  submission into a 'forfeit_win' and awards XP. Here nobody forfeited — the
  content changed under them — so no winner and no XP. Any already-submitted
  duel_attempts row is left as-is (outcome stays 'pending', xp_awarded 0, since
  XP is only awarded when a duel resolves). Once status = 'expired',
  resolve_expired_duel (which acts only on 'accepted') and submit_duel_attempt
  (which rejects status <> 'accepted') both leave the row alone.

  Statuses 'declined', 'cancelled', 'completed', 'expired' are untouched.

  Usage: run the whole file. Step 1 previews; step 2 updates inside a
  transaction. Check the preview counts, then change ROLLBACK to COMMIT.
*/

-- 1. Preview: what would be expired.
SELECT status, count(*) AS duels
FROM public.duel_challenges
WHERE status IN ('pending', 'accepted')
  AND question_set_id LIKE 'original-practice-%'
GROUP BY status
ORDER BY status;

-- 2. Expire them.
BEGIN;

WITH expired AS (
  UPDATE public.duel_challenges
     SET status = 'expired',
         completed_at = now(),
         winner_user_id = NULL,
         is_tie = false
   WHERE status IN ('pending', 'accepted')
     AND question_set_id LIKE 'original-practice-%'
  RETURNING id, status
)
SELECT count(*) AS duels_expired FROM expired;

-- Sanity: no open duel should remain on these sets (expect 0 rows).
SELECT id, status FROM public.duel_challenges
WHERE status IN ('pending', 'accepted')
  AND question_set_id LIKE 'original-practice-%';

ROLLBACK;  -- change to COMMIT once the preview and counts look right
