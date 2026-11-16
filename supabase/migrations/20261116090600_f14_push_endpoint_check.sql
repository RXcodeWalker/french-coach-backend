/*
  # F14 (security review 2026-11, database half): push endpoints must be https URLs

  push_subscriptions.endpoint was free text, and the hourly notification job POSTs to it.
  It must now be an https:// URL with no whitespace, at most 2048 characters (real push
  service endpoints are a few hundred). No host allow-list: that is an owner decision.

  A row that does not fit could only have been written by hand, and the job cannot deliver
  to it, so it is deleted before the CHECK is added (otherwise the CHECK would not apply).
*/

DELETE FROM public.push_subscriptions
WHERE endpoint !~ '^https://[^[:space:]]+$' OR length(endpoint) > 2048;

ALTER TABLE public.push_subscriptions DROP CONSTRAINT IF EXISTS push_subscriptions_endpoint_https;
ALTER TABLE public.push_subscriptions
  ADD CONSTRAINT push_subscriptions_endpoint_https
  CHECK (endpoint ~ '^https://[^[:space:]]+$' AND length(endpoint) <= 2048);
