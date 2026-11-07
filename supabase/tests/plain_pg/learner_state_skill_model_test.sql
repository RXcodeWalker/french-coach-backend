-- Behaviour spec for 20261106100000_learner_state_skill_model.sql. Run after prelude.sql,
-- 20261103150000_learner_state.sql and that migration (README.md); ends with 'ALL PASSED'.
\set ON_ERROR_STOP 1
insert into auth.users values ('00000000-0000-0000-0000-0000000000a1','a@x'),('00000000-0000-0000-0000-0000000000b2','b@x');
insert into public.profiles select id, split_part(email,'@',1) from auth.users;
create or replace function pg_temp.as_user(u text) returns void language plpgsql as $$ begin perform set_config('request.jwt.claim.sub', u, false); end $$;
create or replace function pg_temp.expect_error(sql text, needle text) returns void language plpgsql as $$
begin
  begin execute sql; exception when others then
    if position(needle in sqlerrm) > 0 then return; end if;
    raise exception 'expected % got %', needle, sqlerrm;
  end;
  raise exception 'expected error % but succeeded: %', needle, sql;
end $$;
grant execute on function pg_temp.as_user(text) to authenticated;
grant execute on function pg_temp.expect_error(text,text) to authenticated;

set role authenticated;
select pg_temp.as_user('00000000-0000-0000-0000-0000000000a1');
-- the new key is accepted, upserted like the others
insert into public.learner_state (user_id, key, value)
  values ('00000000-0000-0000-0000-0000000000a1', 'skill_model', '{"version":"learner-model-v1","devices":{},"legacy":null}');
insert into public.learner_state (user_id, key, value)
  values ('00000000-0000-0000-0000-0000000000a1', 'skill_model', '{"version":"learner-model-v1","devices":{"d1":{"updatedAt":1,"skills":{}}},"legacy":null}')
  on conflict (user_id, key) do update set value = excluded.value;
select (value->'devices' ? 'd1') as upserted from public.learner_state where key = 'skill_model';
-- the old keys still work
insert into public.learner_state (user_id, key, value) values ('00000000-0000-0000-0000-0000000000a1', 'learn_settings', '{}');
-- the list stays closed
select pg_temp.expect_error($q$insert into public.learner_state (user_id, key, value) values ('00000000-0000-0000-0000-0000000000a1', 'notebook', '{}')$q$, 'learner_state_key_check');
-- shape and size checks still apply to the new key
select pg_temp.expect_error($q$insert into public.learner_state (user_id, key, value) values ('00000000-0000-0000-0000-0000000000a1', 'review_pool', '[]')$q$, 'check');
-- owner-only: another account can neither read nor write this row
select pg_temp.as_user('00000000-0000-0000-0000-0000000000b2');
select count(*) = 0 as cannot_read from public.learner_state where user_id = '00000000-0000-0000-0000-0000000000a1';
select pg_temp.expect_error($q$insert into public.learner_state (user_id, key, value) values ('00000000-0000-0000-0000-0000000000a1', 'skill_model', '{}')$q$, 'row-level security');
reset role;
do $$ begin
  if (select count(*) from public.learner_state where key = 'skill_model') <> 1 then raise exception 'expected one skill_model row'; end if;
  if not (select (value->'devices' ? 'd1') from public.learner_state where key = 'skill_model') then raise exception 'upsert lost'; end if;
end $$;
select 'ALL PASSED';
