"""A client that WAITS for its reply must not be made to wait a second for it.

*** THE BUG THIS EXISTS FOR, AND WHY NOTHING CAUGHT IT. ***

`_read_frame`'s only exit from its accumulate loop is a recv TIMEOUT. So a peer
that sends a request and then holds the socket open -- which is what a real
Viewer does, and what "request/reply protocol" means -- pays the full `idle`
window (1.0 s) on EVERY message before the server begins composing an answer.
Prod runs `POL_LOBBY_EMIT=derive`, a multi-turn conversation loop, so it is a
second per MESSAGE.

Every other suite in this tree misses it, and they miss it the same way: their
test clients `sendall(...)` and then close, or shut down the write side, which
makes the next `recv` return EOF instantly. **The only client that waits is the
real one.** So this file's whole contribution is a peer that does nothing after
sending -- exactly what the Viewer does -- and a clock.

Measured against the real reader on 2026-08-19, before the fix:

    client CLOSES after sending:    57 ms
    client WAITS for the reply:   1072 ms      <- ~1.0 s of dead wait per op

For scale: the entire database cost of a group-list serve on prod's filesystem
is ~0.34 ms. This was three orders of magnitude bigger than everything else on
the request path put together, and it is what "our server runs so much slower
than [SE] on almost everything" was.

The fix is the predicate `_http_request_complete` already gave the HTTP half of
this same port: `_lobby_frame_complete` decrypts the first eight bytes and asks
whether `40 + <declared payload length>` equals what we hold -- the same
self-validating test `_lobby_bind` uses to pick a session.

WHAT THIS ASSERTS, none of which is about a number being small:

  1. a complete frame is recognised, under the session's own IV;
  2. a PARTIAL frame is NOT -- the predicate must never truncate a request;
  3. an unknown IV answers False, so the reader degrades to the old idle window
     rather than to a dropped connection (the predicate can only make the read
     return sooner, never differently);
  4. and end to end: a waiting client's read completes far inside the idle
     window, while a peer sending a partial frame still gets the full window.
"""
import os
import socket
import struct
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "services"))

TMP = tempfile.mkdtemp(prefix="lobby-latency-")
os.environ["POL_LOG_DIR"] = TMP
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_ACCOUNTS_DB"] = os.path.join(TMP, "accounts.db")
# A FIXED SESSION IV. `_lobby_iv_candidates` normally derives the candidate
# list from live sessions; POL_LOBBY_IV pins it to one, which is what makes
# this runnable with no login, no listener and no client. The predicate
# under test does not care where the candidates came from.
os.environ["POL_LOBBY_IV"] = "1122334455667788"

import responders as R                                             # noqa: E402

FAILS = []
#: The reader's idle window. The point of the fix is to finish well inside it.
IDLE = 1.0
#: Generous: a correct early return is ~1 ms, and this still fails loudly if the
#: predicate stops firing (which costs the full second).
BUDGET_MS = 300


def check(ok, label, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  --  {detail}"
                                                       if detail else ""))
    if not ok:
        FAILS.append(label)


def lobby_frame(payload_len=64, iv=None):
    """A request frame in SE's shape: 40-byte header + payload, encrypted.

    `[0]` is the type (0x02 on every real request) and `[4:8]` the LE u32
    payload length -- the two fields `_lobby_header_ok` validates.
    """
    pt = bytearray(40 + payload_len)
    pt[0] = 0x02
    pt[1], pt[2] = 0x00, 0x09                       # 0:9, the handle list
    struct.pack_into("<I", pt, 4, payload_len)
    return R._lobby_crypt(bytes(pt), iv)


def read_with(peer_ip, send, iv):
    """Run the REAL reader against a socket pair; return (ms, bytes read)."""
    srv, cli = socket.socketpair()
    out = {}

    def reader():
        t0 = time.perf_counter()
        buf = R._read_frame(srv, idle=IDLE, maxwait=8.0, minlen=8,
                            until=R._lobby_until(peer_ip, http=True))
        out["ms"] = (time.perf_counter() - t0) * 1000
        out["n"] = len(buf)

    t = threading.Thread(target=reader)
    t.start()
    time.sleep(0.05)                     # let the reader block in recv
    send(cli)
    t.join(20)
    cli.close()
    srv.close()
    return out


def main():
    iv = R._lobby_iv()
    frame = lobby_frame(64, iv)
    print(f"frame: {len(frame)}B (40 header + 64 payload), IV {iv.hex()}\n")

    print("the predicate ->")
    check(R._lobby_frame_complete(frame), "a whole frame is recognised")
    check(not R._lobby_frame_complete(frame[:-1]),
          "one byte short is NOT -- it must never truncate a request")
    check(not R._lobby_frame_complete(frame + b"\x00"),
          "one byte over is NOT either")
    check(not R._lobby_frame_complete(b"\x00" * 8),
          "a buffer shorter than a header is not a frame")
    check(not R._lobby_frame_complete(b"\xff" * 104),
          "bytes that decrypt to no valid header answer False -- "
          "the reader then waits exactly as it did before")

    print("\nthe reader, with a client that WAITS (what the Viewer does) ->")
    got = read_with("127.0.0.1", lambda c: c.sendall(frame), iv)
    check(got["n"] == len(frame), "the whole frame was read",
          f"{got['n']}B of {len(frame)}B")
    check(got["ms"] < BUDGET_MS,
          f"and it returned in well under the {IDLE:g}s idle window",
          f"{got['ms']:.1f} ms")

    print("\n...and a PARTIAL frame still gets the full window ->")
    # The safety property: an incomplete request must not be answered early.
    # This is the case the predicate has to stay silent for, and it costs the
    # idle window by design.
    got = read_with("127.0.0.1", lambda c: c.sendall(frame[:60]), iv)
    check(got["n"] == 60, "the partial frame is returned only after the wait",
          f"{got['n']}B in {got['ms']:.0f} ms")
    check(got["ms"] >= IDLE * 1000 * 0.8,
          "i.e. the predicate did NOT fire on an incomplete request",
          f"{got['ms']:.0f} ms")

    print("\n...and HTTP over the same port still short-circuits ->")
    got = read_with("127.0.0.1",
                    lambda c: c.sendall(b"GET /pml/index.pml HTTP/1.1\r\n"
                                        b"Host: wh000.pol.com\r\n\r\n"), iv)
    check(got["ms"] < BUDGET_MS, "the portal's own early exit is unaffected",
          f"{got['ms']:.1f} ms")

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s): " + ", ".join(FAILS))
        return 1
    print("all lobby-latency checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
