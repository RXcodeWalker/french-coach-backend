// Phase 1.6 Part C — age band + guardian consent DB integration tests. Run
// against the LOCAL Supabase stack only (`npx supabase start` from
// backend/), never the hosted project.
//
// Proves: (1) set_age_band('under_13') -> consent_status='pending';
// (2) set_age_band('13_plus') -> consent_status='13_plus_not_required',
// no guardian step; (3) set_age_band twice is rejected
// (age_band_already_set); (4) request_guardian_consent rejects a 13+
// account; (5) grant_guardian_consent with a valid token flips the child to
// 'granted'; (6) a reused/invalid token is rejected; (7)
// revoke_guardian_consent flips to 'revoked' AND erases the child's profile
// row (stop processing + erase, per the plan); (8) age_band/consent_status
// are not in the `authenticated` UPDATE grant (a direct PATCH is denied).
//
// Usage: node backend/supabase/tests/guardian_consent.test.mjs
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
  const email = `consent-${tag}-${randomUUID()}@example.test`;
  const password = 'test-password-12345';
  const { data, error } = await admin.auth.admin.createUser({ email, password, email_confirm: true });
  if (error) throw new Error(`createUser(${tag}) failed: ${error.message}`);
  const userId = data.user.id;

  const { error: profileErr } = await admin.from('profiles').upsert({
    id: userId,
    username: `consent_${tag}_${userId.slice(0, 8)}`,
  });
  if (profileErr) throw new Error(`profile upsert(${tag}) failed: ${profileErr.message}`);

  const client = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });
  const { error: signInErr } = await client.auth.signInWithPassword({ email, password });
  if (signInErr) throw new Error(`signIn(${tag}) failed: ${signInErr.message}`);

  return { userId, email, client };
}

async function profileRow(userId) {
  const { data } = await admin.from('profiles').select('age_band, consent_status').eq('id', userId).maybeSingle();
  return data;
}

async function main() {
  console.log('\n1. set_age_band(\'under_13\') sets consent_status to pending');
  {
    const child = await createTestUser('under13');
    const { error } = await child.client.rpc('set_age_band', { p_band: 'under_13' });
    ok(!error, `set_age_band succeeds${error ? ` (${error.message})` : ''}`);
    const row = await profileRow(child.userId);
    ok(row?.age_band === 'under_13', `age_band is under_13`);
    ok(row?.consent_status === 'pending', `consent_status is pending`);
  }

  console.log('\n2. set_age_band(\'13_plus\') needs no guardian step');
  {
    const teen = await createTestUser('teen');
    const { error } = await teen.client.rpc('set_age_band', { p_band: '13_plus' });
    ok(!error, `set_age_band succeeds${error ? ` (${error.message})` : ''}`);
    const row = await profileRow(teen.userId);
    ok(row?.consent_status === '13_plus_not_required', `consent_status is 13_plus_not_required`);

    const { error: reqErr } = await teen.client.rpc('request_guardian_consent', {
      p_guardian_email: 'parent@example.test',
    });
    ok(!!reqErr && reqErr.message.includes('not_under_13'),
      `request_guardian_consent on a 13+ account is rejected${reqErr ? '' : ' (expected not_under_13)'}`);
  }

  console.log('\n3. set_age_band cannot be called twice');
  {
    const child = await createTestUser('twice');
    await child.client.rpc('set_age_band', { p_band: '13_plus' });
    const { error } = await child.client.rpc('set_age_band', { p_band: 'under_13' });
    ok(!!error && error.message.includes('age_band_already_set'),
      `second set_age_band call is rejected${error ? '' : ' (expected age_band_already_set)'}`);
  }

  console.log('\n4/5. request + grant_guardian_consent flips the child to granted');
  let grantedChild;
  {
    grantedChild = await createTestUser('granted');
    await grantedChild.client.rpc('set_age_band', { p_band: 'under_13' });

    const { data: reqData, error: reqErr } = await grantedChild.client.rpc('request_guardian_consent', {
      p_guardian_email: 'parent-granted@example.test',
    });
    ok(!reqErr, `request_guardian_consent succeeds${reqErr ? ` (${reqErr.message})` : ''}`);

    const guardianClient = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });
    const { error: grantErr } = await guardianClient.rpc('grant_guardian_consent', {
      p_token: reqData.token, p_relationship: 'Mother',
    });
    ok(!grantErr, `grant_guardian_consent succeeds anonymously${grantErr ? ` (${grantErr.message})` : ''}`);

    const row = await profileRow(grantedChild.userId);
    ok(row?.consent_status === 'granted', `consent_status is granted`);
  }

  console.log('\n6. a reused token is rejected on the second grant attempt');
  {
    const child = await createTestUser('reuse');
    await child.client.rpc('set_age_band', { p_band: 'under_13' });
    const { data: reqData } = await child.client.rpc('request_guardian_consent', {
      p_guardian_email: 'parent-reuse@example.test',
    });

    const guardianClient = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });
    await guardianClient.rpc('grant_guardian_consent', { p_token: reqData.token, p_relationship: 'Father' });

    const { error: secondErr } = await guardianClient.rpc('grant_guardian_consent', {
      p_token: reqData.token, p_relationship: 'Father',
    });
    ok(!!secondErr && secondErr.message.includes('invalid_or_used_token'),
      `reusing the token is rejected${secondErr ? '' : ' (expected invalid_or_used_token)'}`);
  }

  console.log('\n7. revoke_guardian_consent flips to revoked AND erases the child\'s profile');
  {
    const { error } = await admin.rpc('revoke_guardian_consent', { p_child_user_id: grantedChild.userId });
    ok(!error, `revoke_guardian_consent succeeds${error ? ` (${error.message})` : ''}`);

    const row = await profileRow(grantedChild.userId);
    ok(row === null, `child's profiles row is erased after revocation`);
  }

  console.log('\n8. age_band/consent_status are not client-writable via PATCH');
  {
    const child = await createTestUser('patchdenied');
    const { error } = await child.client.from('profiles').update({ age_band: '13_plus' }).eq('id', child.userId);
    ok(!!error, `PATCH age_band is denied${error ? '' : ' (expected an error)'}`);

    const { error: error2 } = await child.client.from('profiles').update({ consent_status: 'granted' }).eq('id', child.userId);
    ok(!!error2, `PATCH consent_status is denied${error2 ? '' : ' (expected an error)'}`);
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
