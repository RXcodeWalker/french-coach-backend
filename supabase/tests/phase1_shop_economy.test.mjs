// Shop Phase 1 — DB integration tests (plan §16). Run against the LOCAL
// Supabase stack only (`npx supabase start` from backend/), never the
// hosted project. No existing JS/pgTAP test harness exists in backend/
// (it's a Python service elsewhere) or in the frontend beyond vitest unit
// tests of pure functions, so this is a standalone script using
// @supabase/supabase-js (already a frontend dependency — Node's ESM
// resolution walks up from this file's directory to find it, no separate
// package.json needed here) rather than wiring a DB-dependent suite into
// `npm test`, which runs without any local infra.
//
// Usage: node backend/supabase/tests/phase1_shop_economy.test.mjs
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
  const email = `phase1-${tag}-${randomUUID()}@example.test`;
  const password = 'test-password-12345';
  const { data, error } = await admin.auth.admin.createUser({ email, password, email_confirm: true });
  if (error) throw new Error(`createUser(${tag}) failed: ${error.message}`);
  const userId = data.user.id;

  const { error: profileErr } = await admin.from('profiles').upsert({ id: userId, username: `phase1_${tag}_${userId.slice(0, 8)}` });
  if (profileErr) throw new Error(`profile upsert(${tag}) failed: ${profileErr.message}`);

  const client = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });
  const { error: signInErr } = await client.auth.signInWithPassword({ email, password });
  if (signInErr) throw new Error(`signIn(${tag}) failed: ${signInErr.message}`);

  return { userId, client };
}

function psql(sql) {
  return execSync(`docker exec supabase_db_French_2.0 psql -U postgres -d postgres -t -A -c "${sql.replace(/"/g, '\\"')}"`, { encoding: 'utf8' });
}

async function grantGems(userId, amount) {
  // mint_gems is capped at 20/call and 450/day — grant test balances
  // directly as the postgres superuser instead. service_role has no
  // table-level INSERT grant on gem_events (deliberately, see the schema
  // migration's header — even the trusted key can't bypass the RPCs), and
  // this CLI version has no `db execute` subcommand, so this runs the
  // insert straight against the local container's postgres via psql.
  psql(`INSERT INTO gem_events (id, user_id, delta, kind, metadata) VALUES ('${randomUUID()}', '${userId}', ${amount}, 'grant', '{"reason":"test_setup"}')`);
}

async function main() {
  console.log('Creating two test users...');
  const a = await createTestUser('a');
  const b = await createTestUser('b');
  await grantGems(a.userId, 5000);
  await grantGems(b.userId, 5000);

  console.log('\n1. Same idempotency key twice, same item');
  {
    const key = randomUUID();
    const r1 = await a.client.rpc('purchase_shop_item', { p_item_id: 'frame_ardoise', p_idempotency_key: key });
    const r2 = await a.client.rpc('purchase_shop_item', { p_item_id: 'frame_ardoise', p_idempotency_key: key });
    ok(!r1.error && r1.data.ok && !r1.data.replayed, 'first purchase succeeds, not replayed');
    ok(!r2.error && r2.data.ok && r2.data.replayed === true, 'second call with same key returns replayed:true');
    ok(r1.data.balance === r2.data.balance, 'balance unchanged between the two calls');
    const { count } = await admin.from('gem_events').select('*', { count: 'exact', head: true }).eq('user_id', a.userId).eq('item_id', 'frame_ardoise');
    ok(count === 1, 'exactly one gem_events row for this purchase');
  }

  console.log('\n2. Same idempotency key, different item');
  {
    const key = randomUUID();
    const r1 = await a.client.rpc('purchase_shop_item', { p_item_id: 'nameplate_encre', p_idempotency_key: key });
    const r2 = await a.client.rpc('purchase_shop_item', { p_item_id: 'frame_emeraude', p_idempotency_key: key });
    ok(!r1.error && r1.data.ok, 'first purchase (nameplate_encre) succeeds');
    ok(!r2.error && r2.data.replayed === true, 'second call replays the FIRST purchase, not a new charge');
    const { data: inv } = await admin.from('user_inventory').select('item_id').eq('user_id', a.userId).eq('item_id', 'frame_emeraude');
    ok(inv.length === 0, 'the second item was never granted');
  }

  console.log('\n3. User B replays user A\'s raw key string');
  {
    // streak_freeze: no requirement, unlimited max_owned — safe to buy
    // repeatedly by either user without tripping already_owned/requirement
    // checks that would confound this test's own assertion.
    const rawKey = 'shared-raw-key-attempt';
    const rA = await a.client.rpc('purchase_shop_item', { p_item_id: 'streak_freeze', p_idempotency_key: rawKey });
    const rB = await b.client.rpc('purchase_shop_item', { p_item_id: 'streak_freeze', p_idempotency_key: rawKey });
    ok(!rA.error && rA.data.ok && !rA.data.replayed, `A's purchase succeeds normally${rA.error ? ` (error: ${rA.error.message})` : ''}`);
    ok(!rB.error && rB.data.ok && !rB.data.replayed, `B's purchase with the SAME raw key is a normal new purchase, not a replay${rB.error ? ` (error: ${rB.error.message})` : ''}`);
    const { data: bInv } = await admin.from('user_inventory').select('qty').eq('user_id', b.userId).eq('item_id', 'streak_freeze').maybeSingle();
    ok(bInv?.qty === 1, 'B actually owns the item (was charged, not silently skipped)');
  }

  console.log('\n9. Purchase with unmet requirement');
  {
    const before = await admin.from('gem_events').select('*', { count: 'exact', head: true }).eq('user_id', b.userId);
    const r = await b.client.rpc('purchase_shop_item', { p_item_id: 'avatar_couronne', p_idempotency_key: randomUUID() });
    ok(r.error && /requirement_not_met/.test(r.error.message), 'raises requirement_not_met (missing bete_de_mode achievement)');
    const after = await admin.from('gem_events').select('*', { count: 'exact', head: true }).eq('user_id', b.userId);
    ok(before.count === after.count, 'no ledger row written');
    const { data: inv } = await admin.from('user_inventory').select('*').eq('user_id', b.userId).eq('item_id', 'avatar_couronne');
    ok(inv.length === 0, 'no inventory row written');
  }

  console.log('\n5. Same item purchased concurrently, max_owned=1');
  {
    const [r1, r2] = await Promise.all([
      b.client.rpc('purchase_shop_item', { p_item_id: 'frame_ardoise', p_idempotency_key: randomUUID() }),
      b.client.rpc('purchase_shop_item', { p_item_id: 'frame_ardoise', p_idempotency_key: randomUUID() }),
    ]);
    const results = [r1, r2];
    const succeeded = results.filter(r => !r.error && r.data.ok);
    const alreadyOwned = results.filter(r => r.error && /already_owned/.test(r.error.message));
    ok(succeeded.length === 1, 'exactly one concurrent purchase succeeds');
    ok(alreadyOwned.length === 1, 'the other raises already_owned');
    const { data: inv } = await admin.from('user_inventory').select('qty').eq('user_id', b.userId).eq('item_id', 'frame_ardoise').single();
    ok(inv.qty === 1, 'qty never exceeds 1');
  }

  console.log('\n4. Two concurrent purchases, balance covers only one');
  {
    const { userId: dId, client: d } = await createTestUser('d');
    await grantGems(dId, 250); // exactly one frame_ardoise (250), not two
    const [r1, r2] = await Promise.all([
      d.rpc('purchase_shop_item', { p_item_id: 'frame_ardoise', p_idempotency_key: randomUUID() }),
      d.rpc('purchase_shop_item', { p_item_id: 'nameplate_encre', p_idempotency_key: randomUUID() }),
    ]);
    const results = [r1, r2];
    const succeeded = results.filter(r => !r.error && r.data.ok);
    const insufficient = results.filter(r => r.error && /insufficient_gems/.test(r.error.message));
    ok(succeeded.length === 1, 'exactly one succeeds when balance covers only one');
    ok(insufficient.length === 1, 'other raises insufficient_gems');
    const { data: balRows } = await admin.from('gem_events').select('delta').eq('user_id', dId);
    const balance = balRows.reduce((s, r) => s + r.delta, 0);
    ok(balance >= 0, 'balance never negative');
  }

  console.log('\n6. Client UPDATE profiles SET gems/inventory/avatar_emoji/equipped_frame with anon key');
  {
    const { error: e1 } = await a.client.from('profiles').update({ gems: 999999 }).eq('id', a.userId);
    const { error: e2 } = await a.client.from('profiles').update({ inventory: {} }).eq('id', a.userId);
    const { error: e3 } = await a.client.from('profiles').update({ avatar_emoji: '🚀' }).eq('id', a.userId);
    const { error: e4 } = await a.client.from('profiles').update({ equipped_frame: 'frame_ardoise' }).eq('id', a.userId);
    ok(!!e1, 'UPDATE gems denied');
    ok(!!e2, 'UPDATE inventory denied');
    ok(!!e3, 'UPDATE avatar_emoji denied');
    ok(!!e4, 'UPDATE equipped_frame denied');
  }

  console.log('\n7. Client INSERT into gem_events / user_inventory with anon key');
  {
    const { error: e1 } = await a.client.from('gem_events').insert({ id: randomUUID(), user_id: a.userId, delta: 100, kind: 'grant' });
    const { error: e2 } = await a.client.from('user_inventory').insert({ user_id: a.userId, item_id: 'frame_ardoise', qty: 99 });
    ok(!!e1, 'INSERT gem_events denied');
    ok(!!e2, 'INSERT user_inventory denied');
  }

  console.log('\n8. Client INSERT into scoring_envelopes / session_transcripts with anon key');
  {
    const { error: e1 } = await a.client.from('scoring_envelopes').insert({
      attempt_id: randomUUID(), session_id: randomUUID(), user_id: a.userId,
      content_provenance: 'original-practice', envelope: {},
    });
    const { error: e2 } = await a.client.from('session_transcripts').insert({
      session_id: randomUUID(), user_id: a.userId, schema_version: '1',
      content_provenance: 'original-practice', stt: {}, transcript: {},
    });
    ok(!!e1, 'INSERT scoring_envelopes denied');
    ok(!!e2, 'INSERT session_transcripts denied');
  }

  console.log('\n10. equip_cosmetic for an unowned item');
  {
    const { data: before } = await admin.from('profiles').select('equipped_nameplate').eq('id', b.userId).single();
    const r = await b.client.rpc('equip_cosmetic', { p_slot: 'nameplate', p_item_id: 'nameplate_tricolore' });
    ok(r.error && /not_owned/.test(r.error.message), 'raises not_owned');
    const { data: after } = await admin.from('profiles').select('equipped_nameplate').eq('id', b.userId).single();
    ok(before.equipped_nameplate === after.equipped_nameplate, 'profiles unchanged');
  }

  console.log('\n(equip happy path, not numbered in §16 but needed to trust #10)');
  {
    const r = await a.client.rpc('equip_cosmetic', { p_slot: 'nameplate', p_item_id: 'nameplate_encre' });
    ok(!r.error, 'equip of an owned item succeeds');
    const { data } = await admin.from('profiles').select('equipped_nameplate').eq('id', a.userId).single();
    ok(data.equipped_nameplate === 'nameplate_encre', 'profiles.equipped_nameplate reflects the equip');
  }

  console.log('\n11. Refund migration run twice');
  {
    // The migration file itself only runs once per the CLI's applied-
    // migrations ledger; the actual idempotency guarantee under test is the
    // ON CONFLICT DO NOTHING on both inserts, so re-run that SQL directly.
    const before = await admin.from('gem_events').select('*', { count: 'exact', head: true }).like('id', 'opening:v1:%');
    psql(`INSERT INTO gem_events (id, user_id, delta, kind, metadata) SELECT 'opening:v1:' || id, id, gems, 'grant', '{}'::jsonb FROM profiles WHERE gems <> 0 ON CONFLICT (id) DO NOTHING`);
    const after = await admin.from('gem_events').select('*', { count: 'exact', head: true }).like('id', 'opening:v1:%');
    ok(before.count === after.count, 'second run inserts zero additional opening-balance rows');
  }

  console.log('\n12. consume_item replayed with an old key');
  {
    const purchase = await a.client.rpc('purchase_shop_item', { p_item_id: 'streak_freeze', p_idempotency_key: randomUUID() });
    ok(!purchase.error, 'setup: bought a streak_freeze');
    const key = randomUUID();
    const c1 = await a.client.rpc('consume_item', { p_item_id: 'streak_freeze', p_idempotency_key: key });
    const c2 = await a.client.rpc('consume_item', { p_item_id: 'streak_freeze', p_idempotency_key: key });
    ok(!c1.error && c1.data.ok && !c1.data.replayed, 'first consume succeeds');
    ok(!c2.error && c2.data.replayed === true, 'replay with old key returns replayed:true');
    ok(c1.data.qty === c2.data.qty, 'no second decrement — qty identical between the two responses');
  }

  console.log('\n13. mint_gems past 450 in one occurred_at day');
  {
    const { userId: eId, client: e } = await createTestUser('e');
    const day = new Date().toISOString();
    let total = 0;
    let cappedSeen = false;
    for (let i = 0; i < 30 && !cappedSeen; i++) {
      const r = await e.rpc('mint_gems', { p_idempotency_key: randomUUID(), p_amount: 20, p_occurred_at: day });
      ok(!r.error, `mint call ${i} does not error`);
      if (r.data?.capped) cappedSeen = true;
      else total += 20;
    }
    ok(cappedSeen, 'cap eventually returns capped:true instead of erroring');
    ok(total <= 450, 'total minted before capping stays within the 450 bound');
  }

  console.log('\n14. mint_gems with p_amount = 5000 or occurred_at 60 days back');
  {
    const r1 = await a.client.rpc('mint_gems', { p_idempotency_key: randomUUID(), p_amount: 5000, p_occurred_at: new Date().toISOString() });
    ok(r1.error && /invalid_amount/.test(r1.error.message), 'amount 5000 rejected');
    const sixtyDaysAgo = new Date(Date.now() - 60 * 86400000).toISOString();
    const r2 = await a.client.rpc('mint_gems', { p_idempotency_key: randomUUID(), p_amount: 5, p_occurred_at: sixtyDaysAgo });
    ok(r2.error && /invalid_occurred_at/.test(r2.error.message), 'occurred_at 60 days back rejected');
  }

  console.log('\n15. EXECUTE on every RPC as anon / PUBLIC');
  {
    const anonClient = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });
    const r1 = await anonClient.rpc('purchase_shop_item', { p_item_id: 'frame_ardoise', p_idempotency_key: randomUUID() });
    const r2 = await anonClient.rpc('mint_gems', { p_idempotency_key: randomUUID(), p_amount: 5, p_occurred_at: new Date().toISOString() });
    const r3 = await anonClient.rpc('equip_cosmetic', { p_slot: 'frame', p_item_id: 'frame_ardoise' });
    const r4 = await anonClient.rpc('consume_item', { p_item_id: 'streak_freeze', p_idempotency_key: randomUUID() });
    // All four should fail — either PostgREST permission denial (no anon EXECUTE
    // grant) or the function's own not_authenticated guard if the grant somehow
    // allowed the call through with no JWT.
    ok(!!r1.error, 'anon purchase_shop_item denied');
    ok(!!r2.error, 'anon mint_gems denied');
    ok(!!r3.error, 'anon equip_cosmetic denied');
    ok(!!r4.error, 'anon consume_item denied');
  }

  console.log('\n16. \\df+ on all four RPCs — owner postgres, search_path set');
  {
    const out = execSync(
      `docker exec supabase_db_French_2.0 psql -U postgres -d postgres -t -A -F"," -c "SELECT p.proname, pg_get_userbyid(p.proowner), p.prosecdef, p.proconfig FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname IN ('mint_gems','purchase_shop_item','equip_cosmetic','consume_item')"`,
      { encoding: 'utf8' }
    ).trim().split('\n');
    ok(out.length === 4, 'all four functions found');
    ok(out.every(l => l.includes(',postgres,t,')), 'every function owned by postgres with security_definer=true');
    ok(out.every(l => l.includes('search_path=pg_catalog, public, pg_temp')), 'every function has search_path set');
  }

  console.log('\n17. Post-migration smoke: privacy toggle, username rename, migration_version write');
  {
    const { error: privacyErr } = await a.client.from('profiles').update({ leaderboard_visibility: 'friends' }).eq('id', a.userId);
    ok(!privacyErr, `privacy toggle still succeeds${privacyErr ? ` (error: ${privacyErr.message})` : ''}`);

    const newUsername = `renamed_${randomUUID().slice(0, 8)}`;
    const { error: renameErr } = await a.client.rpc('rename_username', { new_username: newUsername });
    ok(!renameErr, `username rename still succeeds${renameErr ? ` (error: ${renameErr.message})` : ''}`);

    const { error: migErr } = await a.client.from('profiles').update({ migration_version: 2 }).eq('id', a.userId);
    ok(!migErr, `migration_version write still succeeds${migErr ? ` (error: ${migErr.message})` : ''}`);
  }

  console.log(`\n${pass} passed, ${fail} failed`);
  if (fail > 0) {
    console.log('Failures:', failures.join('; '));
    process.exit(1);
  }
}

main().catch(err => {
  console.error('Test run crashed:', err);
  process.exit(1);
});
