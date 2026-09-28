#!/usr/bin/env python3
"""Lobby send pacing follows the login NICK's client build, not our own echo.

    python tests/test_ps2_pacing.py

The lobby hello's magic[+0x09] is byte [6] of the auth record WE sent (the
account-status code in the "client IP" field), so it said PS2 for every client.
Pacing now reads the NICK client signature the auth hop records in the session;
with none known it keeps the old answer so a real PS2 is never left unpaced.
The record-layout callers of _peer_is_ps2() are deliberately unchanged.
"""
import os
import random
import socket
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))

tmp = tempfile.mkdtemp(prefix="pacing-")
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import pgtest  # noqa: E402
pgtest.use_fresh_database()
os.environ["POL_STAMP_FILE"] = os.path.join(tmp, "stamps.json")
os.environ["POL_SESSION_FILE"] = os.path.join(tmp, "auth-sessions.json")
os.environ["POL_DATA_DIR"] = tmp
os.environ["POL_LOG_DIR"] = tmp
os.environ["POL_CAPTURE"] = "0"
os.environ["POL_AUTH_OBSERVE"] = "1"
os.environ.pop("POL_PS2_DETECT", None)

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


PC = "TTTTTAISTTTTTTTTTTTTT"
PS2 = "TTTTT7ITTTGaItbIQ8nHA"

print("the pacing verdict")
for sid, sig in (("u-pc", PC), ("u-ps2", PS2), ("u-none", None),
                 ("u-odd", "TTTTTXXTTTTTTTTTTTTTT")):
    responders._session_put(sid, iv=os.urandom(8), client_sig=sig)
responders.session_bind("u-pc")
chk("PC signature, hello byte 0x00 -> NOT paced",
    responders._lobby_pace_ps2(0x00), False)
responders.session_bind("u-ps2")
chk("PS2 signature, hello byte 0x00 -> paced", responders._lobby_pace_ps2(0x00),
    True)
chk("PS2 signature wins over a PC-looking hello byte",
    responders._lobby_pace_ps2(0xFA), True)
responders.session_bind("u-none")
chk("no signature -> the old answer (0x00 = paced)",
    responders._lobby_pace_ps2(0x00), True)
chk("no signature -> the old answer (0xfa = not paced)",
    responders._lobby_pace_ps2(0xFA), False)
responders.session_bind("u-odd")
chk("unfamiliar signature -> the old answer", responders._lobby_pace_ps2(0x00),
    True)
responders.session_bind("u-pc")
os.environ["POL_PS2_DETECT"] = "magic"
chk("POL_PS2_DETECT=magic -> the hello byte alone",
    responders._lobby_pace_ps2(0x00), True)
os.environ.pop("POL_PS2_DETECT")

print("the record-layout gate is untouched")
responders._peer_build.ps2 = True
chk("_peer_is_ps2() still reads the hello byte on a PC session",
    responders._peer_is_ps2(), True)

print("the auth hop records the signature (socket pair)")
TOK = _tokens(0x33, 1)[0][0][:43].encode("latin-1")
IV = sessioncrypt.iv_from_user_token(TOK)
P0, S0 = sessioncrypt.bf_setkey(b"\0" * 8)
NICK = (b"NICK UTESTPS201:0123456789abcdef0123456789abcdef:" + PS2.encode()
        + b"TESTTOKEN12NNNN")
srv, cli = socket.socketpair()
t = threading.Thread(target=responders.handle_authserv,
                     args=(srv, ("203.0.113.6", 40001), 51241, "srv", 51242))
t.start()
cli.settimeout(10)
responders._recv_line(cli)
cli.sendall(b"USER x 8 * :" + TOK + b"\r\n")
responders._recv_line(cli)
cli.sendall(sessioncrypt.ofb_apply(P0, S0, IV, NICK) + b"\r\n")
try:
    cli.recv(4096)
except OSError:
    pass
t.join(30)
cli.close()
sid = responders._sid_for_user_token(TOK)
with responders._SESSIONS_LOCK:
    slot = dict(responders._SESSIONS.get(sid) or {})
chk("session slot carries the NICK's client signature", slot.get("client_sig"),
    PS2)
responders.session_bind(sid)
chk("...and a lobby connection bound to it paces as a PS2",
    responders._lobby_pace_ps2(0xFA), True)

print("FAILED: %d" % bad if bad else "all ok")
sys.exit(1 if bad else 0)
