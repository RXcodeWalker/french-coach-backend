-- F9 (security review 2026-11): backdating occurred_at cannot multiply the 450-a-day gem cap.
-- Run with run_all_migrations.mjs (README.md); ends with 'ALL PASSED'.
insert into auth.users (id, email) values ('ffffffff-0000-4000-8000-000000000007', 'f@example.test');
create table public._r (refused int);
insert into public._r values (0);
grant all on public._r to authenticated;
select set_config('request.jwt.claim.sub', 'ffffffff-0000-4000-8000-000000000007', false);
set role authenticated;

-- One day's cap per occurred_at date still answers capped:true, never an error.
do $$ declare i int; v jsonb; begin
  for i in 1..23 loop
    v := public.mint_gems('same-day-' || i, 20, now() - interval '1 minute');
  end loop;
  if not coalesce((v->>'capped')::bool, false) then
    raise exception 'F9: the occurred_at-day cap no longer answers capped:true';
  end if;
end $$;
reset role;
delete from public.gem_events where user_id = 'ffffffff-0000-4000-8000-000000000007';
set role authenticated;

-- 30 backdated days in one sitting: at most 450 is minted, and the rest is refused with the
-- retryable daily_mint_limit (the client's queue keeps it for a later day).
do $$ declare d int; i int; begin
  for d in 0..29 loop
    for i in 1..22 loop
      begin
        perform public.mint_gems('k-' || d || '-' || i, 20, now() - make_interval(days => d) - interval '1 minute');
      exception when invalid_parameter_value then
        if sqlerrm <> 'daily_mint_limit' then raise; end if;
        update public._r set refused = refused + 1;
      end;
    end loop;
  end loop;
end $$;
reset role;
do $$ begin
  if (select sum(delta) from public.gem_events where user_id = 'ffffffff-0000-4000-8000-000000000007') > 450 then
    raise exception 'F9: more than one day''s cap was minted at once (%)',
      (select sum(delta) from public.gem_events where user_id = 'ffffffff-0000-4000-8000-000000000007');
  end if;
  if (select refused from public._r) = 0 then
    raise exception 'F9: nothing was refused with daily_mint_limit';
  end if;
end $$;

-- A replay of an already-minted key is still answered, not refused.
set role authenticated;
select (public.mint_gems('k-0-1', 20, now() - interval '1 minute')->>'replayed') as replay_answered;
reset role;

-- Rows minted on an earlier server day do not count against today's.
update public.gem_events set created_at = now() - interval '2 days', occurred_at = now() - interval '2 days'
  where user_id = 'ffffffff-0000-4000-8000-000000000007';
set role authenticated;
do $$ begin
  if public.mint_gems('next-day-1', 20, now() - interval '1 minute') ? 'capped' then
    raise exception 'F9: an earlier server day''s mints blocked today''s';
  end if;
end $$;
reset role;

drop table public._r;
select 'ALL PASSED';
