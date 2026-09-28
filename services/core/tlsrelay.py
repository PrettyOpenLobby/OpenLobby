"""Relaying TLS hellos and UCS CGI requests that arrive on the lobby band."""
import os
import select
import socket
import time
from srvcore import log



def _is_tls_hello(buf):
    """True if `buf` opens a TLS/SSL handshake record.

    A TLS record is [u8 type][u16 version][u16 length]; type 0x16 is handshake
    and the versions we can see are SSL 3.0 (03 00) through TLS 1.2 (03 03).
    The PS2 Viewer's is genuine SSLv3:

        16 03 00 00 35 01 00 00 31 03 00 ... 00 0a 00 04 00 05 00 09 00 03 00 08

    -- six suites, byte-for-byte the list in Dockerfile.ssl3 (3DES/RC4x2/DES and
    two export), which is what identifies it as the Viewer's own TLS rather than
    anything else that might land here.
    """
    return len(buf) >= 3 and buf[0] == 0x16 and buf[1] == 0x03 and buf[2] <= 0x03


def _relay_ucs_cgi(conn, method, path, hdrs, body, peer):
    """Relay a /pml-cgi-bin/ request on the band port to the ucscgi service and
    pass its response back verbatim. Same idea as stub.py::_try_ucs_cgi on :80 --
    one ucscgi process owns the account-DB session, so the wizard and the portal-
    band fetch reach the SAME flow. `conn` is a raw socket; method/path are bytes
    (path carries the query); hdrs is a bytes->bytes dict; body is the POST body.
    """
    target = os.environ.get("POL_UCS_CGI_HOST", "ucs-plain")
    up_port = int(os.environ.get("POL_UCS_CGI_PORT", "8080"))
    skip = (b"connection", b"keep-alive", b"transfer-encoding")
    req = method.decode("latin1") + " " + path.decode("latin1") + " HTTP/1.1\r\n"
    for k, v in hdrs.items():
        if k.lower() in skip:
            continue
        req += k.decode("latin1", "replace") + ": " + v.decode("latin1", "replace") + "\r\n"
    req += "Connection: close\r\n\r\n"
    try:
        with socket.create_connection((target, up_port), timeout=15) as up:
            up.sendall(req.encode("latin1", "replace") + (body or b""))
            chunks = []
            while True:
                d = up.recv(65536)
                if not d:
                    break
                chunks.append(d)
    except OSError as exc:
        log("lobby", f"{peer}   ucs-cgi relay to {target}:{up_port} FAILED: {exc}")
        try:
            conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n"
                         b"Connection: close\r\n\r\n")
        except OSError:
            pass
        return
    raw = b"".join(chunks)
    log("lobby", f"{peer}   ucs-cgi {path.decode('latin1', 'replace').split('?')[0]} "
                 f"-> {target}:{up_port} ({len(raw)} bytes)")
    try:
        conn.sendall(raw)
    except OSError:
        pass


def _relay_tls(conn, first, peer, port):
    """Hand a TLS connection on a BAND port to the SSLv3 terminator.

    Why this exists: the Viewer does HTTPS over the SAME 5130x band as its plain
    PML fetches -- it never uses :443 -- so an https:// URL (notably the
    Content-ID acquisition servlet, /pml-cgi-bin/UMENZ001.cgi on userctl.pol.com)
    arrives HERE. Before this, `_parse_lobby_hello` read the TLS record header as
    a lobby message (`opcode=309` is 0x0135 = the record's length field), logged
    "NOT a lobby hello", and then PROCEEDED -- answering a ClientHello with lobby
    bytes. The client reads that as a certificate it cannot authenticate and
    reports POL-1312, which is why that error looked like a trust-store problem:
    it had simply never been shown a certificate at all.

    We cannot terminate it in-process (no current Python/OpenSSL speaks SSLv3),
    so the bytes are pumped to the stunnel terminator, which decrypts and forwards
    plaintext HTTP to the portal. POL_SSL3_RELAY=0 restores the old behaviour.
    """
    if os.environ.get("POL_SSL3_RELAY", "1") != "1":
        log("lobby", f"{peer} port {port}: TLS hello but POL_SSL3_RELAY=0 -- "
                     "falling through to the lobby parser (it will answer with "
                     "lobby bytes and the client will raise POL-1312)")
        return
    host = os.environ.get("POL_SSL3_HOST", "ssl3web")
    dst = int(os.environ.get("POL_SSL3_PORT", "51443"))
    log("lobby", f"{peer} port {port}: {len(first)}B TLS ClientHello "
                 f"(SSLv3-family) -- relaying to the terminator at {host}:{dst}")
    try:
        up = socket.create_connection((host, dst), timeout=10)
    except OSError as exc:
        log("lobby", f"{peer}   terminator unreachable ({exc}) -- dropping the "
                     "connection rather than answering TLS with lobby bytes")
        return
    up.sendall(first)
    c2s, s2c = len(first), 0
    try:
        conn.setblocking(False)
        up.setblocking(False)
        last = time.time()
        while time.time() - last < 30:
            ready, _, _ = select.select([conn, up], [], [], 1.0)
            if not ready:
                continue
            for src in ready:
                dstsock = up if src is conn else conn
                try:
                    chunk = src.recv(16384)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    chunk = b""
                if not chunk:
                    raise _RelayDone()
                dstsock.sendall(chunk)
                if src is conn:
                    c2s += len(chunk)
                else:
                    s2c += len(chunk)
                last = time.time()
    except _RelayDone:
        pass
    except OSError as exc:
        log("lobby", f"{peer}   relay ended: {exc}")
    finally:
        try:
            up.close()
        except OSError:
            pass
    log("lobby", f"{peer} port {port}: TLS relay closed "
                 f"({c2s}B client->terminator, {s2c}B back). The DECRYPTED "
                 "request is in the http log.")


class _RelayDone(Exception):
    """Either side closed; ends the pump without looking like an error."""
