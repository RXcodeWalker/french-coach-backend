// Friend Duels Phase 2 — DB integration tests (plan
// i-am-implementing-phase-shimmering-tide.md). Run against the LOCAL
// Supabase stack only (`npx supabase start` from backend/), never the
// hosted project.
//
// Every scoring_envelopes/igcse_question_sets row below is a FIXTURE
// inserted directly via the service-role admin client — same documented
// status as phase7_envelope_mint.test.mjs / phase1_daily_challenge.test.mjs:
// this proves the RPCs' mechanics (score binding, session binding,
// concurrent resolution, state invariants) are correct against the
// documented shape, not integrity against real student work.
//
// Usage: node backend/supabase/tests/phase2_friend_duels.test.mjs
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
  const email = `phase2duel-${tag}-${randomUUID()}@example.test`;
  const password = 'test-password-12345';
  const { data, error } = await admin.auth.admin.createUser({ email, password, email_confirm: true });
  if (error) throw new Error(`createUser(${tag}) failed: ${error.message}`);
  const userId = data.user.id;

  const { error: profileErr } = await admin.from('profiles').upsert({ id: userId, username: `phase2duel_${tag}_${userId.slice(0, 8)}` });
  if (profileErr) throw new Error(`profile upsert(${tag}) failed: ${profileErr.message}`);

  const client = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });
  const { error: signInErr } = await client.auth.signInWithPassword({ email, password });
  if (signInErr) throw new Error(`signIn(${tag}) failed: ${signInErr.message}`);

  return { userId, client };
}

async function createQuestionSet(tag, statusValue = 'published') {
  const id = `phase2duel-set-${tag}-${randomUUID()}`;
  const { error } = await admin.from('igcse_question_sets').insert({
    id,
    schema_version: '1',
    content_hash: randomUUID(),
    payload: { fixture: true },
    status: statusValue,
  });
  if (error) throw new Error(`createQuestionSet(${tag}) failed: ${error.message}`);
  return id;
}

/** Minimal fixture envelope — only the columns/jsonb fields the RPCs read. */
function fixtureEnvelopeRow(userId, { attemptId = randomUUID(), sessionId = randomUUID(), total = 28, provenance = 'original-practice', questionSetId } = {}) {
  return {
    attempt_id: attemptId,
    session_id: sessionId,
    user_id: userId,
    content_provenance: provenance,
    envelope: { total, attemptId, sessionId, questionSetId },
  };
}

async function befriend(x, y) {
  const r1 = await x.client.rpc('send_friend_request', { target_user_id: y.userId });
  if (r1.error) throw new Error(`send_friend_request failed: ${r1.error.message}`);
  const r2 = await y.client.rpc('respond_friend_request', { target_user_id: x.userId, action: 'accept' });
  if (r2.error) throw new Error(`respond_friend_request failed: ${r2.error.message}`);
}

async function main() {
  console.log('Creating three test users (a/b duelists, c unrelated)...');
  const a = await createTestUser('a');
  const b = await createTestUser('b');
  const c = await createTestUser('c');

  console.log('\n1. Setup: a<->b friended');
  {
    await befriend(a, b);
    ok(true, 'a and b are friends');
  }

  const publishedSet = await createQuestionSet('published');
  const draftSet = await createQuestionSet('draft', 'draft');

  console.log('\n2. create_duel_challenge');
  let duelId;
  {
    const r = await a.client.rpc('create_duel_challenge', { p_opponent_user_id: b.userId, p_question_set_id: publishedSet });
    ok(!r.error && r.data.ok && r.data.status === 'pending', `happy path succeeds${r.error ? ` (${r.error.message})` : ''}`);
    duelId = r.data.duel_id;

    const rSelf = await a.client.rpc('create_duel_challenge', { p_opponent_user_id: a.userId, p_question_set_id: publishedSet });
    ok(rSelf.error && /cannot_duel_self/.test(rSelf.error.message), `self-duel rejected${rSelf.error ? '' : ' (expected an error)'}`);

    const rNotFriend = await a.client.rpc('create_duel_challenge', { p_opponent_user_id: c.userId, p_question_set_id: publishedSet });
    ok(rNotFriend.error && /not_friends/.test(rNotFriend.error.message), `non-friend rejected${rNotFriend.error ? '' : ' (expected an error)'}`);

    const rUnknownSet = await a.client.rpc('create_duel_challenge', { p_opponent_user_id: b.userId, p_question_set_id: randomUUID() });
    ok(rUnknownSet.error && /unknown_question_set/.test(rUnknownSet.error.message), `unknown set rejected${rUnknownSet.error ? '' : ' (expected an error)'}`);

    const rDraftSet = await a.client.rpc('create_duel_challenge', { p_opponent_user_id: b.userId, p_question_set_id: draftSet });
    ok(rDraftSet.error && /unknown_question_set/.test(rDraftSet.error.message), `draft set rejected${rDraftSet.error ? '' : ' (expected an error)'}`);

    const anonClient = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });
    const rAnon = await anonClient.rpc('create_duel_challenge', { p_opponent_user_id: b.userId, p_question_set_id: publishedSet });
    ok(!!rAnon.error, `anon EXECUTE denied${rAnon.error ? '' : ' (expected an error)'}`);
  }

  console.log('\n2b. create_duel_challenge rate limit (21st in a day)');
  {
    // Fresh challenger, isolated from every other create_duel_challenge call
    // in this suite — the rate limit counts ALL of a challenger's duels in
    // the last day regardless of opponent (mirrors send_friend_request), so
    // reusing `a` here would double-count against its other test-driven duels.
    const rl = await createTestUser('ratelimiter');
    const rlTarget = await createTestUser('ratelimittarget');
    await befriend(rl, rlTarget);
    for (let i = 0; i < 20; i++) {
      const r = await rl.client.rpc('create_duel_challenge', { p_opponent_user_id: rlTarget.userId, p_question_set_id: publishedSet });
      if (r.error) throw new Error(`unexpected rate-limit setup failure at i=${i}: ${r.error.message}`);
    }
    const r21 = await rl.client.rpc('create_duel_challenge', { p_opponent_user_id: rlTarget.userId, p_question_set_id: publishedSet });
    ok(r21.error && /duel_rate_limited/.test(r21.error.message), `21st duel in a day rejected${r21.error ? '' : ' (expected an error)'}`);
  }

  console.log('\n3. respond_duel_challenge');
  {
    const rAccept1 = await b.client.rpc('respond_duel_challenge', { p_duel_id: duelId, p_action: 'accept' });
    ok(!rAccept1.error && rAccept1.data.ok && rAccept1.data.status === 'accepted', `accept succeeds${rAccept1.error ? ` (${rAccept1.error.message})` : ''}`);

    const { data: row } = await admin.from('duel_challenges').select('*').eq('id', duelId).single();
    ok(row.expires_at !== null, 'expires_at set on accept');
    ok(row.responded_at !== null, 'responded_at set on accept');

    const rAccept2 = await b.client.rpc('respond_duel_challenge', { p_duel_id: duelId, p_action: 'accept' });
    ok(!rAccept2.error && rAccept2.data.status === 'accepted', 'double-accept by correct opponent is idempotent');

    const rStranger = await c.client.rpc('respond_duel_challenge', { p_duel_id: duelId, p_action: 'accept' });
    ok(rStranger.error && /unknown_duel/.test(rStranger.error.message), `unrelated user c gets unknown_duel, not a status leak${rStranger.error ? '' : ' (expected an error — the leak the review closed)'}`);

    const rChallengerAccept = await a.client.rpc('respond_duel_challenge', { p_duel_id: duelId, p_action: 'accept' });
    ok(rChallengerAccept.error && /invalid_actor_for_action/.test(rChallengerAccept.error.message), `challenger accept on own sent invite rejected${rChallengerAccept.error ? '' : ' (expected an error)'}`);

    const rChallengerDecline = await a.client.rpc('respond_duel_challenge', { p_duel_id: duelId, p_action: 'decline' });
    ok(rChallengerDecline.error && /invalid_actor_for_action/.test(rChallengerDecline.error.message), `challenger decline on own sent invite rejected${rChallengerDecline.error ? '' : ' (expected an error)'}`);

    const rOpponentCancel = await b.client.rpc('respond_duel_challenge', { p_duel_id: duelId, p_action: 'cancel' });
    ok(rOpponentCancel.error && /invalid_actor_for_action/.test(rOpponentCancel.error.message), `opponent cancel rejected${rOpponentCancel.error ? '' : ' (expected an error)'}`);

    // Pending decline-then-accept and cancel-by-challenger, using fresh pending duels.
    const rDecl = await a.client.rpc('create_duel_challenge', { p_opponent_user_id: b.userId, p_question_set_id: publishedSet });
    const declDuelId = rDecl.data.duel_id;
    const rDecline = await b.client.rpc('respond_duel_challenge', { p_duel_id: declDuelId, p_action: 'decline' });
    ok(!rDecline.error && rDecline.data.status === 'declined', 'decline succeeds');
    const rDeclineThenAccept = await b.client.rpc('respond_duel_challenge', { p_duel_id: declDuelId, p_action: 'accept' });
    ok(!rDeclineThenAccept.error && rDeclineThenAccept.data.status === 'declined', 'accept-attempt after decline no-ops at current status');

    const rCancelSetup = await a.client.rpc('create_duel_challenge', { p_opponent_user_id: b.userId, p_question_set_id: publishedSet });
    const cancelDuelId = rCancelSetup.data.duel_id;
    const rCancel = await a.client.rpc('respond_duel_challenge', { p_duel_id: cancelDuelId, p_action: 'cancel' });
    ok(!rCancel.error && rCancel.data.status === 'cancelled', `cancel-by-challenger on pending succeeds${rCancel.error ? ` (${rCancel.error.message})` : ''}`);

    // Concurrent accept+decline race from the opponent.
    const rRaceSetup = await a.client.rpc('create_duel_challenge', { p_opponent_user_id: b.userId, p_question_set_id: publishedSet });
    const raceDuelId = rRaceSetup.data.duel_id;
    const [raceAccept, raceDecline] = await Promise.all([
      b.client.rpc('respond_duel_challenge', { p_duel_id: raceDuelId, p_action: 'accept' }),
      b.client.rpc('respond_duel_challenge', { p_duel_id: raceDuelId, p_action: 'decline' }),
    ]);
    const raceNoErrors = !raceAccept.error && !raceDecline.error;
    ok(raceNoErrors, `concurrent accept+decline from opponent resolves with no error thrown${raceNoErrors ? '' : ` (accept: ${raceAccept.error?.message}, decline: ${raceDecline.error?.message})`}`);
    const { data: raceRow } = await admin.from('duel_challenges').select('status').eq('id', raceDuelId).single();
    ok(raceRow.status === 'accepted' || raceRow.status === 'declined', `race resolves to exactly one outcome (got ${raceRow.status})`);

    const rFakeId = await b.client.rpc('respond_duel_challenge', { p_duel_id: randomUUID(), p_action: 'accept' });
    ok(rFakeId.error && /unknown_duel/.test(rFakeId.error.message), `nonexistent duel_id raises unknown_duel${rFakeId.error ? '' : ' (expected an error)'}`);

    const anonClient = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });
    const rAnon = await anonClient.rpc('respond_duel_challenge', { p_duel_id: duelId, p_action: 'accept' });
    ok(!!rAnon.error, `anon EXECUTE denied${rAnon.error ? '' : ' (expected an error)'}`);
  }

  console.log('\n4. start_duel_attempt');
  let sessionA, sessionB;
  {
    const rA = await a.client.rpc('start_duel_attempt', { p_duel_id: duelId });
    ok(!rA.error && rA.data.ok, `challenger starts${rA.error ? ` (${rA.error.message})` : ''}`);
    sessionA = rA.data.session_id;

    const rB = await b.client.rpc('start_duel_attempt', { p_duel_id: duelId });
    ok(!rB.error && rB.data.ok, `opponent starts${rB.error ? ` (${rB.error.message})` : ''}`);
    sessionB = rB.data.session_id;

    ok(sessionA !== sessionB, 'each side mints a distinct session_id');
    ok(rA.data.question_set_id === publishedSet && rB.data.question_set_id === publishedSet, 'both bound to the same question_set_id');

    const rARepeat = await a.client.rpc('start_duel_attempt', { p_duel_id: duelId });
    ok(!rARepeat.error && rARepeat.data.session_id === sessionA, 'repeat call before submit returns the same session (idempotent)');

    const rStranger = await c.client.rpc('start_duel_attempt', { p_duel_id: duelId });
    ok(rStranger.error && /unknown_duel/.test(rStranger.error.message), `non-participant rejected${rStranger.error ? '' : ' (expected an error)'}`);

    const rPendingSetup = await a.client.rpc('create_duel_challenge', { p_opponent_user_id: b.userId, p_question_set_id: publishedSet });
    const rPendingStart = await a.client.rpc('start_duel_attempt', { p_duel_id: rPendingSetup.data.duel_id });
    ok(rPendingStart.error && /duel_not_active/.test(rPendingStart.error.message), `against pending duel rejected${rPendingStart.error ? '' : ' (expected an error)'}`);

    const anonClient = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });
    const rAnon = await anonClient.rpc('start_duel_attempt', { p_duel_id: duelId });
    ok(!!rAnon.error, `anon EXECUTE denied${rAnon.error ? '' : ' (expected an error)'}`);
  }

  console.log('\n5. submit_duel_attempt envelope guards');
  {
    const rForged = await a.client.rpc('submit_duel_attempt', { p_duel_id: duelId, p_attempt_id: randomUUID() });
    ok(rForged.error && /unknown_envelope/.test(rForged.error.message), `forged attempt_id rejected${rForged.error ? '' : ' (expected an error)'}`);

    const badProvRow = { attempt_id: randomUUID(), session_id: randomUUID(), user_id: a.userId, content_provenance: 'confidential-internal', envelope: {} };
    const { error: badProvInsErr } = await admin.from('scoring_envelopes').insert(badProvRow);
    ok(!!badProvInsErr, `table CHECK rejects confidential-internal provenance${badProvInsErr ? '' : ' (expected an error)'}`);

    // scoring_envelopes enforces exactly one original (non-regraded) envelope
    // per session_id (20260717130000_add_scoring_envelopes_idempotency.sql),
    // so sessionA (reserved for a/duelId) can back at most one fixture row
    // here. question_set_mismatch is checked before session_not_bound in the
    // RPC, so a mismatched-set envelope on a random session_id still proves
    // question_set_mismatch specifically. invalid_envelope_total is checked
    // AFTER session_not_bound, so that fixture needs a genuinely-reserved,
    // not-yet-consumed session — use sessionA itself (still unconsumed at
    // this point in the test).
    const mismatchSet = await createQuestionSet('mismatch');
    const mismatchRow = fixtureEnvelopeRow(a.userId, { sessionId: randomUUID(), questionSetId: mismatchSet, total: 30 });
    await admin.from('scoring_envelopes').insert(mismatchRow);
    const rMismatch = await a.client.rpc('submit_duel_attempt', { p_duel_id: duelId, p_attempt_id: mismatchRow.attempt_id });
    ok(rMismatch.error && /question_set_mismatch/.test(rMismatch.error.message), `mismatched question set rejected${rMismatch.error ? '' : ' (expected an error)'}`);

    const wrongSessionRow = fixtureEnvelopeRow(a.userId, { sessionId: `exam-sim-${Date.now()}`, questionSetId: publishedSet, total: 30 });
    await admin.from('scoring_envelopes').insert(wrongSessionRow);
    const rWrongSession = await a.client.rpc('submit_duel_attempt', { p_duel_id: duelId, p_attempt_id: wrongSessionRow.attempt_id });
    ok(rWrongSession.error && /session_not_bound/.test(rWrongSession.error.message), `wrong session rejected${rWrongSession.error ? '' : ' (expected an error)'}`);

    const badTotalRow = fixtureEnvelopeRow(a.userId, { sessionId: sessionA, questionSetId: publishedSet, total: 999 });
    await admin.from('scoring_envelopes').insert(badTotalRow);
    const rBadTotal = await a.client.rpc('submit_duel_attempt', { p_duel_id: duelId, p_attempt_id: badTotalRow.attempt_id });
    ok(rBadTotal.error && /invalid_envelope_total/.test(rBadTotal.error.message), `out-of-range total rejected${rBadTotal.error ? '' : ' (expected an error)'}`);

    const cRow = fixtureEnvelopeRow(c.userId, { questionSetId: publishedSet, total: 20 });
    await admin.from('scoring_envelopes').insert(cRow);
    const rStranger = await c.client.rpc('submit_duel_attempt', { p_duel_id: duelId, p_attempt_id: cRow.attempt_id });
    ok(rStranger.error && /unknown_duel/.test(rStranger.error.message), `non-participant rejected${rStranger.error ? '' : ' (expected an error)'}`);

    const rPendingSetup = await a.client.rpc('create_duel_challenge', { p_opponent_user_id: b.userId, p_question_set_id: publishedSet });
    const rPendingSubmit = await a.client.rpc('submit_duel_attempt', { p_duel_id: rPendingSetup.data.duel_id, p_attempt_id: randomUUID() });
    ok(rPendingSubmit.error && /duel_not_active|unknown_envelope/.test(rPendingSubmit.error.message), `against non-accepted duel rejected${rPendingSubmit.error ? '' : ' (expected an error)'}`);
  }

  console.log('\n6. First submit -> waiting_on_opponent, second (higher-scoring) submit -> completed with correct winner/XP');
  let winDuelId, winSessionA, winSessionB;
  {
    // Fresh duel — duelId's sessionA was already consumed by test 5's
    // badTotalRow fixture (scoring_envelopes allows only one original
    // envelope per session_id).
    const rCreate = await a.client.rpc('create_duel_challenge', { p_opponent_user_id: b.userId, p_question_set_id: publishedSet });
    winDuelId = rCreate.data.duel_id;
    await b.client.rpc('respond_duel_challenge', { p_duel_id: winDuelId, p_action: 'accept' });
    winSessionA = (await a.client.rpc('start_duel_attempt', { p_duel_id: winDuelId })).data.session_id;
    winSessionB = (await b.client.rpc('start_duel_attempt', { p_duel_id: winDuelId })).data.session_id;

    const rowA = fixtureEnvelopeRow(a.userId, { sessionId: winSessionA, questionSetId: publishedSet, total: 20 });
    await admin.from('scoring_envelopes').insert(rowA);
    const rSubmitA = await a.client.rpc('submit_duel_attempt', { p_duel_id: winDuelId, p_attempt_id: rowA.attempt_id });
    ok(!rSubmitA.error && rSubmitA.data.ok && rSubmitA.data.waiting_on_opponent === true, `first submit waits on opponent${rSubmitA.error ? ` (${rSubmitA.error.message})` : ''}`);

    const { count: xpBefore } = await admin.from('xp_events').select('*', { count: 'exact', head: true }).eq('user_id', a.userId).eq('metadata->>duel_id', winDuelId);
    ok(xpBefore === 0, 'no xp_events rows yet after first submit');

    const rowB = fixtureEnvelopeRow(b.userId, { sessionId: winSessionB, questionSetId: publishedSet, total: 32 });
    await admin.from('scoring_envelopes').insert(rowB);
    const rSubmitB = await b.client.rpc('submit_duel_attempt', { p_duel_id: winDuelId, p_attempt_id: rowB.attempt_id });
    ok(!rSubmitB.error && rSubmitB.data.ok && rSubmitB.data.status === 'completed', `second submit completes the duel${rSubmitB.error ? ` (${rSubmitB.error.message})` : ''}`);
    ok(rSubmitB.data.winner_user_id === b.userId, 'higher-scoring submitter (b) is the winner');

    const expectedAmount = Math.round(32 / 40 * 50) + 30;
    const { data: winEvents } = await admin.from('xp_events').select('*').eq('user_id', b.userId).eq('metadata->>duel_id', winDuelId);
    ok(winEvents.length === 1 && winEvents[0].amount === expectedAmount, `exactly one xp_events row at the win-formula amount (expected ${expectedAmount}, got ${winEvents[0]?.amount})`);

    const { data: loseEvents } = await admin.from('xp_events').select('*').eq('user_id', a.userId).eq('metadata->>duel_id', winDuelId);
    ok(loseEvents.length === 0, 'loser gets zero xp_events rows');
  }

  console.log('\n7. Tie path');
  let tieDuelId, tieSessionA, tieSessionB;
  {
    const rCreate = await a.client.rpc('create_duel_challenge', { p_opponent_user_id: b.userId, p_question_set_id: publishedSet });
    tieDuelId = rCreate.data.duel_id;
    await b.client.rpc('respond_duel_challenge', { p_duel_id: tieDuelId, p_action: 'accept' });
    tieSessionA = (await a.client.rpc('start_duel_attempt', { p_duel_id: tieDuelId })).data.session_id;
    tieSessionB = (await b.client.rpc('start_duel_attempt', { p_duel_id: tieDuelId })).data.session_id;

    const rowA = fixtureEnvelopeRow(a.userId, { sessionId: tieSessionA, questionSetId: publishedSet, total: 25 });
    const rowB = fixtureEnvelopeRow(b.userId, { sessionId: tieSessionB, questionSetId: publishedSet, total: 25 });
    await admin.from('scoring_envelopes').insert(rowA);
    await admin.from('scoring_envelopes').insert(rowB);

    await a.client.rpc('submit_duel_attempt', { p_duel_id: tieDuelId, p_attempt_id: rowA.attempt_id });
    const rFinal = await b.client.rpc('submit_duel_attempt', { p_duel_id: tieDuelId, p_attempt_id: rowB.attempt_id });
    ok(!rFinal.error && rFinal.data.is_tie === true && rFinal.data.winner_user_id === null, `tie recorded${rFinal.error ? ` (${rFinal.error.message})` : ''}`);

    const { data: attempts } = await admin.from('duel_attempts').select('*').eq('duel_id', tieDuelId);
    ok(attempts.every(r => r.outcome === 'tie'), 'both outcome=tie');

    const { data: aXp } = await admin.from('xp_events').select('*').eq('user_id', a.userId).eq('metadata->>duel_id', tieDuelId);
    const { data: bXp } = await admin.from('xp_events').select('*').eq('user_id', b.userId).eq('metadata->>duel_id', tieDuelId);
    ok(aXp.length === 1 && aXp[0].amount === 15, `a gets 15 XP for the tie (got ${aXp[0]?.amount})`);
    ok(bXp.length === 1 && bXp[0].amount === 15, `b gets 15 XP for the tie (got ${bXp[0]?.amount})`);
  }

  console.log('\n8. Replay: same attempt_id resubmitted; cross-duel attempt_id reuse');
  {
    // Fresh duel, only `a` submits — replay must be tested while the duel is
    // still 'accepted' (status is checked before the idempotent short-circuit
    // in submit_duel_attempt's own step order, so replaying against an
    // already-'completed' duel like tieDuelId would hit duel_not_active
    // first, not already_claimed).
    const rCreate = await a.client.rpc('create_duel_challenge', { p_opponent_user_id: b.userId, p_question_set_id: publishedSet });
    const replayDuelId = rCreate.data.duel_id;
    await b.client.rpc('respond_duel_challenge', { p_duel_id: replayDuelId, p_action: 'accept' });
    const replaySessionA = (await a.client.rpc('start_duel_attempt', { p_duel_id: replayDuelId })).data.session_id;
    const replayRow = fixtureEnvelopeRow(a.userId, { sessionId: replaySessionA, questionSetId: publishedSet, total: 25 });
    await admin.from('scoring_envelopes').insert(replayRow);
    await a.client.rpc('submit_duel_attempt', { p_duel_id: replayDuelId, p_attempt_id: replayRow.attempt_id });

    const rReplay = await a.client.rpc('submit_duel_attempt', { p_duel_id: replayDuelId, p_attempt_id: replayRow.attempt_id });
    ok(!rReplay.error && rReplay.data.already_claimed === true, `same attempt_id/duel/user resubmit is already_claimed:true${rReplay.error ? ` (${rReplay.error.message})` : ''}`);

    const { count } = await admin.from('duel_attempts').select('*', { count: 'exact', head: true }).eq('duel_id', replayDuelId).eq('user_id', a.userId);
    ok(count === 1, 'no dupe row created by replay');

    // Cross-duel reuse: create a brand new accepted duel and try to reuse replayRow.attempt_id there.
    const rCross = await a.client.rpc('create_duel_challenge', { p_opponent_user_id: b.userId, p_question_set_id: publishedSet });
    const crossDuelId = rCross.data.duel_id;
    await b.client.rpc('respond_duel_challenge', { p_duel_id: crossDuelId, p_action: 'accept' });
    await a.client.rpc('start_duel_attempt', { p_duel_id: crossDuelId });
    const rCrossSubmit = await a.client.rpc('submit_duel_attempt', { p_duel_id: crossDuelId, p_attempt_id: replayRow.attempt_id });
    ok(rCrossSubmit.error && /attempt_already_claimed/.test(rCrossSubmit.error.message), `cross-duel attempt_id reuse raises attempt_already_claimed, not a raw unique-violation${rCrossSubmit.error ? '' : ' (expected an error)'}`);
  }

  console.log('\n9. Concurrent resolution: both participants submit simultaneously, exactly one completed result, no duplicated XP');
  {
    const rCreate = await a.client.rpc('create_duel_challenge', { p_opponent_user_id: b.userId, p_question_set_id: publishedSet });
    const concDuelId = rCreate.data.duel_id;
    await b.client.rpc('respond_duel_challenge', { p_duel_id: concDuelId, p_action: 'accept' });
    const concSessionA = (await a.client.rpc('start_duel_attempt', { p_duel_id: concDuelId })).data.session_id;
    const concSessionB = (await b.client.rpc('start_duel_attempt', { p_duel_id: concDuelId })).data.session_id;

    const rowA = fixtureEnvelopeRow(a.userId, { sessionId: concSessionA, questionSetId: publishedSet, total: 18 });
    const rowB = fixtureEnvelopeRow(b.userId, { sessionId: concSessionB, questionSetId: publishedSet, total: 22 });
    await admin.from('scoring_envelopes').insert(rowA);
    await admin.from('scoring_envelopes').insert(rowB);

    const [rA, rB] = await Promise.all([
      a.client.rpc('submit_duel_attempt', { p_duel_id: concDuelId, p_attempt_id: rowA.attempt_id }),
      b.client.rpc('submit_duel_attempt', { p_duel_id: concDuelId, p_attempt_id: rowB.attempt_id }),
    ]);
    ok(!rA.error && !rB.error, `both concurrent submits succeed${rA.error ? ` (a: ${rA.error.message})` : ''}${rB.error ? ` (b: ${rB.error.message})` : ''}`);
    const completedCount = [rA.data?.status, rB.data?.status].filter(s => s === 'completed').length;
    ok(completedCount === 1, `exactly one result reports completed (got ${completedCount})`);

    const { count: attemptCount } = await admin.from('duel_attempts').select('*', { count: 'exact', head: true }).eq('duel_id', concDuelId);
    ok(attemptCount === 2, 'exactly 2 duel_attempts rows');

    const { count: xpCount } = await admin.from('xp_events').select('*', { count: 'exact', head: true }).eq('metadata->>duel_id', concDuelId);
    ok(xpCount === 1, `no duplicated xp_events rows (got ${xpCount})`);
  }

  console.log('\n10. Expiry/forfeit');
  {
    // (a) zero submissions -> expired, zero XP.
    const rCreateA = await a.client.rpc('create_duel_challenge', { p_opponent_user_id: b.userId, p_question_set_id: publishedSet });
    const expireNoSubDuelId = rCreateA.data.duel_id;
    await b.client.rpc('respond_duel_challenge', { p_duel_id: expireNoSubDuelId, p_action: 'accept' });
    await admin.from('duel_challenges').update({ expires_at: new Date(Date.now() - 3600_000).toISOString() }).eq('id', expireNoSubDuelId);

    const rSync = await a.client.rpc('sync_duel_status', { p_duel_id: expireNoSubDuelId });
    ok(!rSync.error && rSync.data.status === 'expired', `zero-submission duel resolves to expired${rSync.error ? ` (${rSync.error.message})` : ` (got status: ${rSync.data?.status})`}`);
    const { count: expireXpCount } = await admin.from('xp_events').select('*', { count: 'exact', head: true }).eq('metadata->>duel_id', expireNoSubDuelId);
    ok(expireXpCount === 0, 'zero XP awarded for a pure expiry');

    // (b) one submission -> forfeit win for the submitter.
    const rCreateB = await a.client.rpc('create_duel_challenge', { p_opponent_user_id: b.userId, p_question_set_id: publishedSet });
    const forfeitDuelId = rCreateB.data.duel_id;
    await b.client.rpc('respond_duel_challenge', { p_duel_id: forfeitDuelId, p_action: 'accept' });
    const forfeitSessionA = (await a.client.rpc('start_duel_attempt', { p_duel_id: forfeitDuelId })).data.session_id;
    const forfeitRow = fixtureEnvelopeRow(a.userId, { sessionId: forfeitSessionA, questionSetId: publishedSet, total: 30 });
    await admin.from('scoring_envelopes').insert(forfeitRow);
    await a.client.rpc('submit_duel_attempt', { p_duel_id: forfeitDuelId, p_attempt_id: forfeitRow.attempt_id });

    await admin.from('duel_challenges').update({ expires_at: new Date(Date.now() - 3600_000).toISOString() }).eq('id', forfeitDuelId);

    const rOtherSideSync = await b.client.rpc('sync_duel_status', { p_duel_id: forfeitDuelId });
    ok(!rOtherSideSync.error && rOtherSideSync.data.status === 'completed', `one-submission duel resolves to completed via forfeit${rOtherSideSync.error ? ` (${rOtherSideSync.error.message})` : ''}`);
    ok(rOtherSideSync.data.winner_user_id === a.userId, 'submitter (a) wins by forfeit');

    const { data: forfeitAttempt } = await admin.from('duel_attempts').select('*').eq('duel_id', forfeitDuelId).eq('user_id', a.userId).single();
    ok(forfeitAttempt.outcome === 'forfeit_win', `attempt outcome is forfeit_win (got ${forfeitAttempt.outcome})`);

    const { data: forfeitXp } = await admin.from('xp_events').select('*').eq('user_id', a.userId).eq('metadata->>duel_id', forfeitDuelId);
    ok(forfeitXp.length === 1, `exactly one xp_events row for the forfeit win (got ${forfeitXp.length})`);
  }

  console.log('\n11. RLS cross-user denial');
  {
    const { data: cSelect, error: cSelectErr } = await c.client.from('duel_challenges').select('*').eq('id', duelId);
    ok(!cSelectErr && (cSelect ?? []).length === 0, `c's client gets empty SELECT, not an error${cSelectErr ? ` (error: ${cSelectErr.message})` : ''}`);

    const rCStart = await c.client.rpc('start_duel_attempt', { p_duel_id: duelId });
    ok(rCStart.error && /unknown_duel/.test(rCStart.error.message), `c calling start_duel_attempt on a/b's duel gets unknown_duel${rCStart.error ? '' : ' (expected an error)'}`);

    const rCSubmit = await c.client.rpc('submit_duel_attempt', { p_duel_id: duelId, p_attempt_id: randomUUID() });
    ok(rCSubmit.error && /unknown_duel/.test(rCSubmit.error.message), `c calling submit_duel_attempt on a/b's duel gets unknown_duel${rCSubmit.error ? '' : ' (expected an error)'}`);

    const rCSync = await c.client.rpc('sync_duel_status', { p_duel_id: duelId });
    ok(rCSync.error && /unknown_duel/.test(rCSync.error.message), `c calling sync_duel_status on a/b's duel gets unknown_duel${rCSync.error ? '' : ' (expected an error)'}`);

    const rCRespond = await c.client.rpc('respond_duel_challenge', { p_duel_id: duelId, p_action: 'accept' });
    ok(rCRespond.error && /unknown_duel/.test(rCRespond.error.message), `c calling respond_duel_challenge on a/b's real duel_id gets unknown_duel, not {ok:true,status:...}${rCRespond.error ? '' : ' (expected an error — the exact leak the review closed)'}`);
  }

  console.log('\n12. resolve_expired_duel has zero external grants');
  {
    const rAdmin = await admin.rpc('resolve_expired_duel', { p_duel_id: duelId });
    ok(!!rAdmin.error, `service_role client cannot call resolve_expired_duel directly${rAdmin.error ? '' : ' (expected an error — no GRANT exists at all)'}`);
    const rAuth = await a.client.rpc('resolve_expired_duel', { p_duel_id: duelId });
    ok(!!rAuth.error, `authenticated client cannot call resolve_expired_duel directly${rAuth.error ? '' : ' (expected an error)'}`);
  }

  console.log('\n13. Friendship removed mid-duel does not retroactively affect an already-accepted duel');
  {
    const rCreate = await a.client.rpc('create_duel_challenge', { p_opponent_user_id: b.userId, p_question_set_id: publishedSet });
    const decoupleDuelId = rCreate.data.duel_id;
    await b.client.rpc('respond_duel_challenge', { p_duel_id: decoupleDuelId, p_action: 'accept' });

    const rRemove = await a.client.rpc('remove_friend', { target_user_id: b.userId });
    ok(!rRemove.error, `remove_friend succeeds${rRemove.error ? ` (${rRemove.error.message})` : ''}`);

    const decoupleSessionA = (await a.client.rpc('start_duel_attempt', { p_duel_id: decoupleDuelId })).data?.session_id;
    const decoupleSessionB = (await b.client.rpc('start_duel_attempt', { p_duel_id: decoupleDuelId })).data?.session_id;
    ok(!!decoupleSessionA && !!decoupleSessionB, 'both sides can still start after friendship removal (intentional decoupling)');

    const rowA = fixtureEnvelopeRow(a.userId, { sessionId: decoupleSessionA, questionSetId: publishedSet, total: 15 });
    const rowB = fixtureEnvelopeRow(b.userId, { sessionId: decoupleSessionB, questionSetId: publishedSet, total: 10 });
    await admin.from('scoring_envelopes').insert(rowA);
    await admin.from('scoring_envelopes').insert(rowB);
    await a.client.rpc('submit_duel_attempt', { p_duel_id: decoupleDuelId, p_attempt_id: rowA.attempt_id });
    const rFinal = await b.client.rpc('submit_duel_attempt', { p_duel_id: decoupleDuelId, p_attempt_id: rowB.attempt_id });
    ok(!rFinal.error && rFinal.data.status === 'completed', `duel still resolves normally after friendship removal${rFinal.error ? ` (${rFinal.error.message})` : ''}`);

    // Re-friend for any later tests relying on a<->b friendship.
    await befriend(a, b);
  }

  console.log('\n14. State-invariant/adversarial-transition class');
  {
    const rCreate = await a.client.rpc('create_duel_challenge', { p_opponent_user_id: b.userId, p_question_set_id: publishedSet });
    const invDuelId = rCreate.data.duel_id;

    const rBadAccepted = await admin.from('duel_challenges').update({ status: 'accepted', expires_at: null }).eq('id', invDuelId);
    ok(!!rBadAccepted.error, `CHECK rejects accepted with NULL expires_at${rBadAccepted.error ? '' : ' (expected an error)'}`);

    const rBadCompleted = await admin.from('duel_challenges').update({ status: 'completed', completed_at: null }).eq('id', invDuelId);
    ok(!!rBadCompleted.error, `CHECK rejects completed with NULL completed_at${rBadCompleted.error ? '' : ' (expected an error)'}`);

    const rBadTieWinner = await admin.from('duel_challenges').update({
      status: 'completed', completed_at: new Date().toISOString(), is_tie: true, winner_user_id: a.userId,
    }).eq('id', invDuelId);
    ok(!!rBadTieWinner.error, `CHECK rejects is_tie=true with a winner_user_id set${rBadTieWinner.error ? '' : ' (expected an error)'}`);

    const rBadForeignWinner = await admin.from('duel_challenges').update({
      status: 'completed', completed_at: new Date().toISOString(), is_tie: false, winner_user_id: c.userId,
    }).eq('id', invDuelId);
    ok(!!rBadForeignWinner.error, `CHECK rejects winner_user_id not in (challenger_id, opponent_id)${rBadForeignWinner.error ? '' : ' (expected an error)'}`);

    // Force the structurally-unreachable resolve_expired_duel v_count=2 branch.
    const rCreateInv = await a.client.rpc('create_duel_challenge', { p_opponent_user_id: b.userId, p_question_set_id: publishedSet });
    const unreachableDuelId = rCreateInv.data.duel_id;
    await b.client.rpc('respond_duel_challenge', { p_duel_id: unreachableDuelId, p_action: 'accept' });
    const uSessionA = (await a.client.rpc('start_duel_attempt', { p_duel_id: unreachableDuelId })).data.session_id;
    const uSessionB = (await b.client.rpc('start_duel_attempt', { p_duel_id: unreachableDuelId })).data.session_id;

    const uRowA = fixtureEnvelopeRow(a.userId, { sessionId: uSessionA, questionSetId: publishedSet, total: 10 });
    const uRowB = fixtureEnvelopeRow(b.userId, { sessionId: uSessionB, questionSetId: publishedSet, total: 12 });
    await admin.from('scoring_envelopes').insert(uRowA);
    await admin.from('scoring_envelopes').insert(uRowB);

    // Insert both duel_attempts rows directly (bypassing submit_duel_attempt's
    // own status-flip), leaving duel_challenges.status='accepted' with a past expires_at.
    await admin.from('duel_attempts').insert([
      { duel_id: unreachableDuelId, user_id: a.userId, attempt_id: uRowA.attempt_id, score_total: 10, outcome: 'pending' },
      { duel_id: unreachableDuelId, user_id: b.userId, attempt_id: uRowB.attempt_id, score_total: 12, outcome: 'pending' },
    ]);
    await admin.from('duel_challenges').update({ expires_at: new Date(Date.now() - 3600_000).toISOString() }).eq('id', unreachableDuelId);

    const rInvariant = await a.client.rpc('sync_duel_status', { p_duel_id: unreachableDuelId });
    ok(rInvariant.error && /invariant_violation/.test(rInvariant.error.message), `forced 2-attempts-while-accepted raises invariant_violation, not a silent resolution${rInvariant.error ? '' : ' (expected an error)'}`);

    // No terminal-status row can be pushed back to accepted/pending via any of the 5 RPCs.
    const rCreateTerm = await a.client.rpc('create_duel_challenge', { p_opponent_user_id: b.userId, p_question_set_id: publishedSet });
    const termDuelId = rCreateTerm.data.duel_id;
    await b.client.rpc('respond_duel_challenge', { p_duel_id: termDuelId, p_action: 'decline' });

    const rRespondOnDeclined = await b.client.rpc('respond_duel_challenge', { p_duel_id: termDuelId, p_action: 'accept' });
    ok(!rRespondOnDeclined.error && rRespondOnDeclined.data.status === 'declined', 'respond on declined duel safely no-ops, never mutates status backward');

    const rStartOnDeclined = await a.client.rpc('start_duel_attempt', { p_duel_id: termDuelId });
    ok(rStartOnDeclined.error && /duel_not_active/.test(rStartOnDeclined.error.message), `start on declined duel rejected${rStartOnDeclined.error ? '' : ' (expected an error)'}`);

    const rSubmitOnDeclined = await a.client.rpc('submit_duel_attempt', { p_duel_id: termDuelId, p_attempt_id: randomUUID() });
    ok(rSubmitOnDeclined.error && /duel_not_active/.test(rSubmitOnDeclined.error.message), `submit on declined duel rejected${rSubmitOnDeclined.error ? '' : ' (expected an error)'}`);

    const { data: termRow } = await admin.from('duel_challenges').select('status').eq('id', termDuelId).single();
    ok(termRow.status === 'declined', 'terminal duel status never mutated backward by any RPC call');
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
