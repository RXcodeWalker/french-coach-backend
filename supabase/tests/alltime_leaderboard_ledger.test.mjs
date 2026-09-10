// Phase 1.3 — ledger-backed all-time leaderboard DB integration tests.
// Run against the LOCAL Supabase stack only (`npx supabase start` from
// backend/), never the hosted project.
//
// Proves: (1) `authenticated` can no longer UPDATE profiles.total_xp (or the
// other stat columns) — 42501; (2) the all_time_leaderboard view total for a
// user equals xp_baseline + SUM(xp_events.amount) after inserting ledger rows
// via submit_xp_event; (3) a direct PATCH cannot move the board;
// (4) achievements stays client-writable (deliberate — see the migration).
//
// Usage: node backend/supabase/tests/alltime_leaderboard_ledger.test.mjs
// Requires: local stack up (npx supabase start), reads keys from
// `npx supabase status -o json` at run time so nothing is hardcoded here.

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

async function createTestUser(tag, { baseline = 0 } = {}) {
  const email = `altxp-${tag}-${randomUUID()}@example.test`;
  const password = 'test-password-12345';
  const { data, error } = await admin.auth.admin.createUser({ email, password, email_confirm: true });
  if (error) throw new Error(`createUser(${tag}) failed: ${error.message}`);
  const userId = data.user.id;

  const { error: profileErr } = await admin.from('profiles').upsert({
    id: userId,
    username: `altxp_${tag}_${userId.slice(0, 8)}`,
    leaderboard_visibility: 'global',
    xp_baseline: baseline,
  });
  if (profileErr) throw new Error(`profile upsert(${tag}) failed: ${profileErr.message}`);

  const client = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });
  const { error: signInErr } = await client.auth.signInWithPassword({ email, password });
  if (signInErr) throw new Error(`signIn(${tag}) failed: ${signInErr.message}`);

  return { userId, client };
}

function idemKey(tag) {
  return `xp-${Date.now()}-${tag}-${Math.random().toString(36).slice(2, 8)}`;
}

async function viewTotalFor(userId) {
  const { data } = await admin
    .from('all_time_leaderboard')
    .select('total_xp')
    .eq('user_id', userId)
    .maybeSingle();
  return data?.total_xp ?? null;
}

async function main() {
  console.log('Creating test users (A baseline 1000, B baseline 0)...');
  const a = await createTestUser('a', { baseline: 1000 });
  const b = await createTestUser('b', { baseline: 0 });

  console.log('\n1. authenticated can no longer UPDATE profiles.total_xp');
  {
    const { error } = await a.client.from('profiles').update({ total_xp: 999999 }).eq('id', a.userId);
    ok(!!error && /permission denied|42501/i.test(error.message + (error.code ?? '')),
      `PATCH total_xp is denied${error ? '' : ' (expected 42501)'}`);
  }

  console.log('\n2. the other stat columns are also no longer client-writable');
  {
    for (const col of ['current_level', 'streak_days', 'longest_streak', 'sessions_count', 'total_words_spoken']) {
      const { error } = await a.client.from('profiles').update({ [col]: 5 }).eq('id', a.userId);
      ok(!!error, `PATCH ${col} is denied${error ? '' : ' (expected an error)'}`);
    }
  }

  console.log('\n3. achievements IS still client-writable (deliberate — no server derivation yet)');
  {
    const { error } = await a.client.from('profiles').update({ achievements: ['first_session'] }).eq('id', a.userId);
    ok(!error, `PATCH achievements succeeds${error ? ` (unexpected error: ${error.message})` : ''}`);
  }

  console.log('\n4. view total = xp_baseline with no ledger rows');
  {
    ok((await viewTotalFor(a.userId)) === 1000, `A's board total is its baseline (1000)`);
    ok((await viewTotalFor(b.userId)) === 0, `B's board total is 0`);
  }

  console.log('\n5. submit_xp_event rows move the board by exactly their summed amount');
  {
    const r1 = await b.client.rpc('submit_xp_event', {
      p_source: 'practice', p_amount: 30, p_idempotency_key: idemKey('b1'),
      p_occurred_at: new Date().toISOString(), p_metadata: {},
    });
    const r2 = await b.client.rpc('submit_xp_event', {
      p_source: 'exam', p_amount: 45, p_idempotency_key: idemKey('b2'),
      p_occurred_at: new Date().toISOString(), p_metadata: {},
    });
    ok(!r1.error && !r2.error, `two ledger events inserted${r1.error || r2.error ? ` (${(r1.error || r2.error).message})` : ''}`);

    const { data: rows } = await admin.from('xp_events').select('amount').eq('user_id', b.userId);
    const sum = (rows ?? []).reduce((s, x) => s + x.amount, 0);
    ok(sum === 75, `xp_events sum for B is 75 (got ${sum})`);
    ok((await viewTotalFor(b.userId)) === 75, `B's board total is 0 baseline + 75 ledger = 75`);
  }

  console.log('\n6. a negative ledger event reduces the board total (no floor at the view)');
  {
    const r = await b.client.rpc('submit_xp_event', {
      p_source: 'daily_news', p_amount: -5, p_idempotency_key: idemKey('b3'),
      p_occurred_at: new Date().toISOString(), p_metadata: {},
    });
    ok(!r.error, `negative event inserted${r.error ? ` (${r.error.message})` : ''}`);
    ok((await viewTotalFor(b.userId)) === 70, `B's board total is 75 - 5 = 70`);
  }

  console.log('\n7. A: baseline + ledger compose');
  {
    const r = await a.client.rpc('submit_xp_event', {
      p_source: 'practice', p_amount: 50, p_idempotency_key: idemKey('a1'),
      p_occurred_at: new Date().toISOString(), p_metadata: {},
    });
    ok(!r.error, `A ledger event inserted${r.error ? ` (${r.error.message})` : ''}`);
    ok((await viewTotalFor(a.userId)) === 1050, `A's board total is 1000 baseline + 50 ledger = 1050`);
  }

  console.log('\n8. a direct PATCH still cannot move the board (total_xp column is dead to the view)');
  {
    // total_xp is not client-writable (test 1), and even a service-role write
    // to it is ignored by the view — the view no longer reads that column.
    await admin.from('profiles').update({ total_xp: 5_000_000 }).eq('id', b.userId);
    ok((await viewTotalFor(b.userId)) === 70, `B's board total unchanged at 70 despite total_xp = 5,000,000`);
  }

  console.log(`\n${pass} passed, ${fail} failed`);
  if (fail > 0) {
    console.log('\nFailures:');
    failures.forEach((f) => console.log(`  - ${f}`));
    process.exit(1);
  }
}

main().catch((err) => {
  console.error('Test run crashed:', err);
  process.exit(1);
});
