// Parent view (20261104090000) — parent_links + RPC contract tests. Run against
// the LOCAL Supabase stack only (`npx supabase start` from backend/), never the
// hosted project.
//
// Proves: (1) parent_links is unreadable directly by anon and authenticated;
// (2) anon cannot create a link; (3) create returns a 64-hex token once and the
// label trimmed; (4) anon get_parent_report returns the learner's sessions with
// no transcript or question text, and counts the view; (5) only first answers
// from speaking_samples are returned; (6) another user cannot revoke the link;
// (7) a revoked link answers 'invalid_link'; (8) at most 5 active links.
//
// Usage: node backend/supabase/tests/parent_links.test.mjs

import { createClient } from '@supabase/supabase-js';
import { execSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';

const status = JSON.parse(
  execSync('npx supabase status -o json', { cwd: new URL('../..', import.meta.url), encoding: 'utf8' })
);
const API_URL = status.API_URL;
const ANON_KEY = status.ANON_KEY;
const SERVICE_ROLE_KEY = status.SERVICE_ROLE_KEY;

const opts = { auth: { autoRefreshToken: false, persistSession: false } };
const admin = createClient(API_URL, SERVICE_ROLE_KEY, opts);
const anon = createClient(API_URL, ANON_KEY, opts);

let pass = 0;
let fail = 0;
const failures = [];
function ok(cond, label) {
  if (cond) { pass++; console.log(`  ok - ${label}`); }
  else { fail++; failures.push(label); console.log(`  FAIL - ${label}`); }
}

async function createTestUser(tag) {
  const email = `parent-${tag}-${randomUUID()}@example.test`;
  const password = 'test-password-12345';
  const { data, error } = await admin.auth.admin.createUser({ email, password, email_confirm: true });
  if (error) throw new Error(`createUser(${tag}) failed: ${error.message}`);
  const userId = data.user.id;
  const { error: profileErr } = await admin.from('profiles').upsert({ id: userId, username: `parent_${tag}_${userId.slice(0, 8)}` });
  if (profileErr) throw new Error(`profile upsert(${tag}) failed: ${profileErr.message}`);
  const client = createClient(API_URL, ANON_KEY, opts);
  const { error: signInErr } = await client.auth.signInWithPassword({ email, password });
  if (signInErr) throw new Error(`signIn(${tag}) failed: ${signInErr.message}`);
  return { userId, client };
}

async function main() {
  const learner = await createTestUser('learner');
  const other = await createTestUser('other');

  const { error: seedErr } = await admin.from('sessions').insert([
    { id: `pv-${randomUUID()}`, user_id: learner.userId, mode: 'practice', topic_key: 'school', question_text: 'SECRET QUESTION', transcript: 'SECRET TRANSCRIPT', score: 6.5, duration_sec: 300, feedback: { biggest_opportunity: 'Use the past tense' } },
    { id: `pv-${randomUUID()}`, user_id: learner.userId, mode: 'exam', score: 7.5, duration_sec: 600, feedback: { exam: { practiceOnly: false, marks: null } } },
  ]);
  if (seedErr) throw new Error(`seed sessions failed: ${seedErr.message}`);
  const sample = (id, followUp) => ({
    user_id: learner.userId, id, created_at: new Date().toISOString(), metrics_version: 1, surface: 'learn', style: 'coach',
    question_id: 'q1', follow_up: followUp, transcript_source: 'whisper', duration_sec: 30, recorded_s: 30, start_delay_s: 1,
    speaking_span_s: 25, voiced_s: 20, pause_count: 2, pauses_over_1s: 1, pauses_over_2s: 0, longest_pause_s: 1.5,
    total_pause_s: 3, word_count: 40, words_per_minute: 90,
  });
  const { error: spErr } = await admin.from('speaking_samples').insert([sample(`a-${randomUUID()}`, false), sample(`b-${randomUUID()}`, true)]);
  if (spErr) throw new Error(`seed speaking_samples failed: ${spErr.message}`);

  console.log('\n(1) direct table access');
  {
    const { error: e1 } = await anon.from('parent_links').select('*');
    ok(!!e1, 'anon cannot select parent_links');
    const { data, error: e2 } = await learner.client.from('parent_links').select('*');
    ok(!!e2 || (Array.isArray(data) && data.length === 0), 'authenticated cannot read parent_links rows directly');
  }

  console.log('\n(2) anon cannot create');
  {
    const { error } = await anon.rpc('create_parent_link', { p_label: 'x' });
    ok(!!error, 'anon create_parent_link is denied');
  }

  console.log('\n(3) create');
  const { data: created, error: createErr } = await learner.client.rpc('create_parent_link', { p_label: '  Mum  ' });
  ok(!createErr && /^[0-9a-f]{64}$/.test(created?.token ?? ''), 'create returns a 64-hex token');
  ok(created?.label === 'Mum', 'label is trimmed');

  console.log('\n(4)+(5) anon report');
  {
    const { data, error } = await anon.rpc('get_parent_report', { p_token: created.token });
    ok(!error && Array.isArray(data?.sessions) && data.sessions.length === 2, 'report has the learner’s two sessions');
    ok(!JSON.stringify(data).includes('SECRET'), 'no transcript or question text in the report');
    ok(data?.sessions?.some((s) => s.nextStep === 'Use the past tense'), 'coach next step is included');
    ok(Array.isArray(data?.speaking) && data.speaking.length === 1, 'only first answers from speaking_samples');
    const { data: links } = await learner.client.rpc('list_parent_links');
    ok(links?.[0]?.viewCount === 1 && links?.[0]?.lastViewedAt, 'the view is counted and visible to the learner');
  }

  console.log('\n(6)+(7) revoke');
  {
    const { error: e1 } = await other.client.rpc('revoke_parent_link', { p_id: created.id });
    ok(e1?.message?.includes('link_not_found'), 'another user cannot revoke the link');
    const { error: e2 } = await learner.client.rpc('revoke_parent_link', { p_id: created.id });
    ok(!e2, 'the learner can revoke their link');
    const { error: e3 } = await anon.rpc('get_parent_report', { p_token: created.token });
    ok(e3?.message?.includes('invalid_link'), 'a revoked link answers invalid_link');
  }

  console.log('\n(8) limit');
  {
    for (let i = 0; i < 5; i++) await learner.client.rpc('create_parent_link', { p_label: `l${i}` });
    const { error } = await learner.client.rpc('create_parent_link', { p_label: 'sixth' });
    ok(error?.message?.includes('too_many_links'), 'a sixth active link is refused');
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
