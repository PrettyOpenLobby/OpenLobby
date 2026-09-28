"""Login directory responder (TCP 51240): redirects the client to the auth node."""
import socket
from srvcore import hexdump, log
from authtoken import build_redirect_token
from . import authcap, authnode, lobbysession



# --------------------------------------------------------------------------- #
# Login directory responder (TCP 51240)
# --------------------------------------------------------------------------- #
def handle_directory(conn, addr, node_ip, node_port, srv_name):
    # Bind this thread to a session of its OWN. Provisional (one id per
    # connection) until something on the wire names the launch: the USER token on
    # the auth band, a validating IV on the lobby band. Never the address -- two
    # clients behind the Docker bridge share one, and sharing a slot is how the
    # second was served the first's account.
    lobbysession.session_bind(lobbysession._sid_for_connection(addr[0], addr[1]))
    peer = f"{addr[0]}:{addr[1]}"
    node_ip = node_ip or authcap._self_ip()      # per client unless POL_AUTH_IP pins it
    try:
        conn.settimeout(10)
        token = build_redirect_token(node_ip, node_port)
        # Two 300 lines like an auth node: the base-32 redirect (IP:port) AND the
        # 64-alpha KEY token. We send the zero-token so the client derives K=0 for
        # the first auth hop -> we can read its NICK. (A directory that sends only
        # the redirect leaves the client with no key and it sends nothing.)
        greeting = (f":{srv_name} 300 * {token}\r\n"
                    f":{srv_name} 300 * {authnode.TOKEN0}\r\n"
                    f"ERROR :Closing Link: [unknown@{addr[0]}] (redirect)\r\n"
                    ).encode()
        conn.sendall(greeting)
        log("directory", f"{peer} greeted -> redirect to {node_ip}:{node_port}\n"
                          + hexdump(greeting))
        # The real directory closes right after; give the client a beat to read.
        try:
            conn.recv(256)
        except socket.timeout:
            pass
    except Exception as e:
        log("directory", f"{peer} error: {e}")
    finally:
        conn.close()
