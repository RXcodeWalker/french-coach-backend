/*
  # Close revoke_guardian_consent(p_child_user_id) to every client

  It was anon-callable with nothing but a child's user id, and it erases that
  child's profile (cascading all their data): anyone who learned a child's id
  could wipe the account. No role may call it now. The replacement,
  revoke_guardian_consent(p_token text), is the next migration. Applied to
  production 2026-10-03 (this version is the one production recorded).
*/

REVOKE EXECUTE ON FUNCTION public.revoke_guardian_consent(uuid) FROM PUBLIC, anon, authenticated;
