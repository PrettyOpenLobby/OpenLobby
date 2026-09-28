"""The world responder the lobby hands off to."""
import os
import titles                   # the title-plugin seam (services/titles.py)  # noqa: E402
from srvcore import hexdump, log, save_capture
import sessioncrypt
from . import authserv, lobbyreply, lobbysession



# --------------------------------------------------------------------------- #
# World responder (the endpoint the lobby hands off to)
# --------------------------------------------------------------------------- #
# The client dials this directly by raw IP from the lobby's world handoff (the
# POL-0008 boundary). A title's world traffic actually rides the auth band (see
# `_game_notice_reply`), so this is a CAPTURE HARNESS: it gives that handoff a
# live endpoint (so the client connects instead of timing out on a dead port),
# records the client's first world frames, and attempts a decrypt under the
# session key (K=0 default, like the lobby). It never answers a protocol it
# cannot speak.


def _world_key():
    return bytes.fromhex(os.environ.get("POL_WORLD_KEY", "00" * 8))


def handle_world(conn, addr, port, stub_ip):
    # Bind this thread to a session of its OWN. Provisional (one id per
    # connection) until something on the wire names the launch: the USER token on
    # the auth band, a validating IV on the lobby band. Never the address -- two
    # clients behind the Docker bridge share one, and sharing a slot is how the
    # second was served the first's account.
    lobbysession.session_bind(lobbysession._sid_for_connection(addr[0], addr[1]))
    peer = f"{addr[0]}:{addr[1]}"
    emit = os.environ.get("POL_WORLD_EMIT", "0") == "1"
    try:
        # THE CORE'S WORLD PORT IS A CAPTURE HARNESS whatever titles are
        # loaded (a title's world, if it has one, is its own service); the
        # title description after it says what rides the auth band.
        desc = titles.describe() or "no title module loaded"
        log("world", f"{peer} port {port}: connected; capture-only here. {desc}")
        buf = authserv._read_for(conn, seconds=8, want_crlf=False)
        if not buf:
            log("world", f"{peer} port {port}: connected but sent no data "
                         "(client may expect the server to speak first -- note it)")
            return
        cap = save_capture(f"world-{port}", buf)
        log("world", f"{peer} port {port}: {len(buf)}B world opener; saved {cap}\n"
                     + hexdump(buf))
        # Attempt a read, reusing the lobby's IV-sweep decrypt (same cipher family).
        key = _world_key()
        P, S = sessioncrypt.bf_setkey(key)
        iv_name, off, pt, _hits = lobbyreply._lobby_try_decrypt(buf, P, S)
        if pt is not None:
            printable = sum(1 for c in pt if 32 <= c < 127)
            log("world", f"{peer} best decode key={key.hex()} {iv_name} "
                         f"body_off={hex(off)} ({printable}/{len(pt)} printable)\n"
                         + hexdump(pt[:96]))
        if emit:
            log("world", f"{peer} POL_WORLD_EMIT set but no world protocol is "
                         "spoken on this port -- refusing to send fabricated "
                         "bytes. Decode the capture first.")
        # Keep observing: the opener may be followed by more once it (times out).
        more = authserv._read_for(conn, seconds=8, want_crlf=False)
        if more:
            save_capture(f"world-{port}-more", more)
            log("world", f"{peer} +{len(more)}B more\n" + hexdump(more))
    except Exception as e:
        log("world", f"{peer} port {port} error: {e}")
    finally:
        conn.close()
