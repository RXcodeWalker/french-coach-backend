-- Stub of the Supabase pieces the teacher-dashboard migration needs (roles, auth.uid()/auth.jwt(),
-- and the earlier tables it references), for running it on a plain local Postgres. See README.md here.
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'anon') then create role anon nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then create role authenticated nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role') then create role service_role nologin bypassrls; end if;
end $$;
create schema auth;
create table auth.users (id uuid primary key, email text);
create function auth.uid() returns uuid language sql stable as $$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;
create function auth.jwt() returns jsonb language sql stable as $$ select coalesce(nullif(current_setting('request.jwt.claims', true), ''), '{}')::jsonb $$;
grant usage on schema auth to anon, authenticated, service_role;
grant execute on all functions in schema auth to anon, authenticated, service_role;
grant usage on schema public to anon, authenticated, service_role;
create table public.profiles (id uuid primary key references auth.users(id) on delete cascade, username text);
grant select on public.profiles to authenticated;
create table public.sessions (id text primary key, user_id uuid not null references public.profiles(id) on delete cascade, mode text not null default 'practice', topic_key text, question_text text, transcript text, word_count int default 0, score numeric(4,2), xp_earned int default 0, duration_sec int default 0, feedback jsonb, created_at timestamptz default now());
alter table public.sessions enable row level security;
create policy own on public.sessions for all to authenticated using (auth.uid() = user_id) with check (auth.uid() = user_id);
grant select, insert, update on public.sessions to authenticated;
create table public.scoring_envelopes (attempt_id text primary key, session_id text not null, user_id uuid not null references public.profiles(id), content_provenance text not null, envelope jsonb not null, created_at timestamptz default now() not null, regraded_from text);
create table public.exam_feedback_reports (id uuid primary key default gen_random_uuid(), session_id text not null, envelope_id text not null unique references public.scoring_envelopes(attempt_id), user_id uuid not null, feedback_version text not null, report jsonb not null, created_at timestamptz default now());
create table public.ai_quota_limits (feature text primary key, daily_limit int not null);
create table public.ai_usage_grants (id text primary key, user_id uuid not null references public.profiles(id), feature text not null references public.ai_quota_limits(feature), grant_date date not null, created_at timestamptz default now());
create function is_admin() returns boolean language sql stable as $$ select coalesce((auth.jwt()->'app_metadata'->>'role') = 'admin', false) $$;
grant all on all tables in schema public to service_role;
