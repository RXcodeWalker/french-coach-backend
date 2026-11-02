-- Admin = app_metadata.role = 'admin' AND the JWT email is the owner's.
-- Mirrors lib/auth.py's is_admin_payload (ADMIN_EMAILS, default the owner) so a
-- role granted to any other account opens nothing at the RLS layer either.
-- To add an admin later, add the email here in a new migration AND to ADMIN_EMAILS.
create or replace function is_admin()
returns boolean language sql stable security definer as $$
  select coalesce(
    (auth.jwt()->'app_metadata'->>'role') = 'admin'
    and lower(coalesce(auth.jwt()->>'email', '')) in ('omjhamvar29@gmail.com'),
    false)
$$;
