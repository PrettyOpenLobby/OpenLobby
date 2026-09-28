#!/usr/bin/env python3
"""The auth hop's OFB IV comes from the USER line, not from a search.

    python tests/test_auth_user_iv.py

`USER x 8 * :<token>`: the first 8 bytes of the SE-base64 token are the IV the
client encrypts its NICK under (learned from Project Crystal Server). Checked
here on synthetic tokens (against logged real logins the same reading agreed
with recover_iv's brute-force answer on 464 of 470), and then driven over a
socket pair through handle_authserv itself, so the wire path is proved and not
just the helper.
"""
import os
import random
import socket
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))

tmp = tempfile.mkdtemp(prefix="useriv-")
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import pgtest  # noqa: E402
pgtest.use_fresh_database()
os.environ["POL_DATA_DIR"] = tmp
os.environ["POL_LOG_DIR"] = tmp
os.environ["POL_CAPTURE"] = "0"
os.environ["POL_AUTH_OBSERVE"] = "1"
os.environ.pop("POL_IV_FROM_USER", None)

import responders  # noqa: E402
import sessioncrypt  # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


def _tokens(seed, n):
    """Synthetic USER tokens: 32 random bytes in SE-base64, 43 symbols plus the
    client's 4-char line checksum, as a real client writes them."""
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        raw = bytes(rng.getrandbits(8) for _ in range(32))
        line = sessioncrypt.frame_line(
            ("USER x 8 * :" + sessioncrypt.b64encode(raw)[:43]).encode(), pad=b"")
        out.append((line.split(b":", 1)[1].decode("latin-1"), raw[:8].hex()))
    return out


print("the USER token's first 8 bytes")
PAIRS = _tokens(0x1F, 4)
for tok, iv in PAIRS:
    got = sessioncrypt.iv_from_user_token(tok)
    chk("token %s..." % tok[:8], got.hex() if got else None, iv)
    # the twin: one symbol changed must NOT give the logged IV
    mutated = ("S" if tok[3] != "S" else "T").join((tok[:3], tok[4:]))
    got = sessioncrypt.iv_from_user_token(mutated)
    chk("token %s... with [3] changed does not match" % tok[:8],
        got is not None and got.hex() == iv, False)
chk("bytes and str agree", sessioncrypt.iv_from_user_token(PAIRS[0][0].encode()),
    sessioncrypt.iv_from_user_token(PAIRS[0][0]))
chk("too short -> None", sessioncrypt.iv_from_user_token("TSG8In"), None)
chk("outside the alphabet -> None",
    sessioncrypt.iv_from_user_token("!!!!!!!!!!!!!!!"), None)

print("try_iv: one known IV, one block per key")
P0, S0 = sessioncrypt.bf_setkey(b"\0" * 8)
NICK = (b"NICK UTESTPC001:0123456789abcdef0123456789abcdef:"
        b"TTTTTAISTTTTTTTTTTTTTTESTTOKEN12NNNN")
iv = bytes.fromhex(PAIRS[2][1])
ct = sessioncrypt.ofb_apply(P0, S0, iv, NICK)
chk("right key + right IV reads the NICK",
    sessioncrypt.try_iv(P0, S0, ct, iv), (iv, NICK))
chk("right key + wrong IV does not",
    sessioncrypt.try_iv(P0, S0, ct, bytes.fromhex(PAIRS[3][1])), (None, None))
P1, S1 = sessioncrypt.bf_setkey(b"\x01" + b"\0" * 7)
chk("wrong key + right IV does not", sessioncrypt.try_iv(P1, S1, ct, iv),
    (None, None))


def login(user_token, nick_line, key=b"\0" * 8, iv=None):
    """Drive handle_authserv over a socket pair; return the authserv log text."""
    srv, cli = socket.socketpair()
    before = open(os.path.join(tmp, "authserv.log"), encoding="utf-8").read() \
        if os.path.exists(os.path.join(tmp, "authserv.log")) else ""
    t = threading.Thread(target=responders.handle_authserv,
                         args=(srv, ("203.0.113.5", 40000), 51241, "srv", 51242))
    t.start()
    cli.settimeout(10)
    responders._recv_line(cli)                         # greeting
    cli.sendall(b"USER x 8 * :" + user_token + b"\r\n")
    responders._recv_line(cli)                         # the key line
    P, S = sessioncrypt.bf_setkey(key)
    cli.sendall(sessioncrypt.ofb_apply(P, S, iv, nick_line) + b"\r\n")
    try:
        cli.recv(4096)
    except OSError:
        pass
    t.join(30)
    cli.close()
    after = open(os.path.join(tmp, "authserv.log"), encoding="utf-8").read()
    return after[len(before):]


print("handle_authserv over a socket pair")
tok = PAIRS[2][0].encode()
out = login(tok, NICK, iv=iv)
chk("IV taken from the USER line", "iv_via=USER" in out, True)
chk("...and it is the token's", "IV=%s nick=b'UTESTPC001'" % iv.hex() in out,
    True)
# The twin: a USER token whose IV is NOT the one the NICK was sent under must
# fall back to the search -- and the search still finds the real IV.
out = login(PAIRS[3][0].encode(), NICK, iv=iv)
chk("a USER token that does not fit -> search path", "iv_via=search" in out, True)
chk("...which still reads the NICK", "IV=%s nick=" % iv.hex() in out, True)
chk("...and says the USER IV did not fit", "fits no candidate key" in out or
    "decrypts the NICK under no candidate key" in out, True)
os.environ["POL_IV_FROM_USER"] = "0"
out = login(tok, NICK, iv=iv)
chk("POL_IV_FROM_USER=0 -> search path only", "iv_via=search" in out, True)
os.environ.pop("POL_IV_FROM_USER")

print("FAILED: %d" % bad if bad else "all ok")
sys.exit(1 if bad else 0)
