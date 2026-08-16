// Phase 4 — Shadowing Mode: DB integration tests (implementation plan
// i-am-implementing-phase-sunny-lagoon.md §6). Run against the LOCAL
// Supabase stack only (`npx supabase start` from backend/).
//
// Usage: node backend/supabase/tests/phase4_shadowing.test.mjs
// Requires: local stack up (npx supabase start), reads keys from
// `npx supabase status -o json` at run time so nothing is hardcoded here.
//
// consume_shadowing_coaching_quota / release_shadowing_coaching_grant are
// service_role-only RPCs (mirroring how FastAPI calls them with the service
// key, never on behalf of a signed-in user) -- tests for those two call
// through `admin.rpc(...)`, not through a.client/b.client. get_shadowing_coaching_quota
// is the one authenticated-callable RPC and is exercised through the
// per-user clients.

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
  const email = `phase4-shadowing-${tag}-${randomUUID()}@example.test`;
  const password = 'test-password-12345';
  const { data, error } = await admin.auth.admin.createUser({ email, password, email_confirm: true });
  if (error) throw new Error(`createUser(${tag}) failed: ${error.message}`);
  const userId = data.user.id;

  const { error: profileErr } = await admin.from('profiles').upsert({ id: userId, username: `phase4_shad_${tag}_${userId.slice(0, 8)}` });
  if (profileErr) throw new Error(`profile upsert(${tag}) failed: ${profileErr.message}`);

  const client = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });
  const { error: signInErr } = await client.auth.signInWithPassword({ email, password });
  if (signInErr) throw new Error(`signIn(${tag}) failed: ${signInErr.message}`);

  return { userId, client };
}

function fixtureShadowingRow(userId, { id = randomUUID(), phraseId = 'shad_liaison_01', score = 82 } = {}) {
  return {
    id,
    user_id: userId,
    phrase_id: phraseId,
    provider: 'azure',
    assessor_version: 'pronunciation-v3',
    score,
    could_not_assess: false,
    sub_scores: { accuracy: 85, fluency: 80, completeness: 100, prosody: null },
    rhythm_metrics: { speechRateWpm: 140 },
    coaching_delivered: false,
  };
}

async function main() {
  console.log('Creating two test users...');
  const a = await createTestUser('a');
  const b = await createTestUser('b');

  console.log('\n1. Owner can INSERT + SELECT their own shadowing_attempts row');
  {
    const row = fixtureShadowingRow(a.userId);
    const { error: insErr } = await a.client.from('shadowing_attempts').insert(row);
    ok(!insErr, `insert succeeds${insErr ? ` (error: ${insErr.message})` : ''}`);
    const { data, error: selErr } = await a.client.from('shadowing_attempts').select('*').eq('id', row.id).single();
    ok(!selErr && data?.id === row.id, `owner can select own row${selErr ? ` (error: ${selErr.message})` : ''}`);
  }

  console.log("\n2. Cross-user RLS: B cannot SELECT A's row");
  {
    const row = fixtureShadowingRow(a.userId);
    await admin.from('shadowing_attempts').insert(row);
    const { data } = await b.client.from('shadowing_attempts').select('*').eq('id', row.id);
    ok((data ?? []).length === 0, "B's select of A's row returns empty");
  }

  console.log("\n3. Cross-user RLS: B cannot INSERT with user_id = A");
  {
    const row = fixtureShadowingRow(a.userId);
    const { error } = await b.client.from('shadowing_attempts').insert(row);
    ok(!!error, `B inserting as A denied${error ? '' : ' (expected an error)'}`);
  }

  console.log('\n4. ignoreDuplicates re-insert of the same id leaves exactly one row');
  {
    const row = fixtureShadowingRow(a.userId);
    await a.client.from('shadowing_attempts').upsert(row, { onConflict: 'id', ignoreDuplicates: true });
    await a.client.from('shadowing_attempts').upsert(row, { onConflict: 'id', ignoreDuplicates: true });
    const { count } = await admin.from('shadowing_attempts').select('*', { count: 'exact', head: true }).eq('id', row.id);
    ok(count === 1, `exactly one row after duplicate upsert (got ${count})`);
  }

  console.log('\n5. Append-only: authenticated UPDATE of an own row is denied');
  {
    const row = fixtureShadowingRow(a.userId);
    await admin.from('shadowing_attempts').insert(row);
    const { error, count } = await a.client.from('shadowing_attempts').update({ score: 99 }).eq('id', row.id).select('*', { count: 'exact' });
    const { data: after } = await admin.from('shadowing_attempts').select('score').eq('id', row.id).single();
    ok((!!error || (count ?? 0) === 0) && Number(after.score) === 82, `UPDATE denied or no-op, score unchanged${error ? '' : ` (count=${count})`}`);
  }

  console.log('\n6. Append-only: authenticated DELETE of an own row is denied');
  {
    const row = fixtureShadowingRow(a.userId);
    await admin.from('shadowing_attempts').insert(row);
    await a.client.from('shadowing_attempts').delete().eq('id', row.id);
    const { data: after } = await admin.from('shadowing_attempts').select('id').eq('id', row.id).maybeSingle();
    ok(after?.id === row.id, 'row still exists after client DELETE attempt');
  }

  console.log('\n7. consume_... grants on calls 1-3 with used = 1, 2, 3');
  {
    for (let i = 1; i <= 3; i++) {
      const r = await admin.rpc('consume_shadowing_coaching_quota', { p_user_id: a.userId, p_idempotency_key: `seq-${i}` });
      ok(!r.error && r.data.granted === true && r.data.used === i, `call ${i} granted, used=${i}${r.error ? ` (error: ${r.error.message})` : ` (got used=${r.data?.used})`}`);
    }
  }

  console.log("\n8. Call 4 (new key, same day) -> granted:false, reason:'daily_limit_reached'; row count stays 3");
  {
    const r = await admin.rpc('consume_shadowing_coaching_quota', { p_user_id: a.userId, p_idempotency_key: 'seq-4' });
    ok(!r.error && r.data.granted === false && r.data.reason === 'daily_limit_reached', `4th call denied with daily_limit_reached${r.error ? ` (error: ${r.error.message})` : ` (got ${JSON.stringify(r.data)})`}`);
    const { count } = await admin.from('shadowing_coaching_grants').select('*', { count: 'exact', head: true }).eq('user_id', a.userId);
    ok(count === 3, `row count stays 3 (got ${count})`);
  }

  console.log('\n9. Replay of a used key -> granted:true, replayed:true, no second row');
  {
    const r = await admin.rpc('consume_shadowing_coaching_quota', { p_user_id: a.userId, p_idempotency_key: 'seq-1' });
    ok(!r.error && r.data.granted === true && r.data.replayed === true, `replay grants, replayed:true${r.error ? ` (error: ${r.error.message})` : ''}`);
    const { count } = await admin.from('shadowing_coaching_grants').select('*', { count: 'exact', head: true }).eq('user_id', a.userId);
    ok(count === 3, `still exactly 3 rows after replay (got ${count})`);
  }

  console.log('\n10. Replay after the cap is hit still grants the paid key; a fresh key still denies');
  {
    const replay = await admin.rpc('consume_shadowing_coaching_quota', { p_user_id: a.userId, p_idempotency_key: 'seq-2' });
    ok(!replay.error && replay.data.granted === true, `already-paid key still grants after cap hit${replay.error ? ` (error: ${replay.error.message})` : ''}`);
    const fresh = await admin.rpc('consume_shadowing_coaching_quota', { p_user_id: a.userId, p_idempotency_key: 'seq-5' });
    ok(!fresh.error && fresh.data.granted === false, `fresh key still denied after cap hit${fresh.error ? ` (error: ${fresh.error.message})` : ''}`);
  }

  console.log('\n11. Concurrency at the boundary: 6 consume_... calls with 6 distinct keys -> exactly 3 granted:true');
  {
    const c = await createTestUser('concurrency');
    const keys = Array.from({ length: 6 }, (_, i) => `race-${i}`);
    const results = await Promise.all(keys.map(k =>
      admin.rpc('consume_shadowing_coaching_quota', { p_user_id: c.userId, p_idempotency_key: k })
    ));
    const grantedCount = results.filter(r => !r.error && r.data.granted === true).length;
    ok(grantedCount === 3, `exactly 3 of 6 concurrent calls granted (got ${grantedCount})`);
    const { count } = await admin.from('shadowing_coaching_grants').select('*', { count: 'exact', head: true }).eq('user_id', c.userId);
    ok(count === 3, `exactly 3 rows written (got ${count})`);
  }

  console.log('\n12. Refund: consume 3 -> release one -> used drops to 2 -> a fresh consume is granted again');
  {
    const d = await createTestUser('refund');
    await admin.rpc('consume_shadowing_coaching_quota', { p_user_id: d.userId, p_idempotency_key: 'r-1' });
    await admin.rpc('consume_shadowing_coaching_quota', { p_user_id: d.userId, p_idempotency_key: 'r-2' });
    await admin.rpc('consume_shadowing_coaching_quota', { p_user_id: d.userId, p_idempotency_key: 'r-3' });
    const rel = await admin.rpc('release_shadowing_coaching_grant', { p_user_id: d.userId, p_idempotency_key: 'r-2' });
    ok(!rel.error && rel.data.released === true && rel.data.used === 2, `release succeeds, used drops to 2${rel.error ? ` (error: ${rel.error.message})` : ` (got ${JSON.stringify(rel.data)})`}`);
    const fresh = await admin.rpc('consume_shadowing_coaching_quota', { p_user_id: d.userId, p_idempotency_key: 'r-4' });
    ok(!fresh.error && fresh.data.granted === true, `a fresh consume is granted again after refund${fresh.error ? ` (error: ${fresh.error.message})` : ''}`);
  }

  console.log("\n13. Refund idempotency: release_... twice returns released:false the second time; no negative count");
  {
    const e = await createTestUser('refund-idem');
    await admin.rpc('consume_shadowing_coaching_quota', { p_user_id: e.userId, p_idempotency_key: 'i-1' });
    const r1 = await admin.rpc('release_shadowing_coaching_grant', { p_user_id: e.userId, p_idempotency_key: 'i-1' });
    const r2 = await admin.rpc('release_shadowing_coaching_grant', { p_user_id: e.userId, p_idempotency_key: 'i-1' });
    ok(!r1.error && r1.data.released === true, 'first release: released:true');
    ok(!r2.error && r2.data.released === false, `second release: released:false${r2.error ? ` (error: ${r2.error.message})` : ''}`);
    ok(r2.data.used === 0, `used stays at 0, never negative (got ${r2.data?.used})`);
  }

  console.log("\n14. Refund isolation: releasing A's grant does not change B's count");
  {
    const f1 = await createTestUser('iso-f1');
    const f2 = await createTestUser('iso-f2');
    await admin.rpc('consume_shadowing_coaching_quota', { p_user_id: f1.userId, p_idempotency_key: 'k-1' });
    await admin.rpc('consume_shadowing_coaching_quota', { p_user_id: f2.userId, p_idempotency_key: 'k-1' });
    await admin.rpc('release_shadowing_coaching_grant', { p_user_id: f1.userId, p_idempotency_key: 'k-1' });
    const { count } = await admin.from('shadowing_coaching_grants').select('*', { count: 'exact', head: true }).eq('user_id', f2.userId);
    ok(count === 1, `B's count unaffected by A's release (got ${count})`);
  }

  console.log('\n15. authenticated cannot EXECUTE consume_... or release_...');
  {
    const r1 = await a.client.rpc('consume_shadowing_coaching_quota', { p_user_id: a.userId, p_idempotency_key: 'deny-1' });
    ok(!!r1.error, `authenticated consume_... denied${r1.error ? '' : ' (expected an error)'}`);
    const r2 = await a.client.rpc('release_shadowing_coaching_grant', { p_user_id: a.userId, p_idempotency_key: 'deny-1' });
    ok(!!r2.error, `authenticated release_... denied${r2.error ? '' : ' (expected an error)'}`);
  }

  console.log('\n16. anon cannot EXECUTE consume_... or release_...');
  {
    const anonClient = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });
    const r1 = await anonClient.rpc('consume_shadowing_coaching_quota', { p_user_id: a.userId, p_idempotency_key: 'deny-2' });
    ok(!!r1.error, `anon consume_... denied${r1.error ? '' : ' (expected an error)'}`);
    const r2 = await anonClient.rpc('release_shadowing_coaching_grant', { p_user_id: a.userId, p_idempotency_key: 'deny-2' });
    ok(!!r2.error, `anon release_... denied${r2.error ? '' : ' (expected an error)'}`);
  }

  console.log("\n17. get_shadowing_coaching_quota() returns the caller's own count only; anon gets not_authenticated");
  {
    const r = await a.client.rpc('get_shadowing_coaching_quota');
    ok(!r.error && r.data.used === 3 && r.data.limit === 3, `A's own quota reads used=3,limit=3${r.error ? ` (error: ${r.error.message})` : ` (got ${JSON.stringify(r.data)})`}`);
    // anon has no EXECUTE grant at all on get_shadowing_coaching_quota (only
    // `authenticated` does), so PostgREST denies at the permission layer
    // (42501) before the function body's own not_authenticated check would
    // ever run -- a stricter outcome than the function's internal guard.
    const anonClient = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });
    const rAnon = await anonClient.rpc('get_shadowing_coaching_quota');
    ok(!!rAnon.error, `anon call denied${rAnon.error ? '' : ' (expected an error)'}`);
  }

  console.log("\n18. Quota isolation: A's 3 grants do not affect B's used");
  {
    const rB = await b.client.rpc('get_shadowing_coaching_quota');
    ok(!rB.error && rB.data.used === 0, `B's quota is independent of A's (got used=${rB.data?.used})`);
  }

  console.log("\n19. xp_events accepts source='shadowing' and rejects source='shadowing_bogus'");
  {
    const goodId = randomUUID();
    const { error: goodErr } = await admin.from('xp_events').insert({ id: goodId, user_id: a.userId, amount: 5, source: 'shadowing', occurred_at: new Date().toISOString() });
    ok(!goodErr, `insert with source='shadowing' succeeds${goodErr ? ` (error: ${goodErr.message})` : ''}`);
    const { error: badErr } = await admin.from('xp_events').insert({ id: randomUUID(), user_id: a.userId, amount: 5, source: 'shadowing_bogus', occurred_at: new Date().toISOString() });
    ok(!!badErr, `insert with source='shadowing_bogus' rejected${badErr ? '' : ' (expected an error)'}`);
  }

  console.log("\n20. submit_xp_event('shadowing', ...) succeeds for an authenticated caller");
  {
    const r = await a.client.rpc('submit_xp_event', {
      p_source: 'shadowing', p_amount: 5, p_idempotency_key: `shad-${randomUUID()}`,
      p_occurred_at: new Date().toISOString(), p_metadata: {},
    });
    ok(!r.error && r.data.ok === true, `submit_xp_event('shadowing', ...) succeeds${r.error ? ` (error: ${r.error.message})` : ''}`);
  }

  console.log("\n21. Cap is source-agnostic: 2900 XP of source='practice' + 200 of 'shadowing' -> cap exceeded");
  {
    const g = await createTestUser('cap-agnostic');
    // xp_events.amount is CHECKed to [-100, 500] per row, so 2900 XP must be
    // seeded across multiple rows (500 x 5 + 400).
    const seedRows = [500, 500, 500, 500, 500, 400].map(amount => ({
      id: randomUUID(), user_id: g.userId, amount, source: 'practice', occurred_at: new Date().toISOString(),
    }));
    const { error: seedErr } = await admin.from('xp_events').insert(seedRows);
    ok(!seedErr, `seed 2900 practice XP inserted${seedErr ? ` (error: ${seedErr.message})` : ''}`);
    const r = await g.client.rpc('submit_xp_event', {
      p_source: 'shadowing', p_amount: 200, p_idempotency_key: `over-${randomUUID()}`,
      p_occurred_at: new Date().toISOString(), p_metadata: {},
    });
    ok(r.error && /rolling_24h_xp_cap_exceeded/.test(r.error.message), `adding a new source cannot widen the cap${r.error ? '' : ' (expected rolling_24h_xp_cap_exceeded)'}`);
  }

  console.log('\n22. Profile delete cascades away both shadowing_attempts and shadowing_coaching_grants');
  {
    const h = await createTestUser('cascade');
    const row = fixtureShadowingRow(h.userId);
    await admin.from('shadowing_attempts').insert(row);
    await admin.rpc('consume_shadowing_coaching_quota', { p_user_id: h.userId, p_idempotency_key: 'cascade-1' });

    await admin.auth.admin.deleteUser(h.userId);

    const { count: attemptsCount } = await admin.from('shadowing_attempts').select('*', { count: 'exact', head: true }).eq('user_id', h.userId);
    const { count: grantsCount } = await admin.from('shadowing_coaching_grants').select('*', { count: 'exact', head: true }).eq('user_id', h.userId);
    ok(attemptsCount === 0, `shadowing_attempts cascaded away (got ${attemptsCount})`);
    ok(grantsCount === 0, `shadowing_coaching_grants cascaded away (got ${grantsCount})`);
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
