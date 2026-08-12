// Shop Phase 7 — DB integration tests for mint_gems_from_envelope (plan §16
// pattern, extended for this phase). Run against the LOCAL Supabase stack
// only (`npx supabase start` from backend/), never the hosted project.
//
// IMPORTANT — read before trusting these results: scoring_envelopes is
// empty in every real environment today (content_provenance is gated to
// 'original-practice', which does not exist yet — see this migration's own
// header, 20260812110000_phase7_envelope_mint.sql). Every envelope row
// below is a FIXTURE inserted directly via the service-role admin client,
// standing in for what server/index.ts would eventually write with
// SUPABASE_SERVICE_KEY once the Assessment Engine produces real
// original-practice attempts. A clean pass here proves the RPC's mechanics
// (idempotency, provenance guard, amount derivation, auth) are correct
// against the documented shape — it does NOT prove integrity against real
// student work, because no real work exists yet to test against. Treat
// this as infrastructure verification, not the Phase 7 production gate.
//
// Usage: node backend/supabase/tests/phase7_envelope_mint.test.mjs
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
  const email = `phase7-${tag}-${randomUUID()}@example.test`;
  const password = 'test-password-12345';
  const { data, error } = await admin.auth.admin.createUser({ email, password, email_confirm: true });
  if (error) throw new Error(`createUser(${tag}) failed: ${error.message}`);
  const userId = data.user.id;

  const { error: profileErr } = await admin.from('profiles').upsert({ id: userId, username: `phase7_${tag}_${userId.slice(0, 8)}` });
  if (profileErr) throw new Error(`profile upsert(${tag}) failed: ${profileErr.message}`);

  const client = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });
  const { error: signInErr } = await client.auth.signInWithPassword({ email, password });
  if (signInErr) throw new Error(`signIn(${tag}) failed: ${signInErr.message}`);

  return { userId, client };
}

/** Minimal fixture envelope — only the columns/jsonb fields mint_gems_from_envelope reads. */
function fixtureEnvelopeRow(userId, { attemptId = randomUUID(), sessionId = randomUUID(), total = 28, provenance = 'original-practice' } = {}) {
  return {
    attempt_id: attemptId,
    session_id: sessionId,
    user_id: userId,
    content_provenance: provenance,
    envelope: { total, attemptId, sessionId },
  };
}

async function main() {
  console.log('Creating two test users...');
  const a = await createTestUser('a');
  const b = await createTestUser('b');

  console.log('\n1. Mint from a fixture original-practice envelope succeeds, amount derived from total');
  {
    const row = fixtureEnvelopeRow(a.userId, { total: 20 }); // 20/40 -> amount 10
    const { error: insErr } = await admin.from('scoring_envelopes').insert(row);
    ok(!insErr, `fixture envelope inserted${insErr ? ` (error: ${insErr.message})` : ''}`);

    const r = await a.client.rpc('mint_gems_from_envelope', { p_attempt_id: row.attempt_id });
    ok(!r.error && r.data.ok && !r.data.replayed, `mint succeeds, not replayed${r.error ? ` (error: ${r.error.message})` : ''}`);
    ok(r.data.amount === 10, `amount derived as 10 from total=20/40 (got ${r.data.amount})`);

    const { data: events } = await admin.from('gem_events').select('*').eq('user_id', a.userId).eq('kind', 'earn');
    ok(events.length === 1 && events[0].delta === 10, 'exactly one earn row, delta matches amount');
    ok(events[0].metadata?.attempt_id === row.attempt_id, 'metadata records the source attempt_id');
  }

  console.log('\n2. Replaying the same attempt_id mints nothing a second time');
  {
    const row = fixtureEnvelopeRow(a.userId, { total: 40 });
    await admin.from('scoring_envelopes').insert(row);
    const r1 = await a.client.rpc('mint_gems_from_envelope', { p_attempt_id: row.attempt_id });
    const r2 = await a.client.rpc('mint_gems_from_envelope', { p_attempt_id: row.attempt_id });
    ok(!r1.error && !r1.data.replayed, 'first call mints normally');
    ok(!r2.error && r2.data.replayed === true, 'second call returns replayed:true');
    ok(r1.data.balance === r2.data.balance, 'balance unchanged between the two calls');
    const { count } = await admin.from('gem_events').select('*', { count: 'exact', head: true }).eq('metadata->>attempt_id', row.attempt_id);
    ok(count === 1, 'exactly one gem_events row for this attempt, ever');
  }

  console.log('\n3. Perfect envelope (total=40) mints the same ceiling as a client-asserted perfect answer (20)');
  {
    const row = fixtureEnvelopeRow(a.userId, { total: 40 });
    await admin.from('scoring_envelopes').insert(row);
    const r = await a.client.rpc('mint_gems_from_envelope', { p_attempt_id: row.attempt_id });
    ok(!r.error && r.data.amount === 20, `amount is 20 for a perfect total (got ${r.error ? r.error.message : r.data.amount})`);
  }

  console.log('\n4. Zero-score envelope still mints the 1-gem floor, never 0');
  {
    const row = fixtureEnvelopeRow(a.userId, { total: 0 });
    await admin.from('scoring_envelopes').insert(row);
    const r = await a.client.rpc('mint_gems_from_envelope', { p_attempt_id: row.attempt_id });
    ok(!r.error && r.data.amount === 1, `amount floors at 1 for total=0 (got ${r.error ? r.error.message : r.data.amount})`);
  }

  console.log("\n5. Confidential-internal envelope is rejected — never mints from non-redistributable content");
  {
    // scoring_envelopes has a CHECK forcing content_provenance = 'original-practice',
    // so a confidential-internal row can never exist in this table by construction.
    // This proves the RPC's own belt-and-braces guard is unreachable dead code today,
    // which is the correct state — the CHECK is upstream of it. Insert is expected to
    // fail at the table level, not the RPC.
    const row = fixtureEnvelopeRow(a.userId, { provenance: 'confidential-internal' });
    const { error: insErr } = await admin.from('scoring_envelopes').insert(row);
    ok(!!insErr, `table CHECK itself rejects confidential-internal content_provenance${insErr ? '' : ' (expected an error, got none)'}`);
  }

  console.log('\n6. User B cannot mint from user A\'s envelope');
  {
    const row = fixtureEnvelopeRow(a.userId, { total: 20 });
    await admin.from('scoring_envelopes').insert(row);
    const r = await b.client.rpc('mint_gems_from_envelope', { p_attempt_id: row.attempt_id });
    ok(r.error && /unknown_envelope/.test(r.error.message), `B raises unknown_envelope for A's attempt_id${r.error ? '' : ' (expected an error)'}`);
  }

  console.log('\n7. Unknown attempt_id raises unknown_envelope, mints nothing');
  {
    const before = await admin.from('gem_events').select('*', { count: 'exact', head: true }).eq('user_id', a.userId);
    const r = await a.client.rpc('mint_gems_from_envelope', { p_attempt_id: randomUUID() });
    ok(r.error && /unknown_envelope/.test(r.error.message), 'raises unknown_envelope');
    const after = await admin.from('gem_events').select('*', { count: 'exact', head: true }).eq('user_id', a.userId);
    ok(before.count === after.count, 'no ledger row written');
  }

  console.log('\n8. EXECUTE denied for anon / PUBLIC');
  {
    const anonClient = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });
    const r = await anonClient.rpc('mint_gems_from_envelope', { p_attempt_id: randomUUID() });
    ok(!!r.error, `anon call denied${r.error ? '' : ' (expected an error)'}`);
  }

  console.log('\n9. Client cannot forge a scoring_envelopes row (A5 lockdown still holds)');
  {
    const { error } = await a.client.from('scoring_envelopes').insert(
      fixtureEnvelopeRow(a.userId, { total: 40 })
    );
    ok(!!error, `client-side INSERT into scoring_envelopes denied${error ? '' : ' (expected an error — A5 regression)'}`);
  }

  console.log('\n10. Malformed/out-of-range total is rejected');
  {
    const row = fixtureEnvelopeRow(a.userId, {});
    row.envelope = { total: 999, attemptId: row.attempt_id }; // out of the 0-40 rubric range
    await admin.from('scoring_envelopes').insert(row);
    const r = await a.client.rpc('mint_gems_from_envelope', { p_attempt_id: row.attempt_id });
    ok(r.error && /invalid_envelope_total/.test(r.error.message), `out-of-range total (999) rejected${r.error ? '' : ' (expected an error)'}`);
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
