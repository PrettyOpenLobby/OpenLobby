#!/usr/bin/env python3
"""Pin the mail handlers' credential rules (POP3 USER/PASS + APOP, SMTP MAIL FROM).

WHY THIS EXISTS. Until 2026-09-05 POP3 `USER <name>` / `PASS anything` opened
any member's inbox, APOP was checked only for accounts that had opted into a
plaintext mail password, and SMTP took any `MAIL FROM` -- so any peer on the
tailnet could read anyone's mail and send as anyone. The Viewer never needed
that laxity: PlayOnline Mail runs inside a signed-in Viewer, and the session
table records the address each session was opened from. The rules now are:

  * a stored mail password is CHECKED (plaintext for APOP/PASS, the PBKDF2
    hash for PASS when no plaintext is kept);
  * with no password on file, the peer must hold the mailbox owner's LIVE
    session (accounts.member_online_from) -- both to read and to send as them;
  * POL_MAIL_STRICT=1 refuses password-less accounts outright;
  * POL_MAIL_SESSION_CHECK=0 / POL_MAIL_SENDER_CHECK=0 restore the old
    accept-anything behaviour, and are the escape hatch if a real client is
    ever refused.

Run: python tools/mail_auth_test.py
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "services"))

_TMP = tempfile.mkdtemp(prefix="mailauth-")
import pgtest  # noqa: E402
DB = pgtest.use_fresh_database()
os.environ["POL_ACCOUNTS"] = "1"
os.environ.pop("POL_MAIL_STRICT", None)
os.environ.pop("POL_MAIL_SESSION_CHECK", None)
os.environ.pop("POL_MAIL_SENDER_CHECK", None)
os.environ.setdefault("POL_LOG_DIR", os.path.join(_TMP, "logs"))

import accounts  # noqa: E402
import responders as R  # noqa: E402

FAILED = []


def check(label, got, want=True):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label
          + ("" if ok else f": {got!r}  (want {want!r})"))
    if not ok:
        FAILED.append(label)


c = accounts.connect(DB)
accounts.create_polid(c, "MAILPOLID", "polid-pw-1")
lex = accounts.add_member(c, "MAILPOLID", "casmember", "pw-lex-0001")
bob = accounts.add_member(c, "MAILPOLID", "bobmember", "pw-bob-0001")
accounts.assign_mail_address(c, lex, "lex")
accounts.assign_mail_address(c, bob, "bob")
c.commit()
DOM = R.MAIL_DOMAIN
LEX, BOB = f"lex@{DOM}", f"bob@{DOM}"
HERE, ELSEWHERE = "198.51.100.5", "198.51.100.6"
BANNER = "<1.2@test>"


def apop(pw):
    return hashlib.md5((BANNER + pw).encode()).hexdigest()


print("\nNo password on file, no live session: nothing gets in")
check("POP3 PASS refused", R._mail_login_allowed("lex", HERE, "whatever")[0], False)
check("APOP refused", R._pop3_check_apop("lex", BANNER, apop("x"), HERE)[0], False)
check("SMTP MAIL FROM refused", R._smtp_sender_allowed(LEX, HERE)[0], False)
check("SMTP null sender passes (impersonates nobody)",
      R._smtp_sender_allowed("", HERE)[0], True)
check("SMTP foreign domain refused",
      R._smtp_sender_allowed("lex@example.com", HERE)[0], False)
check("SMTP unknown local address refused",
      R._smtp_sender_allowed(f"nobody@{DOM}", HERE)[0], False)

print("\nThe member's own signed-in Viewer (a live session from this address)")
accounts.open_session(c, lex, nick="Lex", peer_ip=HERE)
c.commit()
check("POP3 PASS from the session's address", R._mail_login_allowed("lex", HERE, "x")[0])
check("APOP from the session's address", R._pop3_check_apop("lex", BANNER, apop("x"), HERE)[0])
check("SMTP as lex from the session's address", R._smtp_sender_allowed(LEX, HERE)[0])
check("...also with the bare local part", R._smtp_sender_allowed("lex", HERE)[0])
check("POP3 PASS from ANOTHER address still refused",
      R._mail_login_allowed("lex", ELSEWHERE, "x")[0], False)
check("SMTP as lex from another address refused",
      R._smtp_sender_allowed(LEX, ELSEWHERE)[0], False)
check("the impersonation case: lex's box cannot send as bob",
      R._smtp_sender_allowed(BOB, HERE)[0], False)
check("...nor read bob's inbox", R._mail_login_allowed("bob", HERE, "x")[0], False)

print("\nA stored mail password is checked, and then the address stops mattering")
accounts.set_mail_password(c, lex, "mailpw-lex")
c.commit()
check("PASS right password, other address", R._mail_login_allowed("lex", ELSEWHERE, "mailpw-lex")[0])
check("PASS wrong password, own address", R._mail_login_allowed("lex", HERE, "nope")[0], False)
check("APOP right digest, other address",
      R._pop3_check_apop("lex", BANNER, apop("mailpw-lex"), ELSEWHERE)[0])
check("APOP wrong digest, own address",
      R._pop3_check_apop("lex", BANNER, apop("nope"), HERE)[0], False)
check("PASS is constant-time-compared, not prefix-matched",
      R._mail_login_allowed("lex", HERE, "mailpw-ca")[0], False)
accounts.set_mail_password(c, lex, "hashed-only", store_plain=False)
c.commit()
check("PASS against the hash when no plaintext is kept",
      R._mail_login_allowed("lex", ELSEWHERE, "hashed-only")[0])
check("APOP cannot be verified against a hash -> session rule (own address ok)",
      R._pop3_check_apop("lex", BANNER, apop("hashed-only"), HERE)[0])
check("APOP cannot be verified against a hash -> session rule (other address no)",
      R._pop3_check_apop("lex", BANNER, apop("hashed-only"), ELSEWHERE)[0], False)

print("\nThe knobs")
os.environ["POL_MAIL_STRICT"] = "1"
check("STRICT: bob (no password) refused even from his session's address",
      (accounts.open_session(c, bob, nick="Bob", peer_ip=HERE), c.commit(),
       R._mail_login_allowed("bob", HERE, "x")[0])[-1], False)
check("STRICT: APOP for bob refused too", R._pop3_check_apop("bob", BANNER, apop("x"), HERE)[0], False)
os.environ.pop("POL_MAIL_STRICT")
os.environ["POL_MAIL_SESSION_CHECK"] = "0"
check("SESSION_CHECK off: password-less login from anywhere (old behaviour)",
      R._mail_login_allowed("bob", ELSEWHERE, "x")[0])
check("SESSION_CHECK off: sender from anywhere", R._smtp_sender_allowed(BOB, ELSEWHERE)[0])
os.environ.pop("POL_MAIL_SESSION_CHECK")
os.environ["POL_MAIL_SENDER_CHECK"] = "0"
check("SENDER_CHECK off: foreign domain passes", R._smtp_sender_allowed("x@example.com", HERE)[0])
os.environ.pop("POL_MAIL_SENDER_CHECK")

print("\nSessions end, and the door closes with them")
accounts.close_sessions(c, bob)
c.commit()
check("bob logged out: refused", R._mail_login_allowed("bob", HERE, "x")[0], False)
check("unknown mailbox is refused, strict or not",
      R._mail_login_allowed("ghost", HERE, "x")[0], False)

print("\nA PlayOnline-type account (POP 51260) logs in as the POL ID")
# `APOP <POLID> <digest>`, digest = MD5(banner + MAIL password).
primary = accounts.member_by_polid(c, "MAILPOLID")
local = primary["mail_address"].split("@", 1)[0]
accounts.set_mail_password(c, primary["id"], "mailpw-51260")
c.commit()
check("POL ID resolves to its primary member's mailbox",
      R._pop3_mailbox_user("MAILPOLID"), local)
check("a mail name is left alone", R._pop3_mailbox_user("bob"), "bob")
check("a lowercase name is never taken as a POL ID (mail names are lowercase)",
      R._pop3_mailbox_user("mailpolid"), "mailpolid")
check("an unknown uppercase name is left alone", R._pop3_mailbox_user("NOSUCHID"), "NOSUCHID")
check("twin: the raw POL ID is still 'no such mailbox' without the resolver",
      R._pop3_check_apop("MAILPOLID", BANNER, apop("mailpw-51260"), ELSEWHERE)[0], False)
check("APOP as the POL ID with the mail password is accepted",
      R._pop3_check_apop(R._pop3_mailbox_user("MAILPOLID"), BANNER,
                         apop("mailpw-51260"), ELSEWHERE)[0])
check("APOP as the POL ID with a wrong password is refused",
      R._pop3_check_apop(R._pop3_mailbox_user("MAILPOLID"), BANNER,
                         apop("nope"), ELSEWHERE)[0], False)
check("a wrong APOP digest does not log 'verified'",
      R._pop3_check_apop(local, BANNER, apop("nope"), ELSEWHERE)[1] == "verified", False)

print("\n...and SENDS as <POLID>@pol.com (SMTP 51261)")
check("MAIL FROM the POL ID, no live session: refused",
      R._smtp_sender_allowed(f"MAILPOLID@{DOM}", ELSEWHERE)[0], False)
accounts.open_session(c, primary["id"], nick="Pri", peer_ip=ELSEWHERE)
c.commit()
check("MAIL FROM the POL ID from its member's live session: allowed",
      R._smtp_sender_allowed(f"MAILPOLID@{DOM}", ELSEWHERE)[0])
check("twin: a lowercase POL ID as sender is still refused",
      R._smtp_sender_allowed(f"mailpolid@{DOM}", ELSEWHERE)[0], False)
before = len(accounts.list_mail(c, primary["mail_address"]))
R._smtp_deliver([f"MAILPOLID@{DOM}"], b"From: x@pol.com\r\nSubject: re\r\n\r\nhi\r\n", "test")
R._smtp_deliver([f"mailpolid@{DOM}"], b"From: x@pol.com\r\nSubject: re2\r\n\r\nhi\r\n", "test")
check("mail TO <POLID>@pol.com (either case) lands in that member's real mailbox",
      len(accounts.list_mail(c, primary["mail_address"])) - before, 2)
check("...and no phantom box named after the POL ID",
      len(accounts.list_mail(c, f"mailpolid@{DOM}")), 0)
R._smtp_deliver([f"bob@{DOM}"], b"From: x@pol.com\r\nSubject: b\r\n\r\nhi\r\n", "test")
check("a real mailbox is untouched by the POL ID rule",
      len(accounts.list_mail(c, BOB)), 1)

c.close()
print()
if FAILED:
    print(f"FAILED {len(FAILED)}: " + "; ".join(FAILED))
    sys.exit(1)
print("mail_auth_test: all checks passed")
