"""Auth node capture harness, and the address this server advertises."""
import os
import socket
from srvcore import advertise_for, hexdump, log, save_capture
from . import lobbysession



# --------------------------------------------------------------------------- #
# Auth node capture harness (TCP 5124x)
# --------------------------------------------------------------------------- #
def handle_authcap(conn, addr, port, srv_name):
    """Log the client's cleartext USER and encrypted NICK, then HOLD the socket.

    This is the dump harness. By the time the client has sent its encrypted NICK
    it has already computed the session key K and written it to ctx+936, and it
    is now blocked waiting for our auth reply. We deliberately send NOTHING and
    hold the connection open (POL_AUTH_HOLD seconds): that keeps the client's ctx
    (K + IV + RSA context) resident and stable so a process dump captures it. We
    do not answer, because a malformed reply could make the client tear the
    connection down and free the ctx.
    """
    # Bind this thread to a session of its OWN. Provisional (one id per
    # connection) until something on the wire names the launch: the USER token on
    # the auth band, a validating IV on the lobby band. Never the address -- two
    # clients behind the Docker bridge share one, and sharing a slot is how the
    # second was served the first's account.
    lobbysession.session_bind(lobbysession._sid_for_connection(addr[0], addr[1]))
    peer = f"{addr[0]}:{addr[1]}"
    hold = int(os.environ.get("POL_AUTH_HOLD", "300"))
    try:
        conn.settimeout(15)
        buf = b""
        deadline = 0
        while buf.count(b"\r\n") < 2 and deadline < 8:
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                deadline += 1
                continue
            if not chunk:
                break
            buf += chunk
        cap = save_capture(f"resp-{port}", buf)
        user = buf.split(b"\r\n", 1)[0]
        log("authcap", f"{peer} port {port} got {len(buf)}B; USER={user!r}; "
                        f"saved {cap}", )
        log("authcap", f"{peer} hexdump:\n" + hexdump(buf))
        log("authcap", f">>> KEY IS NOW RESIDENT. Dump pol.exe now. "
                        f"Holding this connection open for {hold}s. <<<")
        # Hold silently so the ctx stays alive for the memory dump.
        conn.settimeout(hold)
        try:
            more = conn.recv(4096)
            if more:
                save_capture(f"resp-{port}-more", more)
                log("authcap", f"{peer} +{len(more)}B after hold\n"
                               + hexdump(more))
        except socket.timeout:
            log("authcap", f"{peer} hold window elapsed; closing")
    except Exception as e:
        log("authcap", f"{peer} error: {e}")
    finally:
        conn.close()


_SELF_IP = [None]


def _advertise_configured():
    return bool(_SELF_IP[0] or os.environ.get("POL_ADVERTISE"))


def _self_ip():
    """The address to tell THIS client to dial next.

    Global default = main()'s stub_ip, else POL_ADVERTISE (a container that runs
    a responder without main(), e.g. the ucs path, used to fall through to
    127.0.0.1 here). Then srvcore.advertise_for chooses per client: a LAN peer
    gets the LAN address it reached us on (or POL_ADVERTISE_LAN), a tailnet peer
    keeps the global one. Found live 2026-09-18: the real PS2 was redirected
    from ci000 to 127.0.0.1:51241 and hung on "verifying user info".
    """
    base = _SELF_IP[0] or os.environ.get("POL_ADVERTISE") or "127.0.0.1"
    return advertise_for(base)
