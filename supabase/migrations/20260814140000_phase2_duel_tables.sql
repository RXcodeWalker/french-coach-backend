/*
  # Friend Duels Phase 2 — tables (plan i-am-implementing-phase-shimmering-tide.md)

  duel_challenges: plain challenger_id/opponent_id, NO canonical ordering
  (unlike friendships) — a duel is directional and repeatable between the
  same pair, not a one-row-per-unordered-pair singleton.

  State invariants (DB-enforced, row-local only — see plan's "State
  invariants" section for the full RPC-enforced half, which cannot be
  expressed here since CHECK constraints can't reference duel_attempts):
    - pending -> expires_at/responded_at/completed_at/winner_user_id all NULL.
    - accepted -> expires_at and responded_at NOT NULL; completed_at/winner_user_id NULL.
    - declined/cancelled -> responded_at NOT NULL; completed_at/winner_user_id NULL.
    - completed/expired -> completed_at NOT NULL.
    - expired -> winner_user_id NULL and is_tie=false (a one-sided forfeit is
      represented as 'completed', not 'expired').
    - winner_user_id, when set, is always one of challenger_id/opponent_id.
    - is_tie=true -> winner_user_id NULL.

  No trigger is introduced for the RPC-enforced half — the RPC layer is the
  only write path (no client INSERT/UPDATE/DELETE grant exists on either
  table) and every mutation happens under a SELECT...FOR UPDATE lock on the
  parent duel row, which is sufficient to make those invariants true without
  a trigger duplicating that safety.
*/

CREATE TABLE public.duel_challenges (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  challenger_id    uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  opponent_id      uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  question_set_id  text NOT NULL REFERENCES public.igcse_question_sets(id),
  status           text NOT NULL DEFAULT 'pending'
                     CHECK (status = ANY (ARRAY['pending','accepted','declined','cancelled','completed','expired'])),
  created_at       timestamptz NOT NULL DEFAULT now(),
  responded_at     timestamptz,
  expires_at       timestamptz,
  completed_at     timestamptz,
  winner_user_id   uuid REFERENCES public.profiles(id),
  is_tie           boolean NOT NULL DEFAULT false,
  CONSTRAINT duel_challenges_not_self CHECK (challenger_id <> opponent_id),
  CONSTRAINT duel_challenges_pending_shape CHECK (
    status <> 'pending' OR (expires_at IS NULL AND responded_at IS NULL AND completed_at IS NULL AND winner_user_id IS NULL)
  ),
  CONSTRAINT duel_challenges_accepted_shape CHECK (
    status <> 'accepted' OR (expires_at IS NOT NULL AND responded_at IS NOT NULL AND completed_at IS NULL AND winner_user_id IS NULL)
  ),
  CONSTRAINT duel_challenges_declined_cancelled_shape CHECK (
    status NOT IN ('declined', 'cancelled') OR (responded_at IS NOT NULL AND completed_at IS NULL AND winner_user_id IS NULL)
  ),
  CONSTRAINT duel_challenges_terminal_has_completed_at CHECK (
    status NOT IN ('completed', 'expired') OR completed_at IS NOT NULL
  ),
  CONSTRAINT duel_challenges_expired_shape CHECK (
    status <> 'expired' OR (winner_user_id IS NULL AND is_tie = false)
  ),
  CONSTRAINT duel_challenges_winner_is_participant CHECK (
    winner_user_id IS NULL OR winner_user_id IN (challenger_id, opponent_id)
  ),
  CONSTRAINT duel_challenges_tie_has_no_winner CHECK (NOT is_tie OR winner_user_id IS NULL)
);
CREATE INDEX duel_challenges_challenger_idx ON public.duel_challenges (challenger_id, status);
CREATE INDEX duel_challenges_opponent_idx   ON public.duel_challenges (opponent_id, status);

ALTER TABLE public.duel_challenges ENABLE ROW LEVEL SECURITY;
CREATE POLICY "duel_challenges participant read" ON public.duel_challenges
  FOR SELECT USING (auth.uid() = challenger_id OR auth.uid() = opponent_id);
-- No INSERT/UPDATE/DELETE policy — RPC-only mutation, matching friendships.
GRANT SELECT ON public.duel_challenges TO authenticated, service_role;

CREATE TABLE public.duel_sessions (
  duel_id     uuid NOT NULL REFERENCES public.duel_challenges(id) ON DELETE CASCADE,
  user_id     uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  session_id  text NOT NULL UNIQUE,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (duel_id, user_id)
);
ALTER TABLE public.duel_sessions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "duel_sessions owner read" ON public.duel_sessions
  FOR SELECT USING (auth.uid() = user_id);
GRANT SELECT ON public.duel_sessions TO authenticated, service_role;

CREATE TABLE public.duel_attempts (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  duel_id      uuid NOT NULL REFERENCES public.duel_challenges(id) ON DELETE CASCADE,
  user_id      uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  attempt_id   text NOT NULL UNIQUE REFERENCES public.scoring_envelopes(attempt_id),
  score_total  numeric NOT NULL,
  xp_awarded   integer NOT NULL DEFAULT 0,
  outcome      text NOT NULL DEFAULT 'pending'
                 CHECK (outcome = ANY (ARRAY['pending','win','loss','tie','forfeit_win'])),
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (duel_id, user_id)
);
ALTER TABLE public.duel_attempts ENABLE ROW LEVEL SECURITY;
CREATE POLICY "duel_attempts participant read" ON public.duel_attempts
  FOR SELECT USING (EXISTS (
    SELECT 1 FROM public.duel_challenges d
    WHERE d.id = duel_attempts.duel_id AND (d.challenger_id = auth.uid() OR d.opponent_id = auth.uid())
  ));
GRANT SELECT ON public.duel_attempts TO authenticated, service_role;

-- Owner-privileged (NOT security_invoker) — matches weekly_leaderboard/
-- all_time_leaderboard, not discoverable_profiles: joins public_profile
-- (already cross-user readable) for both participants, so it must run as
-- owner to see both, then re-applies duel_challenges' own participant
-- filter explicitly since bypassing RLS also bypasses that base-table
-- policy. Do NOT set security_invoker=true here — profiles' own SELECT
-- RLS is self-scoped (auth.uid()=id), so an invoker-mode join straight to
-- profiles (not public_profile) would silently drop the OTHER
-- participant's row. Using public_profile sidesteps that entirely.
CREATE VIEW public.duel_challenges_view AS
SELECT
  d.id AS duel_id, d.challenger_id, cp.username AS challenger_username, cp.avatar_emoji AS challenger_avatar_emoji,
  d.opponent_id, op.username AS opponent_username, op.avatar_emoji AS opponent_avatar_emoji,
  d.question_set_id, d.status, d.created_at, d.responded_at, d.expires_at, d.completed_at,
  d.winner_user_id, d.is_tie
FROM public.duel_challenges d
JOIN public.public_profile cp ON cp.id = d.challenger_id
JOIN public.public_profile op ON op.id = d.opponent_id
WHERE d.challenger_id = auth.uid() OR d.opponent_id = auth.uid();

GRANT SELECT ON public.duel_challenges_view TO authenticated;
