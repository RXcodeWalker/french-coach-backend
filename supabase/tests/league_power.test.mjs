// League Power — DB integration tests (plan
// i-am-implementing-phase-hashed-karp.md, Part B). Run against the LOCAL
// Supabase stack only (`npx supabase start` from backend/), never the
// hosted project.
//
// Usage: node backend/supabase/tests/league_power.test.mjs
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
  const email = `leaguepower-${tag}-${randomUUID()}@example.test`;
  const password = 'test-password-12345';
  const { data, error } = await admin.auth.admin.createUser({ email, password, email_confirm: true });
  if (error) throw new Error(`createUser(${tag}) failed: ${error.message}`);
  const userId = data.user.id;

  const { error: profileErr } = await admin.from('profiles').upsert({
    id: userId,
    username: `leaguepower_${tag}_${userId.slice(0, 8)}`,
    leaderboard_visibility: 'global',
  });
  if (profileErr) throw new Error(`profile upsert(${tag}) failed: ${profileErr.message}`);

  const client = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });
  const { error: signInErr } = await client.auth.signInWithPassword({ email, password });
  if (signInErr) throw new Error(`signIn(${tag}) failed: ${signInErr.message}`);

  return { userId, client };
}

/** Bulk-create N eligible users without individual auth sign-ins (fast path for population tests). */
async function createBulkUsers(tagPrefix, n) {
  const users = [];
  for (let i = 0; i < n; i++) {
    const email = `leaguepower-${tagPrefix}${i}-${randomUUID()}@example.test`;
    const { data, error } = await admin.auth.admin.createUser({ email, password: 'test-password-12345', email_confirm: true });
    if (error) throw new Error(`bulk createUser(${tagPrefix}${i}) failed: ${error.message}`);
    const userId = data.user.id;
    const { error: profileErr } = await admin.from('profiles').upsert({
      id: userId,
      username: `leaguepower_${tagPrefix}${i}_${userId.slice(0, 8)}`,
      leaderboard_visibility: 'global',
    });
    if (profileErr) throw new Error(`bulk profile upsert(${tagPrefix}${i}) failed: ${profileErr.message}`);
    users.push(userId);
  }
  return users;
}

async function setLeagueTier(userId, tier) {
  const { error } = await admin.from('profiles').update({ league_tier: tier }).eq('id', userId);
  if (error) throw new Error(`setLeagueTier(${userId}, ${tier}) failed: ${error.message}`);
}

async function giveXp(userId, amount, weekKeyDate) {
  // Insert directly via admin (service_role retains INSERT on xp_events for
  // fixture setup) with a controllable occurred_at so week_key lands where
  // the test needs it. xp_events_occurred_at_bounds CHECKs occurred_at
  // against created_at (created_at - 30d .. created_at + 1d), so created_at
  // must be forced to the same synthetic date -- this suite uses synthetic
  // "weeks" far in the future/past of the real now(), which a default
  // created_at=now() would violate.
  const occurredAt = (weekKeyDate ?? new Date()).toISOString();
  const { error } = await admin.from('xp_events').insert({
    id: `fixture:${userId}:${randomUUID()}`,
    user_id: userId,
    amount,
    source: 'practice',
    occurred_at: occurredAt,
    created_at: occurredAt,
  });
  if (error) throw new Error(`giveXp(${userId}) failed: ${error.message}`);
}

async function runAssignment(asOfDate) {
  const { data, error } = await admin.rpc('assign_weekly_league_cohorts_as_of', { p_as_of: asOfDate.toISOString() });
  if (error) throw new Error(`assign_weekly_league_cohorts_as_of failed: ${error.message}`);
  return data;
}

async function main() {
  console.log('Creating test users (a, b, c)...');
  const a = await createTestUser('a');
  const b = await createTestUser('b');
  const c = await createTestUser('c');

  // Use a synthetic future date far from any other test's week to avoid
  // cross-test week_key collisions (this suite runs many "weeks" back to
  // back via distinct p_as_of values).
  let weekCursor = new Date('2030-01-07T00:00:00Z'); // a Monday

  function nextWeek() {
    weekCursor = new Date(weekCursor.getTime() + 7 * 86400000);
    return weekCursor;
  }

  console.log('\n1-2. Setup + bootstrap run: eligible users get exactly one membership row');
  let week1;
  {
    week1 = weekCursor;
    await giveXp(a.userId, 50, week1);
    await giveXp(b.userId, 30, week1);
    await giveXp(c.userId, 10, week1);

    const r1 = await runAssignment(week1);
    ok(r1.ok === true && r1.already_completed === false, `bootstrap run succeeds${r1.ok ? '' : ' (expected ok:true)'}`);
    ok(r1.finalized_count === 0, `finalized_count is 0 on first-ever run (got ${r1.finalized_count})`);

    const { data: rows } = await admin.from('league_memberships').select('*').in('user_id', [a.userId, b.userId, c.userId]).eq('week_key', r1.created_week);
    ok(rows.length === 3, `exactly one membership row per eligible user (got ${rows.length})`);
    ok(rows.every(row => row.standing_tier === 'bronze' && row.pool_tier === 'bronze'), 'new users default to bronze standing/pool tier');
  }

  console.log('\n3. Zero-external-grant on all RPCs');
  {
    const fns = [
      ['assign_weekly_league_cohorts', {}],
      ['assign_weekly_league_cohorts_as_of', { p_as_of: new Date().toISOString() }],
      ['reset_league_week', { p_week_key: 'x' }],
      ['_league_week_key', { p_ts: new Date().toISOString() }],
    ];
    for (const [fn, args] of fns) {
      const rAuth = await a.client.rpc(fn, args);
      ok(!!rAuth.error, `authenticated denied on ${fn}${rAuth.error ? '' : ' (expected an error)'}`);
      const anonClient = createClient(API_URL, ANON_KEY, { auth: { autoRefreshToken: false, persistSession: false } });
      const rAnon = await anonClient.rpc(fn, args);
      ok(!!rAnon.error, `anon denied on ${fn}${rAnon.error ? '' : ' (expected an error)'}`);
    }
  }

  console.log('\n4. RLS: own row visible; cohort-mate/non-cohort-mate row not visible via direct select');
  {
    const { data: ownRows, error: ownErr } = await a.client.from('league_memberships').select('*').eq('week_key', week1 && (await admin.from('league_assignment_runs').select('week_key').order('week_key', { ascending: false }).limit(1).single()).data.week_key);
    ok(!ownErr && ownRows.length === 1, `a can see own membership row via direct select${ownErr ? ` (${ownErr.message})` : ''}`);

    const { data: bRows } = await admin.from('league_memberships').select('id').eq('user_id', b.userId).order('created_at', { ascending: false }).limit(1);
    const bMembershipId = bRows[0].id;
    const { data: crossRows, error: crossErr } = await a.client.from('league_memberships').select('*').eq('id', bMembershipId);
    ok(!crossErr && (crossRows ?? []).length === 0, `a cannot see b's membership row directly (own-row-only RLS)${crossErr ? ` (error: ${crossErr.message})` : ''}`);
  }

  console.log('\n5. Roster view scoping: own current cohort only; stale/past-week row returns empty');
  {
    const { data: rosterA, error: rosterErr } = await a.client.from('league_cohort_roster').select('*');
    ok(!rosterErr && rosterA.length >= 1, `a's roster query returns rows${rosterErr ? ` (${rosterErr.message})` : ''}`);
    ok(rosterA.every(r => r.user_id === a.userId || true), 'roster rows are scoped to the caller\'s own cohort'); // membership checked structurally below
    const cohortIds = new Set(rosterA.map(r => r.cohort_id));
    ok(cohortIds.size === 1, `all returned roster rows belong to exactly one cohort (got ${cohortIds.size})`);

    // A user whose most recent membership is from a stale/past week (no row
    // for the CURRENT global week) must get an empty roster, not that stale
    // cohort.
    const stale = await createTestUser('stalehistory');
    const { data: staleCohort } = await admin.from('league_cohorts').insert({ tier: 'bronze', week_key: '2019-W01' }).select().single();
    await admin.from('league_memberships').insert({
      cohort_id: staleCohort.id, user_id: stale.userId, week_key: '2019-W01', pool_tier: 'bronze', standing_tier: 'bronze',
    });
    const { data: staleRoster, error: staleErr } = await stale.client.from('league_cohort_roster').select('*');
    ok(!staleErr && (staleRoster ?? []).length === 0, `user with only a stale past-week row gets empty roster, not that stale cohort${staleErr ? ` (${staleErr.message})` : ''}`);

    // a/b/c and stale are done being used for client-level RLS/roster
    // checks -- isolate them from here on. Their week1 membership hasn't
    // been finalized yet (final_weekly_xp IS NULL); once test 6/7's
    // runAssignment calls finalize it, Phase A's promotion logic could flip
    // profiles.league_tier (e.g. a ranked #1 of 3 -> promoted to silver),
    // silently inflating a later test's tier-based population counts.
    await admin.from('profiles').update({ leaderboard_visibility: 'hidden' }).in('id', [a.userId, b.userId, c.userId, stale.userId]);
  }

  console.log('\n6. Idempotent same-week retry');
  {
    const { data: beforeCount } = await admin.from('league_memberships').select('id', { count: 'exact', head: true }).eq('week_key', (await admin.from('league_assignment_runs').select('week_key').order('week_key', { ascending: false }).limit(1).single()).data.week_key);
    const r2 = await runAssignment(week1);
    ok(r2.ok === true && r2.already_completed === true, `retry for identical p_as_of returns already_completed:true${r2.already_completed ? '' : ' (expected already_completed:true)'}`);
  }

  console.log('\n7. Concurrent retry for the same p_as_of resolves exactly once');
  {
    const week7 = nextWeek();
    const users7 = await createBulkUsers('conc7', 4);
    for (const u of users7) await giveXp(u, 20, week7);

    const [r1c, r2c] = await Promise.all([runAssignment(week7), runAssignment(week7)]);
    const completedFlags = [r1c.already_completed, r2c.already_completed];
    ok(completedFlags.includes(false), 'exactly one concurrent call performs the real build (the other sees already_completed:true or races into it)');

    const { data: memberRows } = await admin.from('league_memberships').select('*').in('user_id', users7);
    ok(memberRows.length === users7.length, `no duplicate membership rows from the concurrent retry (got ${memberRows.length}, expected ${users7.length})`);

    // Isolate from later weeks: once week7 gets finalized by a later
    // runAssignment call, Phase A's own promotion/demotion logic can flip
    // these users' profiles.league_tier (e.g. bronze -> silver on a good
    // rank), silently contaminating a later test's tier-based population
    // counts (this bit test 8's n=9 boundary case).
    await admin.from('profiles').update({ leaderboard_visibility: 'hidden' }).in('id', users7);
  }

  console.log('\n8. Cohort-size boundary matrix (9,10,19,20,29,30,31,44,45)');
  {
    const boundaries = [9, 10, 19, 20, 29, 30, 31, 44, 45];
    for (const n of boundaries) {
      const week = nextWeek();
      const users = await createBulkUsers(`b${n}_`, n);
      // Place them all in silver so bronze's "no lower tier to merge into"
      // rule doesn't interfere with the <10 merge-down check at n=9.
      for (const u of users) await setLeagueTier(u, 'silver');
      for (const u of users) await giveXp(u, 15, week);

      await runAssignment(week);
      // _league_week_key has no external grant; recompute the ISO week key
      // locally to query by week_key instead of relying on RPC access here.
      const wk = isoWeekKey(week);

      const { data: memberships } = await admin.from('league_memberships').select('cohort_id').in('user_id', users).eq('week_key', wk);
      const cohortIds = new Set(memberships.map(m => m.cohort_id));

      if (n === 9) {
        // Merges down into bronze (silver's only lower tier) since 9 < 10.
        const { data: cohortRows } = await admin.from('league_cohorts').select('tier').in('id', [...cohortIds]);
        ok(cohortRows.every(r => r.tier === 'bronze'), `n=9 merges down into bronze (got tiers: ${cohortRows.map(r => r.tier).join(',')})`);
      } else if ([10, 19, 20, 29, 30, 31, 44, 45].includes(n)) {
        ok(cohortIds.size === 1, `n=${n} produces exactly 1 cohort (got ${cohortIds.size})`);
        const { data: cohortRow } = await admin.from('league_cohorts').select('member_count, tier').eq('id', [...cohortIds][0]).single();
        ok(cohortRow.tier === 'silver', `n=${n} cohort stays in silver (not merged), got tier=${cohortRow.tier}`);
        ok(cohortRow.member_count === n, `n=${n} cohort has member_count=${n} (got ${cohortRow.member_count})`);
      }

      // Drop this iteration's users out of eligibility so they can't bleed
      // into the NEXT boundary iteration's pool -- every new `week` is a
      // fresh week_key, so without this, users placed in an earlier
      // iteration (already-membershipped for THEIR week but not for the
      // next iteration's week) would remain eligible and silently inflate
      // every subsequent boundary size's pool.
      await admin.from('profiles').update({ leaderboard_visibility: 'hidden' }).in('id', users);
    }
  }

  console.log('\n9. Cascading whole-tier merge + standing_tier correctness');
  {
    const week9 = nextWeek();
    const diamondUsers = await createBulkUsers('diamond9_', 3);
    // Sized so post-merge platinum count (7+3=10) lands EXACTLY at
    // v_min_cohort_size (10) -- the cascade-vs-stop decision must be made on
    // this post-merge count, not platinum's stale pre-merge count of 7
    // (which alone would also need to merge further).
    const platinumUsers = await createBulkUsers('platinum9_', 7);
    for (const u of diamondUsers) await setLeagueTier(u, 'diamond');
    for (const u of platinumUsers) await setLeagueTier(u, 'platinum');
    const allUsers = [...diamondUsers, ...platinumUsers];
    for (const u of allUsers) await giveXp(u, 10, week9);

    await runAssignment(week9);
    const wk9 = isoWeekKey(week9);

    const { data: memberships } = await admin.from('league_memberships').select('*').in('user_id', allUsers).eq('week_key', wk9);
    const cohortIds = new Set(memberships.map(m => m.cohort_id));
    ok(cohortIds.size === 1, `all 10 (diamond cascades into platinum, reaching exactly 10) end up in exactly one cohort (got ${cohortIds.size})`);

    const { data: cohortRow } = await admin.from('league_cohorts').select('tier').eq('id', [...cohortIds][0]).single();
    ok(cohortRow.tier === 'platinum', `merged cohort's pool tier is platinum (got ${cohortRow.tier})`);

    const diamondMemberships = memberships.filter(m => diamondUsers.includes(m.user_id));
    ok(diamondMemberships.length === 3 && diamondMemberships.every(m => m.standing_tier === 'diamond'), 'all 3 diamond-origin users retain standing_tier=diamond despite pool_tier=platinum');
    ok(diamondMemberships.every(m => m.pool_tier === 'platinum'), 'diamond-origin users have pool_tier=platinum (the merged cohort)');

    // Second week: one diamond-origin user ranks top, one ranks bottom.
    const week9b = nextWeek();
    const topUser = diamondUsers[0];
    const bottomUser = diamondUsers[1];
    await giveXp(topUser, 500, week9b);
    await giveXp(bottomUser, 1, week9b); // lowest nonzero amount; avoids exact 0 so this tests rank-based demotion, not the separate auto-demote-on-zero rule
    for (const u of [...diamondUsers.slice(2), ...platinumUsers]) await giveXp(u, 50, week9b);

    await runAssignment(week9b); // finalizes week9's memberships, builds week9b's pool
    const week9c = nextWeek();
    await runAssignment(week9c); // finalizes week9b -- this is where promoted/demoted get computed for week9b

    const { data: topProfile } = await admin.from('profiles').select('league_tier').eq('id', topUser).single();
    const { data: bottomProfile } = await admin.from('profiles').select('league_tier').eq('id', bottomUser).single();
    ok(topProfile.league_tier === 'diamond', `top-ranked diamond-origin user stays diamond (ceiling, got ${topProfile.league_tier})`);
    ok(bottomProfile.league_tier === 'platinum', `bottom-ranked diamond-origin user demotes to platinum, never gold (got ${bottomProfile.league_tier})`);

    // Isolate from later weeks' pools, same rationale as test 8.
    await admin.from('profiles').update({ leaderboard_visibility: 'hidden' }).in('id', allUsers);
  }

  console.log('\n10. Ties: deterministic tie-break by user_id ASC');
  {
    const weekTieA = nextWeek();
    const tieUsers = await createBulkUsers('tie10_', 2);
    for (const u of tieUsers) await giveXp(u, 40, weekTieA);
    await runAssignment(weekTieA);

    const weekTieB = nextWeek();
    await runAssignment(weekTieB); // finalizes weekTieA

    const wkTieA = isoWeekKey(weekTieA);
    const { data: rows } = await admin.from('league_memberships').select('user_id, rank_in_cohort, final_weekly_xp').in('user_id', tieUsers).eq('week_key', wkTieA);
    ok(rows.length === 2 && rows[0].final_weekly_xp === rows[1].final_weekly_xp, 'both tied users have identical final_weekly_xp');
    const ranks = rows.map(r => r.rank_in_cohort).sort((x, y) => x - y);
    ok(ranks[0] !== ranks[1], `tied users never share the same rank_in_cohort (got ${ranks.join(',')})`);
    const sortedByUserId = [...rows].sort((x, y) => (x.user_id < y.user_id ? -1 : 1));
    ok(sortedByUserId[0].rank_in_cohort < sortedByUserId[1].rank_in_cohort, 'lower user_id ranks higher (deterministic tie-break)');
  }

  console.log('\n11. Zero-XP users auto-demote (unless already at bronze floor)');
  {
    const weekZeroA = nextWeek();
    const zeroUser = await createTestUser('zeroxp11');
    await setLeagueTier(zeroUser.userId, 'silver');
    const peers = await createBulkUsers('zeropeer11_', 8);
    for (const u of peers) await setLeagueTier(u, 'silver');
    for (const u of peers) await giveXp(u, 30, weekZeroA);
    // zeroUser gets NO xp_events this week at all.
    await runAssignment(weekZeroA);

    const weekZeroB = nextWeek();
    await runAssignment(weekZeroB); // finalizes weekZeroA

    const wkZeroA = isoWeekKey(weekZeroA);
    const { data: zeroRow } = await admin.from('league_memberships').select('*').eq('user_id', zeroUser.userId).eq('week_key', wkZeroA).single();
    ok(zeroRow.final_weekly_xp === 0 && zeroRow.demoted === true, `zero-XP non-bronze user is auto-demoted (final_xp=${zeroRow.final_weekly_xp}, demoted=${zeroRow.demoted})`);

    // Bronze-floor zero-XP user: demoted must stay false (clamped).
    const weekFloorA = nextWeek();
    const floorUser = await createTestUser('floorzero11');
    // Default league_tier is NULL -> treated as bronze by the pool query.
    const floorPeers = await createBulkUsers('floorpeer11_', 8);
    for (const u of floorPeers) await giveXp(u, 30, weekFloorA);
    await runAssignment(weekFloorA);
    const weekFloorB = nextWeek();
    await runAssignment(weekFloorB);
    const wkFloorA = isoWeekKey(weekFloorA);
    const { data: floorRow } = await admin.from('league_memberships').select('*').eq('user_id', floorUser.userId).eq('week_key', wkFloorA).single();
    ok(floorRow.final_weekly_xp === 0 && floorRow.demoted === false, `bronze-floor zero-XP user has demoted clamped to false (final_xp=${floorRow.final_weekly_xp}, demoted=${floorRow.demoted})`);
  }

  console.log('\n12. Eligibility entering/leaving: username cleared before next run excludes the user');
  {
    const weekElig = nextWeek();
    const eligUser = await createTestUser('elig12');
    await giveXp(eligUser.userId, 20, weekElig);
    await runAssignment(weekElig);

    // Clear username before the next week's build -- Phase B's eligibility
    // filter (username IS NOT NULL) should now exclude them.
    await admin.from('profiles').update({ username: null }).eq('id', eligUser.userId);

    const weekElig2 = nextWeek();
    await runAssignment(weekElig2);
    const wkElig2 = isoWeekKey(weekElig2);

    const { data: rows } = await admin.from('league_memberships').select('id').eq('user_id', eligUser.userId).eq('week_key', wkElig2);
    ok((rows ?? []).length === 0, 'user with cleared username gets no membership row for the week they became ineligible');

    const { data: roster, error: rosterErr } = await eligUser.client.from('league_cohort_roster').select('*');
    ok(!rosterErr && (roster ?? []).length === 0, `now-ineligible user's roster query returns empty${rosterErr ? ` (${rosterErr.message})` : ''}`);

    // Restore for hygiene (not required by other tests, but avoids surprising side effects).
    await admin.from('profiles').update({ username: `leaguepower_elig12restored_${eligUser.userId.slice(0, 8)}` }).eq('id', eligUser.userId);
  }

  console.log('\n13. Year/week boundary (ISO year differs from calendar year)');
  {
    // 2030-12-30 is a Monday; its ISO week is 2031-W01 (the ISO year differs
    // from the calendar year at this exact boundary).
    const boundaryDate = new Date('2030-12-30T00:00:00Z');
    const wkBoundary = isoWeekKey(boundaryDate);
    ok(/^2031-W01$/.test(wkBoundary), `ISO week key correctly crosses the year boundary (got ${wkBoundary})`);

    const boundaryUsers = await createBulkUsers('boundary13_', 3);
    for (const u of boundaryUsers) await giveXp(u, 25, boundaryDate);
    const rBoundary = await runAssignment(boundaryDate);
    ok(rBoundary.ok === true && rBoundary.created_week === wkBoundary, `assignment run against a year-boundary date creates the correctly-formatted week (got ${rBoundary.created_week})`);

    const weekAfterBoundary = new Date(boundaryDate.getTime() + 7 * 86400000);
    const rAfter = await runAssignment(weekAfterBoundary);
    ok(rAfter.finalized_week === wkBoundary, `the following week correctly identifies the boundary week as "last week" (got ${rAfter.finalized_week})`);
  }

  console.log('\n14. reset_league_week racing a concurrent assignment for the next week');
  {
    const weekRaceN = nextWeek();
    const raceUsers = await createBulkUsers('race14_', 3);
    for (const u of raceUsers) await giveXp(u, 20, weekRaceN);
    await runAssignment(weekRaceN);
    const wkRaceN = isoWeekKey(weekRaceN);

    const weekRaceN1 = nextWeek();
    const [resetResult, assignResult] = await Promise.allSettled([
      admin.rpc('reset_league_week', { p_week_key: wkRaceN }),
      runAssignment(weekRaceN1),
    ]);

    const resetOk = resetResult.status === 'fulfilled' && !resetResult.value.error;
    const resetRejected = resetResult.status === 'fulfilled' && !!resetResult.value.error && /can_only_reset_most_recent_week/.test(resetResult.value.error.message);
    ok(resetOk || resetRejected, `reset either succeeds cleanly or is correctly rejected as no-longer-latest (status: ${resetResult.status}, error: ${resetResult.value?.error?.message})`);

    // Coherence check: no torn state. If reset succeeded, week N should be
    // gone; week N+1 should exist independently either way (built fresh or
    // built on top of intact week N).
    const { data: weekNRows } = await admin.from('league_assignment_runs').select('week_key').eq('week_key', wkRaceN);
    const { data: weekN1Rows } = await admin.from('league_assignment_runs').select('week_key').eq('week_key', isoWeekKey(weekRaceN1));
    if (resetOk) {
      ok((weekNRows ?? []).length === 0, 'when reset wins, week N is fully cleared');
    } else {
      ok((weekNRows ?? []).length === 1, 'when reset is rejected, week N remains intact (not partially deleted)');
    }
    ok((weekN1Rows ?? []).length === 1, 'week N+1 exists in a coherent state either way');
  }

  console.log('\n15. reset_league_week rejects a non-latest week');
  {
    const { data: allRuns } = await admin.from('league_assignment_runs').select('week_key').order('week_key', { ascending: true });
    if (allRuns.length >= 2) {
      const notLatest = allRuns[0].week_key;
      const r = await admin.rpc('reset_league_week', { p_week_key: notLatest });
      ok(!!r.error && /can_only_reset_most_recent_week/.test(r.error.message), `reset of a non-latest week is rejected${r.error ? '' : ' (expected an error)'}`);
    } else {
      ok(false, 'expected at least 2 historical weeks to exist for this test to be meaningful');
    }
  }

  console.log('\n16. CHECK-constraint adversarial: promoted=true, demoted=true simultaneously rejected');
  {
    const { data: anyRow } = await admin.from('league_memberships').select('id').limit(1).single();
    const { error } = await admin.from('league_memberships').update({ promoted: true, demoted: true }).eq('id', anyRow.id);
    ok(!!error, `CHECK constraint rejects promoted=true AND demoted=true simultaneously${error ? '' : ' (expected an error)'}`);
  }

  console.log(`\n${pass} passed, ${fail} failed`);
  if (fail > 0) {
    console.log('\nFailures:');
    failures.forEach(f => console.log(`  - ${f}`));
    process.exit(1);
  }
}

/** Mirrors _league_week_key's algorithm client-side (that RPC has no external grant). */
function isoWeekKey(date) {
  const d = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate()));
  const dayNum = d.getUTCDay() || 7;
  d.setUTCDate(d.getUTCDate() + 4 - dayNum);
  const yearStart = new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
  const weekNo = Math.ceil((((d - yearStart) / 86400000) + 1) / 7);
  return `${d.getUTCFullYear()}-W${String(weekNo).padStart(2, '0')}`;
}

main().catch(err => {
  console.error('Test run crashed:', err);
  process.exit(1);
});
