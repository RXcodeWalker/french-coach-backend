-- Stubs for ai_cost_tracking_test.sql, after prelude.sql: the Azure ledgers, the two billing tables and has_premium()
-- (same status rule as migration 20261104230000, trimmed to what the cost summary reads).
create table public.azure_speech_usage (id uuid primary key default gen_random_uuid(), user_id uuid references public.profiles(id) on delete set null, source text not null default 'learn', seconds numeric(10,3) not null, status text not null default 'settled', created_at timestamptz not null default now());
create table public.azure_tts_usage (id uuid primary key default gen_random_uuid(), user_id uuid references public.profiles(id) on delete set null, characters integer not null, status text not null default 'reserved', created_at timestamptz not null default now());
create table public.billing_subscriptions (stripe_subscription_id text primary key, user_id uuid not null references public.profiles(id) on delete cascade, status text not null, current_period_end timestamptz);
create table public.billing_razorpay_subscriptions (razorpay_subscription_id text primary key, user_id uuid not null references public.profiles(id) on delete cascade, status text not null, current_period_end timestamptz);
create function public.has_premium(p_user_id uuid) returns boolean language sql stable as $$
  select exists (select 1 from public.billing_subscriptions s where s.user_id = p_user_id and s.status in ('active','trialing','past_due')
                 and (s.current_period_end is null or s.current_period_end > now() - interval '3 days')) $$;
