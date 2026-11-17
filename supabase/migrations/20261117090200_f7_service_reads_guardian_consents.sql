/*
  # F7 (security review 2026-11): the backend reads guardian_consents

  POST /api/consent/send-guardian-email now mails only the caller's own consent request that
  is still open, to that request's guardian address. It looks the request up with the service
  key. 20260911110000 revoked everything from PUBLIC and granted SELECT to authenticated only;
  a table created by a CLI migration does not always inherit service_role's grant, so this makes
  it explicit. Idempotent: harmless where service_role already has it.
*/

GRANT SELECT ON public.guardian_consents TO service_role;
