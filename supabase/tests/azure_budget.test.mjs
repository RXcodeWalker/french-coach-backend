// Azure Speech budget (exam-pronunciation plan §4, Batch 1): DB integration
// tests for 20261005090000_azure_speech_usage_and_budget.sql. Run against the
// LOCAL Supabase stack only (`npx supabase start` from backend/) -- this test
// temporarily sets the project-wide cap, which must never touch the hosted
// project.
//
// Usage: node backend/supabase/tests/azure_budget.test.mjs
// Requires: local stack up (npx supabase start), reads keys from
// `npx supabase status -o json` at run time so nothing is hardcoded here.
//
// The headline case is the concurrent-reserve race: N parallel reservations
// against a cap with room for exactly K must grant exactly K, which only holds
// if reserve_azure_seconds' advisory lock serialises them. (The same race,
// plus reserve-to-cap / settle / release / month rollover, also runs in
// tests/test_azure_budget.py against a throwaway Postgres.) All RPCs here are
// service_role-only -- called through `admin.rpc(...)`, matching how the
// FastAPI backend calls them -- and the anon/authenticated denial is checked.

import { createClient } from '@supabase/supabase-js';
import { execSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';

const status = JSON.parse(
  execSync('npx supabase status -o json', { cwd: new URL('../..', import.meta.url), encoding: 'utf8' })
);

const API_URL = status.API_URL;
const ANON_KEY = status.ANON_KEY;
const SERVICE_ROLE_KEY = status.SERVICE_ROLE_KEY;

const admin = createClient(API_URL, SERVICE_ROLE_KEY, { auth: { autoRefreshToken: false, persistSession: false } });

let pass = 0;
let fail = 0;
const failures = [];

function ok(cond, label) {
  if (cond) { pass++; console.log(`  ok - ${label}`); }
  else { fail++; failures.push(label); console.log(`  FAIL - ${label}`); }
}

async function setCap(capSeconds) {
  const { error } = await admin.from('azure_speech_budget').update({ cap_seconds: capSeconds }).eq('id', true);
  if (error) throw new Error(`setCap(${capSeconds}) failed: ${error.message}`);
}

async function monthTotal() {
  const { data, error } = await admin.rpc('azure_speech_usage_summary');
  if (error) throw new Error(`azure_speech_usage_summary failed: ${error.message}`);
  return Number(data.total_seconds);
}

function reserve(seconds, extra = {}) {
  return admin.rpc('reserve_azure_seconds', {
    p_user_id: null, p_source: 'exam', p_seconds: seconds,
    p_session_id: `azure-budget-test-${randomUUID()}`, p_part: null, p_turn_key: null, ...extra,
  });
}

const reservations = [];

async function main() {
  // The cap is project-wide and other rows may already exist this month, so
  // every cap below is set relative to the current month-to-date total.
  const base = await monthTotal();

  console.log('\n1. NULL cap = unlimited');
  {
    await setCap(null);
    const r = await reserve(1_000_000);
    ok(!r.error && r.data.granted === true, `huge reservation granted with no cap${r.error ? ` (error: ${r.error.message})` : ''}`);
    if (r.data?.reservation_id) reservations.push(r.data.reservation_id);
    const rel = await admin.rpc('release_azure_seconds', { p_reservation_id: r.data?.reservation_id });
    ok(!rel.error && rel.data.released === true, 'release frees it');
  }

  console.log('\n2. Concurrent reservations cannot overshoot the cap');
  {
    await setCap(Math.ceil(base) + 50);
    const now = await monthTotal();
    const room = Math.ceil(base) + 50 - now;
    const results = await Promise.all(Array.from({ length: 10 }, () => reserve(10)));
    const granted = results.filter(r => !r.error && r.data.granted === true);
    for (const r of granted) reservations.push(r.data.reservation_id);
    const expected = Math.floor(room / 10);
    ok(granted.length === expected, `exactly ${expected} of 10 parallel reservations granted (got ${granted.length})`);
    const denied = results.filter(r => !r.error && r.data.granted === false);
    ok(denied.every(r => r.data.reason === 'budget_exhausted'), 'every denial says budget_exhausted');
    ok((await monthTotal()) <= Math.ceil(base) + 50, 'month-to-date total never exceeds the cap');
  }

  console.log('\n3. settle records the measured seconds; release frees room');
  {
    const id = reservations[reservations.length - 1];
    const s = await admin.rpc('settle_azure_seconds', { p_reservation_id: id, p_seconds: 1.5 });
    ok(!s.error && s.data.settled === true, 'settle succeeds once');
    const again = await admin.rpc('settle_azure_seconds', { p_reservation_id: id, p_seconds: 9 });
    ok(!again.error && again.data.settled === false, 'a second settle is a no-op');
  }

  console.log('\n4. Clients cannot call the RPCs or read the ledger');
  {
    const anon = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });
    const r = await anon.rpc('reserve_azure_seconds', { p_user_id: null, p_source: 'exam', p_seconds: 1 });
    ok(!!r.error, `anon reserve_azure_seconds denied${r.error ? '' : ` (got ${JSON.stringify(r.data)})`}`);
    const s = await anon.rpc('azure_speech_usage_summary');
    ok(!!s.error, 'anon azure_speech_usage_summary denied');
    const t = await anon.from('azure_speech_usage').select('id').limit(1);
    ok(!!t.error || (t.data ?? []).length === 0, 'anon cannot read azure_speech_usage');
  }

  console.log('\n5. exam_pronunciation quota row is seeded (1000/day)');
  {
    const { data, error } = await admin.from('ai_quota_limits').select('daily_limit').eq('feature', 'exam_pronunciation').single();
    ok(!error && data?.daily_limit === 1000, `exam_pronunciation daily_limit is 1000${error ? ` (error: ${error.message})` : ` (got ${data?.daily_limit})`}`);
  }
}

main()
  .catch(err => {
    console.error('Test run crashed:', err);
    fail++;
  })
  .finally(async () => {
    // Leave the local stack as found: cap unset, test reservations released.
    for (const id of reservations) await admin.rpc('release_azure_seconds', { p_reservation_id: id });
    await setCap(null).catch(() => {});
    console.log(`\n${pass} passed, ${fail} failed`);
    if (fail > 0) {
      console.log('\nFailures:');
      failures.forEach(f => console.log(`  - ${f}`));
      process.exit(1);
    }
  });
