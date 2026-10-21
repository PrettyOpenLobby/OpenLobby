#!/usr/bin/env python3
"""The keepalive PING carries the server clock the way the client reads it.

    python tests/test_ping_clock.py

The client's PING handler (PS2 pol.pex 0x1249c0 -> 0x1248a0) ignores any
argument that is not 7 symbols; a 7-symbol one is decoded with the session
alphabet, checked with a byte of its own, and moves the client clock forward.
The decoder below is the client's, written from the disassembly, not ours.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import pgtest  # noqa: E402
pgtest.use_fresh_database()
import responders  # noqa: E402,F401
import sessioncrypt  # noqa: E402
from core import authresume  # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


def client_parse(arg):
    """0x1248a0: the clock value, or None when the client ignores the PING."""
    arg = arg[1:] if arg.startswith(":") else arg
    s = arg[:7]
    if len(s) != 7 or "." in s:
        return None
    inv = {c: i for i, c in enumerate(sessioncrypt.B64)}
    if any(c not in inv for c in s):
        return None
    out = bytearray()
    for grp in (s[:4], s[4:] + "T"):
        v = (inv[grp[0]] << 18) | (inv[grp[1]] << 12) | (inv[grp[2]] << 6) | inv[grp[3]]
        out += bytes(((v >> 16) & 255, (v >> 8) & 255, v & 255))
    b0, b1, b2, b3, b4 = out[:5]
    if b4 != (b0 ^ ((b1 ^ ((b2 ^ (b3 << 3)) << 3)) << 3)) & 0xFF:
        return None
    return (b0 << 24 | b1 << 16 | b2 << 8 | b3) & 0x7FFFFFFF


chk("worked example from the disassembly", authresume.ping_token(1791156967), "yjKx1Xi")
for t in (0, 1, 1791156967, 1791200000, 2**31 - 1, 1999999999):
    tok = authresume.ping_token(t)
    chk("t=%d: 7 symbols the client decodes back to t" % t, client_parse(tok), t)
chk("the old counter is ignored by the client", client_parse("POL6"), None)

print()
print("test_ping_clock: " + ("OK" if not bad else "FAILED (%d)" % bad))
sys.exit(1 if bad else 0)
