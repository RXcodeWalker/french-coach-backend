-- Behaviour spec for 20261103090000_teacher_dashboard.sql. Run per README.md; ends with 'ALL PASSED'.
\set ON_ERROR_STOP 1
-- users: T teacher, T2 other teacher, A,B students, X stranger
insert into auth.users values ('00000000-0000-0000-0000-00000000000a','t@x'),('00000000-0000-0000-0000-00000000000b','t2@x'),('00000000-0000-0000-0000-0000000000a1','a@x'),('00000000-0000-0000-0000-0000000000b1','b@x'),('00000000-0000-0000-0000-0000000000c1','x@x');
insert into public.profiles select id, split_part(email,'@',1) from auth.users;
insert into public.teacher_invites (code, max_uses) values ('PILOT1', 1);
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
select pg_temp.as_user('00000000-0000-0000-0000-00000000000a');
select pg_temp.expect_error($$select create_class('Y11')$$, 'not_a_teacher');
select pg_temp.expect_error($$select redeem_teacher_invite('nope','Ms D')$$, 'invalid_code');
select redeem_teacher_invite('pilot1','Ms Dupont','Lycée');
select pg_temp.as_user('00000000-0000-0000-0000-00000000000b');
select pg_temp.expect_error($$select redeem_teacher_invite('PILOT1','Mr B')$$, 'code_used_up');
reset role; insert into public.teacher_invites (code) values ('PILOT2'); set role authenticated;
select redeem_teacher_invite('PILOT2','Mr B');
select (create_class('Other class')->>'ok')::bool;

select pg_temp.as_user('00000000-0000-0000-0000-00000000000a');
create temp table c as select create_class('Year 11 French','June 2027','Europe/Paris') r;
reset role; grant select on c to authenticated, service_role; set role authenticated;
select (select r->>'join_code' from c) ~ '^[A-HJKMNP-Z2-9]{6}$' as code_ok;
-- teacher can't join own
select pg_temp.expect_error(format('select join_class(%L)', (select r->>'join_code' from c)), 'own_class');

-- student A previews + joins
select pg_temp.as_user('00000000-0000-0000-0000-0000000000a1');
select preview_class((select r->>'join_code' from c));
select join_class(lower((select r->>'join_code' from c)));
select my_class();
-- student A cannot read roster
select pg_temp.expect_error(format('select * from teacher_class_roster(%L)', (select r->>'class_id' from c)), 'forbidden');
-- A has a session from before join? insert own session now
insert into public.sessions (id, user_id, mode, topic_key, score, created_at, practice_only, feedback) values ('s-old','00000000-0000-0000-0000-0000000000a1','practice','school',6, now() - interval '3 days', null, '{"cefrLevel":"A2","critical_count":1}');
insert into public.sessions (id, user_id, mode, topic_key, score, feedback) values ('s-new','00000000-0000-0000-0000-0000000000a1','practice','school',7, '{"cefrLevel":"A2","critical_count":2}');
-- B joins too
select pg_temp.as_user('00000000-0000-0000-0000-0000000000b1');
select join_class((select r->>'join_code' from c));
-- other teacher can't read this class
select pg_temp.as_user('00000000-0000-0000-0000-00000000000b');
select pg_temp.expect_error(format('select * from teacher_class_sessions(%L, null)', (select r->>'class_id' from c)), 'forbidden');
select count(*) as other_teacher_sees_classes from public.classes where name = 'Year 11 French';

select pg_temp.as_user('00000000-0000-0000-0000-00000000000a');
select student_id, display_name, status from teacher_class_roster((select (r->>'class_id')::uuid from c));
select id, cefr_level, critical_count from teacher_class_sessions((select (r->>'class_id')::uuid from c), now() - interval '30 days');  -- only s-new (D7)

-- envelope for A
reset role;
insert into public.scoring_envelopes values ('att-1','exam-1','00000000-0000-0000-0000-0000000000a1','original-practice','{"total":27,"rolePlayTasks":[{"mark":2},{"mark":1},{"mark":2},{"mark":2},{"mark":1}],"communication":{"mark":10},"qualityOfLanguage":{"mark":9,"errors":[{"quote":"je suis allé au parc hier et je mange","correction":"j''ai mangé","kind":"grammar"}]}}', now(), null);
insert into public.exam_feedback_reports (session_id, envelope_id, user_id, feedback_version, report) values ('exam-1','att-1','00000000-0000-0000-0000-0000000000a1','v','{"qualityOfLanguage":{"errors":[{"quote":"je mange","correction":"j''ai mangé","category":"tense"}]}}');
insert into public.exam_attempt_meta values ('00000000-0000-0000-0000-0000000000a1','exam-1','examSim',true,null,now());
set role authenticated;
select total, role_play, role_play_max, communication, quality_of_language, jsonb_array_length(report_errors) rep, conduct_mode, counts_toward_progress from teacher_class_exams((select (r->>'class_id')::uuid from c), null);
select (teacher_exam_attempt((select (r->>'class_id')::uuid from c), 'att-1')->>'student_name');
select save_teacher_mark((select (r->>'class_id')::uuid from c), 'att-1', '{"sessionId":"exam-1","markedBy":"t","markedAt":"x","marks":[]}', 29, false);
select teacher_total, teacher_shared from teacher_class_exams((select (r->>'class_id')::uuid from c), null);
select pg_temp.as_user('00000000-0000-0000-0000-0000000000a1');
select count(*) as a_sees_unshared from my_shared_teacher_marks();
select count(*) as a_sees_raw_marks from public.teacher_marks;
select pg_temp.as_user('00000000-0000-0000-0000-00000000000a');
select save_teacher_mark((select (r->>'class_id')::uuid from c), 'att-1', '{"sessionId":"exam-1","markedBy":"t","markedAt":"x","marks":[]}', 29, true);
select pg_temp.as_user('00000000-0000-0000-0000-0000000000a1');
select total, teacher_name from my_shared_teacher_marks();

-- assignments
select pg_temp.as_user('00000000-0000-0000-0000-00000000000a');
create temp table asg as select create_assignment((select (r->>'class_id')::uuid from c), 'mock_exam', 'Mock 1', null, null, null, now() + interval '3 days', null) r
  union all select create_assignment((select (r->>'class_id')::uuid from c), 'learn_topic', 'Holidays x3', 'holidays', null, 3, now() + interval '3 days', array['00000000-0000-0000-0000-0000000000b1'::uuid]);
reset role; grant select on asg to authenticated, service_role; set role authenticated;
select pg_temp.expect_error(format('select create_assignment(%L, %L, %L, null, null, null, now()+interval ''1 day'', null)', (select r->>'class_id' from c), 'learn_topic', 'bad'), 'invalid_assignment');
select pg_temp.as_user('00000000-0000-0000-0000-0000000000a1');
select kind, title from my_assignments();  -- A sees only the mock
select pg_temp.as_user('00000000-0000-0000-0000-0000000000b1');
select kind, title from my_assignments();  -- B sees both
-- allowance (service role)
reset role; set role service_role;
create temp table ids as select (array_agg((r->>'assignment_id')::uuid))[1] mock, (array_agg((r->>'assignment_id')::uuid))[2] learn from asg;
select assignment_allowance('00000000-0000-0000-0000-0000000000a1', (select mock from ids)) as a_mock;
select assignment_allowance('00000000-0000-0000-0000-0000000000a1', (select learn from ids)) as a_learn_not_targeted;
insert into public.ai_usage_grants values ('00000000-0000-0000-0000-0000000000a1:assignment_exam:asg:'||(select mock from ids)||':exam-9','00000000-0000-0000-0000-0000000000a1','assignment_exam',current_date);
select assignment_allowance('00000000-0000-0000-0000-0000000000a1', (select mock from ids)) as a_mock_used;
select assignment_allowance('00000000-0000-0000-0000-0000000000a1', (select mock from ids), 'asg:'||(select mock from ids)||':exam-9') as a_mock_retry;
reset role; grant select on ids to authenticated;
-- progress: B answers the learn homework twice (grants) + A's mock attempt meta
insert into public.ai_usage_grants values ('00000000-0000-0000-0000-0000000000b1:assignment_learn_answer:asg:'||(select learn from ids)||':k1','00000000-0000-0000-0000-0000000000b1','assignment_learn_answer',current_date),
  ('00000000-0000-0000-0000-0000000000b1:assignment_learn_answer:asg:'||(select learn from ids)||':k2','00000000-0000-0000-0000-0000000000b1','assignment_learn_answer',current_date);
insert into public.exam_attempt_meta values ('00000000-0000-0000-0000-0000000000a1','exam-hw','examSim',true,(select mock from ids),now());
set role authenticated;
select pg_temp.as_user('00000000-0000-0000-0000-00000000000a');
select p.learn_answers, p.exam_attempts, p.student_id = '00000000-0000-0000-0000-0000000000b1' as is_b
  from teacher_assignment_progress((select (r->>'class_id')::uuid from c)) p order by p.learn_answers desc;
select pg_temp.as_user('00000000-0000-0000-0000-0000000000a1');
select pg_temp.expect_error(format('select * from teacher_assignment_progress(%L)', (select r->>'class_id' from c)), 'forbidden');
reset role;
-- authenticated cannot call allowance
set role authenticated;
select pg_temp.expect_error($$select assignment_allowance('00000000-0000-0000-0000-0000000000a1', gen_random_uuid())$$, 'permission denied');
-- leave → access ends
select pg_temp.as_user('00000000-0000-0000-0000-0000000000a1');
select leave_class();
select pg_temp.as_user('00000000-0000-0000-0000-00000000000a');
select count(*) as sessions_after_leave from teacher_class_sessions((select (r->>'class_id')::uuid from c), null);
select pg_temp.expect_error($$select teacher_exam_attempt((select (r->>'class_id')::uuid from c), 'att-1')$$, 'not_found');
-- rotate + deactivate code
select (rotate_class_code((select (r->>'class_id')::uuid from c))->>'join_code') <> (select r->>'join_code' from c) as rotated;
select pg_temp.as_user('00000000-0000-0000-0000-0000000000c1');
select pg_temp.expect_error(format('select join_class(%L)', (select r->>'join_code' from c)), 'invalid_code');
-- direct table writes blocked
select pg_temp.expect_error($$insert into public.classes (teacher_id,name,join_code) values ('00000000-0000-0000-0000-0000000000c1','x','ZZZZZZ')$$, 'permission denied');
select pg_temp.expect_error($$update public.class_members set status='active'$$, 'permission denied');
select pg_temp.as_user('00000000-0000-0000-0000-00000000000a');
select archive_class((select (r->>'class_id')::uuid from c));
select pg_temp.expect_error(format('select * from teacher_class_roster(%L)', (select r->>'class_id' from c)), 'forbidden');
select 'ALL PASSED';
