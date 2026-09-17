// Age-band self-correction ("I mis-selected my age band" toggle in Profile
// settings) DB integration tests. Run against the LOCAL Supabase stack only
// (`npx supabase start` from backend/), never the hosted project.
//
// Proves: (1) correct_age_band requires an age_band to already be set
// (age_band_not_set); (2) correct_age_band('13_plus') from under_13 flips
// consent_status to '13_plus_not_required' and, unlike set_age_band, is
// callable — the one-time restriction only applies to the original RPC;
// (3) correct_age_band('under_13') from 13_plus flips consent_status back
// to 'pending', requiring a fresh guardian confirmation via the existing
// request_guardian_consent flow; (4) calling with the band already in
// effect is rejected (age_band_unchanged); (5) age_band/consent_status stay
// off the `authenticated` UPDATE grant — same posture as set_age_band.
//
// Usage: node backend/supabase/tests/age_band_correction.test.mjs
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
  const email = `agecorrect-${tag}-${randomUUID()}@example.test`;
  const password = 'test-password-12345';
  const { data, error } = await admin.auth.admin.createUser({ email, password, email_confirm: true });
  if (error) throw new Error(`createUser(${tag}) failed: ${error.message}`);
  const userId = data.user.id;

  const { error: profileErr } = await admin.from('profiles').upsert({
    id: userId,
    username: `agecorrect_${tag}_${userId.slice(0, 8)}`,
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
  console.log('\n1. correct_age_band requires an age_band to already be set');
  {
    const fresh = await createTestUser('fresh');
    const { error } = await fresh.client.rpc('correct_age_band', { p_band: '13_plus' });
    ok(!!error && error.message.includes('age_band_not_set'),
      `correct_age_band before onboarding is rejected${error ? '' : ' (expected age_band_not_set)'}`);
  }

  console.log('\n2. correct_age_band(\'13_plus\') from under_13 is instant, no re-verification');
  {
    const child = await createTestUser('to13plus');
    await child.client.rpc('set_age_band', { p_band: 'under_13' });

    const { error } = await child.client.rpc('correct_age_band', { p_band: '13_plus' });
    ok(!error, `correct_age_band succeeds${error ? ` (${error.message})` : ''}`);

    const row = await profileRow(child.userId);
    ok(row?.age_band === '13_plus', `age_band is 13_plus`);
    ok(row?.consent_status === '13_plus_not_required', `consent_status is 13_plus_not_required`);
  }

  console.log('\n3. correct_age_band(\'under_13\') from 13_plus resets consent_status to pending');
  {
    const teen = await createTestUser('tounder13');
    await teen.client.rpc('set_age_band', { p_band: '13_plus' });

    const { error } = await teen.client.rpc('correct_age_band', { p_band: 'under_13' });
    ok(!error, `correct_age_band succeeds${error ? ` (${error.message})` : ''}`);

    const row = await profileRow(teen.userId);
    ok(row?.age_band === 'under_13', `age_band is under_13`);
    ok(row?.consent_status === 'pending', `consent_status is pending`);

    const { error: reqErr } = await teen.client.rpc('request_guardian_consent', {
      p_guardian_email: 'parent-corrected@example.test',
    });
    ok(!reqErr, `request_guardian_consent now works post-correction${reqErr ? ` (${reqErr.message})` : ''}`);
  }

  console.log('\n4. correcting to the band already in effect is rejected');
  {
    const child = await createTestUser('unchanged');
    await child.client.rpc('set_age_band', { p_band: 'under_13' });
    const { error } = await child.client.rpc('correct_age_band', { p_band: 'under_13' });
    ok(!!error && error.message.includes('age_band_unchanged'),
      `no-op correction is rejected${error ? '' : ' (expected age_band_unchanged)'}`);
  }

  console.log('\n5. age_band/consent_status remain off the authenticated UPDATE grant');
  {
    const child = await createTestUser('patchdenied');
    await child.client.rpc('set_age_band', { p_band: 'under_13' });
    const { error } = await child.client.from('profiles').update({ age_band: '13_plus' }).eq('id', child.userId);
    ok(!!error, `PATCH age_band is denied${error ? '' : ' (expected an error)'}`);
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
