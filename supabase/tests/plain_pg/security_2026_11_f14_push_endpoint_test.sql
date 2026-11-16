-- F14 (security review 2026-11): a learner cannot store a non-https push endpoint for the hourly
-- job to fetch. Run with run_all_migrations.mjs (README.md); ends with 'ALL PASSED'.
insert into auth.users (id, email) values ('99999999-0000-4000-8000-000000000001', 'p@example.test');
select set_config('request.jwt.claim.sub', '99999999-0000-4000-8000-000000000001', false);
set role authenticated;
insert into public.push_subscriptions (user_id, endpoint, p256dh, auth_key, timezone)
  values ('99999999-0000-4000-8000-000000000001', 'https://fcm.googleapis.com/fcm/send/abc123', 'p', 'a', 'Asia/Kolkata');
do $$ declare bad text; begin
  foreach bad in array array['http://169.254.169.254/latest/meta-data', 'ftp://x.example/y', 'https://', 'https://slow.example/ x',
                             'https://x.example/' || repeat('a', 2100)] loop
    begin
      insert into public.push_subscriptions (user_id, endpoint, p256dh, auth_key, timezone)
        values ('99999999-0000-4000-8000-000000000001', bad, 'p', 'a', 'Asia/Kolkata');
      raise exception 'F14: stored the push endpoint %', left(bad, 60);
    exception when check_violation then null;
    end;
  end loop;
end $$;
reset role;
do $$ begin
  if (select count(*) from public.push_subscriptions where user_id = '99999999-0000-4000-8000-000000000001') <> 1 then
    raise exception 'F14: the real endpoint was not stored';
  end if;
end $$;
select 'ALL PASSED';
