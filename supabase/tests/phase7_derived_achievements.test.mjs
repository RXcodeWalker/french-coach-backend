// Shop Phase 7 — DB integration tests for recompute_achievements /
// achievements_derived. Run against the LOCAL Supabase stack only.
//
// IMPORTANT: this proves the RPC's structure and safety properties
// (auth guard, idempotency, RLS/grant lockdown, and — critically — that it
// derives ZERO achievements today, by design, since every predicate in
// src/data/achievements.ts depends on a client-only signal). It is not a
// test of any real achievement-derivation logic, because none exists yet.
// See 20260812113000_phase7_derived_achievements.sql's header.
//
// Usage: node backend/supabase/tests/phase7_derived_achievements.test.mjs

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
  const email = `phase7b-${tag}-${randomUUID()}@example.test`;
  const password = 'test-password-12345';
  const { data, error } = await admin.auth.admin.createUser({ email, password, email_confirm: true });
  if (error) throw new Error(`createUser(${tag}) failed: ${error.message}`);
  const userId = data.user.id;

  const { error: profileErr } = await admin.from('profiles').upsert({ id: userId, username: `phase7b_${tag}_${userId.slice(0, 8)}` });
  if (profileErr) throw new Error(`profile upsert(${tag}) failed: ${profileErr.message}`);

  const client = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });
  const { error: signInErr } = await client.auth.signInWithPassword({ email, password });
  if (signInErr) throw new Error(`signIn(${tag}) failed: ${signInErr.message}`);

  return { userId, client };
}

async function main() {
  console.log('Creating test user...');
  const a = await createTestUser('a');

  console.log('\n1. recompute_achievements runs and returns an empty set for a fresh user (no rule fires today)');
  {
    const r = await a.client.rpc('recompute_achievements');
    ok(!r.error, `call succeeds${r.error ? ` (error: ${r.error.message})` : ''}`);
    ok(Array.isArray(r.data) && r.data.length === 0, `returns [] (got ${JSON.stringify(r.data)})`);
  }

  console.log('\n2. Repeated calls are idempotent (still empty, no error, no duplicate rows)');
  {
    await a.client.rpc('recompute_achievements');
    await a.client.rpc('recompute_achievements');
    const r = await a.client.rpc('recompute_achievements');
    ok(!r.error && r.data.length === 0, 'still empty after 3 calls');
    const { count } = await admin.from('achievements_derived').select('*', { count: 'exact', head: true }).eq('user_id', a.userId);
    ok(count === 0, 'no rows were ever written for this user');
  }

  console.log('\n3. Unauthenticated call is denied');
  {
    const anonClient = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });
    const r = await anonClient.rpc('recompute_achievements');
    ok(!!r.error, `anon call denied${r.error ? '' : ' (expected an error)'}`);
  }

  console.log('\n4. Client cannot write achievements_derived directly (RPC-only, matches gem_events/user_inventory pattern)');
  {
    const { error } = await a.client.from('achievements_derived').insert({
      user_id: a.userId, achievement_id: 'premier_pas', source: 'forged',
    });
    ok(!!error, `client-side INSERT denied${error ? '' : ' (expected an error)'}`);
  }

  console.log("\n5. A pre-seeded derived row (simulating a future real rule) round-trips through SELECT and survives recompute");
  {
    // Stands in for what a real future rule would eventually upsert itself.
    // Written directly via service-role/psql since no rule inserts yet —
    // this proves the table + read path + idempotent-upsert contract work
    // correctly, ahead of any real predicate existing.
    execSync(
      `docker exec supabase_db_French_2.0 psql -U postgres -d postgres -t -A -c "INSERT INTO achievements_derived (user_id, achievement_id, source) VALUES ('${a.userId}', 'premier_pas', 'fixture')"`,
      { encoding: 'utf8' }
    );
    const r = await a.client.rpc('recompute_achievements');
    ok(!r.error && r.data.includes('premier_pas'), `recompute_achievements reflects the pre-seeded row (got ${JSON.stringify(r.data)})`);
    const { data: selfRead } = await a.client.from('achievements_derived').select('achievement_id').eq('user_id', a.userId);
    ok(selfRead.length === 1 && selfRead[0].achievement_id === 'premier_pas', 'user can SELECT their own derived achievement row');
  }

  console.log('\n6. User B cannot read user A\'s derived achievements');
  {
    const b = await createTestUser('b');
    const { data, error } = await b.client.from('achievements_derived').select('*').eq('user_id', a.userId);
    ok(!error && data.length === 0, `B's read of A's rows returns nothing${error ? ` (error: ${error.message})` : ''}`);
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
