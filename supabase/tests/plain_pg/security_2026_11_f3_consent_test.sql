-- F3 (security review 2026-11): a pending under-13 account cannot grant its own guardian consent.
-- Run with run_all_migrations.mjs (README.md); ends with 'ALL PASSED'.
insert into auth.users (id, email) values ('bbbbbbbb-0000-4000-8000-000000000002', 'kid2@example.test');
create table public._t (token text);
grant all on public._t to authenticated, anon;

select set_config('request.jwt.claim.sub', 'bbbbbbbb-0000-4000-8000-000000000002', false);
set role authenticated;
select public.set_age_band('under_13')->>'consent_status' as start_status;
insert into public._t select public.request_guardian_consent('nobody@example.test')->>'token';

-- The child's own session cannot use the token.
do $$ begin
  perform public.grant_guardian_consent((select token from public._t), 'parent');
  raise exception 'F3: the child granted their own guardian consent';
exception when insufficient_privilege then null;
end $$;
reset role;
do $$ begin
  if (select consent_status from public.profiles where id = 'bbbbbbbb-0000-4000-8000-000000000002') <> 'pending' then
    raise exception 'F3: consent_status moved off pending';
  end if;
end $$;

-- A guardian with no session (the emailed link) still can, and only once.
select set_config('request.jwt.claim.sub', '', false);
set role anon;
select public.grant_guardian_consent((select token from public._t), 'parent')->>'ok' as guardian_granted;
do $$ begin
  perform public.grant_guardian_consent((select token from public._t), 'parent');
  raise exception 'F3: a used token granted again';
exception when invalid_parameter_value then null;
end $$;
reset role;
do $$ begin
  if (select consent_status from public.profiles where id = 'bbbbbbbb-0000-4000-8000-000000000002') <> 'granted' then
    raise exception 'F3: the guardian''s confirmation did not apply';
  end if;
end $$;

-- Known, owner-accepted behaviour (docs/systems/child-safety-consent.md): correcting the age band
-- from under 13 to 13+ takes the account straight to 13_plus_not_required. Pinned so a change is
-- deliberate, not a pass of the security review.
insert into auth.users (id, email) values ('bbbbbbbb-0000-4000-8000-000000000003', 'kid3@example.test');
select set_config('request.jwt.claim.sub', 'bbbbbbbb-0000-4000-8000-000000000003', false);
set role authenticated;
select public.set_age_band('under_13')->>'consent_status' as start_status;
select public.correct_age_band('13_plus')->>'consent_status' as accepted_correction_status;
reset role;
do $$ begin
  if (select consent_status from public.profiles where id = 'bbbbbbbb-0000-4000-8000-000000000003') <> '13_plus_not_required' then
    raise exception 'F3: correct_age_band changed; update child-safety-consent.md and this test together';
  end if;
end $$;

drop table public._t;
select 'ALL PASSED';
