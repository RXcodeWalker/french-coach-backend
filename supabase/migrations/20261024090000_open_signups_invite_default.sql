/*
  # Public launch: open sign-ups (invite gate off by default)

  New profiles now start as invite_status = 'redeemed', so a fresh sign-up can
  use AI features immediately. consume_ai_quota() is unchanged: it still denies
  anyone not 'redeemed', and the per-user daily caps in ai_quota_limits still
  apply — those caps, not the invite code, are the cost control from here on.

  handle_new_user() does not set invite_status, so the column default is the
  only thing that decides it. Any row still 'unredeemed' is opened too.

  To close sign-ups again (invite-only), revert both statements:
    ALTER TABLE public.profiles ALTER COLUMN invite_status SET DEFAULT 'unredeemed';
  (and leave existing rows as they are).
*/

ALTER TABLE public.profiles ALTER COLUMN invite_status SET DEFAULT 'redeemed';

UPDATE public.profiles SET invite_status = 'redeemed' WHERE invite_status = 'unredeemed';
