// League Power prerequisite — XP event hardening DB integration tests
// (plan i-am-implementing-phase-hashed-karp.md, Part A5). Run against the
// LOCAL Supabase stack only (`npx supabase start` from backend/), never the
// hosted project.
//
// Usage: node backend/supabase/tests/league_xp_hardening.test.mjs
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
  const email = `leaguexp-${tag}-${randomUUID()}@example.test`;
  const password = 'test-password-12345';
  const { data, error } = await admin.auth.admin.createUser({ email, password, email_confirm: true });
  if (error) throw new Error(`createUser(${tag}) failed: ${error.message}`);
  const userId = data.user.id;

  const { error: profileErr } = await admin.from('profiles').upsert({ id: userId, username: `leaguexp_${tag}_${userId.slice(0, 8)}` });
  if (profileErr) throw new Error(`profile upsert(${tag}) failed: ${profileErr.message}`);

  const client = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });
  const { error: signInErr } = await client.auth.signInWithPassword({ email, password });
  if (signInErr) throw new Error(`signIn(${tag}) failed: ${signInErr.message}`);

  return { userId, client };
}

function key(tag) {
  return `xp-${Date.now()}-${tag}-${Math.random().toString(36).slice(2, 8)}`;
}

async function main() {
  console.log('Creating test users...');
  const a = await createTestUser('a');
  const b = await createTestUser('b');

  console.log('\n1. Happy path: practice event inserts, occurred_at clamped into 48h window');
  {
    const farPast = new Date(Date.now() - 1000 * 86400000).toISOString(); // ~1000 days ago
    const idem = key('happy');
    const r = await a.client.rpc('submit_xp_event', {
      p_source: 'practice', p_amount: 20, p_idempotency_key: idem, p_occurred_at: farPast, p_metadata: {},
    });
    ok(!r.error && r.data.ok && r.data.awarded === true, `happy path succeeds${r.error ? ` (${r.error.message})` : ''}`);

    const { data: row } = await admin.from('xp_events').select('*').eq('id', `${a.userId}:${idem}`).single();
    ok(!!row, 'row stored under namespaced id');
    const occurredAtMs = new Date(row.occurred_at).getTime();
    const boundMs = Date.now() - 48 * 3600_000;
    ok(occurredAtMs >= boundMs - 5000, `occurred_at clamped to within 48h window (got ${row.occurred_at})`);
  }

  console.log('\n2. Reject daily_challenge/friend_challenge sources');
  {
    const r1 = await a.client.rpc('submit_xp_event', {
      p_source: 'daily_challenge', p_amount: 20, p_idempotency_key: key('dc'), p_occurred_at: new Date().toISOString(), p_metadata: {},
    });
    ok(r1.error && /source_not_client_submittable/.test(r1.error.message), `daily_challenge rejected${r1.error ? '' : ' (expected an error)'}`);

    const r2 = await a.client.rpc('submit_xp_event', {
      p_source: 'friend_challenge', p_amount: 20, p_idempotency_key: key('fc'), p_occurred_at: new Date().toISOString(), p_metadata: {},
    });
    ok(r2.error && /source_not_client_submittable/.test(r2.error.message), `friend_challenge rejected${r2.error ? '' : ' (expected an error)'}`);
  }

  console.log('\n3. Idempotent retry after cap exhaustion');
  {
    const u = await createTestUser('capretry');
    const firstKey = key('first');
    const rFirst = await u.client.rpc('submit_xp_event', {
      p_source: 'practice', p_amount: 100, p_idempotency_key: firstKey, p_occurred_at: new Date().toISOString(), p_metadata: {},
    });
    ok(!rFirst.error && rFirst.data.awarded === true, `initial submit succeeds${rFirst.error ? ` (${rFirst.error.message})` : ''}`);

    // Push the account to/near the cap with distinct events.
    let i = 0;
    let lastOk = true;
    while (lastOk && i < 40) {
      const r = await u.client.rpc('submit_xp_event', {
        p_source: 'practice', p_amount: 75, p_idempotency_key: key(`fill${i}`), p_occurred_at: new Date().toISOString(), p_metadata: {},
      });
      lastOk = !r.error;
      i++;
    }
    ok(i > 1, `account pushed near/over cap via ${i} fill submissions`);

    // Retry the FIRST (already-successful) idempotency key -- must return
    // awarded:false, never rolling_24h_xp_cap_exceeded, even though the
    // account is now over the cap.
    const rRetry = await u.client.rpc('submit_xp_event', {
      p_source: 'practice', p_amount: 100, p_idempotency_key: firstKey, p_occurred_at: new Date().toISOString(), p_metadata: {},
    });
    ok(!rRetry.error && rRetry.data.ok === true && rRetry.data.awarded === false, `retry of already-recorded event succeeds with awarded:false, not capped${rRetry.error ? ` (${rRetry.error.message})` : ` (got awarded=${rRetry.data?.awarded})`}`);
  }

  console.log('\n4. Concurrent cap submissions from the same user are serialized');
  {
    const u = await createTestUser('concurrent');
    // Each submission is 400; sequential execution allows at most 7 before
    // exceeding 3000 (7*400=2800, 8*400=3200>3000).
    const amount = 400;
    const n = 12;
    const calls = Array.from({ length: n }, (_, i) =>
      u.client.rpc('submit_xp_event', {
        p_source: 'practice', p_amount: amount, p_idempotency_key: key(`conc${i}`), p_occurred_at: new Date().toISOString(), p_metadata: {},
      })
    );
    const results = await Promise.all(calls);
    const succeeded = results.filter(r => !r.error && r.data?.awarded === true).length;
    const capped = results.filter(r => r.error && /rolling_24h_xp_cap_exceeded/.test(r.error.message)).length;
    ok(succeeded + capped === n, `every concurrent call either succeeds or is cleanly capped (succeeded=${succeeded}, capped=${capped}, total=${n})`);
    ok(succeeded === 7, `exactly 7 succeed (matches sequential-execution expectation), proving the advisory lock serializes (got ${succeeded})`);

    const { data: sumRows } = await admin.from('xp_events').select('amount').eq('user_id', u.userId).gt('amount', 0);
    const total = (sumRows ?? []).reduce((s, r) => s + r.amount, 0);
    ok(total <= 3000, `final summed positive total never exceeds 3000 (got ${total})`);
  }

  console.log('\n5. Cross-user idempotency collision: identical raw key, different users');
  {
    const rawKey = key('shared-raw');
    const rA = await a.client.rpc('submit_xp_event', {
      p_source: 'practice', p_amount: 10, p_idempotency_key: rawKey, p_occurred_at: new Date().toISOString(), p_metadata: {},
    });
    const rB = await b.client.rpc('submit_xp_event', {
      p_source: 'practice', p_amount: 10, p_idempotency_key: rawKey, p_occurred_at: new Date().toISOString(), p_metadata: {},
    });
    ok(!rA.error && rA.data.awarded === true, `user a succeeds with raw key${rA.error ? ` (${rA.error.message})` : ''}`);
    ok(!rB.error && rB.data.awarded === true, `user b succeeds with the SAME raw key${rB.error ? ` (${rB.error.message})` : ''}`);

    const { data: rowA } = await admin.from('xp_events').select('*').eq('id', `${a.userId}:${rawKey}`).single();
    const { data: rowB } = await admin.from('xp_events').select('*').eq('id', `${b.userId}:${rawKey}`).single();
    ok(!!rowA && !!rowB && rowA.id !== rowB.id, 'two separate rows exist, namespaced differently');
    ok(rowA.user_id === a.userId && rowB.user_id === b.userId, 'neither row can affect the others user_id');
  }

  console.log('\n6. Legacy-event migration/retry compatibility: same key/user submitted twice');
  {
    const u = await createTestUser('legacyretry');
    const idem = key('legacy');
    const r1 = await u.client.rpc('submit_xp_event', {
      p_source: 'practice', p_amount: 20, p_idempotency_key: idem, p_occurred_at: new Date().toISOString(), p_metadata: {},
    });
    const r2 = await u.client.rpc('submit_xp_event', {
      p_source: 'practice', p_amount: 20, p_idempotency_key: idem, p_occurred_at: new Date().toISOString(), p_metadata: {},
    });
    ok(!r1.error && r1.data.awarded === true, `first call succeeds${r1.error ? ` (${r1.error.message})` : ''}`);
    ok(!r2.error && r2.data.awarded === false, `second call (simulated lost-response retry) reports awarded:false, no error${r2.error ? ` (${r2.error.message})` : ''}`);

    const { count } = await admin.from('xp_events').select('*', { count: 'exact', head: true }).eq('id', `${u.userId}:${idem}`);
    ok(count === 1, `exactly one row exists (got ${count})`);
  }

  console.log('\n7. Cross-user forcing via metadata: user_id always caller\'s own auth.uid()');
  {
    const idem = key('forge');
    const r = await a.client.rpc('submit_xp_event', {
      p_source: 'practice', p_amount: 10, p_idempotency_key: idem, p_occurred_at: new Date().toISOString(),
      p_metadata: { user_id: b.userId, forged: true },
    });
    ok(!r.error, `call with forged user_id in metadata still succeeds${r.error ? ` (${r.error.message})` : ''}`);
    const { data: row } = await admin.from('xp_events').select('*').eq('id', `${a.userId}:${idem}`).single();
    ok(!!row && row.user_id === a.userId, `inserted row's user_id is caller's own auth.uid(), not the metadata value (got ${row?.user_id})`);
  }

  console.log('\n8. Negative amounts never count toward the rolling cap');
  {
    const u = await createTestUser('negative');
    // Submit enough negative events that a naive SUM(amount) would look low.
    // amount is CHECKed to [-100, 500] on xp_events, so -80 x 5 = -400.
    for (let i = 0; i < 5; i++) {
      const r = await u.client.rpc('submit_xp_event', {
        p_source: 'daily_news', p_amount: -80, p_idempotency_key: key(`neg${i}`), p_occurred_at: new Date().toISOString(), p_metadata: {},
      });
      if (r.error) throw new Error(`unexpected negative-submit failure: ${r.error.message}`);
    }
    // Now push close to the positive cap (3000) using amounts within the
    // table's own 500 ceiling; if negatives wrongly offset the cap sum,
    // this would incorrectly succeed further than it should, or the
    // boundary would be off by the negative total (400).
    for (let i = 0; i < 5; i++) {
      const r = await u.client.rpc('submit_xp_event', {
        p_source: 'practice', p_amount: 500, p_idempotency_key: key(`fillpos${i}`), p_occurred_at: new Date().toISOString(), p_metadata: {},
      });
      if (r.error) throw new Error(`unexpected fill-submit failure at i=${i}: ${r.error.message}`);
    }
    // Positive sum is now exactly 2500 (2500+499=2999<=3000 succeeds; +500 would be 3000, still <=3000).
    const rBoundary = await u.client.rpc('submit_xp_event', {
      p_source: 'practice', p_amount: 499, p_idempotency_key: key('boundary'), p_occurred_at: new Date().toISOString(), p_metadata: {},
    });
    ok(!rBoundary.error && rBoundary.data.awarded === true, `positive sum reaches exactly 2999 (well within cap), unaffected by the -400 of prior negative events${rBoundary.error ? ` (${rBoundary.error.message})` : ''}`);

    const rOver = await u.client.rpc('submit_xp_event', {
      p_source: 'practice', p_amount: 2, p_idempotency_key: key('tips'), p_occurred_at: new Date().toISOString(), p_metadata: {},
    });
    ok(rOver.error && /rolling_24h_xp_cap_exceeded/.test(rOver.error.message), `next submission correctly capped against the POSITIVE-only sum (2999), not artificially offset by negatives${rOver.error ? '' : ' (expected an error)'}`);
  }

  console.log('\n9. Direct-insert lockdown: authenticated client cannot INSERT xp_events directly');
  {
    const { error } = await a.client.from('xp_events').insert({
      id: `${a.userId}:${key('direct')}`, user_id: a.userId, amount: 10, source: 'practice', occurred_at: new Date().toISOString(),
    });
    ok(!!error, `direct client insert fails, proving table-level REVOKE INSERT took effect${error ? '' : ' (expected an error)'}`);
  }

  console.log('\n10. anon/authenticated both denied on award_xp itself (regression check)');
  {
    const anonClient = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });
    const rAnon = await anonClient.rpc('award_xp', { p_user_id: a.userId, p_source: 'practice', p_amount: 10, p_idempotency_key: key('anonaward') });
    ok(!!rAnon.error, `anon denied on award_xp${rAnon.error ? '' : ' (expected an error)'}`);

    const rAuth = await a.client.rpc('award_xp', { p_user_id: a.userId, p_source: 'practice', p_amount: 10, p_idempotency_key: key('authaward') });
    ok(!!rAuth.error, `authenticated denied on award_xp${rAuth.error ? '' : ' (expected an error)'}`);
  }

  console.log('\n11. anon denied on submit_xp_event');
  {
    const anonClient = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });
    const rAnon = await anonClient.rpc('submit_xp_event', {
      p_source: 'practice', p_amount: 10, p_idempotency_key: key('anonsubmit'), p_occurred_at: new Date().toISOString(), p_metadata: {},
    });
    ok(!!rAnon.error, `anon denied on submit_xp_event${rAnon.error ? '' : ' (expected an error)'}`);
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
