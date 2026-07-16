/*
  # S4 — scoring_envelopes table

  Mirrors session_transcripts (20260710103213_add_session_transcripts.sql):
  Supabase carries envelopes only for sessions that are legally redistributable
  (contentProvenance = 'original-practice'). SupabaseEnvelopeStore.save() calls
  assertRedistributable() as its first statement and throws before any network
  call for 'confidential-internal' sessions — this table's RLS + CHECK
  constraint are a second line of defense, not the primary guard.

  For the whole of Phase A, every session is confidential-internal, so this
  table stays empty. It exists so the schema and the guard land together.

  Keyed by attempt_id, not session_id — a session can be scored more than once
  (regrades); session_id is carried as a column for listBySession-style lookups.
*/

create table if not exists public.scoring_envelopes (
  attempt_id text primary key,
  session_id text not null,
  user_id uuid references profiles(id) on delete cascade not null,
  content_provenance text not null,
  envelope jsonb not null,
  created_at timestamptz default now() not null,
  constraint scoring_envelopes_redistributable
    check (content_provenance = 'original-practice')
);

create index if not exists scoring_envelopes_session_id_idx
  on public.scoring_envelopes (session_id);

alter table public.scoring_envelopes enable row level security;

drop policy if exists "scoring_envelopes owner read" on public.scoring_envelopes;
drop policy if exists "scoring_envelopes owner write" on public.scoring_envelopes;

create policy "scoring_envelopes owner read" on public.scoring_envelopes
  for select using (auth.uid() = user_id);

create policy "scoring_envelopes owner write" on public.scoring_envelopes
  for insert with check (auth.uid() = user_id);
