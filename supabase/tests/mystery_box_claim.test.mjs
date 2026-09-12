// Reliability plan §2.6 (Option B) — claim_mystery_box DB integration tests.
// Run against the LOCAL Supabase stack only (`npx supabase start` from backend/),
// never the hosted project.
//
// Usage: node backend/supabase/tests/mystery_box_claim.test.mjs
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

async function createTestUser(tag) {
  const email = `mysterybox-${tag}-${randomUUID()}@example.test`;
  const password = 'test-password-12345';
  const { data, error } = await admin.auth.admin.createUser({ email, password, email_confirm: true });
  if (error) throw new Error(`createUser(${tag}) failed: ${error.message}`);
  const userId = data.user.id;

  const { error: profileErr } = await admin.from('profiles').upsert({ id: userId, username: `mysterybox_${tag}_${userId.slice(0, 8)}` });
  if (profileErr) throw new Error(`profile upsert(${tag}) failed: ${profileErr.message}`);

  const client = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });
  const { error: signInErr } = await client.auth.signInWithPassword({ email, password });
  if (signInErr) throw new Error(`signIn(${tag}) failed: ${signInErr.message}`);

  return { userId, client };
}

async function main() {
  console.log('Creating two test users...');
  const a = await createTestUser('a');
  const b = await createTestUser('b');

  console.log('\n1. claim_mystery_box requires auth — anon call fails');
  {
    const anon = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });
    const r = await anon.rpc('claim_mystery_box');
    ok(!!r.error, 'anon call is rejected');
  }

  console.log('\n2. first claim of the day succeeds, awards XP once, and mints a valid reward amount');
  let firstAmount;
  {
    const r = await a.client.rpc('claim_mystery_box');
    ok(!r.error && r.data.ok, `claim succeeds${r.error ? ` (${r.error.message})` : ''}`);
    ok(r.data.already_claimed === false, 'first claim reports already_claimed: false');
    ok([50, 100, 250].includes(r.data.xp_awarded), `xp_awarded is one of the reward tiers (got ${r.data.xp_awarded})`);
    firstAmount = r.data.xp_awarded;

    const { count } = await admin.from('mystery_box_claims').select('*', { count: 'exact', head: true }).eq('user_id', a.userId);
    ok(count === 1, 'exactly one mystery_box_claims row for this user');

    const { data: events } = await admin.from('xp_events').select('*').eq('user_id', a.userId).eq('source', 'mystery_box');
    ok(events.length === 1, 'exactly one xp_events row minted for mystery_box');
    ok(events[0].amount === firstAmount, 'xp_events amount matches the claimed reward');
  }

  console.log('\n3. a second claim the same UTC day is idempotent — no new row, no double XP');
  {
    const r = await a.client.rpc('claim_mystery_box');
    ok(!r.error && r.data.ok, `repeat call still succeeds${r.error ? ` (${r.error.message})` : ''}`);
    ok(r.data.already_claimed === true, 'repeat claim reports already_claimed: true');
    ok(r.data.xp_awarded === firstAmount, 'repeat claim reports the same original amount, not a new roll');

    const { count } = await admin.from('mystery_box_claims').select('*', { count: 'exact', head: true }).eq('user_id', a.userId);
    ok(count === 1, 'still exactly one mystery_box_claims row (no duplicate)');

    const { data: events } = await admin.from('xp_events').select('*').eq('user_id', a.userId).eq('source', 'mystery_box');
    ok(events.length === 1, 'still exactly one xp_events row (no double award)');
  }

  console.log('\n4. two different users each get their own independent daily claim');
  {
    const r = await b.client.rpc('claim_mystery_box');
    ok(!r.error && r.data.ok && r.data.already_claimed === false, `user b claims independently${r.error ? ` (${r.error.message})` : ''}`);
  }

  console.log('\n5. submit_xp_event rejects a direct client-submitted mystery_box source (amount can no longer be self-claimed)');
  {
    const r = await a.client.rpc('submit_xp_event', {
      p_source: 'mystery_box',
      p_amount: 999999,
      p_idempotency_key: `forged-${randomUUID()}`,
      p_occurred_at: new Date().toISOString(),
    });
    ok(!!r.error, 'direct submit_xp_event call for mystery_box is rejected');
  }

  console.log(`\n${pass} passed, ${fail} failed`);
  if (fail > 0) {
    console.log('\nFailures:');
    failures.forEach(f => console.log(`  - ${f}`));
    process.exit(1);
  }
}

main().catch(err => {
  console.error('Test run crashed:', err);
  process.exit(1);
});
