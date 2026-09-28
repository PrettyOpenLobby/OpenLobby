#!/usr/bin/env python3
"""A lobby request split across TCP segments is waited for, not answered blind.

    python tests/test_lobby_split_frame.py

2026-09-27, POL-5204 creating a handle: the Viewer's 1648-byte 0:8 arrived as
its 40-byte header, then the rest more than a second later. The reader gave up
at its 1 s idle gap, no session validated the bare header, and the lobby
answered blind. The header here has the real one's shape (type 02, opcode
00,08, payload 0x648) under a made-up session IV.
"""
import os
import socket
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))

tmp = tempfile.mkdtemp(prefix="splitframe-")
os.environ["POL_ACCOUNTS_DB"] = os.path.join(tmp, "accounts.db")
os.environ["POL_STAMP_FILE"] = os.path.join(tmp, "stamps.json")

import responders  # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


IV = bytes.fromhex("0f1e2d3c4b5a6978")
OTHER_IV = bytes.fromhex("0011223344556677")
TAIL = b"\x00" * 0x648            # the payload bytes do not matter to the reader

responders._lobby_iv_candidates = lambda peer_ip=None: [("s1", OTHER_IV), ("s2", IV)]
responders._lobby_key_for_iv = lambda iv: b"\x00" * 8
# the 40-byte header of a 0x648-byte 0:8, as the client would encrypt it (K=0)
HEAD = responders._lobby_crypt(b"\x02\x00\x08\x00" + (0x648).to_bytes(4, "little")
                               + bytes(32), IV)

print("the header")
pt = responders._lobby_crypt(HEAD[:8], IV)
chk("decrypts to type 02, opcode 00,08", pt[:4].hex(), "02000800")
chk("and declares a 0x648 payload", int.from_bytes(pt[4:8], "little"), 0x648)
chk("bare header is PENDING", responders._lobby_frame_pending(HEAD, "1.2.3.4"), True)
chk("bare header is not complete",
    responders._lobby_frame_complete(HEAD, "1.2.3.4"), False)
chk("the whole frame is complete",
    responders._lobby_frame_complete(HEAD + TAIL, "1.2.3.4"), True)
chk("the whole frame is not pending",
    responders._lobby_frame_pending(HEAD + TAIL, "1.2.3.4"), False)
chk("40 random bytes are not pending",
    responders._lobby_frame_pending(bytes(range(40)), "1.2.3.4"), False)


def split_read(gap):
    a, b = socket.socketpair()

    def send():
        b.sendall(HEAD)
        time.sleep(gap)
        b.sendall(TAIL)
    t = threading.Thread(target=send)
    t.start()
    got = responders._read_frame(a, idle=0.3, maxwait=3.0, minlen=1,
                                 until=responders._lobby_until("1.2.3.4"))
    t.join()
    a.close()
    b.close()
    return len(got)


print("a split frame over a real socket (gap longer than the idle window)")
chk("waits for the tail", split_read(0.8), 40 + 0x648)
os.environ["POL_LOBBY_WAIT_PARTIAL"] = "0"
chk("POL_LOBBY_WAIT_PARTIAL=0: the old read returns the bare header",
    split_read(0.8), 40)
os.environ.pop("POL_LOBBY_WAIT_PARTIAL")

print("an unsplit frame still returns at once")
a, b = socket.socketpair()
b.sendall(HEAD + TAIL)
t0 = time.time()
n = len(responders._read_frame(a, idle=0.3, maxwait=3.0, minlen=1,
                               until=responders._lobby_until("1.2.3.4")))
chk("whole frame", n, 40 + 0x648)
chk("no idle wait (well under 0.3 s)", time.time() - t0 < 0.25, True)
a.close()
b.close()

print("FAILED: %d" % bad if bad else "all ok")
sys.exit(1 if bad else 0)
