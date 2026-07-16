/*
  # Content Management Pipeline — CMS schema

  Adds an admin-managed content layer on top of the existing question/scenario
  data. Introduces:

    1. content_status enum (draft | published | archived) — supersedes is_active
    2. is_admin() helper reading JWT app_metadata.role
    3. topics / questions / scenarios / exam_sets content tables
    4. content_versions audit table + snapshot_before_update() trigger
    5. RLS: published rows are world-readable; admins read/write everything

  Existing backend code queries questions/exam_sets via `.eq("is_active", true)`.
  To stay backwards-compatible while the status workflow rolls out, the questions
  and exam_sets tables retain `is_active` AND gain `status`; a backfill keeps the
  two in sync at migration time. New writes go through `status`.
*/

-- ── Status enum ──────────────────────────────────────────────────────────────
do $$ begin
  create type content_status as enum ('draft', 'published', 'archived');
exception
  when duplicate_object then null;
end $$;

-- ── Admin role helper ────────────────────────────────────────────────────────
create or replace function is_admin()
returns boolean language sql stable security definer as $$
  select coalesce((auth.jwt()->'app_metadata'->>'role') = 'admin', false)
$$;

-- ── Tables ───────────────────────────────────────────────────────────────────
create table if not exists public.topics (
  key text primary key,
  label text not null,
  label_en text not null,
  icon text not null,
  color text not null,
  description text not null,
  locked boolean default false,
  is_advanced boolean default false,
  sort_order int not null default 0,
  status content_status not null default 'draft',
  updated_at timestamptz default now()
);

create table if not exists public.questions (
  id text primary key,
  topic_key text not null,
  text text not null,
  hint text not null default '',
  difficulty smallint check (difficulty in (1,2,3)) not null default 1,
  follow_ups text[] not null default '{}',
  model_answer text not null default '',
  key_vocab jsonb not null default '[]',
  is_past_paper boolean default false,
  year int,
  paper_code text,
  status content_status not null default 'draft',
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

-- If questions pre-existed (SQLite-migrated), make sure the new columns exist.
alter table public.questions add column if not exists status content_status not null default 'draft';
alter table public.questions add column if not exists follow_ups text[] not null default '{}';
alter table public.questions add column if not exists key_vocab jsonb not null default '[]';
alter table public.questions add column if not exists updated_at timestamptz default now();
alter table public.questions add column if not exists created_at timestamptz default now();

create table if not exists public.scenarios (
  id text primary key,
  emoji text not null default '',
  title text not null,
  description text not null default '',
  turns int not null default 15,
  data jsonb not null,
  status content_status not null default 'draft',
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

create table if not exists public.exam_sets (
  id text primary key,
  label text not null,
  question_ids text[] not null default '{}',
  status content_status not null default 'draft',
  updated_at timestamptz default now()
);
alter table public.exam_sets add column if not exists status content_status not null default 'draft';
alter table public.exam_sets add column if not exists updated_at timestamptz default now();

-- Backfill status from any legacy is_active column, then keep is_active in sync
-- so existing `.eq("is_active", true)` reads still work during the transition.
do $$ begin
  if exists (select 1 from information_schema.columns
             where table_name='questions' and column_name='is_active') then
    update public.questions set status = case when is_active then 'published'::content_status
                                              else 'archived'::content_status end
      where status = 'draft';
  end if;
  if exists (select 1 from information_schema.columns
             where table_name='exam_sets' and column_name='is_active') then
    update public.exam_sets set status = case when is_active then 'published'::content_status
                                             else 'archived'::content_status end
      where status = 'draft';
  end if;
end $$;

-- Keep is_active mirrored from status (legacy compatibility shim).
create or replace function sync_is_active_from_status()
returns trigger language plpgsql as $$
begin
  if to_jsonb(NEW) ? 'is_active' then
    NEW.is_active := (NEW.status = 'published');
  end if;
  return NEW;
end $$;

do $$ begin
  if exists (select 1 from information_schema.columns
             where table_name='questions' and column_name='is_active') then
    drop trigger if exists questions_sync_is_active on public.questions;
    create trigger questions_sync_is_active before insert or update on public.questions
      for each row execute function sync_is_active_from_status();
  end if;
  if exists (select 1 from information_schema.columns
             where table_name='exam_sets' and column_name='is_active') then
    drop trigger if exists exam_sets_sync_is_active on public.exam_sets;
    create trigger exam_sets_sync_is_active before insert or update on public.exam_sets
      for each row execute function sync_is_active_from_status();
  end if;
end $$;

-- ── Audit log ────────────────────────────────────────────────────────────────
create table if not exists public.content_versions (
  id uuid default gen_random_uuid() primary key,
  content_type text not null,
  content_id text not null,
  version_number int not null,
  data jsonb not null,
  created_by uuid references auth.users(id),
  created_at timestamptz default now()
);
create unique index if not exists content_versions_unique
  on public.content_versions(content_type, content_id, version_number);

-- ── Versioning trigger ───────────────────────────────────────────────────────
create or replace function snapshot_before_update()
returns trigger language plpgsql security definer as $$
declare v_next int;
begin
  select coalesce(max(version_number), 0) + 1 into v_next
  from content_versions
  where content_type = TG_TABLE_NAME and content_id = OLD.id;

  insert into content_versions(content_type, content_id, version_number, data, created_by)
  values (TG_TABLE_NAME, OLD.id, v_next, row_to_json(OLD)::jsonb, auth.uid());
  return NEW;
end $$;

drop trigger if exists questions_snapshot on public.questions;
create trigger questions_snapshot before update on public.questions
  for each row execute function snapshot_before_update();

drop trigger if exists scenarios_snapshot on public.scenarios;
create trigger scenarios_snapshot before update on public.scenarios
  for each row execute function snapshot_before_update();

drop trigger if exists topics_snapshot on public.topics;
create trigger topics_snapshot before update on public.topics
  for each row execute function snapshot_before_update();

-- ── RLS ──────────────────────────────────────────────────────────────────────
-- Topics
alter table public.topics enable row level security;
drop policy if exists "topics public read"  on public.topics;
drop policy if exists "topics admin read"   on public.topics;
drop policy if exists "topics admin write"  on public.topics;
create policy "topics public read"  on public.topics for select using (status = 'published');
create policy "topics admin read"   on public.topics for select using (is_admin());
create policy "topics admin write"  on public.topics for all using (is_admin()) with check (is_admin());

-- Questions
alter table public.questions enable row level security;
drop policy if exists "questions public read"  on public.questions;
drop policy if exists "questions admin read"   on public.questions;
drop policy if exists "questions admin insert" on public.questions;
drop policy if exists "questions admin update" on public.questions;
drop policy if exists "questions admin delete" on public.questions;
create policy "questions public read"  on public.questions for select using (status = 'published');
create policy "questions admin read"   on public.questions for select using (is_admin());
create policy "questions admin insert" on public.questions for insert with check (is_admin());
create policy "questions admin update" on public.questions for update using (is_admin());
create policy "questions admin delete" on public.questions for delete using (is_admin());

-- Scenarios
alter table public.scenarios enable row level security;
drop policy if exists "scenarios public read"  on public.scenarios;
drop policy if exists "scenarios admin read"   on public.scenarios;
drop policy if exists "scenarios admin insert" on public.scenarios;
drop policy if exists "scenarios admin update" on public.scenarios;
drop policy if exists "scenarios admin delete" on public.scenarios;
create policy "scenarios public read"  on public.scenarios for select using (status = 'published');
create policy "scenarios admin read"   on public.scenarios for select using (is_admin());
create policy "scenarios admin insert" on public.scenarios for insert with check (is_admin());
create policy "scenarios admin update" on public.scenarios for update using (is_admin());
create policy "scenarios admin delete" on public.scenarios for delete using (is_admin());

-- Exam sets
alter table public.exam_sets enable row level security;
drop policy if exists "exam_sets public read" on public.exam_sets;
drop policy if exists "exam_sets admin read"  on public.exam_sets;
drop policy if exists "exam_sets admin write" on public.exam_sets;
create policy "exam_sets public read" on public.exam_sets for select using (status = 'published');
create policy "exam_sets admin read"  on public.exam_sets for select using (is_admin());
create policy "exam_sets admin write" on public.exam_sets for all using (is_admin()) with check (is_admin());

-- Content versions: admin only
alter table public.content_versions enable row level security;
drop policy if exists "versions admin read"   on public.content_versions;
drop policy if exists "versions admin insert" on public.content_versions;
create policy "versions admin read"   on public.content_versions for select using (is_admin());
create policy "versions admin insert" on public.content_versions for insert with check (is_admin());
