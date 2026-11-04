/*
  Usernames may contain hyphens (owner, 2026-11-04). New rule, same in both
  functions and in the main repo's src/services/social/usernameService.ts:
  3–20 characters, starts with a letter, then letters, digits, underscores or
  hyphens, and never ends in a hyphen.

  Only the format check changes; the bodies are otherwise the live production
  definitions (read from pg_get_functiondef on 2026-11-04): reserved-name check,
  first-claim-only for claim_username, the 30-day throttle for rename_username.
  Grants are unchanged (CREATE OR REPLACE keeps them).
*/

CREATE OR REPLACE FUNCTION public.claim_username(new_username text)
 RETURNS void
 LANGUAGE plpgsql
AS $function$
BEGIN
  IF new_username !~ '^[A-Za-z][A-Za-z0-9_-]{1,18}[A-Za-z0-9_]$' THEN
    RAISE EXCEPTION 'invalid_username' USING ERRCODE = '22023';
  END IF;

  IF EXISTS (SELECT 1 FROM reserved_usernames WHERE username = lower(new_username)) THEN
    RAISE EXCEPTION 'username_reserved' USING ERRCODE = '22023';
  END IF;

  UPDATE profiles
  SET username = new_username,
      username_changed_at = now()
  WHERE id = auth.uid()
    AND username IS NULL;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'username_already_set' USING ERRCODE = '22023';
  END IF;
END;
$function$;

CREATE OR REPLACE FUNCTION public.rename_username(new_username text)
 RETURNS void
 LANGUAGE plpgsql
AS $function$
BEGIN
  IF new_username !~ '^[A-Za-z][A-Za-z0-9_-]{1,18}[A-Za-z0-9_]$' THEN
    RAISE EXCEPTION 'invalid_username' USING ERRCODE = '22023';
  END IF;

  IF EXISTS (SELECT 1 FROM reserved_usernames WHERE username = lower(new_username)) THEN
    RAISE EXCEPTION 'username_reserved' USING ERRCODE = '22023';
  END IF;

  UPDATE profiles
  SET username = new_username,
      username_changed_at = now()
  WHERE id = auth.uid()
    AND (username_changed_at IS NULL OR username_changed_at <= now() - interval '30 days');

  IF NOT FOUND THEN
    RAISE EXCEPTION 'rename_throttled' USING ERRCODE = '22023';
  END IF;
END;
$function$;
