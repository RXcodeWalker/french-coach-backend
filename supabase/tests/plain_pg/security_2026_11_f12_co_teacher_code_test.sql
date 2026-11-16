-- F12 (security review 2026-11): a co-teacher the owner removed cannot walk back in with the old code.
-- Run with run_all_migrations.mjs (README.md); ends with 'ALL PASSED'.
insert into auth.users (id, email) values
  ('11111111-0000-4000-8000-0000000000a1', 'owner@example.test'),
  ('11111111-0000-4000-8000-0000000000a2', 'co@example.test'),
  ('11111111-0000-4000-8000-0000000000a3', 'co2@example.test');
insert into public.teacher_profiles (user_id, display_name) values
  ('11111111-0000-4000-8000-0000000000a1', 'Owner'),
  ('11111111-0000-4000-8000-0000000000a2', 'Co'),
  ('11111111-0000-4000-8000-0000000000a3', 'Co2');
create table public._c (class_id uuid, code text);
grant all on public._c to authenticated;

select set_config('request.jwt.claim.sub', '11111111-0000-4000-8000-0000000000a1', false);
set role authenticated;
insert into public._c (class_id) select (public.create_class('9B French')->>'class_id')::uuid;
update public._c set code = public.rotate_co_teacher_code(class_id)->>'co_teacher_code';
select set_config('request.jwt.claim.sub', '11111111-0000-4000-8000-0000000000a2', false);
select public.join_class_as_teacher((select code from public._c))->>'already_teacher' as co_joined;
select set_config('request.jwt.claim.sub', '11111111-0000-4000-8000-0000000000a3', false);
select public.join_class_as_teacher((select code from public._c))->>'already_teacher' as co2_joined;

-- A co-teacher leaving on their own does not clear the code.
do $$ begin
  if (public.remove_class_teacher((select class_id from public._c), '11111111-0000-4000-8000-0000000000a3')->>'co_teacher_code_cleared')::bool then
    raise exception 'F12: a co-teacher leaving cleared the owner''s code';
  end if;
end $$;

-- The owner removes a co-teacher: the code is cleared and the old code no longer works.
select set_config('request.jwt.claim.sub', '11111111-0000-4000-8000-0000000000a1', false);
do $$ begin
  if not (public.remove_class_teacher((select class_id from public._c), '11111111-0000-4000-8000-0000000000a2')->>'co_teacher_code_cleared')::bool then
    raise exception 'F12: removing a co-teacher did not report the code as cleared';
  end if;
  if public.teacher_class_teachers((select class_id from public._c))->>'co_teacher_code' is not null then
    raise exception 'F12: the owner still sees a co-teacher code after the removal';
  end if;
end $$;
select set_config('request.jwt.claim.sub', '11111111-0000-4000-8000-0000000000a2', false);
do $$ begin
  perform public.join_class_as_teacher((select code from public._c));
  raise exception 'F12: the removed co-teacher rejoined with the same code';
exception when insufficient_privilege or invalid_parameter_value then null;
end $$;
do $$ begin
  perform public.teacher_class_roster((select class_id from public._c));
  raise exception 'F12: the removed co-teacher can still read the roster';
exception when insufficient_privilege or invalid_parameter_value then null;
end $$;

-- The owner can invite again with a new code.
select set_config('request.jwt.claim.sub', '11111111-0000-4000-8000-0000000000a1', false);
update public._c set code = public.rotate_co_teacher_code(class_id)->>'co_teacher_code';
select set_config('request.jwt.claim.sub', '11111111-0000-4000-8000-0000000000a2', false);
select public.join_class_as_teacher((select code from public._c))->>'already_teacher' as rejoined_with_new_code;
reset role;

drop table public._c;
select 'ALL PASSED';
