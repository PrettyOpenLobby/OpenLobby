#!/usr/bin/env python3
"""A jailed account logs in and is told so, the way the client expects.

    python tests/test_login_jail.py

Jail is not a refusal code. polcore 0x037db6f0 copies byte +0x21 of the
successful login's gate record to its jail flag, and app.dll then opens
POL_JAIL_USER_LOGIN_URL (snews/<lang>/jail.pml). The accounts table always had
a `jail` state, but every non-active member was refused with 0xC9, so the
state could never reach a client.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))
tmp = tempfile.mkdtemp(prefix="jail-")
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import pgtest  # noqa: E402
pgtest.use_fresh_database()
os.environ["POL_DATA_DIR"] = tmp
os.environ["POL_LOG_DIR"] = tmp
for k in ("POL_LOGIN_PW_KEY", "POL_LOGIN_LOCKOUT_FAILS", "POL_JAIL",
          "POL_LOGIN_LOCKOUT_WINDOW_S", "POL_LOGIN_DIGEST_ENFORCE"):
    os.environ.pop(k, None)

import accounts  # noqa: E402
import responders  # noqa: E402
from authtoken import build_session_token  # noqa: E402
from core import authserv, logingate  # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


db = accounts.connect()
acct = accounts.register_account(db, "Jaily", "Passw0rdJail", contents=(1,))
nick = accounts.get_member(db, acct["polid"])["login_name"]
salts = [build_session_token(0x6a7de436)]
GOOD = accounts.login_digest(salts[0], "Passw0rdJail")


def login():
    conn, member, reject, token = responders.resolve_account(
        nick, "203.0.113.9", b"\0" * 8, cred="tokenAAAAAA",
        client_sig="TTTTTAISTTTTTTTTTTTTT", digest=GOOD, salts=salts)
    if token is not None:
        responders._finish_pending_session(token, member)
    if conn is not None:
        conn.close()
    return reject


def member():
    return accounts.get_member(db, acct["polid"])


def decode(text):
    alpha = logingate._B64
    out = bytearray()
    for i in range(0, len(text), 4):
        v = 0
        for ch in text[i:i + 4]:
            v = (v << 6) | alpha.index(ch)
        out += bytes(((v >> 16) & 255, (v >> 8) & 255, v & 255))
    return bytes(out)


print("the gate record")
raw = decode(logingate.build_gate_record("192.0.2.1", 51220, pad_to=0))
chk("no jail: +0x21 is 0", raw[0x21], 0)
raw = decode(logingate.build_gate_record("192.0.2.1", 51220, pad_to=0,
                                                       jail=True))
chk("jail: +0x21 is 1", raw[0x21], 1)
chk("the fields the branch needs are still set",
    (raw[0x10], raw[0x1b], raw[0x3e] | raw[0x3f] << 8, raw[0x42]), (1, 1, 0x0f80, 1))

print("the login")
chk("an active account logs in", login(), None)
chk("and is not jailed", authserv._member_jailed(db, member()), False)
db.execute("UPDATE member SET status = 'jail' WHERE id = %s", (acct["member_id"],))
db.commit()
chk("a jailed member is NOT refused", login(), None)
chk("and is jailed", authserv._member_jailed(db, member()), True)
os.environ["POL_JAIL"] = "0"
chk("POL_JAIL=0: the old refusal", login(), logingate.REJECT_UNKNOWN_ID)
del os.environ["POL_JAIL"]
db.execute("UPDATE member SET status = 'suspended' WHERE id = %s", (acct["member_id"],))
db.commit()
chk("a suspended member is still refused", login(), logingate.REJECT_UNKNOWN_ID)
db.execute("UPDATE member SET status = 'active' WHERE id = %s", (acct["member_id"],))
db.execute("UPDATE polid SET status = 'jail' WHERE polid = %s", (acct["polid"],))
db.commit()
chk("a jailed POL ID jails its member", authserv._member_jailed(db, member()), True)
chk("and logs in", login(), None)
accounts.set_reject_code(db, acct["polid"], 0xE7)
chk("an explicit refusal still wins over jail", login(), 0xE7)

print()
print("test_login_jail: " + ("OK" if not bad else "FAILED (%d)" % bad))
sys.exit(1 if bad else 0)
