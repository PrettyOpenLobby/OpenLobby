-- Per-account login refusals and login notices (accounts.set_reject_code,
-- accounts.set_login_information).
--
-- reject_code    0, or a status byte the client shows as a refusal when this
--                PlayOnline ID logs in (accounts.LOGIN_REFUSAL_CODES).
-- reject_until   when a temporary refusal ends, ISO-8601 UTC text as the other
--                timestamps in 0001; NULL = until cleared by hand.
-- info_code      0, or a notice code carried by the successful login token
--                (accounts.LOGIN_INFORMATION_CODES).
-- info_repeat    'once' (sent at the next login, then cleared) or 'always'.
-- info_shown_at  for a one-time notice: when a login claimed it (a claim is
--                retried after five minutes if that login never sent it), then
--                when it was delivered.

ALTER TABLE polid ADD COLUMN reject_code   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE polid ADD COLUMN reject_until  TEXT;
ALTER TABLE polid ADD COLUMN info_code     INTEGER NOT NULL DEFAULT 0;
ALTER TABLE polid ADD COLUMN info_repeat   TEXT NOT NULL DEFAULT 'once';
ALTER TABLE polid ADD COLUMN info_shown_at TEXT;
