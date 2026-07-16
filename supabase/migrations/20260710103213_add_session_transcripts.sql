/*
  # S3 STT — session_transcripts table

  Supabase carries derived transcripts only, and only for sessions that are
  legally redistributable (contentProvenance = 'original-practice'). Audio is
  never uploaded; raw-asr.json is never uploaded. SupabaseTranscriptStore.save()
  calls assertRedistributable() as its first statement and throws before any
  network call for 'confidential-internal' sessions — this table's RLS is a
  second line of defense, not the primary guard.

  For the whole of Phase A, every session is confidential-internal, so this
  table stays empty. It exists so the schema and the guard land together, and
  so the app can read practice-session transcripts once S10/S11 produce
  original content.
*/

create table if not exists public.session_transcripts (
  session_id text primary key,
  user_id uuid references profiles(id) on delete cascade not null,
  schema_version text not null,
  content_provenance text not null,
  stt jsonb not null,
  transcript jsonb not null,
  created_at timestamptz default now() not null,
  constraint session_transcripts_redistributable
    check (content_provenance = 'original-practice')
);

alter table public.session_transcripts enable row level security;

drop policy if exists "session_transcripts owner read" on public.session_transcripts;
drop policy if exists "session_transcripts owner write" on public.session_transcripts;

create policy "session_transcripts owner read" on public.session_transcripts
  for select using (auth.uid() = user_id);

create policy "session_transcripts owner write" on public.session_transcripts
  for insert with check (auth.uid() = user_id);
