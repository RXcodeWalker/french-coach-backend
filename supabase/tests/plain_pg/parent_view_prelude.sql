-- Extra stubs the parent-view migrations need on top of prelude.sql: pgcrypto in `extensions`, the
-- exam columns on sessions (20261103140000) and speaking_samples (20261103090000, columns read only).
create schema if not exists extensions;
create extension if not exists pgcrypto with schema extensions;
grant usage on schema extensions to anon, authenticated, service_role;
alter table public.sessions add column if not exists practice_only boolean, add column if not exists exam_marks jsonb;
create table if not exists public.speaking_samples (user_id uuid references public.profiles(id) on delete cascade, id text, created_at timestamptz, topic_key text, follow_up boolean, words_per_minute real, start_delay_s real, pauses_over_2s smallint, longest_pause_s real, word_count smallint, speaking_span_s real, primary key (user_id, id));
