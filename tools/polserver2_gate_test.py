#!/usr/bin/env python3
"""live:patch: a console between two file requests still counts as updating.

    python tools/polserver2_gate_test.py

A console fetches an update one file per connection, so with only open
connections counted the deploy gate saw 0 between two files and a push in
that gap cut the update (the console cannot resume it and starts over). What
must hold:

  * a connection that asked for a file counts while it is open;
  * after it closes, its client still counts for POLP_UPDATE_HOLD seconds;
  * then it does not;
  * a client that only asked for its version (cmd 7) never counts.

The twin: with the hold at 0 the closed connection must count 0, which is the
old behaviour, so the second case cannot pass by accident.
"""
import os
import socket
import struct
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))

import polserver2                                                # noqa: E402

failed = 0


def check(name, ok):
    global failed
    print("%s  %s" % ("ok  " if ok else "FAIL", name))
    failed += 0 if ok else 1


def frame(cmd, body=b"\0" * 0x48):
    pkt = bytearray(struct.pack("<I", 16 + len(body)) + b"\0" * 4 + b"POLP"
                    + struct.pack("<I", cmd) + body)
    struct.pack_into("<I", pkt, 4, polserver2.cksum(pkt))
    return bytes(pkt)


def server(hold):
    os.environ["POLP_UPDATE_HOLD"] = str(hold)
    return polserver2.Server({}, "127.0.0.1", False)


def serve_one(srv, cmd, keep_open=False):
    """Run handle() on one connection that sends `cmd`; returns the client
    socket (open when `keep_open`) and the handler thread."""
    a, b = socket.socketpair()
    t = threading.Thread(target=srv.handle, args=(b, ("192.0.2.7", 40000)), daemon=True)
    t.start()
    a.sendall(frame(cmd))
    time.sleep(0.2)
    if not keep_open:
        a.close()
        t.join(5)
    return a, t


srv = server(300)
a, t = serve_one(srv, 3, keep_open=True)
check("an open connection that asked for a file counts", srv.live_updates() >= 1)
a.close()
t.join(5)
check("its connection has closed", srv.updating == 0)
check("the client still counts right after", srv.live_updates() == 1)
check("and 299 s later", srv.live_updates(time.monotonic() + 299) == 1)
check("but not 301 s later", srv.live_updates(time.monotonic() + 301) == 0)

srv = server(300)
serve_one(srv, 7)
check("a version check (cmd 7) never counts", srv.live_updates() == 0)

srv = server(0)
serve_one(srv, 3)
time.sleep(0.01)
check("twin: with no hold, a closed connection counts 0", srv.live_updates() == 0)

print("%d failed" % failed)
sys.exit(1 if failed else 0)
