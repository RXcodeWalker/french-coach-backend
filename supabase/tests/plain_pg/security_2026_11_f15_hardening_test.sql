-- F15 (security review 2026-11): client roles hold no TRUNCATE/REFERENCES/TRIGGER on public tables,
-- new tables do not get them, and every public function has a pinned search_path.
-- Run with run_all_migrations.mjs (README.md; its prelude copies production's default privileges);
-- ends with 'ALL PASSED'.
do $$ declare bad text; begin
  select string_agg(c.relname || ':' || r.rolname || ':' || p.priv, ', ') into bad
  from pg_class c
  cross join (values ('anon'), ('authenticated')) r(rolname)
  cross join (values ('TRUNCATE'), ('REFERENCES'), ('TRIGGER')) p(priv)
  where c.relnamespace = 'public'::regnamespace and c.relkind in ('r', 'p', 'v', 'm', 'f')
    and has_table_privilege(r.rolname, c.oid, p.priv);
  if bad is not null then raise exception 'F15: client roles still hold %', bad; end if;
end $$;

create table public._new_after_migrations (id int);
do $$ begin
  if has_table_privilege('anon', 'public._new_after_migrations', 'TRUNCATE')
     or has_table_privilege('authenticated', 'public._new_after_migrations', 'TRIGGER')
     or has_table_privilege('authenticated', 'public._new_after_migrations', 'REFERENCES') then
    raise exception 'F15: a new public table still gets TRUNCATE/REFERENCES/TRIGGER by default';
  end if;
end $$;
drop table public._new_after_migrations;

do $$ declare bad text; begin
  select string_agg(p.oid::regprocedure::text, ', ') into bad from pg_proc p
  where p.pronamespace = 'public'::regnamespace
    and not exists (select 1 from unnest(coalesce(p.proconfig, '{}')) c where c like 'search_path=%');
  if bad is not null then raise exception 'F15: functions without a pinned search_path: %', bad; end if;
end $$;

-- The pinned functions still work.
insert into auth.users (id, email) values ('88888888-0000-4000-8000-000000000001', 'u@example.test');
select set_config('request.jwt.claim.sub', '88888888-0000-4000-8000-000000000001', false);
set role authenticated;
select public.claim_username('Lea_29');
select public.is_admin() = false as not_admin;
reset role;
do $$ begin
  if (select username from public.profiles where id = '88888888-0000-4000-8000-000000000001') is distinct from 'Lea_29' then
    raise exception 'F15: claim_username stopped working after its search_path was pinned';
  end if;
end $$;
select public._league_week_key(now()) ~ '^\d{4}-W\d{2}$' as week_key_ok;

select 'ALL PASSED';
