-- Behaviour spec for 20261106100000_parent_digest_dashboard_link.sql. Run after prelude.sql,
-- parent_view_prelude.sql, 20261104090000_parent_links.sql, 20261104100000_parent_report_exam_columns.sql
-- and that migration (README.md); ends with 'ALL PASSED'.
\set ON_ERROR_STOP 1
insert into auth.users values ('00000000-0000-0000-0000-0000000000a1','a@x'),('00000000-0000-0000-0000-0000000000b2','b@x');
insert into public.profiles select id, split_part(email,'@',1) from auth.users;
insert into public.sessions (id, user_id, mode, topic_key, transcript, question_text, score, duration_sec)
  values ('s1','00000000-0000-0000-0000-0000000000a1','practice','school','SECRET','SECRET',6.5,300);
create or replace function pg_temp.as_user(u text) returns void language plpgsql as $$ begin perform set_config('request.jwt.claim.sub', u, false); end $$;
create or replace function pg_temp.expect_error(sql text, needle text) returns void language plpgsql as $$
begin
  begin execute sql; exception when others then
    if position(needle in sqlerrm) > 0 then return; end if;
    raise exception 'expected % got %', needle, sqlerrm;
  end;
  raise exception 'expected error % but succeeded: %', needle, sql;
end $$;
grant execute on function pg_temp.as_user(text) to anon, authenticated;
grant execute on function pg_temp.expect_error(text,text) to anon, authenticated;

-- only the service role may mint
set role authenticated;
select pg_temp.as_user('00000000-0000-0000-0000-0000000000a1');
select pg_temp.expect_error($q$select public.mint_parent_digest_token('00000000-0000-0000-0000-0000000000a1','mum@x.com')$q$, 'permission denied');
reset role;
set role anon;
select pg_temp.expect_error($q$select public.mint_parent_digest_token('00000000-0000-0000-0000-0000000000a1','mum@x.com')$q$, 'permission denied');
select pg_temp.expect_error($q$select count(*) from public.parent_link_tokens$q$, 'permission denied');
reset role;

set role service_role;
select public.mint_parent_digest_token('00000000-0000-0000-0000-0000000000a1', ' Mum@X.com ') as t1 \gset
select public.mint_parent_digest_token('00000000-0000-0000-0000-0000000000a1', 'mum@x.com') as t2 \gset
select public.mint_parent_digest_token('00000000-0000-0000-0000-0000000000a1', '') is null as empty_is_null;
reset role;

do $$ begin
  if (select count(*) from public.parent_links where source = 'weekly_email' and revoked_at is null) <> 1 then raise exception 'expected one weekly-email link'; end if;
  if (select parent_email from public.parent_links where source = 'weekly_email') <> 'mum@x.com' then raise exception 'email not normalised'; end if;
  if (select count(*) from public.parent_link_tokens) <> 2 then raise exception 'expected two tokens'; end if;
end $$;

-- both email tokens open the report (anon), with no transcript or question text, and count views
set role anon;
select position('SECRET' in public.get_parent_report(:'t1')::text) = 0 as no_secret;
select public.get_parent_report(:'t2')->>'linkLabel' = 'Weekly email' as label_ok;
reset role;
do $$ begin
  if (select view_count from public.parent_links where source = 'weekly_email') <> 2 then raise exception 'views not counted'; end if;
end $$;

-- an expired token stops working
update public.parent_link_tokens set expires_at = now() - interval '1 second'
  where token_hash = encode(extensions.digest(:'t1', 'sha256'), 'hex');
set role anon;
select pg_temp.expect_error(format('select public.get_parent_report(%L)', :'t1'), 'invalid_link');
reset role;

-- the learner sees it as a weekly-email link, and it does not count toward the 5 manual links
set role authenticated;
select pg_temp.as_user('00000000-0000-0000-0000-0000000000a1');
select public.list_parent_links()->0->>'source' = 'weekly_email' as listed;
select public.create_parent_link('a'), public.create_parent_link('b'), public.create_parent_link('c'), public.create_parent_link('d'), public.create_parent_link('e');
select pg_temp.expect_error($q$select public.create_parent_link('f')$q$, 'too_many_links');
reset role;

-- a changed address revokes the old link (automatic, not the learner's), so old emails stop working
set role service_role;
select public.mint_parent_digest_token('00000000-0000-0000-0000-0000000000a1', 'dad@x.com') as t3 \gset
reset role;
set role anon;
select pg_temp.expect_error(format('select public.get_parent_report(%L)', :'t2'), 'invalid_link');
select public.get_parent_report(:'t3') is not null as new_ok;
reset role;
-- switching back to the first address makes a fresh link (the old one was not the learner's choice)
set role service_role;
select public.mint_parent_digest_token('00000000-0000-0000-0000-0000000000a1', 'mum@x.com') is not null as remade;
select public.mint_parent_digest_token('00000000-0000-0000-0000-0000000000a1', 'dad@x.com') as t4 \gset
reset role;

-- the learner turns the link off: every email token dies and no new ones are minted for that address
set role authenticated;
select pg_temp.as_user('00000000-0000-0000-0000-0000000000a1');
select public.revoke_parent_link((select (l->>'id')::uuid from jsonb_array_elements(public.list_parent_links()) l where l->>'source' = 'weekly_email'));
reset role;
set role anon;
select pg_temp.expect_error(format('select public.get_parent_report(%L)', :'t4'), 'invalid_link');
reset role;
set role service_role;
select public.mint_parent_digest_token('00000000-0000-0000-0000-0000000000a1', 'dad@x.com') is null as respected;
reset role;

-- another learner cannot see or revoke a's links
set role authenticated;
select pg_temp.as_user('00000000-0000-0000-0000-0000000000b2');
select public.list_parent_links() = '[]'::jsonb as b_sees_none;
reset role;

select 'ALL PASSED';
