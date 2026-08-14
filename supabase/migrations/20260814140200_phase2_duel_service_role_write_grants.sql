/*
  # Fix: service_role write grants on duel_challenges / duel_attempts

  20260814140000_phase2_duel_tables.sql granted service_role SELECT only on
  both tables, matching the established convention (friendships,
  daily_challenge_* all do the same — mutation is RPC-only, even for
  service_role). That convention holds for every OTHER phase's tests because
  none of them need the admin/service-role test client to directly mutate
  rows.

  Friend Duels' own test plan (phase2_friend_duels.test.mjs sections 10 and
  14) is the first to need it: simulating an expired duel requires
  backdating duel_challenges.expires_at directly via the admin client (the
  RPCs have no "set your own expiry" path, by design), and the forced
  structurally-unreachable resolve_expired_duel branch requires inserting
  two duel_attempts rows directly while status stays 'accepted'. Both are
  impossible under a SELECT-only grant — this project has
  auto_expose_new_tables disabled (see 20260811090000's header), so
  service_role gets no default write access the way it might on a
  differently-configured project; every privilege must be explicit here.

  This mirrors scoring_envelopes' own precedent
  (20260717120000_grant_scoring_service_tables.sql): a service-facing table
  gets explicit service_role write grants because a server-side actor
  legitimately needs to write it directly, not through the client-facing RPC
  surface. `authenticated` gets no corresponding grant here — client mutation
  of duel_challenges/duel_attempts remains exclusively RPC-gated, matching
  every other table in this schema. RLS is also unaffected: service_role
  bypasses RLS as the table owner's role for RPC purposes already, and these
  GRANTs only add privilege for the admin client used in tests / any future
  server-side tooling, not for `authenticated`.
*/

GRANT UPDATE ON public.duel_challenges TO service_role;
GRANT INSERT, UPDATE ON public.duel_attempts TO service_role;
