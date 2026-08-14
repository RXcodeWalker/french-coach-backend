// Daily Challenge Phase 1 — DB integration tests (revised plan, all fixes).
// Run against the LOCAL Supabase stack only (`npx supabase start` from backend/),
// never the hosted project.
//
// Every scoring_envelopes/igcse_question_sets row below is a FIXTURE inserted
// directly via the service-role admin client — same documented status as
// phase7_envelope_mint.test.mjs: this proves the RPCs' mechanics (session
// binding, provenance checks, idempotency, XP hardening, seed atomicity) are
// correct against the documented shape, not integrity against real student
// work (no real original-practice content exists yet).
//
// Usage: node backend/supabase/tests/phase1_daily_challenge.test.mjs
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
  const email = `phase1dc-${tag}-${randomUUID()}@example.test`;
  const password = 'test-password-12345';
  const { data, error } = await admin.auth.admin.createUser({ email, password, email_confirm: true });
  if (error) throw new Error(`createUser(${tag}) failed: ${error.message}`);
  const userId = data.user.id;

  const { error: profileErr } = await admin.from('profiles').upsert({ id: userId, username: `phase1dc_${tag}_${userId.slice(0, 8)}` });
  if (profileErr) throw new Error(`profile upsert(${tag}) failed: ${profileErr.message}`);

  const client = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });
  const { error: signInErr } = await client.auth.signInWithPassword({ email, password });
  if (signInErr) throw new Error(`signIn(${tag}) failed: ${signInErr.message}`);

  return { userId, client };
}

async function createQuestionSet(tag) {
  const id = `phase1dc-set-${tag}-${randomUUID()}`;
  const { error } = await admin.from('igcse_question_sets').insert({
    id,
    schema_version: '1',
    content_hash: randomUUID(),
    payload: { fixture: true },
    status: 'published',
  });
  if (error) throw new Error(`createQuestionSet(${tag}) failed: ${error.message}`);
  return id;
}

/** Minimal fixture envelope — only the columns/jsonb fields the RPCs read. */
function fixtureEnvelopeRow(userId, { attemptId = randomUUID(), sessionId = randomUUID(), total = 28, provenance = 'original-practice', questionSetId, createdAt } = {}) {
  const row = {
    attempt_id: attemptId,
    session_id: sessionId,
    user_id: userId,
    content_provenance: provenance,
    envelope: { total, attemptId, sessionId, questionSetId },
  };
  if (createdAt) row.created_at = createdAt;
  return row;
}

async function main() {
  console.log('Creating two test users...');
  const a = await createTestUser('a');
  const b = await createTestUser('b');

  const today = new Date().toISOString().slice(0, 10);

  console.log('\n1. seed_daily_challenge (service_role) seeds today with a published question set');
  let todaySetId;
  {
    await createQuestionSet('seed1');
    const r = await admin.rpc('seed_daily_challenge', { p_challenge_date: today });
    ok(!r.error && r.data.ok, `seed succeeds${r.error ? ` (${r.error.message})` : ''}`);
    const { data: row } = await admin.from('daily_challenge_assignments').select('*').eq('challenge_date', today).single();
    ok(!!row, 'daily_challenge_assignments row exists for today');
    todaySetId = row.question_set_id;
  }

  console.log('\n2. start_daily_challenge reserves a session and returns it');
  let sessionA;
  {
    const r = await a.client.rpc('start_daily_challenge', { p_challenge_date: today });
    ok(!r.error && r.data.ok, `start succeeds${r.error ? ` (${r.error.message})` : ''}`);
    ok(typeof r.data.session_id === 'string' && r.data.session_id.startsWith('daily-'), 'session_id has the daily- prefix');
    ok(r.data.question_set_id === todaySetId, 'returns the assigned question_set_id');
    sessionA = r.data.session_id;
  }

  console.log('\n3. Happy path: submit_daily_challenge_attempt claims and awards XP');
  {
    const row = fixtureEnvelopeRow(a.userId, { sessionId: sessionA, questionSetId: todaySetId, total: 32 });
    const { error: insErr } = await admin.from('scoring_envelopes').insert(row);
    ok(!insErr, `fixture envelope inserted${insErr ? ` (${insErr.message})` : ''}`);

    const r = await a.client.rpc('submit_daily_challenge_attempt', { p_challenge_date: today, p_attempt_id: row.attempt_id });
    ok(!r.error && r.data.ok && r.data.already_claimed === false, `claim succeeds${r.error ? ` (${r.error.message})` : ''}`);
    ok(r.data.xp_awarded === Math.round(32 / 40 * 50), `xp_awarded derived correctly (got ${r.data.xp_awarded})`);

    const { data: attempts } = await admin.from('daily_challenge_attempts').select('*').eq('user_id', a.userId).eq('challenge_date', today);
    ok(attempts.length === 1, 'exactly one daily_challenge_attempts row');

    const { data: events } = await admin.from('xp_events').select('*').eq('user_id', a.userId).eq('source', 'daily_challenge');
    ok(events.length === 1 && events[0].amount === r.data.xp_awarded, 'exactly one xp_events row, amount matches');
  }

  console.log('\n4. Re-submitting the same day is an idempotent already_claimed result, not a new row/XP');
  {
    const { data: before } = await admin.from('xp_events').select('*', { count: 'exact', head: true }).eq('user_id', a.userId).eq('source', 'daily_challenge');
    const row = fixtureEnvelopeRow(a.userId, { sessionId: sessionA, questionSetId: todaySetId, total: 40 });
    await admin.from('scoring_envelopes').insert(row);
    const r = await a.client.rpc('submit_daily_challenge_attempt', { p_challenge_date: today, p_attempt_id: row.attempt_id });
    ok(!r.error && r.data.already_claimed === true, `repeat claim returns already_claimed:true${r.error ? ` (${r.error.message})` : ''}`);
    const { data: after, count } = await admin.from('xp_events').select('*', { count: 'exact' }).eq('user_id', a.userId).eq('source', 'daily_challenge');
    ok(count === 1, 'still exactly one xp_events row for daily_challenge');
  }

  console.log('\n5. start_daily_challenge after completion raises already_completed');
  {
    const r = await a.client.rpc('start_daily_challenge', { p_challenge_date: today });
    ok(r.error && /already_completed/.test(r.error.message), `raises already_completed${r.error ? '' : ' (expected an error)'}`);
  }

  console.log('\n6. content_provenance != original-practice is rejected by the table CHECK before the RPC even runs');
  {
    const row = fixtureEnvelopeRow(b.userId, { provenance: 'confidential-internal' });
    const { error } = await admin.from('scoring_envelopes').insert(row);
    ok(!!error, `table CHECK rejects confidential-internal${error ? '' : ' (expected an error)'}`);
  }

  console.log('\n7. Unknown attempt_id raises unknown_envelope');
  {
    const bStart = await b.client.rpc('start_daily_challenge', { p_challenge_date: today });
    ok(!bStart.error, `B can start (${bStart.error?.message})`);
    const r = await b.client.rpc('submit_daily_challenge_attempt', { p_challenge_date: today, p_attempt_id: randomUUID() });
    ok(r.error && /unknown_envelope/.test(r.error.message), `raises unknown_envelope${r.error ? '' : ' (expected an error)'}`);
  }

  console.log('\n8. questionSetId mismatch raises question_set_mismatch');
  {
    const otherSetId = await createQuestionSet('mismatch');
    const bSession = (await b.client.rpc('start_daily_challenge', { p_challenge_date: today })).data.session_id;
    const row = fixtureEnvelopeRow(b.userId, { sessionId: bSession, questionSetId: otherSetId, total: 30 });
    await admin.from('scoring_envelopes').insert(row);
    const r = await b.client.rpc('submit_daily_challenge_attempt', { p_challenge_date: today, p_attempt_id: row.attempt_id });
    ok(r.error && /question_set_mismatch/.test(r.error.message), `raises question_set_mismatch${r.error ? '' : ' (expected an error)'}`);
  }

  console.log('\n9. Envelope created on the wrong UTC day raises wrong_day');
  {
    const f = await createTestUser('f9');
    const fSession = (await f.client.rpc('start_daily_challenge', { p_challenge_date: today })).data.session_id;
    const row = fixtureEnvelopeRow(f.userId, { sessionId: fSession, questionSetId: todaySetId, total: 30 });
    const { error: insErr } = await admin.from('scoring_envelopes').insert(row);
    // Backdate created_at directly — insert default is now(), so update after insert.
    await admin.from('scoring_envelopes').update({ created_at: '2020-01-01T00:00:00Z' }).eq('attempt_id', row.attempt_id);
    ok(!insErr, `fixture inserted${insErr ? ` (${insErr.message})` : ''}`);
    const r = await f.client.rpc('submit_daily_challenge_attempt', { p_challenge_date: today, p_attempt_id: row.attempt_id });
    ok(r.error && /wrong_day/.test(r.error.message), `raises wrong_day${r.error ? '' : ' (expected an error)'}`);
  }

  console.log("\n10. Cross-user envelope ownership: B cannot claim using A's attempt_id (unknown_envelope, not a leak)");
  {
    const row = fixtureEnvelopeRow(a.userId, { sessionId: sessionA, questionSetId: todaySetId, total: 25 });
    await admin.from('scoring_envelopes').insert(row);
    const r = await b.client.rpc('submit_daily_challenge_attempt', { p_challenge_date: today, p_attempt_id: row.attempt_id });
    ok(r.error && /unknown_envelope/.test(r.error.message), `B raises unknown_envelope for A's attempt_id${r.error ? '' : ' (expected an error)'}`);
  }

  console.log('\n11. Malformed/out-of-range total is rejected');
  {
    const g = await createTestUser('g11');
    const gSession = (await g.client.rpc('start_daily_challenge', { p_challenge_date: today })).data.session_id;
    const row = fixtureEnvelopeRow(g.userId, { sessionId: gSession, questionSetId: todaySetId });
    row.envelope = { total: 999, attemptId: row.attempt_id, questionSetId: todaySetId };
    await admin.from('scoring_envelopes').insert(row);
    const r = await g.client.rpc('submit_daily_challenge_attempt', { p_challenge_date: today, p_attempt_id: row.attempt_id });
    ok(r.error && /invalid_envelope_total/.test(r.error.message), `out-of-range total (999) rejected${r.error ? '' : ' (expected an error)'}`);
  }

  console.log('\n12. unknown_challenge for a date with no assignment');
  {
    const farFuture = '2099-01-01';
    const r = await b.client.rpc('start_daily_challenge', { p_challenge_date: farFuture });
    ok(r.error && /unknown_challenge/.test(r.error.message), `raises unknown_challenge${r.error ? '' : ' (expected an error)'}`);
  }

  console.log('\n13. EXECUTE denied for anon / PUBLIC on every new RPC');
  {
    const anonClient = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });
    const r1 = await anonClient.rpc('start_daily_challenge', { p_challenge_date: today });
    ok(!!r1.error, `anon start_daily_challenge denied${r1.error ? '' : ' (expected an error)'}`);
    const r2 = await anonClient.rpc('submit_daily_challenge_attempt', { p_challenge_date: today, p_attempt_id: randomUUID() });
    ok(!!r2.error, `anon submit_daily_challenge_attempt denied${r2.error ? '' : ' (expected an error)'}`);
    const r3 = await anonClient.rpc('seed_daily_challenge', { p_challenge_date: today });
    ok(!!r3.error, `anon seed_daily_challenge denied${r3.error ? '' : ' (expected an error)'}`);
    const r4 = await anonClient.rpc('seed_daily_challenges_batch');
    ok(!!r4.error, `anon seed_daily_challenges_batch denied${r4.error ? '' : ' (expected an error)'}`);
  }

  console.log('\n14. daily_challenge_sessions RLS: B cannot read A\'s reserved session row');
  {
    const { data, error } = await b.client.from('daily_challenge_sessions').select('*').eq('user_id', a.userId);
    ok(!error && (data ?? []).length === 0, `B sees zero of A's session rows${error ? ` (error: ${error.message})` : ''}`);
  }

  console.log('\n15. daily_challenge_attempts RLS: B cannot read A\'s attempt row directly');
  {
    const { data, error } = await b.client.from('daily_challenge_attempts').select('*').eq('user_id', a.userId);
    ok(!error && (data ?? []).length === 0, `B sees zero of A's attempt rows${error ? ` (error: ${error.message})` : ''}`);
  }

  console.log("\n16. award_xp direct RPC call from an authenticated client — EXECUTE denied (Fix 1's core proof)");
  {
    const r = await a.client.rpc('award_xp', {
      p_user_id: a.userId, p_source: 'daily_challenge', p_amount: 500,
      p_idempotency_key: randomUUID(), p_metadata: {},
    });
    ok(!!r.error, `authenticated client cannot call award_xp directly${r.error ? '' : ' (expected an error — Fix 1 regression)'}`);
  }

  console.log('\n17. start_daily_challenge called twice for the same user/date returns the same session_id, no duplicate row');
  {
    const tomorrow = new Date(Date.now() + 86400000).toISOString().slice(0, 10);
    await admin.rpc('seed_daily_challenge', { p_challenge_date: tomorrow });
    const c = await createTestUser('c17');
    const r1 = await c.client.rpc('start_daily_challenge', { p_challenge_date: tomorrow });
    const r2 = await c.client.rpc('start_daily_challenge', { p_challenge_date: tomorrow });
    ok(!r1.error && !r2.error, `both calls succeed${r1.error ? ` (${r1.error.message})` : ''}${r2.error ? ` (${r2.error.message})` : ''}`);
    ok(r1.data?.session_id === r2.data?.session_id, 'same session_id returned both times');
    const { count } = await admin.from('daily_challenge_sessions').select('*', { count: 'exact', head: true }).eq('user_id', c.userId).eq('challenge_date', tomorrow);
    ok(count === 1, 'exactly one daily_challenge_sessions row');
  }

  console.log('\n18. start_daily_challenge for an already-completed day — already_completed');
  {
    // Reuses user A, who completed `today` in test 3.
    const r = await a.client.rpc('start_daily_challenge', { p_challenge_date: today });
    ok(r.error && /already_completed/.test(r.error.message), `raises already_completed${r.error ? '' : ' (expected an error)'}`);
  }

  console.log('\n19. CORE FIX-2 REGRESSION: a normal ExamMode practice envelope (non-daily- session_id) on today\'s set cannot be claimed');
  {
    const d = await createTestUser('d19');
    await d.client.rpc('start_daily_challenge', { p_challenge_date: today }); // reserves a daily- session, but we deliberately don't use it
    const row = fixtureEnvelopeRow(d.userId, { sessionId: `exam-sim-${Date.now()}`, questionSetId: todaySetId, total: 35 });
    await admin.from('scoring_envelopes').insert(row);
    const r = await d.client.rpc('submit_daily_challenge_attempt', { p_challenge_date: today, p_attempt_id: row.attempt_id });
    ok(r.error && /session_not_bound/.test(r.error.message), `raises session_not_bound for exam-sim- session id${r.error ? '' : ' (expected an error — Fix 2 regression, the whole point of this fix)'}`);
  }

  console.log("\n20. Cross-user session binding: B cannot claim with a session_id reserved for another user, even with a fixture-forced envelope");
  {
    // Fresh pair, but on `today` (not a future date) — the fixture envelope's
    // created_at defaults to now(), which must fall on the challenge_date
    // itself for the wrong_day check (test 9) to pass, so this test can
    // isolate the session_not_bound check specifically instead of tripping
    // wrong_day first.
    const victim = await createTestUser('victim20');
    const victimSession = (await victim.client.rpc('start_daily_challenge', { p_challenge_date: today })).data.session_id;

    const e = await createTestUser('e20');
    await e.client.rpc('start_daily_challenge', { p_challenge_date: today });
    // Forge an envelope owned by E but carrying the victim's reserved session_id string.
    const row = fixtureEnvelopeRow(e.userId, { sessionId: victimSession, questionSetId: todaySetId, total: 30 });
    await admin.from('scoring_envelopes').insert(row);
    const r = await e.client.rpc('submit_daily_challenge_attempt', { p_challenge_date: today, p_attempt_id: row.attempt_id });
    // victimSession belongs to `victim`, not E, so E's own reserved session_id != victimSession -> session_not_bound.
    ok(r.error && /session_not_bound/.test(r.error.message), `raises session_not_bound${r.error ? '' : ' (expected an error)'}`);
  }

  console.log('\n21. seed_daily_challenge concurrent-invocation safety: two calls for the same date, no unique-violation surfaced, exactly one row');
  {
    const futureDate = '2099-06-15';
    const [r1, r2] = await Promise.all([
      admin.rpc('seed_daily_challenge', { p_challenge_date: futureDate }),
      admin.rpc('seed_daily_challenge', { p_challenge_date: futureDate }),
    ]);
    ok(!r1.error && !r2.error, `both concurrent calls succeed${r1.error ? ` (${r1.error.message})` : ''}${r2.error ? ` (${r2.error.message})` : ''}`);
    const { count } = await admin.from('daily_challenge_assignments').select('*', { count: 'exact', head: true }).eq('challenge_date', futureDate);
    ok(count === 1, 'exactly one row for that date');
    ok(r1.data.question_set_id === r2.data.question_set_id, 'both calls agree on the same assigned question_set_id');
  }

  console.log('\n22. seed_daily_challenges_batch seeds both today and tomorrow; re-running is a pure no-op for both');
  {
    const r1 = await admin.rpc('seed_daily_challenges_batch');
    ok(!r1.error && r1.data.today.ok && r1.data.tomorrow.ok, `batch seeds both${r1.error ? ` (${r1.error.message})` : ''}`);
    const r2 = await admin.rpc('seed_daily_challenges_batch');
    ok(!r2.error && r2.data.today.already_seeded === true && r2.data.tomorrow.already_seeded === true, 're-run is a no-op for both');
  }

  console.log('\n23. Claim-recovery integration (localStorage pending-claim) — covered separately in dailyChallengeService.test.ts (Vitest), not this pgTAP-style suite. Placeholder assertion only.');
  {
    ok(true, 'see src/services/dailyChallenge/__tests__/dailyChallengeService.test.ts for the claim-recovery unit test');
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
