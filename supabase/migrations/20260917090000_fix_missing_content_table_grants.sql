/*
  # Fix missing table GRANTs on the CMS content tables

  Same defect as 20260811090000_fix_missing_table_grants.sql, one migration
  earlier in the schema's history: 20260629125717_add_content_tables.sql
  creates topics/questions/scenarios/exam_sets/content_versions with correct
  RLS policies but no GRANT for anon/authenticated. RLS can only narrow access
  a GRANT already allows, so with no base privilege PostgREST answers every
  read with 42501 "permission denied for table X" — surfaced in the browser as
  a flat 403, not the empty result set an RLS miss would produce.

  That migration's `create table if not exists` is why this went unnoticed
  locally: where the tables already existed from Supabase's project-init
  defaults they carried those defaults' grants, so only environments where the
  migration actually created them are broken. This project does not have
  default privileges for new entities enabled (the auto_expose_new_tables note
  in supabase/config.toml).

  Symptom: the admin content browser's list and per-status count queries
  (src/services/content/adminApi.ts listQuestions/listScenarios/countByStatus)
  all 403, and so does every anonymous read of published content that goes
  through PostgREST rather than the backend's service-role client.

  Grants are scoped to exactly the commands each table has a policy for, so
  RLS stays the only thing deciding which rows are visible — the admin-only
  reads are still admin-only, via is_admin() in the policies:
    topics/questions/scenarios/exam_sets  SELECT to anon (public read policy:
                                          status = 'published')
    topics/questions/scenarios/exam_sets  SELECT/INSERT/UPDATE/DELETE to
                                          authenticated (admin policies)
    content_versions                      SELECT/INSERT to authenticated
                                          (admin-only, append-only by design;
                                          no anon — it has no public policy)
  Admin writes normally go through the backend's service-role client, which
  bypasses RLS but still needs its own grant.
*/

GRANT SELECT                         ON public.topics    TO anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.topics    TO authenticated, service_role;

GRANT SELECT                         ON public.questions TO anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.questions TO authenticated, service_role;

GRANT SELECT                         ON public.scenarios TO anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.scenarios TO authenticated, service_role;

GRANT SELECT                         ON public.exam_sets TO anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.exam_sets TO authenticated, service_role;

GRANT SELECT, INSERT                 ON public.content_versions TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.content_versions TO service_role;
