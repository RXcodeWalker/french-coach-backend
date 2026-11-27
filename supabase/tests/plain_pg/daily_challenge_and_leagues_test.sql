-- Daily Challenge and Leagues relaunch (migration 20261126090000 + the bank seed 20261126090100).
-- Run with run_all_migrations.mjs (README.md); ends with 'ALL PASSED'.

-- ── fixtures ────────────────────────────────────────────────────────────────
create or replace function public._t_user(p_n int, p_name text) returns uuid language plpgsql as $$
declare v uuid := ('dc000000-0000-4000-8000-' || lpad(p_n::text, 12, '0'))::uuid;
begin
  insert into auth.users (id, email) values (v, 'dc' || p_n || '@example.test') on conflict do nothing;
  insert into public.profiles (id) values (v) on conflict do nothing;
  if p_name is not null then update public.profiles set username = p_name where id = v; end if;
  return v;
end $$;
create or replace function public._t_as(p uuid) returns void language sql as $$
  select set_config('request.jwt.claim.sub', p::text, false);
  select set_config('request.jwt.claims', json_build_object('sub', p, 'role', 'authenticated')::text, false);
$$;
create or replace function public._t_expect_error(p_sql text, p_needle text) returns void language plpgsql as $$
begin
  execute p_sql;
  raise exception 'expected error % from: %', p_needle, p_sql;
exception when others then
  if sqlerrm not like '%' || p_needle || '%' then
    raise exception 'expected error % from %, got: %', p_needle, p_sql, sqlerrm;
  end if;
end $$;
grant execute on function public._t_as(uuid), public._t_expect_error(text, text) to authenticated, anon;

-- The display positions that rebuild item p_position of a day in its main order.
create or replace function public._t_correct_tiles(p_date date, p_position int) returns int[] language plpgsql as $$
declare v_item public.daily_challenge_bank; v_perm int[]; v_out int[] := '{}'; j int; k int;
begin
  select b.* into v_item from public.daily_challenge_sets s join public.daily_challenge_bank b
    on b.id = s.item_ids[p_position + 1] where s.challenge_date = p_date;
  v_perm := public._daily_challenge_perm(v_item.id, p_date, cardinality(v_item.tiles));
  for j in 1..v_item.word_count loop
    for k in 1..cardinality(v_perm) loop
      if v_perm[k] = j then v_out := v_out || (k - 1); end if;
    end loop;
  end loop;
  return v_out;
end $$;

-- ═══ Part 1: XP ledger caps ══════════════════════════════════════════════
select public._t_user(1, 'xp_ana') is not null;
select public._t_as('dc000000-0000-4000-8000-000000000001');
set role authenticated;
do $$ declare r jsonb; begin
  -- per-event clamp: a claimed 500 is stored as practice's 80
  r := public.submit_xp_event('practice', 500, 'k1', now(), '{}');
  if (r->>'amount')::int <> 80 or not (r->>'capped')::bool then raise exception 'per-event clamp failed: %', r; end if;
  -- idempotent retry
  r := public.submit_xp_event('practice', 500, 'k1', now(), '{}');
  if (r->>'awarded')::bool then raise exception 'retry awarded twice: %', r; end if;
  -- zero-XP sources are worth nothing, but still answer ok (the client stops retrying)
  r := public.submit_xp_event('word_drop', 50, 'k2', now(), '{}');
  if not (r->>'ok')::bool or (r->>'amount')::int <> 0 then raise exception 'word_drop not zeroed: %', r; end if;
  r := public.submit_xp_event('challenge', 400, 'k3', now(), '{}');
  if (r->>'amount')::int <> 0 then raise exception 'mock challenge XP not zeroed: %', r; end if;
  -- negative XP still lands
  r := public.submit_xp_event('daily_news', -5, 'k4', now(), '{}');
  if (r->>'amount')::int <> -5 then raise exception 'negative XP changed: %', r; end if;
end $$;
select public._t_expect_error($$select public.submit_xp_event('daily_challenge', 10, 'k5', now(), '{}')$$, 'source_not_client_submittable');
select public._t_expect_error($$select public.submit_xp_event('mystery_box', 10, 'k6', now(), '{}')$$, 'source_not_client_submittable');
-- per-source daily cap: practice tops out at 800 in 24 h
do $$ declare i int; r jsonb; begin
  for i in 1..15 loop r := public.submit_xp_event('practice', 80, 'p' || i, now(), '{}'); end loop;
  if (r->>'amount')::int <> 0 or not (r->>'capped')::bool then raise exception 'practice day cap not applied: %', r; end if;
end $$;
reset role;
do $$ begin
  if (select sum(amount) from public.xp_events where user_id = 'dc000000-0000-4000-8000-000000000001' and source = 'practice') <> 800 then
    raise exception 'practice day total is not 800';
  end if;
  if (select metadata->>'claimed_amount' from public.xp_events where id = 'dc000000-0000-4000-8000-000000000001:k1') <> '500' then
    raise exception 'claimed amount not kept in metadata';
  end if;
  if exists (select 1 from public.xp_events where user_id = 'dc000000-0000-4000-8000-000000000001' and source in ('word_drop', 'challenge')) then
    raise exception 'a zero-cap source was stored';
  end if;
end $$;
-- overall cap: 1500 across sources
select public._t_as('dc000000-0000-4000-8000-000000000001');
set role authenticated;
do $$ declare i int; r jsonb; begin
  for i in 1..10 loop r := public.submit_xp_event('story', 80, 's' || i, now(), '{}'); end loop;
end $$;
reset role;
do $$ begin
  if (select sum(amount) filter (where amount > 0) from public.xp_events where user_id = 'dc000000-0000-4000-8000-000000000001') > 1500 then
    raise exception 'overall 24h cap exceeded';
  end if;
end $$;
-- a client cannot read or change the limits
select public._t_as('dc000000-0000-4000-8000-000000000001');
set role authenticated;
select public._t_expect_error($$select * from public.xp_source_limits$$, 'permission denied');
reset role;

-- ═══ Part 2: Leagues ══════════════════════════════════════════════════════
-- xp_ana (user 1) joined on her first XP of the week, as Bronze.
do $$ begin
  if not exists (select 1 from public.league_memberships where user_id = 'dc000000-0000-4000-8000-000000000001'
                 and week_key = public._league_week_key(now()) and pool_tier = 'bronze') then
    raise exception 'first XP did not join the league';
  end if;
end $$;
-- No username: no league, and get_my_league says why.
select public._t_user(2, null) is not null;
insert into public.xp_events (id, user_id, amount, source, occurred_at) values ('t2', 'dc000000-0000-4000-8000-000000000002', 20, 'practice', now());
do $$ begin
  if exists (select 1 from public.league_memberships where user_id = 'dc000000-0000-4000-8000-000000000002') then
    raise exception 'a user without a username joined a league';
  end if;
end $$;
select public._t_as('dc000000-0000-4000-8000-000000000002');
set role authenticated;
do $$ begin
  if public.get_my_league()->>'status' <> 'needs_username' then raise exception 'expected needs_username'; end if;
end $$;
reset role;
-- Setting a username later: get_my_league joins them (they already have XP this week).
update public.profiles set username = 'late_leo' where id = 'dc000000-0000-4000-8000-000000000002';
select public._t_as('dc000000-0000-4000-8000-000000000002');
set role authenticated;
do $$ declare r jsonb; begin
  r := public.get_my_league();
  if r->>'status' <> 'active' then raise exception 'late username not joined: %', r; end if;
  if (r->'cohort'->'members') @? '$[*].user_id' then raise exception 'roster leaks user ids'; end if;
  if (select count(*) from jsonb_array_elements(r->'cohort'->'members') m where (m->>'is_me')::bool) <> 1 then
    raise exception 'is_me must mark exactly one row';
  end if;
end $$;
reset role;
-- Hidden visibility: no join.
select public._t_user(3, 'hidden_hugo') is not null;
update public.profiles set leaderboard_visibility = 'hidden' where id = 'dc000000-0000-4000-8000-000000000003';
insert into public.xp_events (id, user_id, amount, source, occurred_at) values ('t3', 'dc000000-0000-4000-8000-000000000003', 20, 'practice', now());
do $$ begin
  if exists (select 1 from public.league_memberships where user_id = 'dc000000-0000-4000-8000-000000000003') then
    raise exception 'a hidden user joined a league';
  end if;
end $$;
-- Cap 30: 31 more Bronze learners open a second cohort.
do $$ declare i int; begin
  for i in 10..40 loop
    perform public._t_user(i, 'bronze_' || i);
    insert into public.xp_events (id, user_id, amount, source, occurred_at)
    values ('b' || i, ('dc000000-0000-4000-8000-' || lpad(i::text, 12, '0'))::uuid, 10 + i, 'practice', now());
  end loop;
  if (select max(member_count) from public.league_cohorts where week_key = public._league_week_key(now())) > 30 then
    raise exception 'a cohort went over 30';
  end if;
  if (select count(*) from public.league_cohorts where week_key = public._league_week_key(now()) and tier = 'bronze') <> 2 then
    raise exception 'expected two bronze cohorts';
  end if;
end $$;
-- Adjacent merge: the first Silver learner of the week joins a small Bronze cohort
-- (the second bronze cohort has 3 members) instead of being alone.
select public._t_user(50, 'silver_sam') is not null;
update public.profiles set league_tier = 'silver' where id = 'dc000000-0000-4000-8000-000000000050';
insert into public.xp_events (id, user_id, amount, source, occurred_at) values ('s50', 'dc000000-0000-4000-8000-000000000050', 30, 'practice', now());
do $$ begin
  if (select pool_tier || '/' || standing_tier from public.league_memberships where user_id = 'dc000000-0000-4000-8000-000000000050') <> 'bronze/silver' then
    raise exception 'silver learner did not merge into the small bronze cohort';
  end if;
end $$;

-- Finalization of a past week (and a missed cron): two past weeks, finalized in order.
do $$ declare i int; v_then timestamptz := now() - interval '14 days'; v_then2 timestamptz := now() - interval '7 days'; u uuid; begin
  for i in 60..69 loop
    u := public._t_user(i, 'past_' || i);
    update public.profiles set league_tier = 'silver' where id = u;
    perform public._league_join(u, v_then, true);
    -- 60 earns most ... 67 least; 68 and 69 earn nothing (left out of the ranking)
    if i <= 67 then
      insert into public.xp_events (id, user_id, amount, source, occurred_at, created_at)
      values ('past' || i, u, (70 - i) * 20, 'practice', v_then, now());
    end if;
  end loop;
  -- one of them is also in the following (also past) week. Inserted directly, as the old
  -- Monday build did, so neither past week is finalized yet: a missed cron.
  insert into public.league_cohorts (id, tier, week_key, member_count)
  values ('dc0c0000-0000-4000-8000-000000000001', 'gold', public._league_week_key(v_then2), 1);
  insert into public.league_memberships (cohort_id, user_id, week_key, pool_tier, standing_tier)
  values ('dc0c0000-0000-4000-8000-000000000001', 'dc000000-0000-4000-8000-000000000060',
          public._league_week_key(v_then2), 'gold', 'gold');
  insert into public.xp_events (id, user_id, amount, source, occurred_at, created_at)
  values ('past60b', 'dc000000-0000-4000-8000-000000000060', 200, 'practice', v_then2, now());
end $$;
do $$ declare r jsonb; begin
  r := public.assign_weekly_league_cohorts();
  if jsonb_array_length(r->'finalized_weeks') <> 2 then raise exception 'expected both past weeks finalized: %', r; end if;
end $$;
do $$ declare w text := public._league_week_key(now() - interval '14 days'); begin
  -- 8 active members: promote floor(1.6) = 1, demote 1 (rank 8); 68/69 unranked, unchanged
  if (select count(*) from public.league_memberships where week_key = w and promoted) <> 1 then raise exception 'expected 1 promotion'; end if;
  if (select count(*) from public.league_memberships where week_key = w and demoted) <> 1 then raise exception 'expected 1 demotion'; end if;
  if (select rank_in_cohort from public.league_memberships where week_key = w and user_id = 'dc000000-0000-4000-8000-000000000068') is not null then
    raise exception 'a zero-XP member was ranked';
  end if;
  if (select league_tier from public.profiles where id = 'dc000000-0000-4000-8000-000000000067') <> 'bronze' then
    raise exception 'bottom member not demoted to bronze';
  end if;
  if (select league_tier from public.profiles where id = 'dc000000-0000-4000-8000-000000000068') <> 'silver' then
    raise exception 'zero-XP member moved tier';
  end if;
  -- 60 was promoted in week 1 (silver -> gold) and its week-2 cohort of one promotes again
  -- only with 150+ XP (it had 200): gold -> platinum.
  if (select league_tier from public.profiles where id = 'dc000000-0000-4000-8000-000000000060') <> 'platinum' then
    raise exception 'expected 60 at platinum after two promotions, got %', (select league_tier from public.profiles where id = 'dc000000-0000-4000-8000-000000000060');
  end if;
  -- idempotent: a second run finalizes nothing
  if (public.assign_weekly_league_cohorts()->>'finalized_count')::int <> 0 then raise exception 'second run re-finalized'; end if;
end $$;
-- the Monday zones are what the screen is told
do $$ declare z record; begin
  select * into z from public._league_zones(30);
  if z.promote_count <> 6 or z.demote_count <> 6 then raise exception 'zones(30) wrong'; end if;
  select * into z from public._league_zones(4);
  if z.promote_count <> 1 or z.demote_count <> 0 or z.promote_min_xp <> 50 then raise exception 'zones(4) wrong'; end if;
  select * into z from public._league_zones(1);
  if z.promote_count <> 1 or z.promote_min_xp <> 150 then raise exception 'zones(1) wrong'; end if;
end $$;
-- last_week reaches the screen
select public._t_as('dc000000-0000-4000-8000-000000000067');
set role authenticated;
do $$ declare r jsonb; begin
  r := public.get_my_league();
  if not (r->'last_week'->>'demoted')::bool then raise exception 'last_week missing: %', r; end if;
  if r->>'tier' <> 'bronze' then raise exception 'tier not bronze after demotion'; end if;
  if r->>'status' <> 'not_joined' then raise exception 'expected not_joined (no XP this week): %', r; end if;
end $$;
-- client roles cannot reach the internals
select public._t_expect_error($$select public._league_join(auth.uid(), now(), true)$$, 'permission denied');
select public._t_expect_error($$select public.assign_weekly_league_cohorts()$$, 'permission denied');
select public._t_expect_error($$select public.reset_league_week('2026-W01')$$, 'permission denied');
select public._t_expect_error($$select * from public.league_cohorts$$, 'permission denied');
reset role;
set role anon;
select public._t_expect_error($$select public.get_my_league()$$, 'permission denied');
reset role;
do $$ begin
  if to_regclass('public.league_cohort_roster') is not null or to_regclass('public.daily_challenge_leaderboard') is not null then
    raise exception 'the definer-rights views are still there';
  end if;
end $$;

-- ═══ Part 3: Daily Challenge ══════════════════════════════════════════════
select public._t_user(100, 'dc_amelie') is not null;
select public._t_user(101, 'dc_speedy') is not null;
select public._t_user(102, 'dc_hidden') is not null;
update public.profiles set leaderboard_visibility = 'hidden' where id = 'dc000000-0000-4000-8000-000000000102';
select public._t_as('dc000000-0000-4000-8000-000000000100');
set role authenticated;
select public._t_expect_error($$select * from public.daily_challenge_bank$$, 'permission denied');
select public._t_expect_error($$select * from public.daily_challenge_runs$$, 'permission denied');
do $$ declare r jsonb; begin
  r := public.daily_challenge_today();
  if r->'run' <> 'null'::jsonb or jsonb_array_length(r->'items') <> 0 then raise exception 'items shown before start: %', r; end if;
  if (r->>'item_count')::int <> 8 then raise exception 'expected 8 items'; end if;
  r := public.daily_challenge_start();
  if jsonb_array_length(r->'items') <> 8 then raise exception 'start did not reveal 8 items'; end if;
  if exists (select 1 from jsonb_array_elements(r->'items') it where it->'solution' <> 'null'::jsonb) then
    raise exception 'a solution was revealed before answering';
  end if;
  if r::text like '%accepted_orders%' or r::text like '%tile_keys%' then raise exception 'answer key leaked'; end if;
end $$;
reset role;
do $$ begin
  if (select count(distinct x) from public.daily_challenge_sets s, unnest(s.item_ids) x
      where s.challenge_date = (now() at time zone 'utc')::date) <> 8 then raise exception 'set items not distinct'; end if;
end $$;
-- a fair run: started two minutes ago
update public.daily_challenge_runs set started_at = now() - interval '2 minutes'
where user_id = 'dc000000-0000-4000-8000-000000000100';
create table public._dc_today (d date);
insert into public._dc_today values ((now() at time zone 'utc')::date);
grant select on public._dc_today to authenticated;
create table public._dc_tiles (pos int primary key, right_tiles int[]);
insert into public._dc_tiles select p, public._t_correct_tiles((now() at time zone 'utc')::date, p) from generate_series(0, 7) p;
grant select on public._dc_tiles to authenticated;
select public._t_as('dc000000-0000-4000-8000-000000000100');
set role authenticated;
do $$ declare r jsonb; d date := (select d from public._dc_today); t int[]; i int; begin
  -- invalid answers
  perform public._t_expect_error(format('select public.daily_challenge_answer(%L, 0, ARRAY[0,0])', d), 'invalid_answer');
  perform public._t_expect_error(format('select public.daily_challenge_answer(%L, 0, ARRAY[99])', d), 'invalid_answer');
  perform public._t_expect_error(format('select public.daily_challenge_answer(%L, 8, ARRAY[0])', d), 'invalid_position');
  -- item 0 right
  r := public.daily_challenge_answer(d, 0, (select right_tiles from public._dc_tiles where pos = 0));
  if not (r->'items'->0->'answer'->>'correct')::bool then raise exception 'right answer marked wrong: %', r->'items'->0; end if;
  if r->'items'->0->'solution' = 'null'::jsonb then raise exception 'no solution after answering'; end if;
  if r->'items'->1->'solution' <> 'null'::jsonb then raise exception 'next item solution leaked'; end if;
  -- item 1 wrong (main order reversed)
  t := (select right_tiles from public._dc_tiles where pos = 1);
  r := public.daily_challenge_answer(d, 1, (select array_agg(x order by o desc) from unnest(t) with ordinality u(x, o)));
  if (r->'items'->1->'answer'->>'correct')::bool then raise exception 'reversed answer marked right'; end if;
  -- a repeat never changes the mark
  r := public.daily_challenge_answer(d, 1, t);
  if not (r->>'repeat')::bool or (r->'items'->1->'answer'->>'correct')::bool then raise exception 'second chance allowed'; end if;
  -- the rest right; the last answer finishes the run
  for i in 2..7 loop r := public.daily_challenge_answer(d, i, (select right_tiles from public._dc_tiles where pos = i)); end loop;
  if r->'run'->>'status' <> 'finished' then raise exception 'run not finished: %', r->'run'; end if;
  if (r->'run'->>'correct_count')::int <> 7 or (r->'run'->>'xp_awarded')::int <> 45 then raise exception 'score/xp wrong: %', r->'run'; end if;
  if (r->'run'->>'flagged')::bool then raise exception 'a fair run was flagged'; end if;
  if (r->'run'->>'rank')::int <> 1 then raise exception 'rank wrong: %', r->'run'; end if;
  if (r->'streak'->>'current')::int <> 1 then raise exception 'streak wrong: %', r->'streak'; end if;
  if exists (select 1 from jsonb_array_elements(r->'items') it where it->'solution' = 'null'::jsonb) then
    raise exception 'finished run does not show every solution';
  end if;
  perform public._t_expect_error(format('select public.daily_challenge_answer(%L, 0, ARRAY[0])', d), 'run_finished');
  -- a second start never restarts the clock or the run
  r := public.daily_challenge_start();
  if r->'run'->>'status' <> 'finished' then raise exception 'second start reset the run'; end if;
end $$;
reset role;
do $$ begin
  if (select amount from public.xp_events where id = 'dc000000-0000-4000-8000-000000000100:daily_challenge:' || ((now() at time zone 'utc')::date)::text) <> 45 then
    raise exception 'daily challenge XP not in the ledger';
  end if;
  -- and that XP put her in this week's league
  if not exists (select 1 from public.league_memberships where user_id = 'dc000000-0000-4000-8000-000000000100'
                 and week_key = public._league_week_key(now())) then
    raise exception 'daily challenge XP did not join the league';
  end if;
end $$;
-- a bot: everything right in milliseconds -> flagged, no XP, not on the board
select public._t_as('dc000000-0000-4000-8000-000000000101');
set role authenticated;
do $$ declare r jsonb; d date := (select d from public._dc_today); i int; begin
  perform public.daily_challenge_start();
  for i in 0..7 loop r := public.daily_challenge_answer(d, i, (select right_tiles from public._dc_tiles where pos = i)); end loop;
  if not (r->'run'->>'flagged')::bool or (r->'run'->>'xp_awarded')::int <> 0 then raise exception 'fast run not flagged: %', r->'run'; end if;
end $$;
reset role;
-- a hidden learner plays fairly
update public.daily_challenge_runs set started_at = now() - interval '1 minute' where false;
select public._t_as('dc000000-0000-4000-8000-000000000102');
set role authenticated;
select public.daily_challenge_start() is not null;
reset role;
update public.daily_challenge_runs set started_at = now() - interval '3 minutes' where user_id = 'dc000000-0000-4000-8000-000000000102';
select public._t_as('dc000000-0000-4000-8000-000000000102');
set role authenticated;
do $$ declare d date := (select d from public._dc_today); i int; begin
  for i in 0..7 loop perform public.daily_challenge_answer(d, i, (select right_tiles from public._dc_tiles where pos = i)); end loop;
end $$;
do $$ declare r jsonb; begin
  r := public.daily_challenge_board('global');
  if (select count(*) from jsonb_array_elements(r->'rows') x where (x->>'is_me')::bool) <> 1 then raise exception 'hidden learner cannot see own row'; end if;
end $$;
reset role;
select public._t_as('dc000000-0000-4000-8000-000000000100');
set role authenticated;
do $$ declare r jsonb; begin
  r := public.daily_challenge_board('global');
  if r::text like '%dc_speedy%' then raise exception 'flagged run on the board'; end if;
  if r::text like '%dc_hidden%' then raise exception 'hidden learner on the global board'; end if;
  if r::text like '%user_id%' then raise exception 'board leaks user ids'; end if;
  if (r->>'total')::int <> 1 then raise exception 'expected one visible row: %', r; end if;
  r := public.daily_challenge_board('friends');
  if (r->>'total')::int <> 1 then raise exception 'friends board should hold only me: %', r; end if;
  perform public._t_expect_error($q$select public.daily_challenge_board('everyone')$q$, 'invalid_scope');
end $$;
-- the retired exam-based path is closed to clients
select public._t_expect_error($$select public.start_daily_challenge(current_date)$$, 'permission denied');
select public._t_expect_error($$select public._daily_challenge_finish(auth.uid(), current_date)$$, 'permission denied');
reset role;
-- an abandoned run closes at the deadline with what was answered
select public._t_user(103, 'dc_leaver') is not null;
select public._t_as('dc000000-0000-4000-8000-000000000103');
set role authenticated;
do $$ declare d date := (select d from public._dc_today); begin
  perform public.daily_challenge_start();
  perform public.daily_challenge_answer(d, 0, (select right_tiles from public._dc_tiles where pos = 0));
end $$;
reset role;
update public.daily_challenge_runs set started_at = now() - interval '30 minutes' where user_id = 'dc000000-0000-4000-8000-000000000103';
update public.daily_challenge_answers set answered_at = now() - interval '29 minutes' where user_id = 'dc000000-0000-4000-8000-000000000103';
select public._t_as('dc000000-0000-4000-8000-000000000103');
set role authenticated;
do $$ declare r jsonb; begin
  r := public.daily_challenge_today();
  if r->'run'->>'status' <> 'finished' or (r->'run'->>'correct_count')::int <> 1 then raise exception 'abandoned run not closed: %', r->'run'; end if;
  if (r->'run'->>'xp_awarded')::int <> 15 then raise exception 'abandoned run XP wrong: %', r->'run'; end if;
end $$;
reset role;

-- the seed workflow builds today and tomorrow
do $$ declare r jsonb; begin
  r := public.seed_daily_challenges_batch();
  if jsonb_array_length(r->'tomorrow') <> 8 then raise exception 'tomorrow not seeded: %', r; end if;
  -- tomorrow avoids today's items while the pool allows
  if exists (select 1 from jsonb_array_elements_text(r->'tomorrow') t where t in (
      select unnest(item_ids) from public.daily_challenge_sets where challenge_date = (now() at time zone 'utc')::date)) then
    raise exception 'tomorrow repeats today';
  end if;
end $$;

drop table public._dc_today; drop table public._dc_tiles;
drop function public._t_user(int, text); drop function public._t_as(uuid);
drop function public._t_expect_error(text, text); drop function public._t_correct_tiles(date, int);
select 'ALL PASSED';
