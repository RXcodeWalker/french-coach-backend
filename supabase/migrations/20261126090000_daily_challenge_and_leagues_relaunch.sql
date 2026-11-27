/*
  # Daily Challenge and Leagues relaunch (main repo docs/systems/daily-challenge-and-leagues.md)

  Three parts, one migration because each depends on the others: the league ranks the
  XP ledger, and the Daily Challenge writes to that ledger.

  ## Part 1 — XP ledger anti-cheat (spec §4)

  submit_xp_event let a client claim ANY amount for any client source, bounded only by a
  rolling 3000-a-day cap. A league ranks weekly XP, so one forged client could take 3000 a
  day and win every league. Now:

  - xp_source_limits holds, per source, a per-event maximum and a rolling-24h maximum, set
    from what the app can honestly award (computeXPGain tops out at 39 per answer; the
    limits leave about 2x headroom for boosters). Sources whose games award no XP by owner
    decision (word_drop, sentence_rebuilder, emoji_master) and the mock Challenges screen
    (challenge) get 0: a forged event for them is worth nothing.
  - The overall rolling-24h cap drops from 3000 to 1500.
  - An over-limit event is CLAMPED, never refused: it is stored with the allowed amount
    (metadata.claimed_amount keeps the claim), or not stored at all when nothing is left,
    and the RPC still answers ok (capped: true). A refusal would leave the event in the
    client's queue, retried on every sync for 90 days.
  - Server-written sources (award_xp: daily_challenge, friend_challenge, mystery_box) are
    untouched and still refused from a client.

  ## Part 2 — Leagues (spec §3)

  The old design pre-built every cohort on Monday from every profile with a username, so
  cohorts were full of people who never practised that week, a learner who joined on
  Tuesday saw "Not ranked yet" for six days, and a missed cron left the whole board stale
  (the roster view anchored on the last completed run).

  - Lazy join: a learner joins this week's league the moment their first positive XP of
    the week lands (AFTER INSERT trigger on xp_events, plus get_my_league as a fallback).
    They go to an open cohort of their own tier (cap 30), else an adjacent-tier cohort still
    under 10 (small weeks merge instead of leaving people alone), else a new cohort.
  - Rollover is server-side and self-healing: _league_rollover finalizes EVERY past week
    that still has unfinalized memberships, oldest first. The Monday workflow calls it
    (assign_weekly_league_cohorts keeps its name), get_my_league calls it, and the join
    trigger tries it without waiting (pg_try_advisory_xact_lock, so an XP write never waits
    on or deadlocks with a rollover). A missed cron can no longer freeze a week.
  - Ranking counts only members with XP > 0 (legacy pre-built members with nothing are
    left out, never demoted). Zones come from ONE function, _league_zones, used by both
    finalization and get_my_league, so the screen can never draw a zone the server does
    not apply. Promotion needs a minimum week (50 XP; 150 in a cohort of 1-2 so a lone
    learner earns it), demotion needs at least 5 active members.
  - RLS-safe reads: league_cohort_roster (a definer-rights view that bypassed RLS and
    exposed user ids) is dropped. get_my_league is a SECURITY DEFINER function scoped by
    auth.uid(), returns no user ids (is_me instead), hides members whose visibility is no
    longer 'global', and is the only cross-user league read.

  ## Part 3 — Daily Challenge (spec §2)

  The old Daily Challenge was a full 15-minute Exam Sim, which charges the account's one
  exam per 7 days (server/examAllowance.ts): a "daily" challenge usable once a week, at
  full AI-marking cost. The relaunch is a short challenge the server owns end to end:

  - daily_challenge_bank: sentences to rebuild from word tiles (seeded by the generated
    *_daily_challenge_bank_seed.sql migration). Service-only: no client role can read an
    answer key.
  - daily_challenge_sets: the same 8 items for everyone on a UTC day, built lazily by the
    first request of the day (and by the existing 6-hourly seed workflow), easiest first,
    avoiding the last 4 days' items.
  - daily_challenge_start reveals the items (tiles shuffled per item and day, so the order
    of the tiles never gives the answer away) and starts the server clock. One run per
    account per day.
  - daily_challenge_answer marks ONE item against the stored keys, once (a repeat returns
    the stored mark), and returns the solution. The last answer (or the 20-minute
    deadline) finishes the run: time is measured on the server, XP is fixed by the server
    (10 + 5 per correct, max 50) through award_xp with the same key the old exam path used
    (one daily_challenge award per account per day, whichever path), and a run faster than
    3 s an item is flagged: no XP, not on the board.
  - daily_challenge_board: top 50 + the caller, by correct answers then time, respecting
    leaderboard_visibility ('global' board; 'friends' only to accepted friends; 'hidden'
    never) and the caller's blocks. No user ids. The old daily_challenge_leaderboard view
    (definer rights, granted to anon, user ids, ignored visibility) is dropped.
  - The exam-based start_daily_challenge/submit_daily_challenge_attempt are retired from
    clients (EXECUTE revoked); their tables stay, holding any history.
*/

-- ═══════════════════════════════════════════════════════════════════════════
-- Part 1 — XP ledger anti-cheat
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE public.xp_source_limits (
  source             text PRIMARY KEY,
  client_submittable boolean NOT NULL,
  max_per_event      integer NOT NULL CHECK (max_per_event >= 0),
  max_per_day        integer NOT NULL CHECK (max_per_day >= 0),
  note               text
);
ALTER TABLE public.xp_source_limits ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.xp_source_limits FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE ON public.xp_source_limits TO service_role;

INSERT INTO public.xp_source_limits (source, client_submittable, max_per_event, max_per_day, note) VALUES
  ('practice',           true,   80, 800, 'Learn answers: computeXPGain <= 39 per answer'),
  ('exam',               true,   80, 240, 'Exam participation XP (<= 24)'),
  ('story',              true,   80, 600, 'Role-play drill answers'),
  ('roleplay',           true,   80, 600, 'Role-play / Scenario Architect'),
  ('daily_news',         true,   80, 240, 'Daily News: once per edition-day'),
  ('listening',          true,   30, 300, 'Listening mode: 15 per round'),
  ('micro_drill',        true,   20, 200, 'Micro drill: 10 per drill'),
  ('accent_analyzer',    true,   10, 200, 'Accent analyzer: <= 2 per take'),
  ('shadowing',          true,   10, 200, 'Shadowing: <= 2 per take'),
  ('minigame',           true,  400, 800, 'Legacy timed minigames (MAX_MINIGAME_XP)'),
  ('word_drop',          true,    0,   0, 'Awards no XP (owner, 2026-10-30)'),
  ('sentence_rebuilder', true,    0,   0, 'Awards no XP (owner, 2026-10-30)'),
  ('emoji_master',       true,    0,   0, 'Awards no XP (owner, 2026-10-30)'),
  ('challenge',          true,    0,   0, 'Mock Challenges screen; no real award'),
  ('daily_challenge',    false,   0,   0, 'Server-only: award_xp in daily_challenge_*'),
  ('friend_challenge',   false,   0,   0, 'Server-only: award_xp in the duel RPCs'),
  ('mystery_box',        false,   0,   0, 'Server-only: award_xp in claim_mystery_box');

-- The per-source rolling sums below read by (user, created_at).
CREATE INDEX IF NOT EXISTS xp_events_user_created_idx ON public.xp_events (user_id, created_at DESC);

CREATE OR REPLACE FUNCTION public.submit_xp_event(
  p_source text,
  p_amount integer,
  p_idempotency_key text,
  p_occurred_at timestamptz,
  p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid;
  v_rule public.xp_source_limits%ROWTYPE;
  v_has_rule boolean;
  v_row_id text;
  v_occurred_at timestamptz;
  v_amount integer;
  v_source_used numeric;
  v_all_used numeric;
  v_metadata jsonb;
  v_inserted boolean;
  c_daily_cap constant integer := 1500;
BEGIN
  me := auth.uid();
  IF me IS NULL THEN
    RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000';
  END IF;

  SELECT * INTO v_rule FROM public.xp_source_limits WHERE source = p_source;
  v_has_rule := FOUND;
  IF p_source IN ('daily_challenge', 'friend_challenge', 'mystery_box')
     OR (v_has_rule AND NOT v_rule.client_submittable) THEN
    RAISE EXCEPTION 'source_not_client_submittable' USING ERRCODE = '22023';
  END IF;

  -- Serializes everything below per caller for the rest of this transaction.
  PERFORM pg_advisory_xact_lock(hashtext('submit_xp_event:' || me::text));

  v_row_id := me::text || ':' || p_idempotency_key;
  -- Idempotency before any cap: a retry of a recorded event always reports success.
  IF EXISTS (SELECT 1 FROM public.xp_events WHERE id = v_row_id) THEN
    RETURN jsonb_build_object('ok', true, 'awarded', false);
  END IF;

  v_occurred_at := LEAST(GREATEST(p_occurred_at, now() - interval '48 hours'), now());
  v_amount := COALESCE(p_amount, 0);

  IF v_amount > 0 THEN
    -- A source with no rule is worth nothing (fail closed; the CHECK constraint already
    -- refuses a source that is not in the schema at all).
    v_amount := LEAST(v_amount, CASE WHEN v_has_rule THEN v_rule.max_per_event ELSE 0 END);

    SELECT COALESCE(SUM(amount) FILTER (WHERE amount > 0), 0) INTO v_source_used
    FROM public.xp_events
    WHERE user_id = me AND source = p_source AND created_at >= now() - interval '24 hours';
    v_amount := LEAST(v_amount, GREATEST(0, CASE WHEN v_has_rule THEN v_rule.max_per_day ELSE 0 END - v_source_used)::integer);

    SELECT COALESCE(SUM(amount) FILTER (WHERE amount > 0), 0) INTO v_all_used
    FROM public.xp_events
    WHERE user_id = me AND created_at >= now() - interval '24 hours';
    v_amount := LEAST(v_amount, GREATEST(0, c_daily_cap - v_all_used)::integer);

    IF v_amount <= 0 THEN
      -- Nothing left to award: answer ok so the client stops retrying this event.
      RETURN jsonb_build_object('ok', true, 'awarded', false, 'capped', true, 'amount', 0);
    END IF;
  ELSIF v_amount < 0 THEN
    v_amount := GREATEST(v_amount, -100);
  ELSE
    RETURN jsonb_build_object('ok', true, 'awarded', false, 'amount', 0);
  END IF;

  v_metadata := COALESCE(p_metadata, '{}'::jsonb);
  IF v_amount <> p_amount THEN
    v_metadata := v_metadata || jsonb_build_object('claimed_amount', p_amount);
  END IF;

  INSERT INTO public.xp_events (id, user_id, amount, source, metadata, occurred_at)
  VALUES (v_row_id, me, v_amount, p_source, v_metadata, v_occurred_at)
  ON CONFLICT (id) DO NOTHING;
  v_inserted := FOUND;

  RETURN jsonb_build_object(
    'ok', true, 'awarded', v_inserted, 'occurred_at', v_occurred_at,
    'amount', v_amount, 'capped', v_amount <> p_amount
  );
END;
$$;
REVOKE EXECUTE ON FUNCTION public.submit_xp_event(text, integer, text, timestamptz, jsonb) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.submit_xp_event(text, integer, text, timestamptz, jsonb) TO authenticated;

-- ═══════════════════════════════════════════════════════════════════════════
-- Part 2 — Leagues
-- ═══════════════════════════════════════════════════════════════════════════

DROP VIEW IF EXISTS public.league_cohort_roster;

CREATE INDEX IF NOT EXISTS league_memberships_unfinalized_idx
  ON public.league_memberships (week_key) WHERE final_weekly_xp IS NULL;
CREATE INDEX IF NOT EXISTS league_cohorts_open_idx
  ON public.league_cohorts (week_key, tier, member_count);

/*
  Zone sizes for a cohort with p_active members who earned XP this week. The single rule:
  finalization applies it and get_my_league reports it (the screen draws only this).
  - promote: top 20% (at least 1); a cohort of 1-2 promotes its leader only with a 150-XP week.
  - every promotion needs a 50-XP week (two or so real answers), so a near-empty week never climbs.
  - demote: bottom 20% (at least 1) once there are 5+ active members; never below 5.
  All UNVALIDATED product numbers (spec §3.4).
*/
CREATE OR REPLACE FUNCTION public._league_zones(
  p_active integer,
  OUT promote_count integer, OUT demote_count integer, OUT promote_min_xp integer
)
LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog, public, pg_temp
AS $$
  SELECT
    CASE WHEN p_active >= 3 THEN GREATEST(1, floor(p_active * 0.2)::int)
         WHEN p_active >= 1 THEN 1 ELSE 0 END,
    CASE WHEN p_active >= 5 THEN GREATEST(1, floor(p_active * 0.2)::int) ELSE 0 END,
    CASE WHEN p_active >= 3 THEN 50 ELSE 150 END;
$$;

CREATE OR REPLACE FUNCTION public._league_next_monday(p_now timestamptz)
RETURNS timestamptz
LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog, public, pg_temp
AS $$
  SELECT (date_trunc('week', p_now AT TIME ZONE 'UTC') + interval '7 days') AT TIME ZONE 'UTC';
$$;

/*
  Finalize one week: every unfinalized membership gets its final XP, a rank among the
  cohort's ACTIVE members (XP > 0; ties: earlier joiner first), and promoted/demoted by
  _league_zones, relative to standing_tier. profiles.league_tier moves one step for the
  rows finalized here. Idempotent: finalized rows are never touched again.
*/
CREATE OR REPLACE FUNCTION public._league_finalize_week(p_week text)
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_tiers text[] := ARRAY['bronze','silver','gold','platinum','diamond'];
  v_count integer;
BEGIN
  WITH totals AS (
    SELECT lm.id, lm.user_id, lm.cohort_id, lm.standing_tier, lm.created_at,
           COALESCE((SELECT SUM(e.amount) FROM public.xp_events e
                     WHERE e.user_id = lm.user_id AND e.week_key = p_week), 0)::numeric AS xp
    FROM public.league_memberships lm
    WHERE lm.week_key = p_week AND lm.final_weekly_xp IS NULL
  ), ranked AS (
    SELECT t.*,
           CASE WHEN t.xp > 0 THEN row_number() OVER (
             PARTITION BY t.cohort_id, (t.xp > 0) ORDER BY t.xp DESC, t.created_at ASC, t.user_id ASC
           ) END AS rnk,
           count(*) FILTER (WHERE t.xp > 0) OVER (PARTITION BY t.cohort_id) AS active_n
    FROM totals t
  ), decided AS (
    SELECT r.*, z.promote_count, z.demote_count, z.promote_min_xp,
           (r.rnk IS NOT NULL AND r.standing_tier <> 'diamond'
             AND r.rnk <= z.promote_count AND r.xp >= z.promote_min_xp) AS up
    FROM ranked r
    CROSS JOIN LATERAL public._league_zones(r.active_n::int) z
  ), upd AS (
    UPDATE public.league_memberships lm SET
      final_weekly_xp = d.xp,
      rank_in_cohort = d.rnk,
      promoted = d.up,
      demoted = (NOT d.up AND d.rnk IS NOT NULL AND d.standing_tier <> 'bronze'
                 AND d.demote_count > 0 AND d.rnk > d.active_n - d.demote_count)
    FROM decided d
    WHERE lm.id = d.id
    RETURNING lm.user_id, lm.standing_tier, lm.promoted, lm.demoted
  ), moved AS (
    UPDATE public.profiles p SET league_tier = v_tiers[
      LEAST(5, GREATEST(1, array_position(v_tiers, u.standing_tier) + CASE WHEN u.promoted THEN 1 ELSE -1 END))
    ]
    FROM upd u
    WHERE p.id = u.user_id AND (u.promoted OR u.demoted)
    RETURNING 1
  )
  SELECT count(*) INTO v_count FROM upd;
  RETURN v_count;
END;
$$;

/*
  Finalize every past week (week_key < the week of p_now) that still has unfinalized
  memberships, oldest first, under the global league lock that reset_league_week also
  takes. p_wait = false (the XP trigger) only TRIES the lock and returns at once if another
  transaction holds it, so an XP write never waits on a rollover.
*/
CREATE OR REPLACE FUNCTION public._league_rollover(p_now timestamptz, p_wait boolean)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_current text := public._league_week_key(p_now);
  v_week text;
  v_n integer;
  v_total integer := 0;
  v_weeks text[] := ARRAY[]::text[];
BEGIN
  IF NOT EXISTS (SELECT 1 FROM public.league_memberships
                 WHERE final_weekly_xp IS NULL AND week_key < v_current) THEN
    RETURN jsonb_build_object('ok', true, 'pending', false, 'finalized_weeks', '[]'::jsonb, 'finalized_count', 0);
  END IF;

  IF p_wait THEN
    PERFORM pg_advisory_xact_lock(hashtext('league_assignment_global'));
  ELSIF NOT pg_try_advisory_xact_lock(hashtext('league_assignment_global')) THEN
    RETURN jsonb_build_object('ok', true, 'pending', true, 'finalized_weeks', '[]'::jsonb, 'finalized_count', 0);
  END IF;

  FOR v_week IN
    SELECT DISTINCT week_key FROM public.league_memberships
    WHERE final_weekly_xp IS NULL AND week_key < v_current
    ORDER BY week_key
  LOOP
    v_n := public._league_finalize_week(v_week);
    v_total := v_total + v_n;
    v_weeks := v_weeks || v_week;
    INSERT INTO public.league_assignment_runs (week_key, completed_at) VALUES (v_week, now())
    ON CONFLICT (week_key) DO UPDATE
      SET completed_at = COALESCE(public.league_assignment_runs.completed_at, EXCLUDED.completed_at);
  END LOOP;

  RETURN jsonb_build_object('ok', true, 'pending', false, 'finalized_weeks', to_jsonb(v_weeks), 'finalized_count', v_total);
END;
$$;

/*
  Put p_user in this week's league if they are eligible (a username, visibility 'global')
  and not in it yet. Returns the cohort id, or NULL when not eligible or (p_wait = false)
  when a past week still waits to be finalized — joining then would capture a stale tier;
  the next XP event or get_my_league joins them instead.
*/
CREATE OR REPLACE FUNCTION public._league_join(p_user uuid, p_now timestamptz, p_wait boolean)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_tiers text[] := ARRAY['bronze','silver','gold','platinum','diamond'];
  v_week text := public._league_week_key(p_now);
  v_cohort uuid;
  v_tier text;
  v_standing text;
  v_idx integer;
  v_profile record;
  v_roll jsonb;
  c_cap constant integer := 30;
  c_merge_below constant integer := 10;
BEGIN
  SELECT cohort_id INTO v_cohort FROM public.league_memberships WHERE week_key = v_week AND user_id = p_user;
  IF FOUND THEN RETURN v_cohort; END IF;

  SELECT username, leaderboard_visibility INTO v_profile FROM public.profiles WHERE id = p_user;
  IF NOT FOUND OR v_profile.username IS NULL OR v_profile.leaderboard_visibility <> 'global' THEN
    RETURN NULL;
  END IF;

  v_roll := public._league_rollover(p_now, p_wait);
  IF (v_roll->>'pending')::boolean THEN RETURN NULL; END IF;

  -- Serializes joins within the week, so two first-XP events cannot both overfill a cohort.
  PERFORM pg_advisory_xact_lock(hashtext('league_join:' || v_week));
  SELECT cohort_id INTO v_cohort FROM public.league_memberships WHERE week_key = v_week AND user_id = p_user;
  IF FOUND THEN RETURN v_cohort; END IF;

  SELECT COALESCE(league_tier, 'bronze') INTO v_standing FROM public.profiles WHERE id = p_user;
  v_idx := array_position(v_tiers, v_standing);

  -- 1. An open cohort of the learner's own tier, oldest first.
  SELECT id, tier INTO v_cohort, v_tier FROM public.league_cohorts
  WHERE week_key = v_week AND tier = v_standing AND member_count < c_cap
  ORDER BY created_at, id LIMIT 1;

  -- 2. Else an adjacent tier's cohort that is still small (a quiet week merges, so no one is alone).
  IF v_cohort IS NULL THEN
    SELECT id, tier INTO v_cohort, v_tier FROM public.league_cohorts
    WHERE week_key = v_week AND member_count < c_merge_below
      AND abs(array_position(v_tiers, tier) - v_idx) = 1
    ORDER BY member_count DESC, created_at, id LIMIT 1;
  END IF;

  -- 3. Else a new cohort of the learner's tier.
  IF v_cohort IS NULL THEN
    INSERT INTO public.league_cohorts (tier, week_key, member_count) VALUES (v_standing, v_week, 0)
    RETURNING id, tier INTO v_cohort, v_tier;
  END IF;

  INSERT INTO public.league_memberships (cohort_id, user_id, week_key, pool_tier, standing_tier)
  VALUES (v_cohort, p_user, v_week, v_tier, v_standing)
  ON CONFLICT (week_key, user_id) DO NOTHING;
  IF FOUND THEN
    UPDATE public.league_cohorts SET member_count = member_count + 1 WHERE id = v_cohort;
  END IF;
  RETURN v_cohort;
END;
$$;

-- The first positive XP of the current week joins the league. Never blocks the XP write.
CREATE OR REPLACE FUNCTION public._league_on_xp_event()
RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  IF NEW.amount > 0 AND NEW.week_key = public._league_week_key(now()) THEN
    BEGIN
      PERFORM public._league_join(NEW.user_id, now(), false);
    EXCEPTION WHEN OTHERS THEN
      RAISE WARNING 'league join skipped for %: %', NEW.user_id, SQLERRM;
    END;
  END IF;
  RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS xp_events_league_join ON public.xp_events;
CREATE TRIGGER xp_events_league_join
  AFTER INSERT ON public.xp_events
  FOR EACH ROW EXECUTE FUNCTION public._league_on_xp_event();

-- The Monday workflow (league-weekly-assignment.yml) keeps calling these names.
CREATE OR REPLACE FUNCTION public.assign_weekly_league_cohorts_as_of(p_as_of timestamptz)
RETURNS jsonb
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
  SELECT public._league_rollover(p_as_of, true)
    || jsonb_build_object('week_key', public._league_week_key(p_as_of));
$$;

CREATE OR REPLACE FUNCTION public.assign_weekly_league_cohorts()
RETURNS jsonb
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
  SELECT public.assign_weekly_league_cohorts_as_of(now());
$$;

-- With lazy joins there is nothing to rebuild; a reset clears a week that has not been
-- finalized (members rejoin on their next XP). A finalized week changed tiers and cannot
-- be undone here.
CREATE OR REPLACE FUNCTION public.reset_league_week(p_week_key text)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext('league_assignment_global'));
  IF EXISTS (SELECT 1 FROM public.league_memberships WHERE week_key = p_week_key AND final_weekly_xp IS NOT NULL) THEN
    RAISE EXCEPTION 'can_only_reset_unfinalized_week' USING ERRCODE = '22023';
  END IF;
  DELETE FROM public.league_memberships WHERE week_key = p_week_key;
  DELETE FROM public.league_cohorts WHERE week_key = p_week_key;
  DELETE FROM public.league_assignment_runs WHERE week_key = p_week_key;
  RETURN jsonb_build_object('ok', true, 'reset_week', p_week_key);
END;
$$;

/*
  The caller's league, RLS-safe: everything is scoped by auth.uid(); no user ids leave.
  Runs the rollover first (so a missed cron heals here) and joins a learner who already
  has XP this week but no membership (e.g. they set their username after earning it).
*/
CREATE OR REPLACE FUNCTION public.get_my_league()
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid := auth.uid();
  v_now timestamptz := now();
  v_week text := public._league_week_key(now());
  v_profile record;
  v_my_xp numeric;
  v_member public.league_memberships%ROWTYPE;
  v_has_member boolean;
  v_status text;
  v_cohort_tier text;
  v_active integer := 0;
  v_zones record;
  v_members jsonb := '[]'::jsonb;
  v_mixed boolean := false;
  v_last jsonb;
BEGIN
  IF me IS NULL THEN RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000'; END IF;
  SELECT * INTO v_zones FROM public._league_zones(0);

  PERFORM public._league_rollover(v_now, true);

  SELECT username, leaderboard_visibility, COALESCE(league_tier, 'bronze') AS tier
    INTO v_profile FROM public.profiles WHERE id = me;
  IF NOT FOUND THEN RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000'; END IF;

  SELECT COALESCE(SUM(amount), 0) INTO v_my_xp FROM public.xp_events WHERE user_id = me AND week_key = v_week;

  SELECT * INTO v_member FROM public.league_memberships WHERE week_key = v_week AND user_id = me;
  v_has_member := FOUND;
  IF NOT v_has_member AND v_my_xp > 0 THEN
    PERFORM public._league_join(me, v_now, true);
    SELECT * INTO v_member FROM public.league_memberships WHERE week_key = v_week AND user_id = me;
    v_has_member := FOUND;
  END IF;

  v_status := CASE
    WHEN v_has_member THEN 'active'
    WHEN v_profile.username IS NULL THEN 'needs_username'
    WHEN v_profile.leaderboard_visibility <> 'global' THEN 'not_global'
    ELSE 'not_joined'
  END;

  IF v_has_member THEN
    SELECT tier INTO v_cohort_tier FROM public.league_cohorts WHERE id = v_member.cohort_id;

    WITH m AS (
      SELECT lm.user_id, lm.created_at, lm.standing_tier,
             p.username, p.avatar_emoji, p.equipped_frame, p.equipped_nameplate, p.leaderboard_visibility,
             COALESCE((SELECT SUM(e.amount) FROM public.xp_events e
                       WHERE e.user_id = lm.user_id AND e.week_key = v_week), 0) AS xp
      FROM public.league_memberships lm
      JOIN public.profiles p ON p.id = lm.user_id
      WHERE lm.cohort_id = v_member.cohort_id
    ), r AS (
      SELECT m.*,
             CASE WHEN m.xp > 0 THEN row_number() OVER (
               PARTITION BY (m.xp > 0) ORDER BY m.xp DESC, m.created_at ASC, m.user_id ASC) END AS rnk
      FROM m
    )
    SELECT
      count(*) FILTER (WHERE r.xp > 0),
      bool_or(r.standing_tier <> v_cohort_tier),
      COALESCE(jsonb_agg(jsonb_build_object(
        'rank', r.rnk,
        'username', r.username,
        'avatar_emoji', r.avatar_emoji,
        'equipped_frame', r.equipped_frame,
        'equipped_nameplate', r.equipped_nameplate,
        'weekly_xp', r.xp,
        'standing_tier', r.standing_tier,
        'is_me', r.user_id = me
      ) ORDER BY r.rnk NULLS LAST, r.created_at) FILTER (
        WHERE r.user_id = me OR (r.username IS NOT NULL AND r.leaderboard_visibility = 'global')
      ), '[]'::jsonb)
    INTO v_active, v_mixed, v_members
    FROM r;

    SELECT * INTO v_zones FROM public._league_zones(v_active);
  END IF;

  SELECT jsonb_build_object(
    'week_key', lm.week_key,
    'pool_tier', lm.pool_tier,
    'standing_tier', lm.standing_tier,
    'rank', lm.rank_in_cohort,
    'final_xp', lm.final_weekly_xp,
    'promoted', lm.promoted,
    'demoted', lm.demoted,
    'active_members', (SELECT count(*) FROM public.league_memberships o
                       WHERE o.cohort_id = lm.cohort_id AND o.rank_in_cohort IS NOT NULL)
  ) INTO v_last
  FROM public.league_memberships lm
  WHERE lm.user_id = me AND lm.week_key < v_week AND lm.final_weekly_xp IS NOT NULL
  ORDER BY lm.week_key DESC LIMIT 1;

  RETURN jsonb_build_object(
    'status', v_status,
    'week_key', v_week,
    'resets_at', public._league_next_monday(v_now),
    'tier', v_profile.tier,
    'my_weekly_xp', v_my_xp,
    'cohort', CASE WHEN v_has_member THEN jsonb_build_object(
      'tier', v_cohort_tier,
      'mixed', COALESCE(v_mixed, false),
      'active_members', v_active,
      'promote_count', v_zones.promote_count,
      'demote_count', v_zones.demote_count,
      'promote_min_xp', v_zones.promote_min_xp,
      'members', v_members
    ) END,
    'last_week', v_last
  );
END;
$$;

-- ═══════════════════════════════════════════════════════════════════════════
-- Part 3 — Daily Challenge
-- ═══════════════════════════════════════════════════════════════════════════

DROP VIEW IF EXISTS public.daily_challenge_leaderboard;

CREATE TABLE public.daily_challenge_bank (
  id              text PRIMARY KEY,
  kind            text NOT NULL DEFAULT 'build' CHECK (kind = 'build'),
  english         text NOT NULL,
  french          text NOT NULL,
  explanation     text NOT NULL,
  theme           text NOT NULL,
  difficulty      smallint NOT NULL CHECK (difficulty BETWEEN 1 AND 3),
  tiles           text[] NOT NULL,
  tile_keys       text[] NOT NULL,
  word_count      smallint NOT NULL CHECK (word_count >= 1),
  accepted_orders jsonb NOT NULL CHECK (jsonb_typeof(accepted_orders) = 'array'),
  active          boolean NOT NULL DEFAULT true,
  updated_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT daily_challenge_bank_tiles_match CHECK (cardinality(tiles) = cardinality(tile_keys)),
  CONSTRAINT daily_challenge_bank_words_fit CHECK (word_count <= cardinality(tiles))
);

CREATE TABLE public.daily_challenge_sets (
  challenge_date date PRIMARY KEY,
  item_ids       text[] NOT NULL CHECK (cardinality(item_ids) >= 1),
  created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE public.daily_challenge_runs (
  user_id        uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  challenge_date date NOT NULL REFERENCES public.daily_challenge_sets(challenge_date),
  started_at     timestamptz NOT NULL DEFAULT now(),
  finished_at    timestamptz,
  item_count     smallint NOT NULL,
  correct_count  smallint NOT NULL DEFAULT 0,
  duration_ms    integer,
  flagged        boolean NOT NULL DEFAULT false,
  xp_awarded     integer NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, challenge_date)
);
CREATE INDEX daily_challenge_runs_board_idx
  ON public.daily_challenge_runs (challenge_date, correct_count DESC, duration_ms ASC)
  WHERE finished_at IS NOT NULL AND NOT flagged;

CREATE TABLE public.daily_challenge_answers (
  user_id        uuid NOT NULL,
  challenge_date date NOT NULL,
  position       smallint NOT NULL CHECK (position >= 0),
  item_id        text NOT NULL REFERENCES public.daily_challenge_bank(id),
  tiles          smallint[] NOT NULL,
  correct        boolean NOT NULL,
  answered_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, challenge_date, position),
  FOREIGN KEY (user_id, challenge_date) REFERENCES public.daily_challenge_runs(user_id, challenge_date) ON DELETE CASCADE
);

-- Served only through the SECURITY DEFINER functions below; no client role reads these
-- tables directly (the bank holds the answer keys).
ALTER TABLE public.daily_challenge_bank ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.daily_challenge_sets ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.daily_challenge_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.daily_challenge_answers ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.daily_challenge_bank, public.daily_challenge_sets,
  public.daily_challenge_runs, public.daily_challenge_answers FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE ON public.daily_challenge_bank, public.daily_challenge_sets,
  public.daily_challenge_runs, public.daily_challenge_answers TO service_role;

/* Today's set: 8 items, the same for everyone on a UTC day. Built once (ON CONFLICT), so
   concurrent first requests agree. Difficulty quotas 3/4/1, easiest first, avoiding the
   previous 4 days' items while the pool allows. */
CREATE OR REPLACE FUNCTION public._daily_challenge_ensure_set(p_date date)
RETURNS text[]
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_ids text[];
  v_recent text[];
  v_pick text[] := ARRAY[]::text[];
  v_quota integer[] := ARRAY[3, 4, 1];
  v_size constant integer := 8;
  d integer;
BEGIN
  SELECT item_ids INTO v_ids FROM public.daily_challenge_sets WHERE challenge_date = p_date;
  IF FOUND THEN RETURN v_ids; END IF;

  SELECT COALESCE(array_agg(DISTINCT x), ARRAY[]::text[]) INTO v_recent
  FROM public.daily_challenge_sets s, unnest(s.item_ids) x
  WHERE s.challenge_date BETWEEN p_date - 4 AND p_date - 1;

  FOR d IN 1..3 LOOP
    v_pick := v_pick || COALESCE((
      SELECT array_agg(id) FROM (
        SELECT id FROM public.daily_challenge_bank
        WHERE active AND difficulty = d AND id <> ALL (v_recent)
        ORDER BY md5(id || ':' || p_date::text) LIMIT v_quota[d]
      ) q), ARRAY[]::text[]);
  END LOOP;

  -- Short of a quota: any recent-free item, then anything not yet picked.
  IF cardinality(v_pick) < v_size THEN
    v_pick := v_pick || COALESCE((
      SELECT array_agg(id) FROM (
        SELECT id FROM public.daily_challenge_bank
        WHERE active AND id <> ALL (v_pick) AND id <> ALL (v_recent)
        ORDER BY md5(id || ':' || p_date::text) LIMIT v_size - cardinality(v_pick)
      ) q), ARRAY[]::text[]);
  END IF;
  IF cardinality(v_pick) < v_size THEN
    v_pick := v_pick || COALESCE((
      SELECT array_agg(id) FROM (
        SELECT id FROM public.daily_challenge_bank
        WHERE active AND id <> ALL (v_pick)
        ORDER BY md5(id || ':' || p_date::text) LIMIT v_size - cardinality(v_pick)
      ) q), ARRAY[]::text[]);
  END IF;

  IF cardinality(v_pick) = 0 THEN
    RAISE EXCEPTION 'no_daily_challenge_items' USING ERRCODE = '22023';
  END IF;

  SELECT array_agg(b.id ORDER BY b.difficulty, md5(b.id || ':' || p_date::text)) INTO v_pick
  FROM public.daily_challenge_bank b WHERE b.id = ANY (v_pick);

  INSERT INTO public.daily_challenge_sets (challenge_date, item_ids) VALUES (p_date, v_pick)
  ON CONFLICT (challenge_date) DO NOTHING;
  SELECT item_ids INTO v_ids FROM public.daily_challenge_sets WHERE challenge_date = p_date;
  RETURN v_ids;
END;
$$;

/* Display order of an item's tiles on a day: a permutation of 1..n (original indexes).
   Display position k (0-based) shows original tile perm[k+1]. Deterministic, so every
   learner sees the same board and the server can map an answer back without storing it. */
CREATE OR REPLACE FUNCTION public._daily_challenge_perm(p_item_id text, p_date date, p_n integer)
RETURNS integer[]
LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog, public, pg_temp
AS $$
  SELECT array_agg(i ORDER BY md5(p_item_id || ':' || p_date::text || ':' || i), i)
  FROM generate_series(1, p_n) i;
$$;

/* One item as the learner sees it; the solution only once it is answered (or the run is done). */
CREATE OR REPLACE FUNCTION public._daily_challenge_item_json(
  p_item public.daily_challenge_bank, p_date date, p_position integer,
  p_answer public.daily_challenge_answers, p_reveal boolean
)
RETURNS jsonb
LANGUAGE plpgsql STABLE
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_perm integer[] := public._daily_challenge_perm(p_item.id, p_date, cardinality(p_item.tiles));
  v_tiles jsonb;
BEGIN
  SELECT jsonb_agg(p_item.tiles[v_perm[k]] ORDER BY k) INTO v_tiles FROM generate_series(1, cardinality(v_perm)) k;
  RETURN jsonb_build_object(
    'position', p_position,
    'english', p_item.english,
    'theme', p_item.theme,
    'difficulty', p_item.difficulty,
    'word_count', p_item.word_count,
    'tiles', v_tiles,
    'answer', CASE WHEN p_answer.position IS NOT NULL THEN jsonb_build_object(
      'tiles', to_jsonb(p_answer.tiles), 'correct', p_answer.correct) END,
    'solution', CASE WHEN p_reveal OR p_answer.position IS NOT NULL THEN jsonb_build_object(
      'french', p_item.french, 'explanation', p_item.explanation) END
  );
END;
$$;

/* Close a run: time from the server clock (last answer, or the deadline for an abandoned
   run), flag an implausibly fast run, award XP once. Unanswered items count as wrong. */
CREATE OR REPLACE FUNCTION public._daily_challenge_finish(p_user uuid, p_date date)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_run public.daily_challenge_runs%ROWTYPE;
  v_correct integer;
  v_last timestamptz;
  v_end timestamptz;
  v_duration integer;
  v_flagged boolean;
  v_xp integer;
  c_deadline constant interval := interval '20 minutes';
  c_min_ms_per_item constant integer := 3000;
BEGIN
  SELECT * INTO v_run FROM public.daily_challenge_runs WHERE user_id = p_user AND challenge_date = p_date FOR UPDATE;
  IF NOT FOUND OR v_run.finished_at IS NOT NULL THEN RETURN; END IF;

  SELECT count(*) FILTER (WHERE correct), max(answered_at) INTO v_correct, v_last
  FROM public.daily_challenge_answers WHERE user_id = p_user AND challenge_date = p_date;

  v_end := LEAST(COALESCE(v_last, v_run.started_at + c_deadline), v_run.started_at + c_deadline);
  v_duration := GREATEST(0, (extract(epoch FROM (v_end - v_run.started_at)) * 1000)::integer);
  v_flagged := v_duration < c_min_ms_per_item * v_run.item_count;
  v_xp := CASE WHEN v_flagged THEN 0 ELSE 10 + 5 * v_correct END;

  UPDATE public.daily_challenge_runs SET
    finished_at = v_end, correct_count = v_correct, duration_ms = v_duration,
    flagged = v_flagged, xp_awarded = v_xp
  WHERE user_id = p_user AND challenge_date = p_date;

  IF v_xp > 0 THEN
    -- Same key the exam-based Daily Challenge used: one daily_challenge award per account per day.
    PERFORM public.award_xp(
      p_user, 'daily_challenge', v_xp,
      p_user::text || ':daily_challenge:' || p_date::text,
      jsonb_build_object('challenge_date', p_date, 'correct', v_correct, 'items', v_run.item_count, 'format', 'build')
    );
  END IF;
END;
$$;

/* The caller's whole Daily Challenge state for p_date (items only once started). */
CREATE OR REPLACE FUNCTION public._daily_challenge_state(p_user uuid, p_date date)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_ids text[];
  v_run public.daily_challenge_runs%ROWTYPE;
  v_has_run boolean;
  v_items jsonb;
  v_rank integer;
  v_participants integer;
  v_streak integer := 0;
  v_best integer := 0;
  v_today date := (now() AT TIME ZONE 'UTC')::date;
BEGIN
  v_ids := public._daily_challenge_ensure_set(p_date);

  SELECT * INTO v_run FROM public.daily_challenge_runs WHERE user_id = p_user AND challenge_date = p_date;
  v_has_run := FOUND;
  IF v_has_run AND v_run.finished_at IS NULL AND now() > v_run.started_at + interval '20 minutes' THEN
    PERFORM public._daily_challenge_finish(p_user, p_date);
    SELECT * INTO v_run FROM public.daily_challenge_runs WHERE user_id = p_user AND challenge_date = p_date;
  END IF;

  IF v_has_run THEN
    SELECT jsonb_agg(public._daily_challenge_item_json(b, p_date, (pos - 1)::int, a, v_run.finished_at IS NOT NULL) ORDER BY pos)
      INTO v_items
    FROM unnest(v_ids) WITH ORDINALITY AS s(item_id, pos)
    JOIN public.daily_challenge_bank b ON b.id = s.item_id
    LEFT JOIN public.daily_challenge_answers a
      ON a.user_id = p_user AND a.challenge_date = p_date AND a.position = pos - 1;
  END IF;

  IF v_has_run AND v_run.finished_at IS NOT NULL AND NOT v_run.flagged THEN
    SELECT 1 + count(*) INTO v_rank FROM public.daily_challenge_runs o
    WHERE o.challenge_date = p_date AND o.finished_at IS NOT NULL AND NOT o.flagged
      AND (o.correct_count > v_run.correct_count
           OR (o.correct_count = v_run.correct_count AND o.duration_ms < v_run.duration_ms));
  END IF;

  SELECT count(*) INTO v_participants FROM public.daily_challenge_runs
  WHERE challenge_date = p_date AND finished_at IS NOT NULL AND NOT flagged;

  -- Consecutive UTC days with a finished run; still alive until today's challenge is missed.
  WITH d AS (
    SELECT challenge_date FROM public.daily_challenge_runs
    WHERE user_id = p_user AND finished_at IS NOT NULL
    ORDER BY challenge_date DESC LIMIT 1000
  ), g AS (
    SELECT challenge_date, challenge_date + (row_number() OVER (ORDER BY challenge_date DESC))::int AS grp FROM d
  ), runs AS (
    SELECT grp, count(*)::int AS len, max(challenge_date) AS last_day FROM g GROUP BY grp
  )
  SELECT COALESCE(max(len) FILTER (WHERE last_day >= v_today - 1), 0), COALESCE(max(len), 0)
    INTO v_streak, v_best FROM runs;

  RETURN jsonb_build_object(
    'challenge_date', p_date,
    'resets_at', ((p_date + 1)::timestamp AT TIME ZONE 'UTC'),
    'item_count', cardinality(v_ids),
    'participants', v_participants,
    'streak', jsonb_build_object('current', v_streak, 'best', v_best),
    'run', CASE WHEN v_has_run THEN jsonb_build_object(
      'status', CASE WHEN v_run.finished_at IS NULL THEN 'in_progress' ELSE 'finished' END,
      'started_at', v_run.started_at,
      'deadline_at', v_run.started_at + interval '20 minutes',
      'finished_at', v_run.finished_at,
      'correct_count', v_run.correct_count,
      'duration_ms', v_run.duration_ms,
      'flagged', v_run.flagged,
      'xp_awarded', v_run.xp_awarded,
      'rank', v_rank
    ) END,
    'items', COALESCE(v_items, '[]'::jsonb)
  );
END;
$$;

CREATE OR REPLACE FUNCTION public.daily_challenge_today()
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid := auth.uid();
BEGIN
  IF me IS NULL THEN RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000'; END IF;
  RETURN public._daily_challenge_state(me, (now() AT TIME ZONE 'UTC')::date);
END;
$$;

CREATE OR REPLACE FUNCTION public.daily_challenge_start()
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid := auth.uid();
  v_date date := (now() AT TIME ZONE 'UTC')::date;
  v_ids text[];
BEGIN
  IF me IS NULL THEN RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000'; END IF;
  IF NOT EXISTS (SELECT 1 FROM public.profiles WHERE id = me) THEN
    RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000';
  END IF;
  v_ids := public._daily_challenge_ensure_set(v_date);
  -- One run per day; a second start keeps the first start time (the clock never restarts).
  INSERT INTO public.daily_challenge_runs (user_id, challenge_date, item_count)
  VALUES (me, v_date, cardinality(v_ids))
  ON CONFLICT (user_id, challenge_date) DO NOTHING;
  RETURN public._daily_challenge_state(me, v_date);
END;
$$;

/* Mark one answer. p_tiles = display positions of the placed tiles, in order. */
CREATE OR REPLACE FUNCTION public.daily_challenge_answer(p_challenge_date date, p_position integer, p_tiles integer[])
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid := auth.uid();
  v_run public.daily_challenge_runs%ROWTYPE;
  v_ids text[];
  v_item public.daily_challenge_bank%ROWTYPE;
  v_perm integer[];
  v_n integer;
  v_keys text[];
  v_correct boolean;
  v_answered integer;
  v_existing public.daily_challenge_answers%ROWTYPE;
  t integer;
BEGIN
  IF me IS NULL THEN RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000'; END IF;

  SELECT * INTO v_run FROM public.daily_challenge_runs
  WHERE user_id = me AND challenge_date = p_challenge_date FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'not_started' USING ERRCODE = '22023'; END IF;

  SELECT item_ids INTO v_ids FROM public.daily_challenge_sets WHERE challenge_date = p_challenge_date;
  IF p_position IS NULL OR p_position < 0 OR p_position >= cardinality(v_ids) THEN
    RAISE EXCEPTION 'invalid_position' USING ERRCODE = '22023';
  END IF;

  -- A repeat of an answered item returns the stored mark, never a second chance.
  SELECT * INTO v_existing FROM public.daily_challenge_answers
  WHERE user_id = me AND challenge_date = p_challenge_date AND position = p_position;
  IF FOUND THEN
    RETURN public._daily_challenge_state(me, p_challenge_date) || jsonb_build_object('repeat', true);
  END IF;

  IF v_run.finished_at IS NOT NULL THEN RAISE EXCEPTION 'run_finished' USING ERRCODE = '22023'; END IF;
  IF now() > v_run.started_at + interval '20 minutes' THEN
    -- Too late for this answer: close the run with what was answered (no exception, which
    -- would roll the close back) and say so.
    PERFORM public._daily_challenge_finish(me, p_challenge_date);
    RETURN public._daily_challenge_state(me, p_challenge_date) || jsonb_build_object('repeat', false, 'expired', true);
  END IF;

  SELECT * INTO v_item FROM public.daily_challenge_bank WHERE id = v_ids[p_position + 1];
  v_n := cardinality(v_item.tiles);
  IF p_tiles IS NULL OR cardinality(p_tiles) = 0 OR cardinality(p_tiles) > v_n
     OR (SELECT count(DISTINCT x) FROM unnest(p_tiles) x) <> cardinality(p_tiles)
     OR EXISTS (SELECT 1 FROM unnest(p_tiles) x WHERE x IS NULL OR x < 0 OR x >= v_n) THEN
    RAISE EXCEPTION 'invalid_answer' USING ERRCODE = '22023';
  END IF;

  v_perm := public._daily_challenge_perm(v_item.id, p_challenge_date, v_n);
  v_keys := ARRAY[]::text[];
  FOREACH t IN ARRAY p_tiles LOOP
    v_keys := v_keys || v_item.tile_keys[v_perm[t + 1]];
  END LOOP;
  -- jsonb array equality is order-sensitive (containment is not, so never use @> here).
  v_correct := EXISTS (SELECT 1 FROM jsonb_array_elements(v_item.accepted_orders) o WHERE o = to_jsonb(v_keys));

  INSERT INTO public.daily_challenge_answers (user_id, challenge_date, position, item_id, tiles, correct)
  VALUES (me, p_challenge_date, p_position, v_item.id, p_tiles::smallint[], v_correct);

  SELECT count(*) INTO v_answered FROM public.daily_challenge_answers
  WHERE user_id = me AND challenge_date = p_challenge_date;
  IF v_answered >= v_run.item_count THEN
    PERFORM public._daily_challenge_finish(me, p_challenge_date);
  END IF;

  RETURN public._daily_challenge_state(me, p_challenge_date) || jsonb_build_object('repeat', false);
END;
$$;

/* The day's board, RLS-safe: no user ids; visibility and the caller's blocks respected. */
CREATE OR REPLACE FUNCTION public.daily_challenge_board(p_scope text DEFAULT 'global', p_date date DEFAULT NULL)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  me uuid := auth.uid();
  v_date date := COALESCE(p_date, (now() AT TIME ZONE 'UTC')::date);
  v_rows jsonb;
  v_total integer;
BEGIN
  IF me IS NULL THEN RAISE EXCEPTION 'not_authenticated' USING ERRCODE = '28000'; END IF;
  IF p_scope NOT IN ('global', 'friends') THEN RAISE EXCEPTION 'invalid_scope' USING ERRCODE = '22023'; END IF;
  IF v_date > (now() AT TIME ZONE 'UTC')::date OR v_date < (now() AT TIME ZONE 'UTC')::date - 30 THEN
    RAISE EXCEPTION 'invalid_date' USING ERRCODE = '22023';
  END IF;

  WITH visible AS (
    SELECT r.user_id, r.correct_count, r.item_count, r.duration_ms, r.finished_at,
           p.username, p.avatar_emoji, p.equipped_frame, p.equipped_nameplate
    FROM public.daily_challenge_runs r
    JOIN public.profiles p ON p.id = r.user_id
    WHERE r.challenge_date = v_date AND r.finished_at IS NOT NULL AND NOT r.flagged
      AND (
        r.user_id = me
        OR (p.username IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM public.blocks b WHERE b.blocker_id = me AND b.blocked_id = r.user_id)
            AND CASE p_scope
              WHEN 'global' THEN p.leaderboard_visibility = 'global'
              ELSE p.leaderboard_visibility IN ('global', 'friends') AND EXISTS (
                SELECT 1 FROM public.friendships f
                WHERE f.status = 'accepted'
                  AND f.user_low = LEAST(me, r.user_id) AND f.user_high = GREATEST(me, r.user_id))
            END)
      )
  ), ranked AS (
    SELECT v.*, rank() OVER (ORDER BY v.correct_count DESC, v.duration_ms ASC) AS rnk,
           row_number() OVER (ORDER BY v.correct_count DESC, v.duration_ms ASC, v.finished_at ASC, v.user_id) AS pos
    FROM visible v
  )
  SELECT count(*)::int,
         COALESCE(jsonb_agg(jsonb_build_object(
           'rank', rnk, 'username', username, 'avatar_emoji', avatar_emoji,
           'equipped_frame', equipped_frame, 'equipped_nameplate', equipped_nameplate,
           'correct_count', correct_count, 'item_count', item_count,
           'duration_ms', duration_ms, 'is_me', user_id = me
         ) ORDER BY pos) FILTER (WHERE pos <= 50 OR user_id = me), '[]'::jsonb)
    INTO v_total, v_rows
  FROM ranked;

  RETURN jsonb_build_object('challenge_date', v_date, 'scope', p_scope, 'total', v_total, 'rows', v_rows);
END;
$$;

-- The 6-hourly seed workflow (daily-challenge-seed.yml) also builds today's and
-- tomorrow's sets, so the first learner of the day never pays for it. The exam-based
-- seed stays (its question-set table may be empty) but can no longer fail the call.
CREATE OR REPLACE FUNCTION public.seed_daily_challenges_batch()
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_today date := (now() AT TIME ZONE 'UTC')::date;
  v_legacy jsonb;
BEGIN
  BEGIN
    v_legacy := jsonb_build_object(
      'today', public.seed_daily_challenge(v_today),
      'tomorrow', public.seed_daily_challenge(v_today + 1));
  EXCEPTION WHEN OTHERS THEN
    v_legacy := jsonb_build_object('error', SQLERRM);
  END;
  RETURN jsonb_build_object(
    'ok', true,
    'today', to_jsonb(public._daily_challenge_ensure_set(v_today)),
    'tomorrow', to_jsonb(public._daily_challenge_ensure_set(v_today + 1)),
    'legacy_exam_sets', v_legacy
  );
END;
$$;

-- ── Grants ─────────────────────────────────────────────────────────────────
-- Internal helpers: no client role (Supabase grants EXECUTE on new functions to anon and
-- authenticated by default, so PUBLIC alone is not enough).
REVOKE EXECUTE ON FUNCTION public._league_zones(integer) FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public._league_next_monday(timestamptz) FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public._league_finalize_week(text) FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public._league_rollover(timestamptz, boolean) FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public._league_join(uuid, timestamptz, boolean) FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public._league_on_xp_event() FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public._daily_challenge_ensure_set(date) FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public._daily_challenge_perm(text, date, integer) FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public._daily_challenge_item_json(public.daily_challenge_bank, date, integer, public.daily_challenge_answers, boolean) FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public._daily_challenge_finish(uuid, date) FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public._daily_challenge_state(uuid, date) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public._league_rollover(timestamptz, boolean) TO service_role;
GRANT EXECUTE ON FUNCTION public._daily_challenge_ensure_set(date) TO service_role;

-- Service-only entry points (workflows).
REVOKE EXECUTE ON FUNCTION public.assign_weekly_league_cohorts() FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.assign_weekly_league_cohorts_as_of(timestamptz) FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.reset_league_week(text) FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.seed_daily_challenges_batch() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.assign_weekly_league_cohorts() TO service_role;
GRANT EXECUTE ON FUNCTION public.assign_weekly_league_cohorts_as_of(timestamptz) TO service_role;
GRANT EXECUTE ON FUNCTION public.reset_league_week(text) TO service_role;
GRANT EXECUTE ON FUNCTION public.seed_daily_challenges_batch() TO service_role;

-- Client entry points: signed-in accounts only.
REVOKE EXECUTE ON FUNCTION public.get_my_league() FROM PUBLIC, anon;
REVOKE EXECUTE ON FUNCTION public.daily_challenge_today() FROM PUBLIC, anon;
REVOKE EXECUTE ON FUNCTION public.daily_challenge_start() FROM PUBLIC, anon;
REVOKE EXECUTE ON FUNCTION public.daily_challenge_answer(date, integer, integer[]) FROM PUBLIC, anon;
REVOKE EXECUTE ON FUNCTION public.daily_challenge_board(text, date) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.get_my_league() TO authenticated;
GRANT EXECUTE ON FUNCTION public.daily_challenge_today() TO authenticated;
GRANT EXECUTE ON FUNCTION public.daily_challenge_start() TO authenticated;
GRANT EXECUTE ON FUNCTION public.daily_challenge_answer(date, integer, integer[]) TO authenticated;
GRANT EXECUTE ON FUNCTION public.daily_challenge_board(text, date) TO authenticated;

-- The exam-based Daily Challenge is retired from clients (its tables keep any history).
REVOKE EXECUTE ON FUNCTION public.start_daily_challenge(date) FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.submit_daily_challenge_attempt(date, text) FROM PUBLIC, anon, authenticated;
