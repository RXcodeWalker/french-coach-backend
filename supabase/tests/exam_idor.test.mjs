// Exam IDOR (Phase 1.1) — DB integration test for the cross-user isolation
// of scoring_envelopes and session_transcripts. Run against the LOCAL
// Supabase stack only (`npx supabase start` from backend/), never the hosted
// project.
//
// What this proves: with RLS in force (the browser path), user B cannot read
// or overwrite user A's envelope / transcript rows. The scoring service
// (server/index.ts in the main repo) uses the SERVICE key, which bypasses
// RLS — its owner enforcement lives in the store code
// (scripts/scoring/supabaseEnvelopeStore.ts,
// scripts/stt/supabaseTranscriptStore.ts), covered by unit tests there. This
// file is the RLS-side backstop and the migration check for
// 20260909120000_scope_original_envelope_index_by_user.sql.
//
// Usage: node backend/supabase/tests/exam_idor.test.mjs
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
  const email = `idor-${tag}-${randomUUID()}@example.test`;
  const password = 'test-password-12345';
  const { data, error } = await admin.auth.admin.createUser({ email, password, email_confirm: true });
  if (error) throw new Error(`createUser(${tag}) failed: ${error.message}`);
  const userId = data.user.id;

  const { error: profileErr } = await admin.from('profiles').upsert({ id: userId, username: `idor_${tag}_${userId.slice(0, 8)}` });
  if (profileErr) throw new Error(`profile upsert(${tag}) failed: ${profileErr.message}`);

  const client = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });
  const { error: signInErr } = await client.auth.signInWithPassword({ email, password });
  if (signInErr) throw new Error(`signIn(${tag}) failed: ${signInErr.message}`);

  return { userId, client };
}

function fixtureEnvelopeRow(userId, sessionId, attemptId = randomUUID()) {
  return {
    attempt_id: attemptId,
    session_id: sessionId,
    user_id: userId,
    content_provenance: 'original-practice',
    envelope: { total: 28, attemptId, sessionId },
  };
}

function fixtureTranscriptRow(userId, sessionId) {
  return {
    session_id: sessionId,
    user_id: userId,
    schema_version: 'session-transcript-v1',
    content_provenance: 'original-practice',
    stt: { model: 'm' },
    transcript: { sessionId, contentProvenance: 'original-practice' },
  };
}

async function main() {
  console.log('Creating two test users...');
  const a = await createTestUser('a');
  const b = await createTestUser('b');

  const sessionId = `exam-sim-${randomUUID()}`;

  console.log("\n1. Seed user A's envelope + transcript for a session (service role)");
  {
    const { error: envErr } = await admin.from('scoring_envelopes').insert(fixtureEnvelopeRow(a.userId, sessionId));
    ok(!envErr, `A's envelope inserted${envErr ? ` (${envErr.message})` : ''}`);
    const { error: txErr } = await admin.from('session_transcripts').insert(fixtureTranscriptRow(a.userId, sessionId));
    ok(!txErr, `A's transcript inserted${txErr ? ` (${txErr.message})` : ''}`);
  }

  console.log("\n2. User B cannot read user A's envelope by session_id");
  {
    const { data } = await b.client.from('scoring_envelopes').select('*').eq('session_id', sessionId);
    ok(Array.isArray(data) && data.length === 0, `B sees zero envelope rows for A's session (got ${data?.length ?? 'null'})`);
  }

  console.log("\n3. User B cannot read user A's transcript by session_id");
  {
    const { data } = await b.client.from('session_transcripts').select('*').eq('session_id', sessionId);
    ok(Array.isArray(data) && data.length === 0, `B sees zero transcript rows for A's session (got ${data?.length ?? 'null'})`);
  }

  console.log("\n4. User A CAN read its own envelope + transcript");
  {
    const { data: env } = await a.client.from('scoring_envelopes').select('*').eq('session_id', sessionId);
    ok(env?.length === 1, `A reads its own envelope (got ${env?.length ?? 'null'})`);
    const { data: tx } = await a.client.from('session_transcripts').select('*').eq('session_id', sessionId);
    ok(tx?.length === 1, `A reads its own transcript (got ${tx?.length ?? 'null'})`);
  }

  console.log("\n5. User B cannot upsert (overwrite) user A's transcript row");
  {
    const { error } = await b.client.from('session_transcripts').upsert(fixtureTranscriptRow(b.userId, sessionId));
    ok(!!error, `B's upsert over A's transcript is denied${error ? '' : ' (expected an error — A5 lockdown regression)'}`);
    // A's row is intact and still owned by A.
    const { data } = await admin.from('session_transcripts').select('user_id').eq('session_id', sessionId).single();
    ok(data?.user_id === a.userId, `A's transcript row still owned by A after B's attempt (owner=${data?.user_id})`);
  }

  console.log("\n6. User B cannot insert an envelope for user A's session_id (client INSERT revoked — A5)");
  {
    const { error } = await b.client.from('scoring_envelopes').insert(fixtureEnvelopeRow(b.userId, sessionId));
    ok(!!error, `B's envelope insert is denied${error ? '' : ' (expected an error — A5 lockdown regression)'}`);
  }

  console.log('\n7. Migration: one-original-per-session unique index is scoped to (user_id, session_id)');
  {
    // Same session_id, different users, both regraded_from null — must both be
    // allowed now that the partial unique index is (user_id, session_id).
    const shared = `exam-sim-${randomUUID()}`;
    const { error: e1 } = await admin.from('scoring_envelopes').insert(fixtureEnvelopeRow(a.userId, shared));
    const { error: e2 } = await admin.from('scoring_envelopes').insert(fixtureEnvelopeRow(b.userId, shared));
    ok(!e1 && !e2, `both users can hold an original envelope for the same session_id${e1 || e2 ? ` (${(e1 || e2).message})` : ''}`);

    // A second original for the SAME (user, session) is still rejected.
    const { error: e3 } = await admin.from('scoring_envelopes').insert(fixtureEnvelopeRow(a.userId, shared));
    ok(!!e3 && /duplicate key|unique/i.test(e3.message), `second original for the same (user, session) still rejected${e3 ? '' : ' (expected a unique violation)'}`);
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
