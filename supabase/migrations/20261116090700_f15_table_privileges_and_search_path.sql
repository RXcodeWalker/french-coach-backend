/*
  # F15 (security review 2026-11): hardening, no reachable exploit

  1. anon and authenticated held TRUNCATE, REFERENCES and TRIGGER on public tables
     (profiles, sessions, and by default privileges every new table). PostgREST cannot use
     them, but nothing needs them: revoked on every existing table and view in public, and
     taken out of the default privileges for tables this role (the migration role) creates.
  2. search_path pinned on the public functions that had none. Each body uses only
     pg_catalog, public tables/functions and schema-qualified auth.*; none is SECURITY
     DEFINER-sensitive to the caller's path after this. Skipped if a function is absent.
     Production also has rls_auto_enable, which no migration defines; it is left alone.
*/

REVOKE TRUNCATE, REFERENCES, TRIGGER ON ALL TABLES IN SCHEMA public FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE TRUNCATE, REFERENCES, TRIGGER ON TABLES FROM anon, authenticated;

DO $$
DECLARE
  f text;
BEGIN
  FOREACH f IN ARRAY ARRAY[
    'public._league_week_key(timestamp with time zone)',
    'public.claim_username(text)',
    'public.email_report_settings_touch()',
    'public.is_admin()',
    'public.rename_username(text)',
    'public.set_updated_at()',
    'public.snapshot_before_update()',
    'public.sync_is_active_from_status()',
    'public.unblock_user(uuid)',
    'public.validate_push_subscription_timezone()'
  ] LOOP
    IF to_regprocedure(f) IS NOT NULL THEN
      EXECUTE format('ALTER FUNCTION %s SET search_path = pg_catalog, public, pg_temp', f);
    END IF;
  END LOOP;
END $$;
