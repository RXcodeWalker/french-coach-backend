-- F11b (security review 2026-11): the app's no-argument export/delete calls resolve to one function
-- and act on the caller's own account. Run with run_all_migrations.mjs (README.md); ends with 'ALL PASSED'.
do $$ begin
  if (select count(*) from pg_proc where pronamespace = 'public'::regnamespace
        and proname in ('export_my_data', 'delete_my_account')) <> 2 then
    raise exception 'F11: export_my_data/delete_my_account still have more than one overload each';
  end if;
end $$;

insert into auth.users (id, email) values ('eeeeeeee-0000-4000-8000-000000000006', 'e@example.test');
select set_config('request.jwt.claim.sub', 'eeeeeeee-0000-4000-8000-000000000006', false);
set role authenticated;
do $$ begin
  if not (public.export_my_data() ? 'profile') or not (public.export_my_data() ? 'exam_pronunciation_evidence') then
    raise exception 'F11: export with no arguments did not return the caller''s data';
  end if;
end $$;
reset role;

-- Production does not have exam_pronunciation_evidence (20261005100000 unapplied): the export
-- still works and leaves that key out.
drop table public.exam_pronunciation_evidence cascade;
set role authenticated;
do $$ begin
  if not (public.export_my_data() ? 'profile') or (public.export_my_data() ? 'exam_pronunciation_evidence') then
    raise exception 'F11: export failed without the exam_pronunciation_evidence table';
  end if;
end $$;
select public.delete_my_account()->>'ok' as deleted_with_no_arguments;
reset role;
do $$ begin
  if exists (select 1 from public.profiles where id = 'eeeeeeee-0000-4000-8000-000000000006') then
    raise exception 'F11: delete with no arguments did not delete the caller''s profile';
  end if;
end $$;

select 'ALL PASSED';
