/*
  # Phase 4 — Shadowing Mode: XP source (implementation plan
  i-am-implementing-phase-sunny-lagoon.md §3)

  Adds 'shadowing' to xp_events_source_check. Unlike daily_challenge /
  friend_challenge, 'shadowing' is deliberately client-submittable via
  submit_xp_event: the rolling 24h cap in submit_xp_event
  (20260815090000_league_xp_event_hardening.sql:107-113) has no `source`
  filter --

    SELECT COALESCE(SUM(amount) FILTER (WHERE amount > 0), 0) INTO v_recent_positive
    FROM public.xp_events
    WHERE user_id = me AND created_at >= now() - interval '24 hours';

  -- so it bounds a user's total XP ingestion rate across every client
  source, not per-source. Adding 'shadowing' therefore raises the maximum
  forgeable XP by exactly zero: an attacker already reaches the same 3000/24h
  ceiling today through 'accent_analyzer' with one PostgREST call. Making
  Shadowing XP server-authoritative (award_xp from FastAPI) would not lower
  that ceiling by a single point, would require an auth header on every
  attempt (not just coached ones), and would cost gems/local level
  progress/the XP toast, since server-awarded sources deliberately skip
  logXpEvent (see the daily_challenge/friend_challenge comments in
  src/types/social.ts). This source array must stay in sync with the
  `XpSource` union in src/types/social.ts.
*/

ALTER TABLE public.xp_events DROP CONSTRAINT xp_events_source_check;
ALTER TABLE public.xp_events ADD CONSTRAINT xp_events_source_check CHECK (source = ANY (ARRAY[
  'practice', 'exam', 'roleplay', 'word_drop', 'daily_news',
  'story', 'listening', 'sentence_rebuilder', 'accent_analyzer',
  'emoji_master', 'micro_drill', 'mystery_box', 'challenge',
  'minigame', 'friend_challenge', 'daily_challenge', 'shadowing'
]));
