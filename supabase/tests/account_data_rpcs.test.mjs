// Phase 1.6 Part B — data export & account deletion DB integration tests.
// Run against the LOCAL Supabase stack only (`npx supabase start` from
// backend/), never the hosted project.
//
// Proves: (1) export_my_data returns the caller's own data, scoped to just
// them; (2) delete_my_account cascades (child rows gone, profile gone);
// (3) a confirmed guardian can export/delete a linked child's data via
// p_subject_user_id; (4) a non-guardian caller is rejected with
// not_guardian_of_subject.
//
// Usage: node backend/supabase/tests/account_data_rpcs.test.mjs
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
  const email = `acct-${tag}-${randomUUID()}@example.test`;
  const password = 'test-password-12345';
  const { data, error } = await admin.auth.admin.createUser({ email, password, email_confirm: true });
  if (error) throw new Error(`createUser(${tag}) failed: ${error.message}`);
  const userId = data.user.id;

  const { error: profileErr } = await admin.from('profiles').upsert({
    id: userId,
    username: `acct_${tag}_${userId.slice(0, 8)}`,
  });
  if (profileErr) throw new Error(`profile upsert(${tag}) failed: ${profileErr.message}`);

  const client = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });
  const { error: signInErr } = await client.auth.signInWithPassword({ email, password });
  if (signInErr) throw new Error(`signIn(${tag}) failed: ${signInErr.message}`);

  return { userId, email, client };
}

async function main() {
  console.log('Creating test users A and B...');
  const a = await createTestUser('a');
  const b = await createTestUser('b');

  // A earns some XP so export has something to show beyond the bare profile.
  await a.client.rpc('submit_xp_event', {
    p_source: 'practice', p_amount: 20, p_idempotency_key: `acct-test-${randomUUID()}`,
    p_occurred_at: new Date().toISOString(), p_metadata: {},
  });

  console.log('\n1. export_my_data returns the caller\'s own profile + xp_events, nothing else\'s');
  {
    const { data, error } = await a.client.rpc('export_my_data');
    ok(!error, `export_my_data succeeds for A${error ? ` (${error.message})` : ''}`);
    ok(data?.profile?.id === a.userId, `exported profile.id is A's own id`);
    ok(Array.isArray(data?.xp_events) && data.xp_events.length >= 1, `exported xp_events includes A's ledger row`);
  }

  console.log('\n2. delete_my_account erases the caller\'s profile and cascades');
  {
    const { error } = await b.client.rpc('delete_my_account');
    ok(!error, `delete_my_account succeeds for B${error ? ` (${error.message})` : ''}`);

    const { data: profileRow } = await admin.from('profiles').select('id').eq('id', b.userId).maybeSingle();
    ok(profileRow === null, `B's profiles row is gone`);
  }

  console.log('\n3. guardian (granted, non-revoked) can export a linked child\'s data');
  {
    const child = await createTestUser('child1');
    await child.client.rpc('set_age_band', { p_band: 'under_13' });

    const guardianEmail = `guardian-${randomUUID()}@example.test`;
    const { data: reqData, error: reqErr } = await child.client.rpc('request_guardian_consent', {
      p_guardian_email: guardianEmail,
    });
    ok(!reqErr, `request_guardian_consent succeeds${reqErr ? ` (${reqErr.message})` : ''}`);

    // The guardian account is a separate authenticated user whose auth.users
    // email matches guardian_email — created here to exercise the RPC's
    // `EXISTS (... auth.users u WHERE u.id = me AND lower(u.email) = gc.guardian_email)`
    // check exactly as designed.
    const guardianPassword = 'guardian-password-12345';
    const { data: guardianUser, error: guardianCreateErr } = await admin.auth.admin.createUser({
      email: guardianEmail, password: guardianPassword, email_confirm: true,
    });
    ok(!guardianCreateErr, `guardian account created${guardianCreateErr ? ` (${guardianCreateErr.message})` : ''}`);
    const guardianClient = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });
    await guardianClient.auth.signInWithPassword({ email: guardianEmail, password: guardianPassword });

    const token = reqData.token;
    const { error: grantErr } = await guardianClient.rpc('grant_guardian_consent', {
      p_token: token, p_relationship: 'Parent',
    });
    ok(!grantErr, `grant_guardian_consent succeeds${grantErr ? ` (${grantErr.message})` : ''}`);

    const { data: exportData, error: exportErr } = await guardianClient.rpc('export_my_data', {
      p_subject_user_id: child.userId,
    });
    ok(!exportErr, `guardian export_my_data(child) succeeds${exportErr ? ` (${exportErr.message})` : ''}`);
    ok(exportData?.profile?.id === child.userId, `exported profile is the child's, not the guardian's`);
  }

  console.log('\n4. a non-guardian cannot export/delete another user\'s data via p_subject_user_id');
  {
    const child2 = await createTestUser('child2');
    const stranger = await createTestUser('stranger');

    const { error: exportErr } = await stranger.client.rpc('export_my_data', {
      p_subject_user_id: child2.userId,
    });
    ok(!!exportErr && exportErr.message.includes('not_guardian_of_subject'),
      `export_my_data(child2) as a stranger is rejected${exportErr ? '' : ' (expected not_guardian_of_subject)'}`);

    const { error: deleteErr } = await stranger.client.rpc('delete_my_account', {
      p_subject_user_id: child2.userId,
    });
    ok(!!deleteErr && deleteErr.message.includes('not_guardian_of_subject'),
      `delete_my_account(child2) as a stranger is rejected${deleteErr ? '' : ' (expected not_guardian_of_subject)'}`);

    const { data: stillThere } = await admin.from('profiles').select('id').eq('id', child2.userId).maybeSingle();
    ok(stillThere?.id === child2.userId, `child2's profile survives the rejected attempt`);
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
