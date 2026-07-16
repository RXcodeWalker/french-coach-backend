/*
  # IGCSE question bank — S11 architecture (schema only, no content)

  Adds public.igcse_question_sets, the runtime store for Cambridge IGCSE
  French 0520 AuthoredQuestionSet content (see docs/architecture/plans —
  S11 Question-Bank Architecture & Data-Model). Reuses the CMS conventions
  already established in 20260629125717_add_content_tables.sql:

    - content_status enum (draft | published | archived) — published == the
      teacher-approval gate.
    - snapshot_before_update() trigger — captures the whole OLD row (incl.
      content_hash) into content_versions on every UPDATE. This trigger
      hardcodes OLD.id, so the primary key here is named `id` (= questionSetId).
    - is_admin() RLS — public reads published rows; admins read/write everything.

  payload jsonb holds the full AuthoredQuestionSet (content + provenance +
  review — everything except the identity/versioning columns broken out
  below). content_hash is a first-class column (not buried in payload) so
  historical-envelope resolution can query it directly:
    select payload from igcse_question_sets where content_hash = $1
    union
    select data->>'payload' from content_versions
      where content_type='igcse_question_sets' and data->>'content_hash' = $1
*/

create table if not exists public.igcse_question_sets (
  id text primary key,
  schema_version text not null,
  content_hash text not null,
  payload jsonb not null,
  status content_status not null default 'draft',
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

create index if not exists igcse_question_sets_content_hash
  on public.igcse_question_sets(content_hash);

drop trigger if exists igcse_question_sets_snapshot on public.igcse_question_sets;
create trigger igcse_question_sets_snapshot before update on public.igcse_question_sets
  for each row execute function snapshot_before_update();

alter table public.igcse_question_sets enable row level security;
drop policy if exists "igcse_question_sets public read"   on public.igcse_question_sets;
drop policy if exists "igcse_question_sets admin read"    on public.igcse_question_sets;
drop policy if exists "igcse_question_sets admin insert"  on public.igcse_question_sets;
drop policy if exists "igcse_question_sets admin update"  on public.igcse_question_sets;
drop policy if exists "igcse_question_sets admin delete"  on public.igcse_question_sets;
create policy "igcse_question_sets public read"  on public.igcse_question_sets for select using (status = 'published');
create policy "igcse_question_sets admin read"   on public.igcse_question_sets for select using (is_admin());
create policy "igcse_question_sets admin insert" on public.igcse_question_sets for insert with check (is_admin());
create policy "igcse_question_sets admin update" on public.igcse_question_sets for update using (is_admin());
create policy "igcse_question_sets admin delete" on public.igcse_question_sets for delete using (is_admin());
