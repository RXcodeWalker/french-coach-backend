-- Behaviour spec for 20261122090000_ai_cost_tracking.sql. Run after prelude.sql, ai_cost_prelude.sql and the migration
-- (see README.md here). The last line printed must be ALL PASSED.
\set ON_ERROR_STOP on

create function pg_temp.expect(label text, got anyelement, want anyelement) returns void language plpgsql as $$
begin
  if got is distinct from want then raise exception 'FAIL %: got %, want %', label, got, want; end if;
end $$;

insert into auth.users (id) values
  ('aaaaaaaa-0000-0000-0000-000000000001'),  -- A: paying Premium
  ('bbbbbbbb-0000-0000-0000-000000000002'),  -- B: free
  ('cccccccc-0000-0000-0000-000000000003'),  -- C: Premium trial
  ('dddddddd-0000-0000-0000-000000000004');  -- D: free, only Azure (not "active")
insert into public.profiles (id) select id from auth.users;
insert into public.billing_subscriptions values
  ('sub_a', 'aaaaaaaa-0000-0000-0000-000000000001', 'active', now() + interval '10 days'),
  ('sub_c', 'cccccccc-0000-0000-0000-000000000003', 'trialing', now() + interval '5 days');

-- A: 1M in + 1M out on the seeded groq chat model = 0.15 + 0.60 = 0.75
insert into public.ai_usage_events (user_id, feature, provider, model, kind, input_tokens, output_tokens) values
  ('aaaaaaaa-0000-0000-0000-000000000001', 'learn_answer', 'groq', 'openai/gpt-oss-120b', 'chat', 1000000, 1000000);
-- B: one hour of audio = 0.111; a 2 s clip is billed at the 10 s floor = 10/3600*0.111 = 0.000308333
insert into public.ai_usage_events (user_id, feature, provider, model, kind, audio_seconds) values
  ('bbbbbbbb-0000-0000-0000-000000000002', 'learn_transcribe', 'groq', 'whisper-large-v3', 'audio', 3600),
  ('bbbbbbbb-0000-0000-0000-000000000002', 'learn_transcribe', 'groq', 'whisper-large-v3', 'audio', 2);
-- C (trial): 2M in = 0.30
insert into public.ai_usage_events (user_id, feature, provider, model, kind, input_tokens) values
  ('cccccccc-0000-0000-0000-000000000003', 'learn_answer', 'groq', 'openai/gpt-oss-120b', 'chat', 2000000);
-- guest, unattributed probe, an unpriced model, an unmeasured call
insert into public.ai_usage_events (user_id, is_guest, feature, provider, model, kind, input_tokens, output_tokens) values
  (null, true,  'feedback',     'groq',   'openai/gpt-oss-120b', 'chat', 1000000, 0),          -- 0.15 guest
  (null, false, 'unattributed', 'groq',   'openai/gpt-oss-120b', 'chat', 0, 1000000);          -- 0.60 probe
insert into public.ai_usage_events (user_id, feature, provider, model, kind, input_tokens, output_tokens) values
  ('bbbbbbbb-0000-0000-0000-000000000002', 'drill', 'gemini', 'gemini-3.6-flash', 'chat', 500, 500);
insert into public.ai_usage_events (user_id, feature, provider, model, kind, measured) values
  ('bbbbbbbb-0000-0000-0000-000000000002', 'drill', 'groq', 'openai/gpt-oss-120b', 'chat', false);
-- an event outside the window must not count
insert into public.ai_usage_events (created_at, user_id, feature, provider, model, kind, input_tokens, output_tokens) values
  (now() - interval '90 days', 'aaaaaaaa-0000-0000-0000-000000000001', 'x', 'groq', 'openai/gpt-oss-120b', 'chat', 1000000, 1000000);

-- Azure on the free tier costs 0; D has Azure only.
insert into public.azure_speech_usage (user_id, seconds) values ('dddddddd-0000-0000-0000-000000000004', 600);

do $$
declare r jsonb;
begin
  r := public.ai_cost_summary(now() - interval '30 days', now() + interval '1 minute');

  -- provider cost: A .75 + B (.111 + .000308333) + C .30 + guest .15 + probe .60 = 1.911308333
  perform pg_temp.expect('provider_usd', (r->>'provider_usd')::numeric, 1.911308::numeric);
  perform pg_temp.expect('azure_usd (free tier)', (r->>'azure_usd')::numeric, 0::numeric);
  perform pg_temp.expect('active users', (r->>'active_users')::int, 3);          -- A, B, C; D is Azure-only
  perform pg_temp.expect('guest_usd', (r->>'guest_usd')::numeric, 0.15::numeric);
  perform pg_temp.expect('unattributed_usd', (r->>'unattributed_usd')::numeric, 0.6::numeric);
  perform pg_temp.expect('unpriced_calls', (r->>'unpriced_calls')::int, 1);
  perform pg_temp.expect('unmeasured_calls', (r->>'unmeasured_calls')::int, 1);
  perform pg_temp.expect('complete is false while gaps exist', (r->>'complete')::boolean, false);
  -- per-user: A .75, B .111308, C .30 -> mean .387103
  perform pg_temp.expect('cost per active user', round((r->>'cost_per_active_user_usd')::numeric, 4), 0.3871::numeric);
  perform pg_temp.expect('premium active', (r->'premium'->>'active_users')::int, 2);
  perform pg_temp.expect('premium usd', (r->'premium'->>'usd')::numeric, 1.05::numeric);
  perform pg_temp.expect('free usd', (r->'free'->>'usd')::numeric, 0.111308::numeric);
  perform pg_temp.expect('paying', (r->'premium'->>'paying_subscribers')::int, 1);
  perform pg_temp.expect('trialing', (r->'premium'->>'trialing_subscribers')::int, 1);
  perform pg_temp.expect('no price -> margin says so', r->'margin', '{"price_set": false}'::jsonb);
  perform pg_temp.expect('top spender is A', r->'top_users'->0->>'user_id', 'aaaaaaaa-0000-0000-0000-000000000001');
  perform pg_temp.expect('gemini flagged unpriced', (select bool_or((m->>'unpriced')::boolean) from jsonb_array_elements(r->'by_model') m where m->>'provider' = 'gemini'), true);
end $$;

-- Price it: $10/month, 3% + $0.30. 30-day window => revenue 10, fees 0.60.
update public.ai_cost_settings set premium_price_usd_monthly = 10, payment_fee_percent = 3, payment_fee_fixed_usd = 0.30;
do $$
declare r jsonb; m jsonb;
begin
  r := public.ai_cost_summary(now() - interval '30 days', now());
  m := r->'margin';
  perform pg_temp.expect('revenue', (m->>'revenue_usd')::numeric, 10::numeric);
  perform pg_temp.expect('fees', (m->>'payment_fees_usd')::numeric, 0.6::numeric);
  -- (10 - 0.6 - 1.05) / 10 = 83.5 %: the trial user's cost is charged against the one payer
  perform pg_temp.expect('gross margin', (m->>'gross_margin_pct')::numeric, 83.5::numeric);
  -- blended also subtracts free cohort 0.111308 -> 82.39 %
  perform pg_temp.expect('blended margin', (m->>'blended_gross_margin_pct')::numeric, 82.39::numeric);
  perform pg_temp.expect('break-even', (m->>'break_even_cost_per_subscriber_usd')::numeric, 9.4::numeric);   -- 10*0.97-0.30
  -- a 15-day window halves revenue (the 90-day-old event stays out either way)
  r := public.ai_cost_summary(now() - interval '15 days', now());
  perform pg_temp.expect('revenue scales with window', (r->'margin'->>'revenue_usd')::numeric, 5::numeric);
end $$;

-- Nobody paying -> margin is null, not -infinity
delete from public.billing_subscriptions where status = 'active';
do $$
declare r jsonb;
begin
  r := public.ai_cost_summary(now() - interval '30 days', now());
  perform pg_temp.expect('no payers: margin null', r->'margin'->'gross_margin_pct', 'null'::jsonb);
  perform pg_temp.expect('no payers: revenue 0', (r->'margin'->>'revenue_usd')::numeric, 0::numeric);
end $$;

-- Pricing a model removes it from the gaps; correcting a rate re-prices history.
insert into public.ai_model_pricing (provider, model, kind, usd_per_m_input, usd_per_m_output) values ('gemini', 'gemini-3.6-flash', 'chat', 1, 2);
update public.ai_model_pricing set usd_per_m_input = 0.30 where model = 'openai/gpt-oss-120b';
do $$
declare r jsonb;
begin
  r := public.ai_cost_summary(now() - interval '30 days', now());
  perform pg_temp.expect('unpriced cleared', (r->>'unpriced_calls')::int, 0);
  -- gemini: 500/1M*1 + 500/1M*2 = 0.0015; gpt-oss input now 0.30: A 0.90, C 0.60, guest 0.30, probe 0.60 unchanged
  perform pg_temp.expect('repriced provider_usd', (r->>'provider_usd')::numeric, round(0.90 + 0.111308333 + 0.0015 + 0.60 + 0.30 + 0.60, 6));
end $$;

-- Azure at a paid rate attributes to the user: 600 s at $1/hour = 0.1666667 -> D appears in per-user cost
update public.ai_cost_settings set azure_speech_usd_per_hour = 1;
do $$
declare r jsonb;
begin
  r := public.ai_cost_summary(now() - interval '30 days', now());
  perform pg_temp.expect('azure_usd', round((r->>'azure_usd')::numeric, 4), 0.1667::numeric);
  perform pg_temp.expect('azure-only user still not active', (r->>'active_users')::int, 3);
end $$;

-- Window must be positive.
do $$ begin
  begin perform public.ai_cost_summary(now(), now() - interval '1 day'); raise exception 'FAIL: reversed window accepted';
  exception when sqlstate '22023' then null; end;
end $$;

-- Privileges: no client role can read the ledger or call the summary.
do $$ begin
  perform pg_temp.expect('anon cannot select', has_table_privilege('anon', 'public.ai_usage_events', 'SELECT'), false);
  perform pg_temp.expect('authenticated cannot select', has_table_privilege('authenticated', 'public.ai_usage_events', 'SELECT'), false);
  perform pg_temp.expect('authenticated cannot call summary', has_function_privilege('authenticated', 'public.ai_cost_summary(timestamptz, timestamptz)', 'EXECUTE'), false);
  perform pg_temp.expect('service_role can call summary', has_function_privilege('service_role', 'public.ai_cost_summary(timestamptz, timestamptz)', 'EXECUTE'), true);
  perform pg_temp.expect('service_role can insert', has_table_privilege('service_role', 'public.ai_usage_events', 'INSERT'), true);
end $$;

select 'ALL PASSED';
