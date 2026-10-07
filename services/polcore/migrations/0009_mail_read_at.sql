-- mail.read_at: POP3 RETR timestamp, so the lobby gate's byte +0x11 can count
-- UNREAD instead of "still undeleted". Without this, a welcome mail (or any
-- mail whose reader never sends DELE -- which is the Viewer's default) keeps
-- the boot badge at 1 forever, because the previous count was
-- `len(list_mail(box))` and `list_mail` already skips DELETED rows only.
--
-- Existing rows migrate as NULL (= unread), so the badge count is the same as
-- it was on the login before this migration; the first RETR from the client
-- clears it for that message and it stays cleared. We do NOT back-fill from
-- `received_at`, because a real unread message would be hidden by that.

ALTER TABLE mail ADD COLUMN read_at TEXT;
