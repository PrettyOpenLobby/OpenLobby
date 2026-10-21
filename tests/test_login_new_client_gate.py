#!/usr/bin/env python3
"""A new client on an account that already holds a token needs a password proof.

    python tests/test_login_new_client_gate.py

POL_TOKEN_ARM_CLIENTS (on): an unseen client signature binds only with the
NICK digest proven or a live arm window. An account with no password copy
cannot prove a digest at all, so it keeps trust-on-first-use (logged) rather
than being refused after the next Viewer update; POL_TOKEN_ARM_NOCOPY=1
gates those too.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))
tmp = tempfile.mkdtemp(prefix="newclient-")
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import pgtest  # noqa: E402
pgtest.use_fresh_database()
os.environ["POL_DATA_DIR"] = tmp
os.environ["POL_LOG_DIR"] = tmp
for k in ("POL_LOGIN_PW_KEY", "POL_LOGIN_LOCKOUT_FAILS", "POL_TOKEN_ARM",
          "POL_TOKEN_ARM_CLIENTS", "POL_TOKEN_ARM_NOCOPY",
          "POL_LOGIN_DIGEST_ENFORCE"):
    os.environ.pop(k, None)

import accounts  # noqa: E402
import responders  # noqa: E402
from authtoken import build_session_token  # noqa: E402
from core import logingate  # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


db = accounts.connect()
salts = [build_session_token(0x6a7de436)]


def make(name, pw):
    a = accounts.register_account(db, name, pw, contents=(1,))
    return a, accounts.get_member(db, a["polid"])["login_name"]


def login(nick, sig, cred, digest=None):
    conn, member, reject, token = responders.resolve_account(
        nick, "203.0.113.9", b"\0" * 8, cred=cred, client_sig=sig,
        digest=digest, salts=salts)
    if token is not None:
        responders._finish_pending_session(token, member)
    if conn is not None:
        conn.close()
    return reject


def disarm(member_id):
    db.execute("UPDATE member SET token_arm_until = NULL WHERE id = %s", (member_id,))
    db.commit()


A, B, C = "TTTTTAISTTTTTTTTTTTTT", "TTTTT7ITTTGaItbIQ8nHA", "TTTTTBISTTTTTTTTTTTTT"
print("an account with a password copy")
acct, nick = make("Gatey", "Passw0rdGate")
good = accounts.login_digest(salts[0], "Passw0rdGate")
chk("holds a copy", accounts.get_login_password(db, acct["member_id"]) is not None, True)
chk("first client binds (armed at registration)", login(nick, A, "tokA00000001", good), None)
disarm(acct["member_id"])
chk("a new client with no proof is refused",
    login(nick, B, "tokB00000001"), logingate.REJECT_BAD_PASSWORD)
chk("the same new client with the password proven binds",
    login(nick, B, "tokB00000001", good), None)

print("an account with NO password copy")
acct2, nick2 = make("Copyless", "Passw0rdNone")
chk("first client binds", login(nick2, A, "tokA00000002"), None)
db.execute("UPDATE member SET login_pw_sealed = NULL WHERE id = %s", (acct2["member_id"],))
db.commit()
disarm(acct2["member_id"])
chk("a new client binds trust-on-first-use (nothing could prove it)",
    login(nick2, B, "tokB00000002"), None)
os.environ["POL_TOKEN_ARM_NOCOPY"] = "1"
chk("POL_TOKEN_ARM_NOCOPY=1 refuses it",
    login(nick2, C, "tokC00000002"), logingate.REJECT_BAD_PASSWORD)
del os.environ["POL_TOKEN_ARM_NOCOPY"]
os.environ["POL_TOKEN_ARM_CLIENTS"] = "0"
chk("POL_TOKEN_ARM_CLIENTS=0: the copy holder's new client binds unproven",
    login(nick, C, "tokC00000001"), None)

print()
print("test_login_new_client_gate: " + ("OK" if not bad else "FAILED (%d)" % bad))
sys.exit(1 if bad else 0)
