-- Behaviour spec for 20261105120000_co_teachers_and_multi_class.sql. Run after prelude.sql and both
-- teacher migrations (README.md); ends with 'ALL PASSED'.
\set ON_ERROR_STOP 1
insert into auth.users values ('00000000-0000-0000-0000-00000000000a','t@x'),('00000000-0000-0000-0000-00000000000b','co@x'),('00000000-0000-0000-0000-00000000000c','t3@x'),('00000000-0000-0000-0000-0000000000a1','a@x');
insert into public.profiles select id, split_part(email,'@',1) from auth.users;
insert into public.teacher_invites (code, max_uses) values ('PILOT', 3);
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
create table pg_temp.ids (k text primary key, v text);
grant all on pg_temp.ids to authenticated;

set role authenticated;
select pg_temp.as_user('00000000-0000-0000-0000-00000000000a');
select redeem_teacher_invite('PILOT','Ms Owner');
insert into pg_temp.ids select 'c1', create_class('Y11 French')->>'class_id';
insert into pg_temp.ids select 'c2', create_class('Y10 French')->>'class_id';
insert into pg_temp.ids select 'cc', rotate_co_teacher_code((select v::uuid from pg_temp.ids where k='c1'))->>'co_teacher_code';
select (select v from pg_temp.ids where k='cc') ~ '^T-[A-HJKMNP-Z2-9]{6}$' as code_ok;

-- a non-teacher can't use the co-teacher code
select pg_temp.as_user('00000000-0000-0000-0000-0000000000a1');
select pg_temp.expect_error(format('select join_class_as_teacher(%L)', (select v from pg_temp.ids where k='cc')), 'not_a_teacher');

-- the co-teacher joins and sees the class like the owner
select pg_temp.as_user('00000000-0000-0000-0000-00000000000b');
select redeem_teacher_invite('PILOT','Mr Co');
select (join_class_as_teacher(lower((select v from pg_temp.ids where k='cc')))->>'already_teacher')::bool = false as joined_as_teacher;
select count(*) = 1 as co_sees_one_class, min(my_role) = 'co_teacher' as role_ok from my_teaching_classes();
select count(*) = 1 as co_reads_classes_table from public.classes;
select pg_temp.expect_error($$select co_teacher_code from public.classes$$, 'permission denied');
select (teacher_class_teachers((select v::uuid from pg_temp.ids where k='c1'))->>'co_teacher_code') is null as code_hidden_from_co;
select jsonb_array_length(teacher_class_teachers((select v::uuid from pg_temp.ids where k='c1'))->'teachers') = 2 as two_teachers;
select count(*) = 0 as roster_ok from teacher_class_roster((select v::uuid from pg_temp.ids where k='c1'));
-- co-teacher can't archive, rotate the co-teacher code, or remove the owner; can't see the owner's other class
select pg_temp.expect_error(format('select archive_class(%L)', (select v from pg_temp.ids where k='c1')), 'owner_only');
select pg_temp.expect_error(format('select rotate_co_teacher_code(%L)', (select v from pg_temp.ids where k='c1')), 'owner_only');
select pg_temp.expect_error(format('select remove_class_teacher(%L, %L)', (select v from pg_temp.ids where k='c1'), '00000000-0000-0000-0000-00000000000a'), 'owner_only');
select pg_temp.expect_error(format('select * from teacher_class_roster(%L)', (select v from pg_temp.ids where k='c2')), 'forbidden');
-- but can set homework and rotate the student join code
select (create_assignment((select v::uuid from pg_temp.ids where k='c1'), 'mock_exam', 'Mock', null, null, null, now() + interval '2 days', null)->>'ok')::bool as co_sets_homework;
select (rotate_class_code((select v::uuid from pg_temp.ids where k='c1'))->>'ok')::bool as co_rotates_join_code;

-- a student joins both classes
reset role;
insert into pg_temp.ids select 'j1', join_code from public.classes where id = (select v::uuid from pg_temp.ids where k='c1');
insert into pg_temp.ids select 'j2', join_code from public.classes where id = (select v::uuid from pg_temp.ids where k='c2');
set role authenticated;
select pg_temp.as_user('00000000-0000-0000-0000-0000000000a1');
select (preview_class((select v from pg_temp.ids where k='j1'))->'teacher_names') = '["Ms Owner", "Mr Co"]'::jsonb as preview_lists_teachers;
select join_class((select v from pg_temp.ids where k='j1'));
select (preview_class((select v from pg_temp.ids where k='j2'))->>'other_class_count')::int = 1 as preview_counts_other;
select join_class((select v from pg_temp.ids where k='j2'));
select count(*) = 2 as in_two_classes from my_classes();
select count(*) = 1 as sees_homework from my_assignments();
-- both teachers see the student in class 1
select pg_temp.as_user('00000000-0000-0000-0000-00000000000b');
select count(*) = 1 as co_sees_student from teacher_class_roster((select v::uuid from pg_temp.ids where k='c1'));
-- leaving one class keeps the other
select pg_temp.as_user('00000000-0000-0000-0000-0000000000a1');
select leave_class((select v::uuid from pg_temp.ids where k='c1'));
select count(*) = 1 as still_in_one, min(class_name) = 'Y10 French' as right_one from my_classes();
select (my_class()->'class'->>'class_name') = 'Y10 French' as old_my_class_ok;
-- 5-class cap
reset role;
set role authenticated;
select pg_temp.as_user('00000000-0000-0000-0000-00000000000c');
select redeem_teacher_invite('PILOT','Ms Three');
insert into pg_temp.ids select 'x'||g, create_class('C'||g)->>'class_id' from generate_series(1,5) g;
reset role;
insert into pg_temp.ids select 'jx'||substr(k,2), c.join_code from pg_temp.ids i join public.classes c on c.id = i.v::uuid where k like 'x%';
set role authenticated;
select pg_temp.as_user('00000000-0000-0000-0000-0000000000a1');
select join_class(v) from pg_temp.ids where k in ('jx1','jx2','jx3','jx4');
select pg_temp.expect_error(format('select join_class(%L)', (select v from pg_temp.ids where k='jx5')), 'too_many_classes_joined');
-- a co-teacher leaves; the owner can't
select pg_temp.as_user('00000000-0000-0000-0000-00000000000b');
select remove_class_teacher((select v::uuid from pg_temp.ids where k='c1'), '00000000-0000-0000-0000-00000000000b');
select pg_temp.expect_error(format('select * from teacher_class_roster(%L)', (select v from pg_temp.ids where k='c1')), 'forbidden');
select pg_temp.as_user('00000000-0000-0000-0000-00000000000a');
select pg_temp.expect_error(format('select remove_class_teacher(%L, %L)', (select v from pg_temp.ids where k='c1'), '00000000-0000-0000-0000-00000000000a'), 'owner_cannot_leave');
select (archive_class((select v::uuid from pg_temp.ids where k='c1'))->>'ok')::bool as owner_archives;
select 'ALL PASSED';
