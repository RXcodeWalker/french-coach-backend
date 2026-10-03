// Phase 3 Batch A — exam_feedback_reports (20261003120000). Run against the
// LOCAL Supabase stack only (`npx supabase start` from backend/), never the
// hosted project.
//
// What this proves: the post-marking exam report table is service-key only —
// no browser client (anon, or the signed-in owner) can read or write it; the
// service role can insert and read; envelope_id is unique (one report per
// envelope) and must reference a real scoring_envelopes row; deleting the
// envelope cascades to its report.
//
// Usage: node backend/supabase/tests/exam_feedback_reports.test.mjs
// Requires: local stack up; keys read from `npx supabase status -o json`.

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
const anon = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });

let pass = 0;
let fail = 0;
const failures = [];

function ok(cond, label) {
  if (cond) { pass++; console.log(`  ok - ${label}`); }
  else { fail++; failures.push(label); console.log(`  FAIL - ${label}`); }
}

async function createTestUser(tag) {
  const email = `feedback-${tag}-${randomUUID()}@example.test`;
  const password = 'test-password-12345';
  const { data, error } = await admin.auth.admin.createUser({ email, password, email_confirm: true });
  if (error) throw new Error(`createUser(${tag}) failed: ${error.message}`);
  const userId = data.user.id;
  const { error: profileErr } = await admin.from('profiles').upsert({ id: userId, username: `fb_${tag}_${userId.slice(0, 8)}` });
  if (profileErr) throw new Error(`profile upsert(${tag}) failed: ${profileErr.message}`);
  const client = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });
  const { error: signInErr } = await client.auth.signInWithPassword({ email, password });
  if (signInErr) throw new Error(`signIn(${tag}) failed: ${signInErr.message}`);
  return { userId, client };
}

const REPORT = {
  feedbackVersion: 'exam-feedback-v0.1',
  rolePlay: { tasks: [], strengths: [], nextStep: null },
  communication: { strengths: [], nextStep: null },
  qualityOfLanguage: { strengths: [], errors: [], nextStep: null },
};

async function main() {
  const a = await createTestUser('a');
  const sessionId = `exam-sim-${randomUUID()}`;
  const attemptId = randomUUID();

  console.log('1. Seed an original envelope (service role)');
  {
    const { error } = await admin.from('scoring_envelopes').insert({
      attempt_id: attemptId,
      session_id: sessionId,
      user_id: a.userId,
      content_provenance: 'original-practice',
      envelope: { total: 28, attemptId, sessionId },
    });
    ok(!error, `envelope inserted${error ? ` (${error.message})` : ''}`);
  }

  const row = { session_id: sessionId, envelope_id: attemptId, user_id: a.userId, feedback_version: REPORT.feedbackVersion, report: REPORT };

  console.log('\n2. The owner cannot insert a report from the browser');
  {
    const { error } = await a.client.from('exam_feedback_reports').insert(row);
    ok(!!error, `owner insert denied${error ? '' : ' (expected an error)'}`);
  }

  console.log('\n3. The service role inserts the report');
  {
    const { error } = await admin.from('exam_feedback_reports').insert(row);
    ok(!error, `service insert ok${error ? ` (${error.message})` : ''}`);
  }

  console.log('\n4. Neither the owner nor anon can read it from the browser');
  {
    const { data: own, error: ownErr } = await a.client.from('exam_feedback_reports').select('*').eq('session_id', sessionId);
    ok(!!ownErr || (Array.isArray(own) && own.length === 0), `owner sees no rows (got ${own?.length ?? ownErr?.message})`);
    const { data: anonRows, error: anonErr } = await anon.from('exam_feedback_reports').select('*').eq('session_id', sessionId);
    ok(!!anonErr || (Array.isArray(anonRows) && anonRows.length === 0), `anon sees no rows (got ${anonRows?.length ?? anonErr?.message})`);
  }

  console.log('\n5. The service role reads it back, filtered by user');
  {
    const { data } = await admin.from('exam_feedback_reports').select('report').eq('envelope_id', attemptId).eq('user_id', a.userId).maybeSingle();
    ok(data?.report?.feedbackVersion === REPORT.feedbackVersion, 'service reads the stored report');
  }

  console.log('\n6. One report per envelope (envelope_id unique -> 23505)');
  {
    const { error } = await admin.from('exam_feedback_reports').insert(row);
    ok(error?.code === '23505', `second insert rejected with 23505 (got ${error?.code ?? 'no error'})`);
  }

  console.log('\n7. envelope_id must reference a real envelope');
  {
    const { error } = await admin.from('exam_feedback_reports').insert({ ...row, envelope_id: randomUUID() });
    ok(error?.code === '23503', `dangling envelope_id rejected with 23503 (got ${error?.code ?? 'no error'})`);
  }

  console.log('\n8. Deleting the envelope cascades to its report');
  {
    await admin.from('scoring_envelopes').delete().eq('attempt_id', attemptId);
    const { data } = await admin.from('exam_feedback_reports').select('id').eq('envelope_id', attemptId);
    ok(Array.isArray(data) && data.length === 0, `report gone after envelope delete (got ${data?.length})`);
  }

  await admin.auth.admin.deleteUser(a.userId);

  console.log(`\n${pass} passed, ${fail} failed`);
  if (fail > 0) {
    for (const f of failures) console.log(`  - ${f}`);
    process.exit(1);
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
