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
DB = os.path.join(_TMP, "accounts.db")
os.environ["POL_ACCOUNTS_DB"] = DB
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
cas = accounts.add_member(c, "MAILPOLID", "casmember", "pw-cas-0001")
bob = accounts.add_member(c, "MAILPOLID", "bobmember", "pw-bob-0001")
accounts.assign_mail_address(c, cas, "cas")
accounts.assign_mail_address(c, bob, "bob")
c.commit()
DOM = R.MAIL_DOMAIN
CAS, BOB = f"cas@{DOM}", f"bob@{DOM}"
HERE, ELSEWHERE = "198.51.100.5", "198.51.100.6"
BANNER = "<1.2@test>"


def apop(pw):
    return hashlib.md5((BANNER + pw).encode()).hexdigest()


print("\nNo password on file, no live session: nothing gets in")
check("POP3 PASS refused", R._mail_login_allowed("cas", HERE, "whatever")[0], False)
check("APOP refused", R._pop3_check_apop("cas", BANNER, apop("x"), HERE)[0], False)
check("SMTP MAIL FROM refused", R._smtp_sender_allowed(CAS, HERE)[0], False)
check("SMTP null sender passes (impersonates nobody)",
      R._smtp_sender_allowed("", HERE)[0], True)
check("SMTP foreign domain refused",
      R._smtp_sender_allowed("cas@example.com", HERE)[0], False)
check("SMTP unknown local address refused",
      R._smtp_sender_allowed(f"nobody@{DOM}", HERE)[0], False)

print("\nThe member's own signed-in Viewer (a live session from this address)")
accounts.open_session(c, cas, nick="Fox", peer_ip=HERE)
c.commit()
check("POP3 PASS from the session's address", R._mail_login_allowed("cas", HERE, "x")[0])
check("APOP from the session's address", R._pop3_check_apop("cas", BANNER, apop("x"), HERE)[0])
check("SMTP as cas from the session's address", R._smtp_sender_allowed(CAS, HERE)[0])
check("...also with the bare local part", R._smtp_sender_allowed("cas", HERE)[0])
check("POP3 PASS from ANOTHER address still refused",
      R._mail_login_allowed("cas", ELSEWHERE, "x")[0], False)
check("SMTP as cas from another address refused",
      R._smtp_sender_allowed(CAS, ELSEWHERE)[0], False)
check("the impersonation case: cas's box cannot send as bob",
      R._smtp_sender_allowed(BOB, HERE)[0], False)
check("...nor read bob's inbox", R._mail_login_allowed("bob", HERE, "x")[0], False)

print("\nA stored mail password is checked, and then the address stops mattering")
accounts.set_mail_password(c, cas, "mailpw-cas")
c.commit()
check("PASS right password, other address", R._mail_login_allowed("cas", ELSEWHERE, "mailpw-cas")[0])
check("PASS wrong password, own address", R._mail_login_allowed("cas", HERE, "nope")[0], False)
check("APOP right digest, other address",
      R._pop3_check_apop("cas", BANNER, apop("mailpw-cas"), ELSEWHERE)[0])
check("APOP wrong digest, own address",
      R._pop3_check_apop("cas", BANNER, apop("nope"), HERE)[0], False)
check("PASS is constant-time-compared, not prefix-matched",
      R._mail_login_allowed("cas", HERE, "mailpw-ca")[0], False)
accounts.set_mail_password(c, cas, "hashed-only", store_plain=False)
c.commit()
check("PASS against the hash when no plaintext is kept",
      R._mail_login_allowed("cas", ELSEWHERE, "hashed-only")[0])
check("APOP cannot be verified against a hash -> session rule (own address ok)",
      R._pop3_check_apop("cas", BANNER, apop("hashed-only"), HERE)[0])
check("APOP cannot be verified against a hash -> session rule (other address no)",
      R._pop3_check_apop("cas", BANNER, apop("hashed-only"), ELSEWHERE)[0], False)

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
check("unknown mailbox is refused when nothing vouches for it (not strict -> allowed as before)",
      R._mail_login_allowed("ghost", HERE, "x")[0], True)

c.close()
print()
if FAILED:
    print(f"FAILED {len(FAILED)}: " + "; ".join(FAILED))
    sys.exit(1)
print("mail_auth_test: all checks passed")
