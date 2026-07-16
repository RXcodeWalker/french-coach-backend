/*
  # Fix: grant PostgREST roles on igcse_question_sets

  20260716120000_add_igcse_question_sets.sql created the table and RLS
  policies but, unlike the tables added via the Supabase dashboard, a plain
  `create table` through a CLI migration does not automatically pick up the
  project's baseline `anon`/`authenticated`/`service_role` grants -- RLS
  policies only take effect once the role already has the underlying SQL
  privilege. Confirmed live: service_role got `permission denied for table
  igcse_question_sets` (42501, a GRANT-level error, not an RLS denial) even
  though the RLS policies were correct. This grants the same privilege set
  the other CMS content tables already have implicitly.
*/

grant select, insert, update, delete on public.igcse_question_sets to anon, authenticated, service_role;
