#!/usr/bin/env python3
"""Failed-login lockout (SE status 0xCB) and the refusal record's tail.

    python tests/test_login_lockout.py

After POL_LOGIN_LOCKOUT_FAILS (5) passwords the NICK digest REFUSED within
POL_LOGIN_LOCKOUT_WINDOW_S (900 s), a member's logins are refused with 0xCB --
the right password too -- until the failures age out or `accounts.py unlock`.
Only real digest refusals count: token mismatches and unproven builds never do.
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.join(HERE, "..", "services")
sys.path.insert(0, SERVICES)

tmp = tempfile.mkdtemp(prefix="lockout-")
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import pgtest  # noqa: E402
pgtest.use_fresh_database()
os.environ["POL_DATA_DIR"] = tmp
os.environ["POL_LOG_DIR"] = tmp
for k in ("POL_LOGIN_PW_KEY", "POL_LOGIN_LOCKOUT_FAILS",
          "POL_LOGIN_LOCKOUT_WINDOW_S", "POL_LOGIN_DIGEST_ENFORCE",
          "POL_REJECT_TAIL"):
    os.environ.pop(k, None)

import accounts  # noqa: E402
import responders  # noqa: E402
from authtoken import build_session_token, token_encode  # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


db = accounts.connect()
acct = accounts.register_account(db, "Locky", "Passw0rdTest", contents=(1,))
mid = acct["member_id"]
nick = accounts.get_member(db, acct["polid"])["login_name"]
PC = "TTTTTAISTTTTTTTTTTTTT"
PS2 = "TTTTT7ITTTGaItbIQ8nHA"
salts = [build_session_token(0x6a7de436)]
GOOD = accounts.login_digest(salts[0], "Passw0rdTest")
WRONG = accounts.login_digest(salts[0], "notmypassword")
CA, CB = responders.REJECT_BAD_PASSWORD, responders.REJECT_LOCKED


def login(digest, sig=PC, cred="tokenAAAAAA"):
    conn, member, reject = responders.resolve_account(
        nick, "203.0.113.9", b"\0" * 8, cred=cred, client_sig=sig,
        digest=digest, salts=salts)
    if conn is not None:
        conn.close()
    return reject


print("the codes")
chk("0xCB is the lockout byte", CB, 0xCB)
chk("and the rest are named", (responders.REJECT_VERSION, responders.REJECT_BUSY,
                               responders.REJECT_UNKNOWN_ERROR), (0xCC, 0xCD, 0xCE))

print("the lockout")
chk("right password proves the PC build", login(GOOD), None)
for i in range(1, 6):
    chk("wrong password #%d -> 0xCA" % i, login(WRONG), CA)
chk("the 6th attempt, wrong -> 0xCB", login(WRONG), CB)
chk("...and the RIGHT password is refused too while locked", login(GOOD), CB)
chk("a locked attempt adds no failure (still 5)",
    accounts.login_failures(db, mid, 900), 5)
db.execute("UPDATE login_fail SET at = at - 1000 WHERE member_id = %s", (mid,))
db.commit()
chk("once the failures age out of the window -> in", login(GOOD), None)
chk("...and the right password forgot them",
    accounts.login_failures(db, mid, 86400), 0)

for i in range(5):
    login(WRONG)
chk("locked again after 5", login(GOOD), CB)
out = subprocess.run([sys.executable, os.path.join(SERVICES, "accounts.py"),
                      "-", "unlock", acct["polid"]],
                     capture_output=True, text=True)
chk("`accounts.py <db> unlock <polid>` says it cleared 5",
    "cleared 5 failed" in out.stdout, True)
chk("...and the member is in again", login(GOOD), None)

print("what must NOT count (the twins)")
for i in range(4):
    login(WRONG)
chk("four failures, then the right password -> in (and reset)", login(GOOD),
    None)
chk("...counter back to 0", accounts.login_failures(db, mid, 900), 0)
accounts.set_client_token(db, mid, PS2, "tokenPS2PS2")
for i in range(8):
    login(WRONG, sig=PS2, cred="tokenPS2PS2")
chk("8 digest mismatches on an UNPROVEN build count nothing",
    accounts.login_failures(db, mid, 900), 0)
chk("...and do not lock the PC out", login(GOOD), None)
for i in range(8):
    login(None, cred="tokenZZZZZZZ")
chk("token mismatches (no digest) count nothing",
    accounts.login_failures(db, mid, 900), 0)
os.environ["POL_LOGIN_LOCKOUT_FAILS"] = "0"
for i in range(7):
    login(WRONG)
chk("POL_LOGIN_LOCKOUT_FAILS=0: never locked", login(GOOD), None)
os.environ["POL_LOGIN_LOCKOUT_FAILS"] = "2"
os.environ["POL_LOGIN_LOCKOUT_WINDOW_S"] = "60"
login(WRONG)
login(WRONG)
chk("knobs: 2 fails in 60 s lock it", login(GOOD), CB)
db.execute("UPDATE login_fail SET at = at - 61 WHERE member_id = %s", (mid,))
db.commit()
chk("...for 60 s", login(GOOD), None)
os.environ.pop("POL_LOGIN_LOCKOUT_FAILS")
os.environ.pop("POL_LOGIN_LOCKOUT_WINDOW_S")

print("the refusal record")
raw = bytearray(25)
raw[6] = CB
raw[12] = 0x02
chk("[16:24] (an SE account's MasterPolId in the capture) is zero now",
    responders.pol_reject_token(CB), token_encode(bytes(raw)))

db.close()
print("FAILED: %d" % bad if bad else "all ok")
sys.exit(1 if bad else 0)
