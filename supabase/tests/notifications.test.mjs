// Phase 4.3 — streak-at-risk / daily-goal notifications DB integration tests.
// Run against the LOCAL Supabase stack only (`npx supabase start` from
// backend/), never the hosted project.
//
// Proves: (1) two concurrent claim-upserts against the same
// (user_id, notif_type, sent_on) return exactly one row across both; (2) the
// operator-precedence fix — a notifications_log row already present for
// streak_at_risk today excludes that user from THAT type's candidates, not
// both types (the regression this migration's WHERE grouping fixes);
// (3) an invalid IANA timezone string is rejected by the
// push_subscriptions_validate_timezone trigger at insert time.
//
// Usage: node backend/supabase/tests/notifications.test.mjs
// Requires: local stack up (npx supabase start), reads keys from
// `npx supabase status -o json` at run time so nothing is hardcoded here.

import { createClient } from '@supabase/supabase-js';
import { execSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';

const status = JSON.parse(
  execSync('npx supabase status -o json', { cwd: new URL('../..', import.meta.url), encoding: 'utf8' })
);

const API_URL = status.API_URL;
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
  const email = `notif-${tag}-${randomUUID()}@example.test`;
  const password = 'test-password-12345';
  const { data, error } = await admin.auth.admin.createUser({ email, password, email_confirm: true });
  if (error) throw new Error(`createUser(${tag}) failed: ${error.message}`);
  const userId = data.user.id;

  const { error: profileErr } = await admin.from('profiles').upsert({
    id: userId,
    username: `notif_${tag}_${userId.slice(0, 8)}`,
  });
  if (profileErr) throw new Error(`profile upsert(${tag}) failed: ${profileErr.message}`);

  return { userId, email };
}

async function claimSend(userId, notifType, sentOn) {
  return admin
    .from('notifications_log')
    .upsert({ user_id: userId, notif_type: notifType, sent_on: sentOn }, { onConflict: 'user_id,notif_type,sent_on', ignoreDuplicates: true })
    .select();
}

async function main() {
  console.log('Creating test users...');
  const a = await createTestUser('a');

  console.log('\n1. two concurrent claim-upserts against the same (user_id, notif_type, sent_on) — exactly one returns a row');
  {
    const sentOn = '2026-09-13';
    const [r1, r2] = await Promise.all([
      claimSend(a.userId, 'streak_at_risk', sentOn),
      claimSend(a.userId, 'streak_at_risk', sentOn),
    ]);
    ok(!r1.error && !r2.error, `both upserts succeeded without error${r1.error || r2.error ? ` (${(r1.error || r2.error).message})` : ''}`);
    const totalRowsReturned = (r1.data?.length ?? 0) + (r2.data?.length ?? 0);
    ok(totalRowsReturned === 1, `exactly one of the two concurrent claims returned a row (got ${totalRowsReturned})`);

    const { data: logRows } = await admin.from('notifications_log').select('*').eq('user_id', a.userId).eq('notif_type', 'streak_at_risk').eq('sent_on', sentOn);
    ok((logRows?.length ?? 0) === 1, `exactly one notifications_log row exists for this user/type/day (got ${logRows?.length})`);
  }

  console.log('\n2. operator-precedence regression: a notifications_log row for daily_goal today excludes that user from get_notification_candidates(\'daily_goal\')');
  {
    const b = await createTestUser('b');
    await admin.from('push_subscriptions').insert({
      user_id: b.userId,
      endpoint: `https://example.test/push/${randomUUID()}`,
      p256dh: 'test-p256dh',
      auth_key: 'test-auth',
      timezone: 'UTC',
    });
    // Make B a plausible daily_goal candidate: notify_daily_goal on (the
    // default) and no sessions today, so today_count (0) < daily_goal.
    await admin.from('profiles').update({ daily_goal: 3 }).eq('id', b.userId);

    const todayUtc = new Date().toISOString().slice(0, 10);
    await admin.from('notifications_log').insert({ user_id: b.userId, notif_type: 'daily_goal', sent_on: todayUtc });

    const { data: candidates, error } = await admin.rpc('get_notification_candidates', { p_notif_type: 'daily_goal' });
    ok(!error, `get_notification_candidates('daily_goal') succeeds${error ? ` (${error.message})` : ''}`);
    const stillCandidate = (candidates ?? []).some((c) => c.user_id === b.userId);
    // This is the regression the WHERE-grouping fix (correction #1) closes:
    // under the old ungrouped `WHERE (streak-cond) OR (goal-cond AND NOT
    // EXISTS(...))`, AND's tighter precedence would apply the idempotency
    // check to the goal branch correctly here (goal-cond is on the right of
    // the AND) — the bug actually manifested for streak_at_risk (the LEFT
    // branch), which the ungrouped form left with no idempotency check at
    // all. Assert the general shape holds for whichever type is queried.
    ok(!stillCandidate, `user with a daily_goal notifications_log row today is excluded from today's daily_goal candidates`);
  }

  console.log('\n2b. operator-precedence regression: a notifications_log row for streak_at_risk today excludes that user from get_notification_candidates(\'streak_at_risk\')');
  {
    const b2 = await createTestUser('b2');
    await admin.from('push_subscriptions').insert({
      user_id: b2.userId,
      endpoint: `https://example.test/push/${randomUUID()}`,
      p256dh: 'test-p256dh',
      auth_key: 'test-auth',
      timezone: 'UTC',
    });
    // Seed two distinct recent-day sessions plus one 20-32h-old session so
    // this user would otherwise be a plausible streak_at_risk candidate.
    const now = Date.now();
    await admin.from('sessions').insert([
      { user_id: b2.userId, mode: 'practice', created_at: new Date(now - 26 * 3_600_000).toISOString() },
      { user_id: b2.userId, mode: 'practice', created_at: new Date(now - 26 * 3_600_000).toISOString() },
      { user_id: b2.userId, mode: 'practice', created_at: new Date(now - 50 * 3_600_000).toISOString() },
    ]);

    const todayUtc = new Date().toISOString().slice(0, 10);
    await admin.from('notifications_log').insert({ user_id: b2.userId, notif_type: 'streak_at_risk', sent_on: todayUtc });

    const { data: candidates, error } = await admin.rpc('get_notification_candidates', { p_notif_type: 'streak_at_risk' });
    ok(!error, `get_notification_candidates('streak_at_risk') succeeds${error ? ` (${error.message})` : ''}`);
    const stillCandidate = (candidates ?? []).some((c) => c.user_id === b2.userId);
    ok(!stillCandidate, `user with a streak_at_risk notifications_log row today is excluded from today's streak_at_risk candidates (the original bug: this branch had no idempotency check under the ungrouped WHERE)`);
  }

  console.log('\n3. invalid IANA timezone is rejected by the push_subscriptions_validate_timezone trigger');
  {
    const c = await createTestUser('c');
    const { error } = await admin.from('push_subscriptions').insert({
      user_id: c.userId,
      endpoint: `https://example.test/push/${randomUUID()}`,
      p256dh: 'test-p256dh',
      auth_key: 'test-auth',
      timezone: 'Not/A_Real_Zone',
    });
    ok(!!error && /invalid_timezone/i.test(error.message), `insert with an invalid timezone is rejected${error ? '' : ' (expected invalid_timezone error)'}`);
  }

  console.log('\n4. a valid IANA timezone is accepted');
  {
    const d = await createTestUser('d');
    const { error } = await admin.from('push_subscriptions').insert({
      user_id: d.userId,
      endpoint: `https://example.test/push/${randomUUID()}`,
      p256dh: 'test-p256dh',
      auth_key: 'test-auth',
      timezone: 'Europe/London',
    });
    ok(!error, `insert with a valid timezone succeeds${error ? ` (${error.message})` : ''}`);
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
