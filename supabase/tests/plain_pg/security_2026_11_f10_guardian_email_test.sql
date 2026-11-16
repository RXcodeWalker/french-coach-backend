-- F10 (security review 2026-11): a login whose email matches the guardian's but is NOT confirmed
-- cannot export or delete the child's account; a confirmed one can.
-- Run with run_all_migrations.mjs (README.md); ends with 'ALL PASSED'.
insert into auth.users (id, email) values ('cccccccc-0000-4000-8000-000000000004', 'child@example.test');
update public.profiles set age_band = 'under_13', consent_status = 'granted' where id = 'cccccccc-0000-4000-8000-000000000004';
insert into public.guardian_consents (child_user_id, guardian_email, token_hash, granted_at)
  values ('cccccccc-0000-4000-8000-000000000004', 'mum@example.test', 'x', now());
insert into public.sessions (id, user_id, mode, transcript)
  values ('s-child-1', 'cccccccc-0000-4000-8000-000000000004', 'practice', 'Je m''appelle Lea.');
insert into auth.users (id, email, email_confirmed_at) values
  ('dddddddd-0000-4000-8000-000000000005', 'mum@example.test', null),
  ('dddddddd-0000-4000-8000-000000000006', 'MUM@example.test', now());

select set_config('request.jwt.claim.sub', 'dddddddd-0000-4000-8000-000000000005', false);
set role authenticated;
do $$ begin
  perform public.export_my_data('cccccccc-0000-4000-8000-000000000004'::uuid);
  raise exception 'F10: an unconfirmed login exported the child''s data';
exception when insufficient_privilege then null;
end $$;
do $$ begin
  perform public.delete_my_account('cccccccc-0000-4000-8000-000000000004'::uuid);
  raise exception 'F10: an unconfirmed login deleted the child''s account';
exception when insufficient_privilege then null;
end $$;

-- The confirmed guardian still can.
select set_config('request.jwt.claim.sub', 'dddddddd-0000-4000-8000-000000000006', false);
do $$ begin
  if public.export_my_data('cccccccc-0000-4000-8000-000000000004'::uuid)->'sessions'->0->>'id' is distinct from 's-child-1' then
    raise exception 'F10: the confirmed guardian''s export is missing the child''s session';
  end if;
end $$;
select public.delete_my_account('cccccccc-0000-4000-8000-000000000004'::uuid)->>'ok' as confirmed_guardian_deleted;
reset role;
do $$ begin
  if exists (select 1 from public.profiles where id = 'cccccccc-0000-4000-8000-000000000004') then
    raise exception 'F10: the confirmed guardian''s delete did not apply';
  end if;
end $$;

select 'ALL PASSED';
