/*
  # F11b (security review 2026-11): drop the zero-argument export/delete overloads

  20260911110000 added export_my_data(uuid DEFAULT NULL) and delete_my_account(uuid
  DEFAULT NULL) without dropping 20260911100000's zero-argument versions, so the app's
  no-argument calls (accountService.ts: rpc('export_my_data'), rpc('delete_my_account'))
  are ambiguous ("function public.export_my_data() is not unique"). Dropping the
  zero-argument versions leaves the defaulted ones, which do the same for the caller's
  own account. IF EXISTS: safe whether or not production still has them.
*/

DROP FUNCTION IF EXISTS public.export_my_data();
DROP FUNCTION IF EXISTS public.delete_my_account();
