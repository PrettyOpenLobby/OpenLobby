"""POP3 and SMTP: the Viewer's mail client against the mail table."""
import datetime
import hashlib
import json
import os
import re
import secrets
import time
from srvcore import _stamp, log
from .deps import accounts, extmail



# --------------------------------------------------------------------------- #
# PlayOnline Mail: po000 = POP3, ma000 = SMTP
#
# The Viewer's mail account wizard names these hosts itself on its Server
# Settings page, and they are plain RFC POP3/SMTP -- the only PlayOnline-specific
# parts are the address form `<local>@pol.com` and the PORTS (51260/51261 for a
# PlayOnline account, 110/25 only for a hand-made generic one; see main()).
#
# `<local>` comes from the account record's string fields (see _acct_payload) --
# the same list the Sender dropdown offers.
#
# What the client actually implements (app.dll, static RE 2026-08-12):
#
#   POP3   APOP USER PASS STAT QUIT RETR DELE NOOP LIST RSET UIDL TOP
#          (command table at app.dll+0x4b9050; there is NO CAPA and no STLS)
#   SMTP   AUTH HELO EHLO "MAIL FROM: " "RCPT TO: " DATA RSET NOOP QUIT
#          (table at +0x4b9108; AUTH is only sent when the account's SMTP-auth
#          setting == 2, and then it is `AUTH CRAM-MD5`)
#
#   * Replies are judged on the FIRST BYTE for POP3 ('+' ok / '-' fail) and on the
#     3-digit code for SMTP -- there is no "+OK"/"-ERR" literal anywhere in the
#     image, so our reply TEXT is free-form.
#   * APOP needs a timestamp banner: the greeting is scanned for the first
#     `<...>` (app.dll+0x296170) and the digest is MD5(banner + password) with the
#     banner truncated at 80 bytes and the password at 128 (+0x2962f0). With no
#     `<...>` in the greeting the client fails the account with POL-0403 BEFORE
#     sending anything -- that is the "connects and says nothing" symptom.
#   * A PlayOnline-type account defaults to APOP (auth mode 1).
# --------------------------------------------------------------------------- #
MAIL_DOMAIN = os.environ.get("POL_MAIL_DOMAIN", "pol.com")

#: How long a mail connection may sit silent before we drop it, and the largest
#: message body SMTP will accumulate. Both were unbounded.
_MAIL_IDLE = int(os.environ.get("POL_MAIL_IDLE", "180"))
_MAIL_MAX_BYTES = int(os.environ.get("POL_MAIL_MAX_KB", "4096")) * 1024


def _mail_db():
    """The account DB, or None when accounts are unavailable/disabled.

    Mail storage rides the SAME sqlite file as everything else rather than a
    maildir of its own: an address belongs to a member, and putting it anywhere
    else means a second thing to back up and a second thing that can disagree
    about who owns `lex@pol.com`.
    """
    if accounts is None or os.environ.get("POL_ACCOUNTS", "1") != "1":
        return None
    try:
        return accounts.connect(os.environ.get("POL_ACCOUNTS_DB",
                                               accounts.DEFAULT_DB))
    except Exception as exc:                       # pragma: no cover - defensive
        log("mail", f"account DB unavailable ({exc!r}); mailbox is read-only stub")
        return None


def _welcome_message(user):
    addr = f"{user}@{MAIL_DOMAIN}"
    # RFC822 date, generated rather than hardcoded: the literal that used to sit
    # here said "Mon, 11 Aug 2026", and 11 Aug 2026 was a TUESDAY. The client
    # parses this with its own CRfc822Parser, so a self-inconsistent date is a
    # gift to a future afternoon of debugging.
    now = datetime.datetime.now(datetime.timezone.utc)
    date = now.strftime("%a, %d %b %Y %H:%M:%S +0000")
    return (
        f"From: PlayOnline <info@{MAIL_DOMAIN}>\r\n"
        f"To: {addr}\r\n"
        "Subject: Welcome to PlayOnline\r\n"
        f"Date: {date}\r\n"
        "Message-Id: <welcome-1@pol.com>\r\n"
        "Mime-Version: 1.0\r\n"
        "Content-Type: text/plain; charset=us-ascii\r\n"
        "\r\n"
        "Your PlayOnline Mail account is working.\r\n"
        "\r\n"
        "This message was delivered by your own server.\r\n"
    ).encode("ascii")


def _pop3_messages(user):
    """[(mail_id, uidl, raw_rfc822_bytes)] for a mailbox, oldest first.

    Backed by the `mail` table. `mail_id` is the DB row so DELE can commit at
    QUIT; it is None for the synthesised fallback.

    A brand-new mailbox is seeded with one welcome message, because an empty
    INBOX and a broken INBOX look identical from the UI and we have lost time to
    that before. The seed goes in as a REAL message, so deleting it sticks --
    the old stub re-synthesised it on every login, which made DELE look broken.

    POL_MAIL_WELCOME=0 suppresses the seed. That exists as a DIAGNOSTIC: the
    client's Receive reliably stalls between LIST and RETR, and an empty mailbox
    splits the cause in two -- if Receive then completes cleanly, the failure is
    in downloading/filing a message it wants; if it stalls on an empty mailbox
    too, the failure is structural and earlier than any message handling.
    """
    seed = os.environ.get("POL_MAIL_WELCOME", "1") == "1"
    db = _mail_db()
    if db is None:
        return [(None, "welcome-1", _welcome_message(user))] if seed else []
    try:
        box = accounts.mail_box_name(user)
        rows = accounts.list_mail(db, box)
        if seed and not rows \
                and not accounts.list_mail(db, box, include_deleted=True):
            accounts.deliver_mail(db, box, _welcome_message(user),
                                  sender=f"info@{MAIL_DOMAIN}",
                                  subject="Welcome to PlayOnline",
                                  uidl="welcome-1")
            rows = accounts.list_mail(db, box)
        return [(r["id"], r["uidl"], bytes(r["raw"])) for r in rows]
    finally:
        db.close()


def _pop3_mailbox_user(user):
    """The mailbox local part a POP3 login name refers to.

    A PlayOnline-type mail account (POP 51260) logs in as the POL ID --
    `APOP ABCD1234 <digest>` -- not as the mail name, and its digest is made
    with the member's MAIL password (MD5(banner + mail_pw_plain)).
    member_by_mail cannot resolve a POL ID, and "no such mailbox" is always
    refused, so without this every such login would be turned away.

    Only an UPPERCASE name is tried as a POL ID. Mail names are lowercase-only
    (accounts.check_mail_local), so a member who picks the mail name `abcd1234`
    can never capture ABCD1234's login. Returns `user` unchanged otherwise.
    """
    if not user or user == user.lower():
        return user
    db = _mail_db()
    if db is None:
        return user
    try:
        row = _mail_polid_member(db, user)
        addr = (row["mail_address"] if row is not None else None) or ""
        local = addr.split("@", 1)[0]
        return local or user
    finally:
        db.close()


def _mail_polid_member(db, name, any_case=False):
    """The member whose POL ID is the local part of `name`, or None.

    The PlayOnline-type account uses the POL ID on the wire for all three:
    the POP3 login, `MAIL FROM: ABCD1234@pol.com` on SMTP 51261, and so the
    From that other members reply to. By default only an
    UPPERCASE name is tried, for the reason _pop3_mailbox_user gives;
    `any_case` is for a RECIPIENT typed by another member, and callers use it
    only after member_by_mail found no mailbox of that name, so a real
    mailbox always wins.
    """
    local = (name or "").split("@", 1)[0]
    if not local or (local == local.lower() and not any_case):
        return None
    return accounts.member_by_polid(db, local.upper())


def _mail_session_allowed(db, row, peer_ip, why):
    """The fallback when NO secret can be checked: the dialling address must hold
    the mailbox owner's LIVE session.

    Until 2026-09-05 an unverifiable login was simply accepted, so any peer on
    the tailnet could read any member's inbox with `USER <name>` / `PASS x`, and
    SMTP took any `MAIL FROM`. Presence is the session table
    (accounts.member_online), and a session row carries the address it was
    opened from -- so "the Viewer that is logged in as this member is the one
    talking to me" is a checkable fact, and it is what the client always
    satisfies: PlayOnline Mail runs inside a signed-in Viewer. A peer that is
    not that Viewer is refused. POL_MAIL_SESSION_CHECK=0 restores the old
    accept-anything behaviour (dev under the Docker bridge sees one address for
    every client, where the check proves little but still passes).
    """
    if os.environ.get("POL_MAIL_SESSION_CHECK", "1") != "1" or not peer_ip:
        return True, why + " (POL_MAIL_SESSION_CHECK off)"
    if row is None:
        return False, why + "; no such mailbox"
    if accounts.member_online_from(db, row["id"], peer_ip):
        return True, why + f"; {peer_ip} holds this member's live session"
    return False, (why + f" and {peer_ip} holds no live session for this "
                         f"member (POL_MAIL_SESSION_CHECK)")


def _mail_login_allowed(user, peer_ip, password):
    """(ok, why) for a POP3 USER/PASS login.

    A stored mail password is CHECKED -- plaintext when the account opted into
    one (the APOP case), the PBKDF2 hash otherwise. With no password on file the
    login falls to the live-session rule (_mail_session_allowed), or is refused
    outright under POL_MAIL_STRICT=1.
    """
    strict = os.environ.get("POL_MAIL_STRICT", "0") == "1"
    db = _mail_db()
    if db is None:
        return (not strict, "no account DB")
    try:
        row = accounts.member_by_mail(db, user)
        if row is None:
            # ALWAYS a refusal, strict or not. No client of ours asks for a
            # mailbox that has no account: accepting it let a peer name
            # anything and be told yes, and it seeded a welcome message into a
            # box nobody owns.
            return False, "no such mailbox"
        keys = row.keys()
        secret = row["mail_pw_plain"] if "mail_pw_plain" in keys else None
        if secret:
            ok = secrets.compare_digest((password or "").encode("utf-8"),
                                        str(secret).encode("utf-8"))
            return ok, ("password verified" if ok else "wrong password")
        pw_hash = row["mail_pw_hash"] if "mail_pw_hash" in keys else None
        if pw_hash:
            ok = accounts.check_password(password or "", pw_hash, row["mail_pw_salt"])
            return ok, ("password verified (hash)" if ok else "wrong password")
        # NO MAIL PASSWORD ON FILE -- but the ACCOUNT password is one we can
        # check, and a sign-up path may have set the mail password from it in
        # the first place. So
        # try it before falling back to the session rule: it turns the accounts
        # that predate mail passwords from unverifiable into verifiable, which
        # is what POL_MAIL_STRICT=1 needs before it can be turned on.
        acct_hash = row["pw_hash"] if "pw_hash" in keys else None
        if acct_hash and password:
            salt = row["pw_salt"] if "pw_salt" in keys else None
            if accounts.check_password(password, acct_hash, salt):
                return True, "verified against the account password"
        if strict:
            return False, "no mail password stored (POL_MAIL_STRICT)"
        return _mail_session_allowed(db, row, peer_ip, "no mail password stored")
    finally:
        db.close()


def _smtp_sender_allowed(sender, peer_ip):
    """(ok, why) for an SMTP `MAIL FROM`.

    The envelope sender must be one of OUR members' addresses, and the peer must
    hold that member's live session -- the same rule as the POP3 fallback, for
    the same reason: a signed-in Viewer is the only thing that legitimately
    sends PlayOnline Mail, and before this any tailnet peer could post mail as
    anybody. A null sender (bounces) passes; the Viewer never sends one, and it
    impersonates nobody. POL_MAIL_SENDER_CHECK=0 turns the whole check off.
    """
    if os.environ.get("POL_MAIL_SENDER_CHECK", "1") != "1":
        return True, "POL_MAIL_SENDER_CHECK off"
    if not sender:
        return True, "null sender"
    db = _mail_db()
    if db is None:
        return True, "no account DB"
    try:
        dom = sender.split("@", 1)[1].lower() if "@" in sender else MAIL_DOMAIN.lower()
        if dom != MAIL_DOMAIN.lower():
            return False, (f"foreign sender domain {dom!r}; every member sends "
                           f"as <name>@{MAIL_DOMAIN}")
        row = accounts.member_by_mail(db, sender)
        if row is None:
            row = _mail_polid_member(db, sender)
            if row is not None:
                return _mail_session_allowed(db, row, peer_ip,
                                             "sender is a member's POL ID")
        if row is None:
            return False, "no member owns this address"
        return _mail_session_allowed(db, row, peer_ip, "sender is a member")
    finally:
        db.close()


def _pop3_check_apop(user, banner, digest, peer_ip=None):
    """(ok, why) for an APOP digest.

    Verifiable only against a PLAINTEXT mail password (MD5(banner+password) is
    not invertible into a salted hash). An account with no `mail_pw_plain`
    falls to the live-session rule (_mail_session_allowed) -- refusing would
    lock out every account created before mail passwords existed, and
    accepting blind (the behaviour until 2026-09-05) let any peer read any
    inbox. POL_MAIL_STRICT=1 refuses instead, which is the right setting once
    every account has a mail password.
    """
    strict = os.environ.get("POL_MAIL_STRICT", "0") == "1"
    db = _mail_db()
    if db is None:
        return (not strict, "no account DB")
    try:
        row = accounts.member_by_mail(db, user)
        if row is None:
            return False, "no such mailbox"          # see _mail_login_allowed
        secret = row["mail_pw_plain"] if "mail_pw_plain" in row.keys() else None
        if not secret:
            if strict:
                return False, "no plaintext mail password stored (POL_MAIL_STRICT)"
            return _mail_session_allowed(db, row, peer_ip,
                                         "no plaintext mail password stored")
        calc = hashlib.md5((banner + secret).encode("utf-8")).hexdigest()
        ok = secrets.compare_digest(calc, (digest or "").lower())
        # The Viewer keeps its OWN copy of the mail password (given to it at
        # sign-up), so a mismatch after a Membership change is the usual case.
        return ok, ("verified" if ok else
                    "wrong password -- the Viewer's saved mail password "
                    "differs from the one on file")
    finally:
        db.close()


def _pop3_msgno(arg, msgs, deleted):
    """Validate a POP3 message number. None = answer -ERR.

    Worth the four lines: the old code did `int(arg)` and indexed straight into
    the list, so one malformed or stale number raised, killed the connection and
    surfaced in the client as POL-0011 ("already closed") -- a network error for
    what is really a protocol -ERR.
    """
    try:
        i = int(arg)
    except (TypeError, ValueError):
        return None
    if i < 1 or i > len(msgs) or i in deleted:
        return None
    return i


def _pop3_commit_deletes(msgs, deleted, peer):
    """Apply a session's DELE set to the DB. Called from QUIT only."""
    ids = [msgs[i - 1][0] for i in sorted(deleted)
           if 1 <= i <= len(msgs) and msgs[i - 1][0] is not None]
    if not ids:
        return
    db = _mail_db()
    if db is None:
        return
    try:
        accounts.delete_mail(db, ids)
        log("mail", f"{peer} POP3 QUIT: deleted {len(ids)} message(s)")
    finally:
        db.close()


class _MailTrace:
    """A file-like wrapper that LOGS EVERY BYTE WE SEND.

    Added because a live Receive stalled after LIST with no error on either side:
    the command log showed what the client asked and nothing about what it got,
    so there was no way to tell a malformed reply from a client that simply
    stopped. A stall is invisible unless both directions are recorded.

    POL_MAIL_TRACE=0 turns it off; POL_MAIL_TRACE_MAX caps each logged reply.
    """

    def __init__(self, f, tag, peer):
        self._f, self._tag, self._peer = f, tag, peer
        self._on = os.environ.get("POL_MAIL_TRACE", "1") == "1"
        self._max = int(os.environ.get("POL_MAIL_TRACE_MAX", "300"))
        self._pending = bytearray()

    def write(self, b):
        if self._on:
            self._pending += b
        return self._f.write(b)

    def flush(self):
        r = self._f.flush()
        if self._on and self._pending:
            txt = bytes(self._pending).decode("ascii", "replace")
            more = "" if len(txt) <= self._max else \
                f" ... (+{len(txt) - self._max}B)"
            log("mail", f"{self._peer} {self._tag} S: {txt[:self._max]!r}{more}")
            self._pending.clear()
        return r

    def readline(self, *a):
        return self._f.readline(*a)


def handle_pop3(conn, addr, port=110):
    """A minimal RFC 1939 POP3 server (USER/PASS/STAT/LIST/UIDL/RETR/TOP/DELE).

    Mailboxes are real and per-account (the `mail` table, keyed on the address's
    local part). USER/PASS is still accept-any -- the Viewer's PlayOnline preset
    uses APOP, PASS is only reachable from a hand-made generic account, and
    rejecting a login we have not seen would hide what it sends. APOP IS checked
    when the account has opted into a plaintext mail password; see
    _pop3_check_apop. Every command AND every reply is logged.
    """
    peer = f"{addr[0]}:{addr[1]}->{port}"
    user = "unknown"
    msgs = []
    deleted = set()
    f = _MailTrace(conn.makefile("rwb"), "POP3", peer)
    # The Viewer's Server Settings page sets User Authentication = APOP, and an
    # APOP client needs a TIMESTAMP BANNER in the greeting -- `<unique@host>` per
    # RFC 1939 -- to compute MD5(banner + password). Without one it has nothing to
    # digest, so it hangs up before sending a single command. That is exactly what
    # happened: DNS resolved po000 to us and mail.log stayed empty, because the
    # old handler only logged COMMANDS and a client that never speaks leaves no
    # trace. Hence the connect/disconnect logging below as well.
    banner = f"<{os.getpid()}.{int(time.time())}@{MAIL_DOMAIN}>"
    # A mail connection that says nothing must not own a thread for the life of
    # the process. Everything else in this file sets a timeout; these two
    # handlers wrapped the socket in makefile() and blocked in readline()
    # forever, so one half-open connection was one permanently lost thread.
    conn.settimeout(_MAIL_IDLE)
    log("mail", f"{peer} POP3 connect, banner {banner}")
    try:
        f.write(f"+OK PlayOnline Mail ready {banner}\r\n".encode("ascii"))
        f.flush()
        while True:
            line = f.readline()
            if not line:
                log("mail", f"{peer} POP3 client closed / went silent after "
                            f"{len(msgs)} message(s) listed")
                break
            line = line.rstrip(b"\r\n")
            log("mail", f"{peer} POP3 C: {line.decode('ascii', 'replace')!r}")
            parts = line.split(b" ", 1)
            cmd = parts[0].upper().decode("ascii", "replace")
            arg = parts[1].decode("ascii", "replace") if len(parts) > 1 else ""

            if cmd == "CAPA":
                f.write(b"+OK\r\nUSER\r\nUIDL\r\nTOP\r\n.\r\n")
            elif cmd == "USER":
                user = _pop3_mailbox_user(arg.split("@", 1)[0]) or "unknown"
                f.write(b"+OK\r\n")
            elif cmd == "PASS":
                ok, why = _mail_login_allowed(user, addr[0], arg)
                if not ok:
                    log("mail", f"{peer} POP3 PASS {user!r} REJECTED ({why})")
                    f.write(b"-ERR authentication failed\r\n")
                    f.flush()
                    continue
                msgs = _pop3_messages(user)
                log("mail", f"{peer} POP3 login {user!r} ({why}): "
                            f"{len(msgs)} message(s)")
                f.write(b"+OK mailbox ready\r\n")
            elif cmd == "APOP":
                # `APOP <name> <md5(banner + mail password)>` -- what the
                # PlayOnline account preset sends (auth mode 1).
                name, _, digest = arg.partition(" ")
                user = _pop3_mailbox_user(name.split("@", 1)[0]) or "unknown"
                ok, why = _pop3_check_apop(user, banner, digest.strip(), addr[0])
                if not ok:
                    log("mail", f"{peer} POP3 APOP {user!r} REJECTED ({why})")
                    f.write(b"-ERR authentication failed\r\n")
                    f.flush()
                    continue
                msgs = _pop3_messages(user)
                log("mail", f"{peer} POP3 APOP {user!r} digest={digest} "
                            f"({why}): {len(msgs)} message(s)")
                f.write(b"+OK mailbox ready\r\n")
            elif cmd == "STAT":
                live = [m for i, m in enumerate(msgs) if i + 1 not in deleted]
                total = sum(len(b) for _, _u, b in live)
                f.write(f"+OK {len(live)} {total}\r\n".encode("ascii"))
            elif cmd in ("LIST", "UIDL"):
                if arg:
                    i = _pop3_msgno(arg, msgs, deleted)
                    if i is None:
                        f.write(b"-ERR no such message\r\n")
                        f.flush()
                        continue
                    _mid, uidl, b = msgs[i - 1]
                    val = uidl if cmd == "UIDL" else str(len(b))
                    f.write(f"+OK {i} {val}\r\n".encode("ascii"))
                else:
                    f.write(b"+OK\r\n")
                    for i, (_mid, uidl, b) in enumerate(msgs, 1):
                        if i in deleted:
                            continue
                        val = uidl if cmd == "UIDL" else str(len(b))
                        f.write(f"{i} {val}\r\n".encode("ascii"))
                    f.write(b".\r\n")
            elif cmd in ("RETR", "TOP"):
                parts2 = arg.split()
                i = _pop3_msgno(parts2[0] if parts2 else "", msgs, deleted)
                if i is None:
                    f.write(b"-ERR no such message\r\n")
                    f.flush()
                    continue
                b = msgs[i - 1][2]
                if cmd == "TOP":
                    # `TOP n lines` = full header block + `lines` body lines. The
                    # client uses it for previews; sending the whole message back
                    # would be answering a different question.
                    try:
                        nlines = int(parts2[1])
                    except (IndexError, ValueError):
                        nlines = 0
                    head, sep, rest = b.partition(b"\r\n\r\n")
                    body = rest.split(b"\r\n")[:nlines] if sep else []
                    b = head + sep + b"\r\n".join(body)
                f.write(f"+OK {len(b)} octets\r\n".encode("ascii"))
                # Byte-stuff any line that begins with '.', per RFC 1939.
                for ln in b.split(b"\r\n"):
                    f.write((b"." + ln if ln.startswith(b".") else ln) + b"\r\n")
                f.write(b".\r\n")
            elif cmd == "DELE":
                i = _pop3_msgno(arg, msgs, deleted)
                if i is None:
                    f.write(b"-ERR no such message\r\n")
                    f.flush()
                    continue
                deleted.add(i)
                f.write(b"+OK\r\n")
            elif cmd in ("NOOP", "RSET"):
                if cmd == "RSET":
                    deleted.clear()
                f.write(b"+OK\r\n")
            elif cmd == "QUIT":
                # RFC 1939 UPDATE state: DELE only marks, QUIT commits. Dropping
                # the connection without QUIT must therefore leave the mailbox
                # untouched -- which is also what makes "Leave mail on server"
                # behave, since that client just never sends DELE.
                _pop3_commit_deletes(msgs, deleted, peer)
                deleted = set()
                f.write(b"+OK bye\r\n")
                f.flush()
                break
            else:
                log("mail", f"{peer} POP3 UNKNOWN command {cmd!r} -- worth adding")
                f.write(b"-ERR unsupported\r\n")
            f.flush()
    except Exception as exc:
        log("mail", f"{peer} POP3 error: {exc!r}")
    finally:
        log("mail", f"{peer} POP3 disconnect (user={user!r})")
        try:
            conn.close()
        except Exception:
            pass


_SMTP_ANGLE_RE = re.compile(r"<([^>]*)>")
_SMTP_BARE_RE  = re.compile(r"(\S+@\S+)")


def _smtp_addr(arg):
    """Pull the address out of `MAIL FROM:<a@b>` / `RCPT TO: a@b`.

    The angle-bracket form is tried FIRST, on purpose. These used to be one
    alternation, `<([^>]*)>|(\\S+@\\S+)`, and re.search is leftmost-match rather
    than best-match: given the standard `RCPT TO:<lex@pol.com>`, the bare-address
    branch matches at "TO:<lex@pol.com>" -- earlier in the string than the `<` --
    so the recipient came back as `TO:<lex@pol.com>`, its domain parsed as
    "pol.com>", and the message was dropped as off-domain.

    It went unnoticed because the Viewer sends `RCPT TO: lex@pol.com` WITH a
    space, where "TO:" has no `@` to attach to and the bare branch matches the
    address correctly. So POL mail worked and every standard client silently did
    not. Found 2026-08-12 while seeding a mailbox with smtplib.
    """
    m = _SMTP_ANGLE_RE.search(arg)
    if m and m.group(1).strip():
        return m.group(1).strip()
    m = _SMTP_BARE_RE.search(arg)
    return m.group(1).strip() if m else ""


def _smtp_header(body, name):
    """First value of a header, for the log line and the stored summary."""
    for raw in body.split(b"\r\n"):
        if not raw:
            break
        if raw.lower().startswith(name.lower().encode() + b":"):
            return raw.split(b":", 1)[1].strip().decode("cp932", "replace")
    return ""


#: *** SE'S OWN SERVICE ADDRESSES, DELIVERED LOCALLY INSTEAD OF DROPPED. ***
#: The Viewer really does submit its abuse reports by SMTP: "Report User" in chat
#: composes a mail to `tos@us.playonline.com` and sends it to US, on port 51261.
#: We answered `250 OK` and then binned it as off-domain, so the client's whole
#: reporting path ended in a black hole with nothing but a log line.
#:
#: `tos@us.playonline.com` is the one MEASURED (2026-08-16, a report the account
#: holder filed); the regional siblings are the obvious companions and cost
#: nothing to accept. `ocr@` and `subject-title@` appear in SE's own mirrored
#: portal pages. Everything here lands in ONE local box so there is a single place
#: to look. Override or extend with POL_MAIL_ALIASES="addr=box,addr=box".
_MAIL_REPORT_BOX = os.environ.get("POL_MAIL_REPORT_BOX", "tos")
_MAIL_ALIASES_DEFAULT = {
    "tos@us.playonline.com": _MAIL_REPORT_BOX,
    "tos@jp.playonline.com": _MAIL_REPORT_BOX,
    "tos@eu.playonline.com": _MAIL_REPORT_BOX,
    "tos@playonline.com": _MAIL_REPORT_BOX,
    "ocr@us.playonline.com": _MAIL_REPORT_BOX,
    "ocr@jp.playonline.com": _MAIL_REPORT_BOX,
    "subject-title@us.playonline.com": _MAIL_REPORT_BOX,
}


def _mail_aliases():
    """{off-domain address: local mailbox name}, lower-cased."""
    out = dict(_MAIL_ALIASES_DEFAULT)
    for item in os.environ.get("POL_MAIL_ALIASES", "").split(","):
        addr, _, box = item.strip().partition("=")
        if addr and box:
            out[addr.strip().lower()] = box.strip()
    return out


def _smtp_deliver(rcpts, body, peer):
    """Deliver to local mailboxes. Returns (delivered, skipped-as-remote).

    LOCAL ONLY, on purpose: this server has no business relaying mail off-box,
    and a private revival has nowhere legitimate to relay it to. Anything not
    addressed to our own domain is logged and dropped, not forwarded.

    ALIASES ARE NOT RELAYING. An aliased address is rewritten to a mailbox on THIS
    server and delivered here; nothing leaves the box. That is what makes the
    client's own report and support addresses usable without opening a relay.
    """
    db = _mail_db()
    if db is None:
        return 0, list(rcpts)
    sender = _smtp_header(body, "From")
    subject = _smtp_header(body, "Subject")
    aliases = _mail_aliases()
    done, remote = 0, []
    try:
        for r in rcpts:
            box = aliases.get(r.strip().lower())
            if box:
                local = box if "@" in box else f"{box}@{MAIL_DOMAIN}"
                accounts.deliver_mail(db, local, bytes(body), sender=sender,
                                      subject=subject)
                done += 1
                log("mail", f"{peer} SMTP {r} is an ALIAS -> delivered to {local} "
                            f"({len(body)}B) subject={subject!r}")
                _archive_report(r, local, sender, subject, body, peer)
                continue
            dom = r.split("@", 1)[1].lower() if "@" in r else MAIL_DOMAIN
            if extmail is not None and dom == extmail.domain():
                # `<name>@<outside domain>` from inside the game is
                # `<name>@pol.com`: the outside name of a local mailbox.
                r, dom = f"{r.split('@', 1)[0]}@{MAIL_DOMAIN}", MAIL_DOMAIN.lower()
            if dom != MAIL_DOMAIN.lower():
                remote.append(r)
                continue
            if accounts.member_by_mail(db, r) is None:
                # `<POLID>@pol.com` is the From a PlayOnline-type account sends
                # with, so it is what a reply comes back to.
                owner = _mail_polid_member(db, r, any_case=True)
                if owner is not None and owner["mail_address"]:
                    log("mail", f"{peer} SMTP {r} is a POL ID -> "
                                f"{owner['mail_address']}")
                    r = owner["mail_address"]
            accounts.deliver_mail(db, r, bytes(body), sender=sender,
                                  subject=subject)
            done += 1
            log("mail", f"{peer} SMTP delivered to {r} ({len(body)}B) "
                        f"subject={subject!r}")
    finally:
        db.close()
    return done, remote


#: *** THE HARASSMENT FORM'S WIRE FORMAT, MEASURED 2026-08-16. *** "Report User"
#: in the chat window sends an ordinary SMTP mail whose BODY is a set of
#: pseudo-XML tags -- not MIME parts, not a form encoding, just tags on their own
#: lines. Captured from a real submission:
#:
#:     Subject: Chat Harassment>Viewer
#:     X-Mailer: SQUARE ENIX PlayOnline Mailer version 1.0000.000.3.1.18.15e
#:
#:     <harassment_form_sender>       the address the reporter TYPED
#:     <harassment_form_suspect>      the reported handle's display name
#:     <harassment_form_application>  "PlayOnline Chat"
#:     <harassment_form_explanation>  their free text
#:     <harassment_form_log>          the client's OWN transcript, multi-line:
#:                                    joins, aways, and each chat line, with the
#:                                    channel; '->' marks the selected line
#:
#: Two things worth knowing. The typed `sender` is NOT the envelope From -- that
#: is the account's real POL address -- so it is a contact hint, not proof of
#: identity. And the transcript is attached automatically; the reporter pastes
#: nothing.
_REPORT_TAG = re.compile(r"<(harassment_form_\w+)>(.*?)</\1>", re.S)
REPORT_DIR = os.environ.get(
    "POL_REPORT_DIR", os.path.join(os.environ.get("POL_DATA_DIR", "/data"),
                                   "reports"))


def _parse_report(body):
    """{tag: text} from a harassment-form body, or {} if it is not one."""
    text = body.decode("utf-8", "replace") if isinstance(
        body, (bytes, bytearray)) else str(body)
    return {m.group(1)[len("harassment_form_"):]: m.group(2).strip()
            for m in _REPORT_TAG.finditer(text)}


def _archive_report(rcpt, local, sender, subject, body, peer):
    """File a parsed report so the dashboard can show it without parsing mail.

    The message itself is in the mailbox either way; this is the readable index.
    Never raises -- an archiving fault must not fail the SMTP transaction that has
    already been accepted.
    """
    try:
        fields = _parse_report(body)
        if not fields:
            return
        rec = {"received_at": _stamp(), "to": rcpt, "delivered_to": local,
               "from": sender, "subject": subject, "peer": peer,
               "fields": fields}
        os.makedirs(REPORT_DIR, exist_ok=True)
        name = "%s-%s.json" % (
            _stamp().replace(":", "").replace("-", "").replace(".", ""),
            hashlib.sha1(bytes(body)).hexdigest()[:8])
        path = os.path.join(REPORT_DIR, name)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        log("mail", f"{peer} REPORT filed: {fields.get('suspect', '?')!r} reported "
                    f"via {fields.get('application', '?')!r} -- {os.path.basename(path)}")
    except Exception as exc:
        log("mail", f"{peer} could not archive the report ({exc!r}); the message "
                    "is still in the mailbox")


def _smtp_is_outside(to):
    """True for a recipient that would leave the server: not our domain, not
    the outside-domain name of a local box, not an alias."""
    dom = to.split("@", 1)[1].lower() if "@" in to else MAIL_DOMAIN.lower()
    if dom == MAIL_DOMAIN.lower() or to.strip().lower() in _mail_aliases():
        return False
    return not (extmail is not None and dom == extmail.domain())


def _smtp_sender_row(db, mail_from):
    row = accounts.member_by_mail(db, mail_from) if mail_from else None
    return row if row is not None or not mail_from \
        else _mail_polid_member(db, mail_from)


def _ext_rcpt_check(mail_from, n_outside):
    """(ok, smtp reply, why) for one more OUTSIDE recipient.

    Accepting an outside recipient with `250` and dropping it at DATA would
    show the player a sent message nobody receives, so it is refused at RCPT,
    visibly, unless the sender
    is a member the admin panel has enabled for outside mail and still has
    room under today's cap.
    """
    if extmail is None or accounts is None:
        return False, b"550 5.7.1 mail outside PlayOnline is not available\r\n", "no extmail"
    db = _mail_db()
    if db is None:
        return False, b"451 4.3.0 try again later\r\n", "no account DB"
    try:
        row = _smtp_sender_row(db, mail_from)
        if not extmail.enabled(row):
            return False, (b"550 5.7.1 this account cannot mail outside PlayOnline\r\n"
                           ), "sender not enabled for outside mail"
        if not extmail.outbound_configured():
            return False, (b"550 5.7.1 mail outside PlayOnline is not available\r\n"
                           ), "POL_EXT_MAIL_KEY not set"
        if not extmail.outside_address(row):
            return False, b"550 5.7.1 this account has no mail name\r\n", "no mail name"
        if n_outside >= extmail.MAX_RCPTS:
            return False, b"452 4.5.3 too many outside recipients\r\n", "recipient limit"
        if extmail.room_today(db, row["id"], "out") <= n_outside:
            return False, (b"550 5.7.1 daily limit for outside mail reached\r\n"
                           ), "daily cap"
        return True, b"250 OK\r\n", "outside, enabled"
    finally:
        db.close()


def _ext_relay(mail_from, remote, body, peer):
    """(ok, detail): send the outside recipients through the provider."""
    db = _mail_db()
    if db is None:
        return False, "no account DB"
    try:
        row = _smtp_sender_row(db, mail_from)
        if not extmail.enabled(row):                 # re-checked: RCPT was earlier
            return False, "sender not enabled"
        frm, subject, text = extmail.outbound_parts(body, row)
        ok, detail = extmail.send(frm, remote, subject, text,
                                  extmail.outside_address(row))
        for r in remote:
            extmail.note(db, row["id"], "out", r, ok, detail)
        log("mail", f"{peer} SMTP outside {'SENT' if ok else 'FAILED'} as {frm!r} "
                    f"to {remote} ({detail})")
        return ok, detail
    finally:
        db.close()


def handle_smtp(conn, addr, port=25):
    """A minimal SMTP server. Mail addressed to our own domain is DELIVERED into
    the recipient's mailbox (so PlayOnline members can mail each other, and
    mail-to-self works as a test); anything else is logged and dropped rather
    than relayed off-box."""
    peer = f"{addr[0]}:{addr[1]}->{port}"
    conn.settimeout(_MAIL_IDLE)          # see the POP3 handler's note
    f = _MailTrace(conn.makefile("rwb"), "SMTP", peer)
    mail_from, rcpts = "", []
    try:
        f.write(b"220 PlayOnline Mail ESMTP\r\n")
        f.flush()
        while True:
            line = f.readline()
            if not line:
                break
            cmd = line.rstrip(b"\r\n")
            log("mail", f"{peer} SMTP C: {cmd.decode('ascii', 'replace')!r}")
            up = cmd.upper()
            text = cmd.decode("ascii", "replace")
            if up.startswith(b"EHLO") or up.startswith(b"HELO"):
                # No AUTH advertised: the client only offers AUTH CRAM-MD5, and
                # only when its own SMTP-auth setting says so (app.dll RVA
                # 0x28da70 state 0x16). Advertising it would invite a login we
                # cannot check any better than we check APOP.
                f.write(b"250-PlayOnline Mail\r\n250 OK\r\n")
            elif up.startswith(b"MAIL FROM"):
                mail_from = _smtp_addr(text)
                rcpts = []
                ok, why = _smtp_sender_allowed(mail_from, addr[0])
                if not ok:
                    log("mail", f"{peer} SMTP MAIL FROM {mail_from!r} REFUSED ({why})")
                    mail_from = ""
                    f.write(b"550 sender not permitted\r\n")
                else:
                    f.write(b"250 OK\r\n")
            elif up.startswith(b"RCPT TO"):
                to = _smtp_addr(text)
                if not to:
                    f.write(b"501 bad recipient\r\n")
                elif _smtp_is_outside(to):
                    ok, reply, why = _ext_rcpt_check(
                        mail_from, sum(1 for r in rcpts if _smtp_is_outside(r)))
                    log("mail", f"{peer} SMTP RCPT {to!r} outside: "
                                f"{'accepted' if ok else 'REFUSED'} ({why})")
                    if ok:
                        rcpts.append(to)
                    f.write(reply)
                else:
                    rcpts.append(to)
                    f.write(b"250 OK\r\n")
            elif up.startswith(b"DATA"):
                if not rcpts:
                    f.write(b"503 need RCPT first\r\n")
                    f.flush()
                    continue
                f.write(b"354 End data with <CR><LF>.<CR><LF>\r\n")
                f.flush()
                body = bytearray()
                too_big = False
                while True:
                    ln = f.readline()
                    if not ln or ln.rstrip(b"\r\n") == b".":
                        break
                    # Undo RFC 5321 dot-stuffing.
                    body += ln[1:] if ln.startswith(b"..") else ln
                    # A DATA block with no terminator used to grow until the
                    # process died. Keep READING to the end of the message (so
                    # the session stays in sync and can be told what happened),
                    # but stop storing.
                    if len(body) > _MAIL_MAX_BYTES:
                        too_big = True
                        del body[_MAIL_MAX_BYTES:]
                if too_big:
                    log("mail", f"{peer} SMTP message over "
                                f"{_MAIL_MAX_BYTES // 1024} KB -- refused")
                    rcpts = []
                    f.write(b"552 message too large\r\n")
                    f.flush()
                    continue
                log("mail", f"{peer} SMTP message from {mail_from!r} to {rcpts} "
                            f"({len(body)}B):\n"
                            + body.decode("cp932", "replace")[:4000])
                # THE HEADER `From:` IS WHAT THE RECIPIENT SEES, so it must name
                # the envelope sender we just admitted -- a checked envelope with
                # a free header is no check at all. The Viewer always writes its
                # own address there.
                hdr_from = _smtp_addr(_smtp_header(body, "From") or "")
                if mail_from and hdr_from and hdr_from.lower() != mail_from.lower() \
                        and os.environ.get("POL_MAIL_SENDER_CHECK", "1") == "1":
                    log("mail", f"{peer} SMTP REFUSED: header From {hdr_from!r} "
                                f"is not the envelope sender {mail_from!r}")
                    rcpts = []
                    f.write(b"550 From header does not match the sender\r\n")
                    f.flush()
                    continue
                done, remote = _smtp_deliver(rcpts, body, peer)
                rcpts = []
                if remote:
                    # Only recipients that passed _ext_rcpt_check reach here.
                    ok, detail = _ext_relay(mail_from, remote, body, peer)
                    if not ok:
                        f.write(b"554 5.4.0 could not deliver outside PlayOnline\r\n")
                        f.flush()
                        continue
                f.write(f"250 OK stored ({done} local, {len(remote)} outside)\r\n"
                        .encode("ascii"))
            elif up.startswith(b"RSET"):
                mail_from, rcpts = "", []
                f.write(b"250 OK\r\n")
            elif up.startswith(b"QUIT"):
                f.write(b"221 bye\r\n")
                f.flush()
                break
            else:
                f.write(b"250 OK\r\n")
            f.flush()
    except Exception as exc:
        log("mail", f"{peer} SMTP error: {exc!r}")
    finally:
        try:
            conn.close()
        except Exception:
            pass
