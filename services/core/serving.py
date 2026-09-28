"""Listener plumbing: accepting connections and the per-connection thread."""
import os
import socket
import time
import threading
from srvcore import bind_peer, log



# --------------------------------------------------------------------------- #
# server plumbing
# --------------------------------------------------------------------------- #
#: Ceiling on handler threads across every listener. Not a load limit -- this
#: server has a handful of clients -- but a backstop: without it, anything that
#: leaks connections climbs until accept() raises EMFILE, and an accept() that
#: raises used to END ITS LISTENER THREAD for the life of the process. The socket
#: stayed bound, so the port still looked alive while every connection queued in
#: the backlog and hung. POL_MAX_CONNS=0 disables the cap.
_MAX_CONNS = int(os.environ.get("POL_MAX_CONNS", "256"))
_CONNS = threading.Semaphore(_MAX_CONNS) if _MAX_CONNS > 0 else None


def _serve_one(handler, conn, addr, port):
    # Who connected and which of our addresses they reached: every address this
    # thread hands the client comes out of _self_ip(), which reads these
    # (srvcore.advertise_for -- a LAN console must not be sent the tailnet IP).
    try:
        bind_peer(addr[0], conn.getsockname()[0])
    except OSError:
        bind_peer(addr[0], None)
    try:
        handler(conn, addr)
    except Exception as e:                  # a handler fault is not a listener fault
        log("resp", f"{addr[0]}:{addr[1]} handler for :{port} died: {e!r}")
        try:
            conn.close()
        except OSError:
            pass
    finally:
        if _CONNS is not None:
            _CONNS.release()


def serve(port, handler, bind="0.0.0.0"):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((bind, port))
    s.listen(64)
    log("resp", f"listening on {port}" + ("" if bind == "0.0.0.0"
                                          else f" ({bind} only)"))
    while True:
        try:
            conn, addr = s.accept()
        except OSError as e:
            # NEVER let the listener die. ECONNABORTED (client vanished during
            # the handshake) and EMFILE (descriptors exhausted) are both
            # recoverable, and both used to be fatal to this port only -- the
            # quietest possible failure, since every other port kept working and
            # it read as a client bug.
            log("resp", f"accept on :{port} failed ({e!r}); listener continues")
            time.sleep(0.1)
            continue
        if _CONNS is not None and not _CONNS.acquire(blocking=False):
            log("resp", f"{addr[0]}:{addr[1]} refused on :{port} -- "
                        f"{_MAX_CONNS} handler threads already live "
                        f"(POL_MAX_CONNS)")
            try:
                conn.close()
            except OSError:
                pass
            continue
        threading.Thread(target=_serve_one, args=(handler, conn, addr, port),
                         daemon=True).start()
