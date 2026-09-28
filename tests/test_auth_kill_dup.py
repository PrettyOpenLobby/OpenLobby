#!/usr/bin/env python3
"""POL_AUTH_KILL_DUP: a new login of a member KILLs the older one.

    python tests/test_auth_kill_dup.py

SE (per Project Crystal Server) sends the old socket `KILL <nick>` and
`ERROR :Closing Link ... (Killed(Kicked by same NICK))`, closes it, and sends no
offline notification. Here games share the auth port and one account may run a
PC and a PS2 at once, so the default scope only kills the same client build
signing in from a different address under a different launch. Default OFF.
"""
import os
import random
import socket
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))

tmp = tempfile.mkdtemp(prefix="killdup-")
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import pgtest  # noqa: E402
pgtest.use_fresh_database()
os.environ["POL_DATA_DIR"] = tmp
os.environ["POL_LOG_DIR"] = tmp
os.environ["POL_CAPTURE"] = "0"
os.environ["POL_AUTH_KILL_SRVIP"] = "192.0.2.1"
os.environ["POL_ACCOUNTS_ENFORCE"] = "0"     # the test nick has no account
for k in ("POL_AUTH_KILL_DUP", "POL_AUTH_KILL_DUP_SCOPE", "POL_AUTH_MODE"):
    os.environ.pop(k, None)

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


P0, S0 = sessioncrypt.bf_setkey(b"\0" * 8)
PC = "TTTTTAISTTTTTTTTTTTTT"
PS2 = "TTTTT7ITTTGaItbIQ8nHA"
IV = bytes.fromhex("0102030405060708")


def chan(ip, sid, sig):
    a, b = socket.socketpair()
    s = responders.ChatSession(b"UKILLTEST", "pol-1000-51241.pol.com", ip, a,
                               P0, S0, IV)
    s.sid, s.client_sig = sid, sig
    return s, b


def lines_on(sock, wait=2.0):
    sock.settimeout(wait)
    buf = b""
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
    except OSError:
        pass
    return [sessioncrypt.ofb_apply(P0, S0, IV, l) for l in buf.split(b"\r\n") if l]


print("the kill itself")
MID = 9001
old, old_peer = chan("203.0.113.1", "u-old", PC)
new, _ = chan("203.0.113.2", "u-new", PC)
responders.PRESENCE.register(MID, old)
chk("same build, other launch, other address -> killed",
    responders._kill_duplicate_logins(MID, new), 1)
got = lines_on(old_peer)
want_kill = (b":pol-1000-51241.pol.com KILL UKILLTEST: 192.0.2.1!"
             b"pol-1000-51241.pol.com[unknown@192.0.2.1]!Kicked by same NICK")
want_err = (b"ERROR :Closing Link: UKILLTEST[~x@203.0.113.1] 192.0.2.1"
            b"(Killed(Kicked by same NICK))")
chk("line 1 = KILL, checksummed like the client's lines",
    got[0] if got else None, sessioncrypt.frame_line(want_kill, pad=b""))
chk("line 2 = ERROR :Closing Link ... (Killed(Kicked by same NICK))",
    got[1] if len(got) > 1 else None, sessioncrypt.frame_line(want_err, pad=b""))
chk("then the socket is closed", len(got), 2)
chk("the old channel is marked killed", (old.killed_dup, old.alive), (True, False))
responders.PRESENCE.unregister(MID, old)

print("what it must NOT kill (the twins)")
for what, ip, sid, sig in (
        ("same address (a game's band on the same PC)", "203.0.113.2", "u-o2", PC),
        ("different client build (PC + PS2 on one account)", "203.0.113.3", "u-o3",
         PS2),
        ("the same launch (another band of it)", "203.0.113.4", "u-new", PC),
        ("a channel with no known launch", "203.0.113.5", None, PC)):
    o, o_peer = chan(ip, sid, sig)
    responders.PRESENCE.register(MID, o)
    chk(what, responders._kill_duplicate_logins(MID, new), 0)
    chk("...and it heard nothing", lines_on(o_peer, 0.3), [])
    responders.PRESENCE.unregister(MID, o)
os.environ["POL_AUTH_KILL_DUP_SCOPE"] = "member"
o, o_peer = chan("203.0.113.2", "u-o6", PS2)
responders.PRESENCE.register(MID, o)
chk("POL_AUTH_KILL_DUP_SCOPE=member (Crystal's rule) kills any other launch",
    responders._kill_duplicate_logins(MID, new), 1)
responders.PRESENCE.unregister(MID, o)
os.environ.pop("POL_AUTH_KILL_DUP_SCOPE")

print("two real logins over socket pairs (welcome mode)")
os.environ["POL_AUTH_MODE"] = "welcome"
os.environ["POL_TOKEN_ARM"] = "0"          # a fresh test account, never armed
# A short keepalive tick: on Windows a shutdown() from another thread does not
# wake a blocked recv (Linux does), so the owning loop sees the kill on its tick.
os.environ["POL_AUTH_PING"] = "1"
NICK = (b"NICK UKILLTWO1:0123456789abcdef0123456789abcdef:" + PC.encode()
        + b"TESTTOKEN12NNNN")
TOKS = [t.encode("latin-1") for t, _iv in _tokens(0x2A, 4)]


def login(tok, ip):
    srv, cli = socket.socketpair()
    threading.Thread(target=responders.handle_authserv, daemon=True,
                     args=(srv, (ip, 40000), 51241, "srv", 51242)).start()
    cli.settimeout(10)
    responders._recv_line(cli)
    cli.sendall(b"USER x 8 * :" + tok + b"\r\n")
    responders._recv_line(cli)
    iv = sessioncrypt.iv_from_user_token(tok)
    cli.sendall(sessioncrypt.ofb_apply(P0, S0, iv, NICK) + b"\r\n")
    # The welcome burst (300, 001, 001, 422, MODE, NOTICE): wait for its last
    # line rather than a fixed time, so a slow first account provision does
    # not leave it in the socket for the checks below.
    end, buf, seen = time.time() + 10.0, b"", False
    cli.settimeout(0.2)
    while time.time() < end and not seen:
        try:
            chunk = cli.recv(65536)
        except OSError:
            continue
        if not chunk:
            break
        buf += chunk
        seen = any(b" NOTICE " in sessioncrypt.ofb_apply(P0, S0, iv, ln)
                   for ln in buf.split(b"\r\n") if ln)
    return cli, iv


def logtext():
    return "".join(open(os.path.join(tmp, f), encoding="utf-8").read()
                   for f in ("authserv.log", "accounts.log")
                   if os.path.exists(os.path.join(tmp, f)))


a, a_iv = login(TOKS[0], "198.51.100.1")
b, _ = login(TOKS[1], "198.51.100.2")


def drain(sock, iv, wait):
    """Every line the socket delivers within `wait` s (or until EOF), decrypted."""
    end, buf, eof = time.time() + wait, b"", False
    while time.time() < end:
        sock.settimeout(max(0.05, end - time.time()))
        try:
            chunk = sock.recv(4096)
        except OSError:
            continue
        if not chunk:
            eof = True
            break
        buf += chunk
    return [sessioncrypt.ofb_apply(P0, S0, iv, l)
            for l in buf.split(b"\r\n") if l], eof


got, eof = drain(a, a_iv, 2.0)
chk("knob OFF (default): the first login gets no KILL (PINGs only)",
    [g for g in got if not g.startswith(b"PING")], [])
chk("...and stays open", eof, False)
os.environ["POL_AUTH_KILL_DUP"] = "1"
c, _ = login(TOKS[2], "198.51.100.3")
got, eof = drain(a, a_iv, 5.0)
got = [g for g in got if not g.startswith(b"PING")]
chk("knob ON: the oldest login got the KILL",
    [g.split(b" ")[1] for g in got[:1]], [b"KILL"])
chk("...and the ERROR", got[1][:25] if len(got) > 1 else None,
    b"ERROR :Closing Link: UKIL")
chk("...then EOF", eof, True)
time.sleep(2.5)
text = logtext()
chk("its close was not treated as a logout",
    "was killed by a newer login -- not a logout" in text, True)
members = [m for m, lst in responders.PRESENCE._by_member.items()
           if any(s.nick == b"UKILLTWO1" for s in lst)]
live = responders.PRESENCE.sessions_for(members[0]) if members else []
chk("only the newest login is left registered, and it is alive",
    [(s.peer_ip, s.alive) for s in live], [(b"198.51.100.3", True)])
for s in (b, c):
    s.close()

print("FAILED: %d" % bad if bad else "all ok")
# The auth threads this test starts are daemons still logging to stdout; a
# normal interpreter shutdown can race them for the stdout lock (fatal error
# after "all ok"). Flush and leave without the shutdown sequence.
sys.stdout.flush()
os._exit(1 if bad else 0)
