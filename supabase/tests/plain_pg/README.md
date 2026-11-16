# Plain-Postgres SQL tests

For checking a migration's functions when `npx supabase start` (Docker) isn't available. `prelude.sql`
stubs only what the migration under test needs (the `anon`/`authenticated`/`service_role` roles,
`auth.uid()`/`auth.jwt()` read from `request.jwt.claim.sub`/`request.jwt.claims`, and minimal earlier
tables), so a pass here proves the SQL and its access rules, not parity with the hosted project.
Never run these against the hosted project.

```bash
createdb -h <socket-dir> -p <port> -U postgres teacher_test
psql -q -v ON_ERROR_STOP=1 -h <socket-dir> -p <port> -U postgres -d teacher_test \
  -f supabase/tests/plain_pg/prelude.sql \
  -f supabase/migrations/20261103091000_teacher_dashboard.sql \
  -f supabase/migrations/20261105120000_co_teachers_and_multi_class.sql \
  -f supabase/tests/plain_pg/teacher_dashboard_test.sql   # or co_teachers_multi_class_test.sql
```

The last line printed must be `ALL PASSED`; any failed expectation stops with an error.

Parent view email links: `-f prelude.sql -f parent_view_prelude.sql -f ../../migrations/20261104090000_parent_links.sql
-f ../../migrations/20261104100000_parent_report_exam_columns.sql -f ../../migrations/20261107100000_parent_digest_dashboard_link.sql
-f parent_digest_link_test.sql`.

## Whole-schema tests (PGlite, no Postgres install)

`run_all_migrations.mjs` applies **every** migration in order to an in-memory Postgres (PGlite) behind
`supabase_prelude.sql` (Supabase's roles, `auth.users`, `auth.uid()`/`auth.jwt()`, the `extensions` schema and the
default table privileges production grants), then runs the test files named on the command line. PGlite is not a
dependency here, so install it in a scratch folder outside both repos and run from there:

```bash
cd <scratch> && npm install @electric-sql/pglite
node <repo>/backend/supabase/tests/plain_pg/run_all_migrations.mjs <repo>/backend/supabase/tests/plain_pg/security_2026_11_f1_profiles_test.sql
```

It stops if any migration fails to apply. The `security_2026_11_*_test.sql` files are the security review's
(main repo `docs/audits/security-review-2026-11.md`) findings as regression tests; each ends with `ALL PASSED`.
