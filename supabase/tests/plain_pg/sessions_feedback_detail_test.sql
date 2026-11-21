-- Behaviour spec for 20261121090000_sessions_feedback_detail.sql: the full Learn feedback a learner saw
-- is stored in its own column, readable only by its owner, size-capped, and never returned by the
-- teacher functions. Run with run_all_migrations.mjs (README.md); ends with 'ALL PASSED'.
insert into auth.users (id, email) values
  ('aaaaaaaa-0000-4000-8000-0000000000d1', 'learner@example.test'),
  ('aaaaaaaa-0000-4000-8000-0000000000d2', 'other@example.test');

select set_config('request.jwt.claim.sub', 'aaaaaaaa-0000-4000-8000-0000000000d1', false);
set role authenticated;
-- The client's upsert, with the new column.
insert into public.sessions (id, user_id, mode, question_text, transcript, feedback, feedback_detail)
  values ('sess-1', 'aaaaaaaa-0000-4000-8000-0000000000d1', 'practice', 'Q', 'T',
          '{"cefrLevel":"A2"}', '{"v":1,"improved_answer":"Je joue au foot.","grammar":{"critical":[{"msg":"x"}]}}')
  on conflict (id) do update set feedback_detail = excluded.feedback_detail;
-- A row without it (old client, offline-queue replay) still inserts, and an upsert that omits the
-- column keeps what is there.
insert into public.sessions (id, user_id, mode) values ('sess-2', 'aaaaaaaa-0000-4000-8000-0000000000d1', 'practice');
insert into public.sessions (id, user_id, mode, feedback) values ('sess-1', 'aaaaaaaa-0000-4000-8000-0000000000d1', 'practice', '{"cefrLevel":"A2"}')
  on conflict (id) do update set feedback = excluded.feedback;
do $$ begin
  if (select feedback_detail->>'improved_answer' from public.sessions where id = 'sess-1') is distinct from 'Je joue au foot.' then
    raise exception 'feedback_detail lost on an upsert that omitted it';
  end if;
end $$;
-- Shape and size are enforced.
do $$ begin
  update public.sessions set feedback_detail = '[1,2]' where id = 'sess-2';
  raise exception 'a non-object feedback_detail was accepted';
exception when check_violation then null;
end $$;
do $$ begin
  update public.sessions set feedback_detail = jsonb_build_object('pad', repeat('x', 40000)) where id = 'sess-2';
  raise exception 'an oversized feedback_detail was accepted';
exception when check_violation then null;
end $$;
-- Another account cannot read it.
select set_config('request.jwt.claim.sub', 'aaaaaaaa-0000-4000-8000-0000000000d2', false);
do $$ begin
  if exists (select 1 from public.sessions where id = 'sess-1') then
    raise exception 'another account can read the session row';
  end if;
end $$;
reset role;

-- No function returns it: the teacher/parent readers select named columns or keys of `feedback` only.
do $$
declare bad text;
begin
  select string_agg(p.proname, ', ') into bad
    from pg_proc p join pg_namespace n on n.oid = p.pronamespace
   where n.nspname = 'public' and p.prosrc ilike '%feedback_detail%';
  if bad is not null then raise exception 'functions read feedback_detail: %', bad; end if;
end $$;
do $$ begin
  if exists (
    select 1 from pg_proc p join pg_namespace n on n.oid = p.pronamespace
     where n.nspname = 'public' and p.proname = 'teacher_student_sessions'
       and pg_get_function_result(p.oid) ilike '%feedback_detail%'
  ) then raise exception 'teacher_student_sessions returns feedback_detail'; end if;
end $$;
select 'ALL PASSED';
