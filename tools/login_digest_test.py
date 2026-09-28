#!/usr/bin/env python3
"""The Viewer's password, checked for real: NICK digest = md5(greeting[:40] + pw).

    python tools/login_digest_test.py

Without it the lobby compares only the NICK blob's 11-char token, which is per
(handle x client build) and not per password -- so any password, even a blank
one, logs in. The 32-hex NICK field is the real check; the formula comes from
Project Crystal Server. It is pinned here to KNOWN-ANSWER vectors: made-up
greetings and a made-up password, with the expected digests written in as
literals (computed once, by hand, with hashlib -- never by the code under
test), plus controls that a deliberately wrong formula does NOT match.
"""
import hashlib
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))

tmp = tempfile.mkdtemp(prefix="digesttest-")
import pgtest  # noqa: E402
pgtest.use_fresh_database()
os.environ["POL_STAMP_FILE"] = os.path.join(tmp, "stamps.json")
os.environ.pop("POL_LOGIN_PW_KEY", None)
# The key's DEFAULT home: the data directory, where accounts.db used to sit.
os.environ.pop("POL_LOGIN_PW_KEYFILE", None)
os.environ["POL_DATA_DIR"] = tmp

import accounts  # noqa: E402
import responders  # noqa: E402
from authtoken import build_session_token  # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


print("the formula, against known-answer vectors")
#: Made-up 44-char greetings (the `300 *` token shape) and a made-up password.
#: DIGESTS are md5(greeting[:40] + PW) as lower-case hex, computed once by hand.
PW = "Tulip-Anvil-42"
VECTORS = [
    ("QK4TBZ7M2XRD9FWN6CPL3VHJ8GSA5EUY1OTIQK4TVSQA",
     "3ad51aa2dd99977d69381eaa1f69111c"),
    ("ZM3PL8QW1NXC6VBR4KTD9HJF2GSY7EUA5OIWZM3PDDQE",
     "fc04292e3ef556e73d66c9d0145b473e"),
]
for greeting, dig in VECTORS:
    tag = greeting[:6]
    chk("%s: right password" % tag,
        accounts.login_digest_ok(dig, [greeting], PW), greeting)
    chk("%s: login_digest reproduces the literal" % tag,
        accounts.login_digest(greeting, PW), dig)
    chk("%s: upper-case hex from the client is accepted" % tag,
        accounts.login_digest_ok(dig.upper(), [greeting], PW), greeting)
    for wrong in (PW.lower(), PW[:-1], ""):
        chk("%s: wrong password %r" % (tag, wrong),
            accounts.login_digest_ok(dig, [greeting], wrong), None)
    other = VECTORS[1][0] if greeting == VECTORS[0][0] else VECTORS[0][0]
    chk("%s: right password, wrong greeting" % tag,
        accounts.login_digest_ok(dig, [other], PW), None)
    # NEGATIVE CONTROLS: a changed formula must not reproduce the literal.
    wrong_formulas = {
        "greeting[:32]": hashlib.md5((greeting[:32] + PW).encode()).hexdigest(),
        "whole greeting": hashlib.md5((greeting + PW).encode()).hexdigest(),
        "password first": hashlib.md5((PW + greeting[:40]).encode()).hexdigest(),
        "sha1[:32]": hashlib.sha1((greeting[:40] + PW).encode()).hexdigest()[:32],
    }
    for name, d in wrong_formulas.items():
        chk("%s: control, %s does not give the pinned digest" % (tag, name),
            d == dig, False)
        chk("%s: control, %s's digest is refused" % (tag, name),
            accounts.login_digest_ok(d, [greeting], PW), None)

print("sealing")
db = accounts.connect()
sealed = accounts.seal_login_password(db, "p@ss!w0rd#1")
chk("the sealed form does not contain the password", "p@ss!w0rd#1" in sealed, False)
chk("round trip", accounts.unseal_login_password(db, sealed), "p@ss!w0rd#1")
chk("a key file was made in the data directory",
    os.path.exists(os.path.join(tmp, "login-pw.key")), True)
tampered = sealed[:-4] + ("AAAA" if not sealed.endswith("AAAA") else "BBBB")
chk("a tampered copy is refused, not mis-read",
    accounts.unseal_login_password(db, tampered), None)
os.environ["POL_LOGIN_PW_KEY"] = "some other key"
chk("a different key cannot open it", accounts.unseal_login_password(db, sealed), None)
os.environ.pop("POL_LOGIN_PW_KEY")

print("capture points")
acct = accounts.register_account(db, "Digesty", PW, contents=(1,))
mid = acct["member_id"]
chk("register_account keeps a copy", accounts.get_login_password(db, mid),
    PW)
accounts.set_account_password(db, acct["polid"], "Maple-Otter-17")
chk("set_account_password replaces it", accounts.get_login_password(db, mid),
    "Maple-Otter-17")
db.execute("UPDATE member SET login_pw_sealed = NULL WHERE id = %s", (mid,))
db.commit()
accounts.verify_member(db, acct["polid"], "wrongwrong")
chk("a FAILED sign-in stores nothing", accounts.get_login_password(db, mid),
    None)
accounts.verify_member(db, acct["polid"], "Maple-Otter-17")
chk("a sign-in stores it for an older account",
    accounts.get_login_password(db, mid), "Maple-Otter-17")
accounts.set_account_password(db, acct["polid"], PW)

print("the lobby login (resolve_account)")
nick = accounts.get_member(db, acct["polid"])["login_name"]
PC = "TTTTTAISTTTTTTTTTTTTT"
PS2 = "TTTTT7ITTTGaItbIQ8nHA"
salts = [VECTORS[0][0]]
good = VECTORS[0][1]
WRONG = accounts.login_digest(salts[0], "notmypassword")


def login(digest, sig, cred="tokenAAAAAA"):
    conn, member, reject = responders.resolve_account(
        nick, "127.0.0.9", b"\0" * 8, cred=cred, client_sig=sig,
        digest=digest, salts=salts)
    if conn is not None:
        conn.close()
    return member is not None, reject


chk("right password, never-played account whose arm LAPSED -> in",
    (db.execute("UPDATE member SET token_arm_until = NULL WHERE id = %s", (mid,)),
     db.commit(), login(good, PC))[2], (True, None))
chk("...and that proved the PC build", accounts.digest_client_proven(db, PC), True)
chk("wrong password on the proven PC build -> refused 0xCA",
    login(WRONG, PC), (False, responders.REJECT_BAD_PASSWORD))
chk("blank password (its digest) on the proven build -> refused",
    login(accounts.login_digest(salts[0], ""), PC),
    (False, responders.REJECT_BAD_PASSWORD))
chk("right password, token MOVED on a known client -> in, re-bound",
    login(good, PC, cred="tokenBBBBBB"), (True, None))
chk("...the client row now holds the new token",
    accounts.get_client_token(db, mid, PC), "tokenBBBBBB")

accounts.set_client_token(db, mid, PS2, "tokenPS2PS2")
chk("wrong digest on an UNPROVEN build falls back to the token (in)",
    login(WRONG, PS2, cred="tokenPS2PS2"), (True, None))
chk("...and does not prove it", accounts.digest_client_proven(db, PS2), False)
os.environ["POL_LOGIN_DIGEST_ENFORCE"] = "all"
chk("POL_LOGIN_DIGEST_ENFORCE=all refuses it anyway",
    login(WRONG, PS2, cred="tokenPS2PS2"), (False, responders.REJECT_BAD_PASSWORD))
os.environ.pop("POL_LOGIN_DIGEST_ENFORCE")
chk("right digest proves the PS2 build", login(good, PS2, cred="tokenPS2PS2"),
    (True, None))
chk("...after which a wrong one is refused",
    login(WRONG, PS2, cred="tokenPS2PS2"), (False, responders.REJECT_BAD_PASSWORD))

db.execute("UPDATE member SET login_pw_sealed = NULL WHERE id = %s", (mid,))
db.commit()
chk("no copy held -> token check only (unchanged behaviour)",
    login(WRONG, PC, cred="tokenBBBBBB"), (True, None))
os.environ["POL_LOGIN_DIGEST"] = "0"
accounts.set_account_password(db, acct["polid"], PW)
chk("POL_LOGIN_DIGEST=0 turns the check off",
    login(WRONG, PC, cred="tokenBBBBBB"), (True, None))
os.environ.pop("POL_LOGIN_DIGEST")

print("salt candidates")
responders.remember_stamp("127.0.0.7", 0x51A7C0DE)
cands = responders.login_digest_salts("127.0.0.7", ["REDIRECTTOKEN", "SESSIONTOKEN"])
chk("this hop's greetings first, newest first", cands[:2],
    ["SESSIONTOKEN", "REDIRECTTOKEN"])
chk("then tokens issued to this address earlier",
    build_session_token(0x51A7C0DE) in cands, True)

db.close()
print("FAILED: %d" % bad if bad else "all ok")
sys.exit(1 if bad else 0)
