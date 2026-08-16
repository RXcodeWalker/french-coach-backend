/*
  # League Power — weekly assignment RPC (revised plan
  i-am-implementing-phase-hashed-karp.md, Part B3)

  Three fixes vs. the prior draft, all present in this version:

  1. Tier-merge loop ordering: iterates array INDICES in reverse
     (REVERSE array_length(v_tier_order,1)..2), not ORDER BY tier-text DESC
     (which would sort 'bronze'..'diamond' alphabetically-descending, the
     opposite of rank order). This visits Diamond, Platinum, Gold, Silver in
     that order -- Bronze (index 1) is never a merge source.
  2. Cohort-count formula: GREATEST(1, CEIL(n/45)), not
     GREATEST(1, ROUND(n/30)) -- gives a smooth size range (~15-45 per
     cohort) instead of an arbitrary split at n=45 under the old ROUND rule.
  3. standing_tier vs. pool_tier: promotion/demotion math
     (array_position/clamping) keys off standing_tier (captured before any
     merge-down), never pool_tier (the merged/effective cohort label) -- see
     B1's header for the full "silently skips a tier" bug this fixes.
  4. Concurrency: a GLOBAL advisory lock
     (pg_advisory_xact_lock(hashtext('league_assignment_global'))), shared
     with reset_league_week (B4), makes assignment and reset fully mutually
     exclusive system-wide. Low-frequency operations (weekly cron, rare
     manual admin action) -- contention is a non-issue.

  Implementer note: the nested DECLARE...BEGIN...END blocks inside the two
  FOR...LOOP constructs and the REVERSE range-iteration syntax are standard
  plpgsql but must be verified against this project's actual local Postgres
  version via `npx supabase db reset` before treating this as final.
*/

CREATE OR REPLACE FUNCTION public._league_week_key(p_ts timestamptz)
RETURNS text
LANGUAGE sql IMMUTABLE
AS $$
  SELECT extract(isoyear FROM (p_ts AT TIME ZONE 'UTC'))::int::text
    || '-W' ||
    lpad(extract(week FROM (p_ts AT TIME ZONE 'UTC'))::int::text, 2, '0');
$$;
REVOKE EXECUTE ON FUNCTION public._league_week_key(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public._league_week_key(timestamptz) TO service_role;

CREATE OR REPLACE FUNCTION public.assign_weekly_league_cohorts_as_of(p_as_of timestamptz)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_this_week text := public._league_week_key(p_as_of);
  v_last_week text := public._league_week_key(p_as_of - interval '7 days');
  v_completed timestamptz;
  v_finalized_count int := 0;
  v_tier_order text[] := ARRAY['bronze','silver','gold','platinum','diamond'];
  v_min_cohort_size int := 10;
  v_max_cohort_size int := 45;
  v_idx int;
  v_tier text;
BEGIN
  -- Global lock: serializes assignment runs against each other AND against
  -- reset_league_week (B4), which takes the same lock.
  PERFORM pg_advisory_xact_lock(hashtext('league_assignment_global'));

  INSERT INTO public.league_assignment_runs (week_key) VALUES (v_this_week) ON CONFLICT DO NOTHING;
  SELECT completed_at INTO v_completed FROM public.league_assignment_runs WHERE week_key = v_this_week;

  IF v_completed IS NOT NULL THEN
    RETURN jsonb_build_object('ok', true, 'already_completed', true, 'week_key', v_this_week);
  END IF;

  -- ===== Phase A: finalize last week's (not-yet-finalized) memberships =====
  CREATE TEMP TABLE _last_week_final AS
  SELECT lm.id, lm.user_id, lm.cohort_id, lm.standing_tier,
         COALESCE((SELECT SUM(e.amount) FROM public.xp_events e
                    WHERE e.user_id = lm.user_id AND e.week_key = v_last_week), 0) AS final_xp
  FROM public.league_memberships lm
  WHERE lm.week_key = v_last_week AND lm.final_weekly_xp IS NULL;

  CREATE TEMP TABLE _last_week_ranked AS
  SELECT *, row_number() OVER (PARTITION BY cohort_id ORDER BY final_xp DESC, user_id ASC) AS rnk,
         count(*) OVER (PARTITION BY cohort_id) AS cohort_size
  FROM _last_week_final;

  -- Promotion/demotion keys off standing_tier (the user's REAL tier entering
  -- the week), not the pooled cohort's tier -- see header comment (fix 3).
  UPDATE public.league_memberships lm SET
    final_weekly_xp = r.final_xp,
    rank_in_cohort = r.rnk,
    promoted = (
      array_position(v_tier_order, r.standing_tier) < array_length(v_tier_order, 1)
      AND r.rnk <= GREATEST(1, ROUND(r.cohort_size * 0.15))
      AND r.final_xp > 0
    ),
    demoted = (
      array_position(v_tier_order, r.standing_tier) > 1
      AND (
        r.rnk > r.cohort_size - GREATEST(1, ROUND(r.cohort_size * 0.15))
        OR r.final_xp = 0
      )
    )
  FROM _last_week_ranked r
  WHERE lm.id = r.id;

  GET DIAGNOSTICS v_finalized_count = ROW_COUNT;

  UPDATE public.profiles p SET league_tier = v_tier_order[
      LEAST(array_length(v_tier_order,1), GREATEST(1, array_position(v_tier_order, lm.standing_tier) + 1))
    ]
  FROM public.league_memberships lm
  WHERE lm.week_key = v_last_week AND lm.user_id = p.id AND lm.promoted = true;

  UPDATE public.profiles p SET league_tier = v_tier_order[
      GREATEST(1, array_position(v_tier_order, lm.standing_tier) - 1)
    ]
  FROM public.league_memberships lm
  WHERE lm.week_key = v_last_week AND lm.user_id = p.id AND lm.demoted = true;

  DROP TABLE _last_week_final; DROP TABLE _last_week_ranked;

  -- ===== Phase B: build this week's cohorts =====
  CREATE TEMP TABLE _league_pool AS
  SELECT p.id AS user_id,
         COALESCE(p.league_tier, 'bronze') AS standing_tier,
         COALESCE(p.league_tier, 'bronze') AS effective_tier -- mutated by the merge step below;
                                                               -- standing_tier is never touched.
  FROM public.profiles p
  WHERE p.username IS NOT NULL AND p.leaderboard_visibility = 'global'
    AND NOT EXISTS (SELECT 1 FROM public.league_memberships lm WHERE lm.week_key = v_this_week AND lm.user_id = p.id);

  -- Whole-tier merge, strictly top-to-bottom by RANK (array index), not text
  -- order (fix 1). Bronze (index 1) is never a merge source.
  FOR v_idx IN REVERSE array_length(v_tier_order, 1)..2 LOOP
    v_tier := v_tier_order[v_idx];
    DECLARE
      v_pool_size int;
    BEGIN
      SELECT count(*) INTO v_pool_size FROM _league_pool WHERE effective_tier = v_tier;
      IF v_pool_size > 0 AND v_pool_size < v_min_cohort_size THEN
        UPDATE _league_pool SET effective_tier = v_tier_order[v_idx - 1] WHERE effective_tier = v_tier;
      END IF;
    END;
  END LOOP;

  FOR v_tier IN SELECT DISTINCT effective_tier FROM _league_pool LOOP
    DECLARE
      v_n int;
      v_num_cohorts int;
      v_new_cohort_ids uuid[];
      v_new_cohort_id uuid;
      i int;
    BEGIN
      SELECT count(*) INTO v_n FROM _league_pool WHERE effective_tier = v_tier;
      v_num_cohorts := GREATEST(1, CEIL(v_n::numeric / v_max_cohort_size));
      v_new_cohort_ids := ARRAY[]::uuid[];
      FOR i IN 1..v_num_cohorts LOOP
        -- INSERT ... RETURNING cannot appear as a value expression inline
        -- (e.g. inside `||`) in plpgsql -- it must be its own statement with
        -- an INTO target, then appended separately.
        INSERT INTO public.league_cohorts (tier, week_key) VALUES (v_tier, v_this_week) RETURNING id INTO v_new_cohort_id;
        v_new_cohort_ids := v_new_cohort_ids || v_new_cohort_id;
      END LOOP;

      WITH banded AS (
        SELECT lp.user_id, lp.standing_tier,
               v_new_cohort_ids[ntile(v_num_cohorts) OVER (
                 ORDER BY md5(lp.user_id::text || v_this_week) ASC, lp.user_id ASC
               )] AS cohort_id
        FROM _league_pool lp
        WHERE lp.effective_tier = v_tier
      )
      INSERT INTO public.league_memberships (cohort_id, user_id, week_key, pool_tier, standing_tier)
      SELECT cohort_id, user_id, v_this_week, v_tier, standing_tier FROM banded;

      UPDATE public.league_cohorts c SET member_count = sub.cnt
      FROM (
        SELECT cohort_id, count(*) AS cnt FROM public.league_memberships
        WHERE week_key = v_this_week GROUP BY cohort_id
      ) sub
      WHERE c.id = sub.cohort_id;
    END;
  END LOOP;

  DROP TABLE _league_pool;

  UPDATE public.league_assignment_runs SET completed_at = p_as_of WHERE week_key = v_this_week;

  RETURN jsonb_build_object(
    'ok', true, 'already_completed', false,
    'finalized_week', v_last_week, 'finalized_count', COALESCE(v_finalized_count, 0),
    'created_week', v_this_week
  );
END;
$$;
REVOKE EXECUTE ON FUNCTION public.assign_weekly_league_cohorts_as_of(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.assign_weekly_league_cohorts_as_of(timestamptz) TO service_role;

CREATE OR REPLACE FUNCTION public.assign_weekly_league_cohorts()
RETURNS jsonb
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp
AS $$
  SELECT public.assign_weekly_league_cohorts_as_of(now());
$$;
REVOKE EXECUTE ON FUNCTION public.assign_weekly_league_cohorts() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.assign_weekly_league_cohorts() TO service_role;
