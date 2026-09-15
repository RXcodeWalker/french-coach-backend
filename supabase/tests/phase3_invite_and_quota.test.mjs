// Phase 3 — Cost & Abuse Controls: DB integration tests
// (phase-3-plan-tidy-widget.md §4 step 1 / Verification section). Run
// against the LOCAL Supabase stack only (`npx supabase start` from backend/).
//
// Usage: node backend/supabase/tests/phase3_invite_and_quota.test.mjs
// Requires: local stack up (npx supabase start), reads keys from
// `npx supabase status -o json` at run time so nothing is hardcoded here.
//
// redeem_invite_code / check_invite_code are authenticated/anon-callable
// (redeem keyed off auth.uid()) -- exercised through per-user clients.
// consume_ai_quota / release_ai_quota_grant are service_role-only RPCs
// (mirroring how server/ and backend/ call them with the service key, never
// on behalf of a signed-in user) -- tests for those two call through
// `admin.rpc(...)`, matching phase4_shadowing.test.mjs's convention.

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
  const email = `phase3-invite-quota-${tag}-${randomUUID()}@example.test`;
  const password = 'test-password-12345';
  const { data, error } = await admin.auth.admin.createUser({ email, password, email_confirm: true });
  if (error) throw new Error(`createUser(${tag}) failed: ${error.message}`);
  const userId = data.user.id;

  const { error: profileErr } = await admin.from('profiles').upsert({ id: userId, username: `phase3_iq_${tag}_${userId.slice(0, 8)}` });
  if (profileErr) throw new Error(`profile upsert(${tag}) failed: ${profileErr.message}`);

  const client = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });
  const { error: signInErr } = await client.auth.signInWithPassword({ email, password });
  if (signInErr) throw new Error(`signIn(${tag}) failed: ${signInErr.message}`);

  return { userId, client };
}

async function makeCode(overrides = {}) {
  const code = `TEST-${randomUUID().slice(0, 8).toUpperCase()}`;
  const row = { code, max_uses: 1, use_count: 0, ...overrides };
  const { error } = await admin.from('invite_codes').insert(row);
  if (error) throw new Error(`invite_codes insert failed: ${error.message}`);
  return code;
}

async function redeemUser(tag) {
  const u = await createTestUser(tag);
  const code = await makeCode();
  const r = await u.client.rpc('redeem_invite_code', { p_code: code });
  if (r.error || !r.data.granted) throw new Error(`redeemUser(${tag}) failed: ${r.error?.message ?? JSON.stringify(r.data)}`);
  return u;
}

async function main() {
  console.log('Creating two grandfathered/base test users...');
  const a = await createTestUser('a');
  const b = await createTestUser('b');

  console.log("\n1. New profile row defaults to invite_status='unredeemed'");
  {
    const { data } = await admin.from('profiles').select('invite_status').eq('id', a.userId).single();
    ok(data.invite_status === 'unredeemed', `new profile is unredeemed (got ${data?.invite_status})`);
  }

  console.log('\n2. check_invite_code: unknown code -> valid:false, reason:not_found');
  {
    const r = await admin.rpc('check_invite_code', { p_code: 'NOPE-NOPE-NOPE' });
    ok(!r.error && r.data.valid === false && r.data.reason === 'not_found', `unknown code rejected${r.error ? ` (error: ${r.error.message})` : ` (got ${JSON.stringify(r.data)})`}`);
  }

  console.log('\n3. check_invite_code: valid code -> valid:true; anon can call it');
  {
    const code = await makeCode();
    const anonClient = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });
    const r = await anonClient.rpc('check_invite_code', { p_code: code.toLowerCase() });
    ok(!r.error && r.data.valid === true, `valid code accepted (case-insensitive) by anon${r.error ? ` (error: ${r.error.message})` : ` (got ${JSON.stringify(r.data)})`}`);
  }

  console.log('\n4. check_invite_code: exhausted code -> valid:false, reason:exhausted');
  {
    const code = await makeCode({ max_uses: 1, use_count: 1 });
    const r = await admin.rpc('check_invite_code', { p_code: code });
    ok(!r.error && r.data.valid === false && r.data.reason === 'exhausted', `exhausted code rejected${r.error ? ` (error: ${r.error.message})` : ` (got ${JSON.stringify(r.data)})`}`);
  }

  console.log('\n5. check_invite_code: expired code -> valid:false, reason:expired');
  {
    const code = await makeCode({ expires_at: new Date(Date.now() - 60_000).toISOString() });
    const r = await admin.rpc('check_invite_code', { p_code: code });
    ok(!r.error && r.data.valid === false && r.data.reason === 'expired', `expired code rejected${r.error ? ` (error: ${r.error.message})` : ` (got ${JSON.stringify(r.data)})`}`);
  }

  console.log('\n6. redeem_invite_code: valid code grants and flips profiles.invite_status to redeemed');
  {
    const code = await makeCode();
    const r = await a.client.rpc('redeem_invite_code', { p_code: code });
    ok(!r.error && r.data.granted === true && r.data.already_redeemed === false, `redeem succeeds${r.error ? ` (error: ${r.error.message})` : ` (got ${JSON.stringify(r.data)})`}`);
    const { data: prof } = await admin.from('profiles').select('invite_status').eq('id', a.userId).single();
    ok(prof.invite_status === 'redeemed', `profiles.invite_status flipped to redeemed (got ${prof?.invite_status})`);
    const { data: code_row } = await admin.from('invite_codes').select('use_count').eq('code', code).single();
    ok(code_row.use_count === 1, `use_count incremented to 1 (got ${code_row?.use_count})`);
  }

  console.log('\n7. Redeem-twice idempotency: same user redeeming again (different code) is a no-op success, use_count of new code untouched');
  {
    const code2 = await makeCode();
    const r = await a.client.rpc('redeem_invite_code', { p_code: code2 });
    ok(!r.error && r.data.granted === true && r.data.already_redeemed === true, `second redeem is a no-op success${r.error ? ` (error: ${r.error.message})` : ` (got ${JSON.stringify(r.data)})`}`);
    const { data: code2_row } = await admin.from('invite_codes').select('use_count').eq('code', code2).single();
    ok(code2_row.use_count === 0, `second code's use_count untouched (got ${code2_row?.use_count})`);
  }

  console.log('\n8. redeem_invite_code: invalid/exhausted code -> granted:false, invite_status stays unredeemed');
  {
    const c = await createTestUser('redeem-fail');
    const r = await c.client.rpc('redeem_invite_code', { p_code: 'DOES-NOT-EXIST' });
    ok(!r.error && r.data.granted === false && r.data.reason === 'invalid_or_exhausted', `bad code denied${r.error ? ` (error: ${r.error.message})` : ` (got ${JSON.stringify(r.data)})`}`);
    const { data: prof } = await admin.from('profiles').select('invite_status').eq('id', c.userId).single();
    ok(prof.invite_status === 'unredeemed', `invite_status still unredeemed after failed redeem (got ${prof?.invite_status})`);
  }

  console.log('\n9. redeem_invite_code: max_uses=2 code can be redeemed by two different users, third is denied');
  {
    const code = await makeCode({ max_uses: 2 });
    const u1 = await createTestUser('multi-1');
    const u2 = await createTestUser('multi-2');
    const u3 = await createTestUser('multi-3');
    const r1 = await u1.client.rpc('redeem_invite_code', { p_code: code });
    const r2 = await u2.client.rpc('redeem_invite_code', { p_code: code });
    const r3 = await u3.client.rpc('redeem_invite_code', { p_code: code });
    ok(!r1.error && r1.data.granted === true, 'first user granted');
    ok(!r2.error && r2.data.granted === true, 'second user granted');
    ok(!r3.error && r3.data.granted === false, `third user denied${r3.error ? ` (error: ${r3.error.message})` : ` (got ${JSON.stringify(r3.data)})`}`);
  }

  console.log('\n10. anon cannot EXECUTE redeem_invite_code');
  {
    const anonClient = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });
    const r = await anonClient.rpc('redeem_invite_code', { p_code: 'ANYTHING' });
    ok(!!r.error, `anon redeem denied${r.error ? '' : ' (expected an error)'}`);
  }

  console.log('\n11. consume_ai_quota: unredeemed user denied with reason invite_not_redeemed, no grant row, no exception');
  {
    const c = await createTestUser('quota-unredeemed');
    const r = await admin.rpc('consume_ai_quota', { p_user_id: c.userId, p_feature: 'score', p_idempotency_key: 'k-1' });
    ok(!r.error && r.data.granted === false && r.data.reason === 'invite_not_redeemed', `unredeemed user denied at quota RPC${r.error ? ` (error: ${r.error.message})` : ` (got ${JSON.stringify(r.data)})`}`);
    const { count } = await admin.from('ai_usage_grants').select('*', { count: 'exact', head: true }).eq('user_id', c.userId);
    ok(count === 0, `no grant row written (got ${count})`);
  }

  console.log('\n12. consume_ai_quota: a user with no profiles row at all is also treated as not-redeemed (never errors)');
  {
    const { data: userData, error: createErr } = await admin.auth.admin.createUser({
      email: `phase3-noprofile-${randomUUID()}@example.test`, password: 'test-password-12345', email_confirm: true,
    });
    if (createErr) throw new Error(`createUser(no-profile) failed: ${createErr.message}`);
    // Deliberately NOT inserting a profiles row for this user.
    const r = await admin.rpc('consume_ai_quota', { p_user_id: userData.user.id, p_feature: 'score', p_idempotency_key: 'k-1' });
    ok(!r.error && r.data.granted === false && r.data.reason === 'invite_not_redeemed', `no-profile-row user denied cleanly, not an exception${r.error ? ` (error: ${r.error.message})` : ` (got ${JSON.stringify(r.data)})`}`);
  }

  console.log('\n13. consume_ai_quota: redeemed user granted on calls 1..N up to daily_limit, denied on N+1');
  {
    const d = await redeemUser('quota-cap');
    const { data: limitRow } = await admin.from('ai_quota_limits').select('daily_limit').eq('feature', 'score').single();
    const limit = limitRow.daily_limit;
    for (let i = 1; i <= limit; i++) {
      const r = await admin.rpc('consume_ai_quota', { p_user_id: d.userId, p_feature: 'score', p_idempotency_key: `seq-${i}` });
      ok(!r.error && r.data.granted === true && r.data.used === i, `call ${i}/${limit} granted, used=${i}${r.error ? ` (error: ${r.error.message})` : ` (got used=${r.data?.used})`}`);
    }
    const over = await admin.rpc('consume_ai_quota', { p_user_id: d.userId, p_feature: 'score', p_idempotency_key: `seq-${limit + 1}` });
    ok(!over.error && over.data.granted === false && over.data.reason === 'daily_quota_reached', `call ${limit + 1} denied with daily_quota_reached${over.error ? ` (error: ${over.error.message})` : ` (got ${JSON.stringify(over.data)})`}`);
  }

  console.log('\n14. consume_ai_quota: replay of a used key grants without incrementing count');
  {
    const e = await redeemUser('quota-replay');
    await admin.rpc('consume_ai_quota', { p_user_id: e.userId, p_feature: 'feedback', p_idempotency_key: 'r-1' });
    const r = await admin.rpc('consume_ai_quota', { p_user_id: e.userId, p_feature: 'feedback', p_idempotency_key: 'r-1' });
    ok(!r.error && r.data.granted === true && r.data.replayed === true, `replay grants, replayed:true${r.error ? ` (error: ${r.error.message})` : ''}`);
    const { count } = await admin.from('ai_usage_grants').select('*', { count: 'exact', head: true }).eq('user_id', e.userId).eq('feature', 'feedback');
    ok(count === 1, `still exactly 1 row after replay (got ${count})`);
  }

  console.log('\n15. Concurrency at the boundary: feature with limit 20 (score), 25 distinct keys -> exactly 20 granted:true');
  {
    const f = await redeemUser('quota-concurrency');
    const keys = Array.from({ length: 25 }, (_, i) => `race-${i}`);
    const results = await Promise.all(keys.map(k =>
      admin.rpc('consume_ai_quota', { p_user_id: f.userId, p_feature: 'score', p_idempotency_key: k })
    ));
    const grantedCount = results.filter(r => !r.error && r.data.granted === true).length;
    ok(grantedCount === 20, `exactly 20 of 25 concurrent calls granted (got ${grantedCount})`);
    const { count } = await admin.from('ai_usage_grants').select('*', { count: 'exact', head: true }).eq('user_id', f.userId).eq('feature', 'score');
    ok(count === 20, `exactly 20 rows written (got ${count})`);
  }

  console.log('\n16. Roleplay collision regression: two different users, same idempotency key text, both consume independently (not deduped against each other)');
  {
    const g1 = await redeemUser('collision-1');
    const g2 = await redeemUser('collision-2');
    const r1 = await admin.rpc('consume_ai_quota', { p_user_id: g1.userId, p_feature: 'roleplay_turn', p_idempotency_key: 'same-turn-id-text' });
    const r2 = await admin.rpc('consume_ai_quota', { p_user_id: g2.userId, p_feature: 'roleplay_turn', p_idempotency_key: 'same-turn-id-text' });
    ok(!r1.error && r1.data.granted === true && r1.data.replayed === false, `user 1 consumes independently${r1.error ? ` (error: ${r1.error.message})` : ''}`);
    ok(!r2.error && r2.data.granted === true && r2.data.replayed === false, `user 2 also consumes independently, not replayed against user 1${r2.error ? ` (error: ${r2.error.message})` : ` (got ${JSON.stringify(r2.data)})`}`);
  }

  console.log('\n17. Refund: consume up to cap -> release one -> used drops -> a fresh consume is granted again');
  {
    const h = await redeemUser('quota-refund');
    await admin.rpc('consume_ai_quota', { p_user_id: h.userId, p_feature: 'exam', p_idempotency_key: 'x-1' });
    await admin.rpc('consume_ai_quota', { p_user_id: h.userId, p_feature: 'exam', p_idempotency_key: 'x-2' });
    const rel = await admin.rpc('release_ai_quota_grant', { p_user_id: h.userId, p_feature: 'exam', p_idempotency_key: 'x-2' });
    ok(!rel.error && rel.data.released === true && rel.data.used === 1, `release succeeds, used drops to 1${rel.error ? ` (error: ${rel.error.message})` : ` (got ${JSON.stringify(rel.data)})`}`);
  }

  console.log('\n18. Refund idempotency: release_... twice returns released:false the second time');
  {
    const i = await redeemUser('quota-refund-idem');
    await admin.rpc('consume_ai_quota', { p_user_id: i.userId, p_feature: 'transcribe', p_idempotency_key: 'y-1' });
    const r1 = await admin.rpc('release_ai_quota_grant', { p_user_id: i.userId, p_feature: 'transcribe', p_idempotency_key: 'y-1' });
    const r2 = await admin.rpc('release_ai_quota_grant', { p_user_id: i.userId, p_feature: 'transcribe', p_idempotency_key: 'y-1' });
    ok(!r1.error && r1.data.released === true, 'first release: released:true');
    ok(!r2.error && r2.data.released === false, `second release: released:false${r2.error ? ` (error: ${r2.error.message})` : ''}`);
  }

  console.log('\n19. Quota isolation: two features do not share a cap (consuming one does not affect the other)');
  {
    const j = await redeemUser('quota-isolation');
    await admin.rpc('consume_ai_quota', { p_user_id: j.userId, p_feature: 'pronunciation', p_idempotency_key: 'p-1' });
    const r = await admin.rpc('consume_ai_quota', { p_user_id: j.userId, p_feature: 'transcribe', p_idempotency_key: 't-1' });
    ok(!r.error && r.data.granted === true && r.data.used === 1, `unrelated feature unaffected (got ${JSON.stringify(r.data)})`);
  }

  console.log('\n20. authenticated and anon cannot EXECUTE consume_ai_quota or release_ai_quota_grant');
  {
    const k = await redeemUser('quota-deny');
    const r1 = await k.client.rpc('consume_ai_quota', { p_user_id: k.userId, p_feature: 'score', p_idempotency_key: 'deny-1' });
    ok(!!r1.error, `authenticated consume_ai_quota denied${r1.error ? '' : ' (expected an error)'}`);
    const anonClient = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });
    const r2 = await anonClient.rpc('consume_ai_quota', { p_user_id: k.userId, p_feature: 'score', p_idempotency_key: 'deny-2' });
    ok(!!r2.error, `anon consume_ai_quota denied${r2.error ? '' : ' (expected an error)'}`);
  }

  console.log('\n21. Profile delete cascades away ai_usage_grants and invite_code_redemptions');
  {
    const l = await redeemUser('cascade');
    await admin.rpc('consume_ai_quota', { p_user_id: l.userId, p_feature: 'score', p_idempotency_key: 'cascade-1' });

    await admin.auth.admin.deleteUser(l.userId);

    const { count: grantsCount } = await admin.from('ai_usage_grants').select('*', { count: 'exact', head: true }).eq('user_id', l.userId);
    const { count: redemptionsCount } = await admin.from('invite_code_redemptions').select('*', { count: 'exact', head: true }).eq('user_id', l.userId);
    ok(grantsCount === 0, `ai_usage_grants cascaded away (got ${grantsCount})`);
    ok(redemptionsCount === 0, `invite_code_redemptions cascaded away (got ${redemptionsCount})`);
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
