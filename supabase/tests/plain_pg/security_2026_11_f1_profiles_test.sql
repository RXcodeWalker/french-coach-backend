-- F1 (security review 2026-11): an account cannot delete its profile and insert a new one with
-- values of its choosing, and deleting the profile does not reset the day's usage.
-- Run with run_all_migrations.mjs (README.md); ends with 'ALL PASSED'.

-- Signup creates the profile row (handle_new_user), with the safe defaults.
insert into auth.users (id, email) values ('aaaaaaaa-0000-4000-8000-000000000001', 'kid@example.test');
do $$ begin
  if (select consent_status from public.profiles where id = 'aaaaaaaa-0000-4000-8000-000000000001') is distinct from '13_plus_not_required' then
    raise exception 'F1: signup did not create the profile row';
  end if;
end $$;

select set_config('request.jwt.claim.sub', 'aaaaaaaa-0000-4000-8000-000000000001', false);
set role authenticated;
select public.set_age_band('under_13')->>'consent_status' as status_after_set_age_band;
reset role;
select public.consume_ai_quota('aaaaaaaa-0000-4000-8000-000000000001', 'learn_answer', 'k' || g)->>'granted' as granted
  from generate_series(1, 4) g;

-- The attack: delete the profile, insert a new one.
set role authenticated;
select public.delete_my_account('aaaaaaaa-0000-4000-8000-000000000001'::uuid)->>'ok' as deleted_own_profile;
do $$ begin
  insert into public.profiles (id, consent_status, age_band, xp_baseline, username)
    values ('aaaaaaaa-0000-4000-8000-000000000001', 'granted', 'under_13', 2000000000, 'admin');
  raise exception 'F1: the account inserted its own profiles row';
exception when insufficient_privilege then null;
end $$;
-- An upsert (what the app used to send) is an INSERT too.
do $$ begin
  insert into public.profiles (id, achievements) values ('aaaaaaaa-0000-4000-8000-000000000001', '{}')
    on conflict (id) do update set achievements = excluded.achievements;
  raise exception 'F1: an upsert still reaches profiles';
exception when insufficient_privilege then null;
end $$;
reset role;

do $$ begin
  if exists (select 1 from public.profiles where id = 'aaaaaaaa-0000-4000-8000-000000000001') then
    raise exception 'F1: a profiles row came back after deletion';
  end if;
  if (select count(*) from public.ai_usage_grants where user_id = 'aaaaaaaa-0000-4000-8000-000000000001' and feature = 'learn_answer') < 3 then
    raise exception 'F1: deleting the profile wiped the day''s learn_answer grants';
  end if;
  if exists (select 1 from pg_policies where schemaname = 'public' and tablename = 'profiles' and cmd = 'INSERT') then
    raise exception 'F1: an INSERT policy on profiles is still there';
  end if;
  if has_table_privilege('anon', 'public.profiles', 'INSERT') or has_table_privilege('authenticated', 'public.profiles', 'INSERT') then
    raise exception 'F1: a client role still holds INSERT on profiles';
  end if;
end $$;

-- The app's own profile writes are column UPDATEs, which still work.
insert into auth.users (id, email) values ('aaaaaaaa-0000-4000-8000-000000000002', 'writer@example.test');
select set_config('request.jwt.claim.sub', 'aaaaaaaa-0000-4000-8000-000000000002', false);
set role authenticated;
update public.profiles set achievements = '{first_session}', daily_goal = 3, notify_streak = false,
  notify_daily_goal = false, migration_version = 1 where id = 'aaaaaaaa-0000-4000-8000-000000000002';
reset role;
do $$ begin
  if (select daily_goal from public.profiles where id = 'aaaaaaaa-0000-4000-8000-000000000002') is distinct from 3 then
    raise exception 'F1: the app''s profile UPDATE no longer applies';
  end if;
end $$;

-- Guardian revocation still erases the child, usage included.
insert into auth.users (id, email) values ('aaaaaaaa-0000-4000-8000-000000000003', 'kid3@example.test');
update public.profiles set age_band = 'under_13', consent_status = 'granted' where id = 'aaaaaaaa-0000-4000-8000-000000000003';
insert into public.guardian_consents (child_user_id, guardian_email, token_hash, granted_at)
  values ('aaaaaaaa-0000-4000-8000-000000000003', 'mum@example.test',
          encode(extensions.digest(repeat('ab', 32), 'sha256'), 'hex'), now());
select public.consume_ai_quota('aaaaaaaa-0000-4000-8000-000000000003', 'learn_answer', 'r1')->>'granted' as child_granted;
select set_config('request.jwt.claim.sub', '', false);
set role anon;
select public.revoke_guardian_consent(repeat('ab', 32))->>'ok' as revoked;
reset role;
do $$ begin
  if exists (select 1 from public.profiles where id = 'aaaaaaaa-0000-4000-8000-000000000003')
     or exists (select 1 from public.ai_usage_grants where user_id = 'aaaaaaaa-0000-4000-8000-000000000003') then
    raise exception 'F1: guardian revocation left the child''s profile or usage behind';
  end if;
end $$;

-- Deleting the login still cascades the grants away.
delete from auth.users where id = 'aaaaaaaa-0000-4000-8000-000000000001';
do $$ begin
  if exists (select 1 from public.ai_usage_grants where user_id = 'aaaaaaaa-0000-4000-8000-000000000001') then
    raise exception 'F1: grants outlived the login';
  end if;
end $$;

select 'ALL PASSED';
