"""POLP patch responder (TCP 54000): the version check and its canned reply."""
import struct
from srvcore import hexdump, log



# --------------------------------------------------------------------------- #
# POLP patch responder (TCP 54000)
# --------------------------------------------------------------------------- #
# The canned cmd-8 reply, captured verbatim from the live SE patch server. Its
# checksum (bytes 4:8) is SE's own and is valid for these exact bytes, so we can
# replay it without reversing the checksum -- as long as we do not modify it.
# status="registered", download host=124.150.156.107, latest version=20110829_E.
# Our target client is already at 20110829_E, so current == latest and it never
# downloads; the download host / checksum are therefore never exercised.
POLP_CMD8 = bytes.fromhex(
    "670000001cc5a086504f4c500800000097644e4e0100000072656769737465726564"
    "003132342e3135302e3135362e3130370030000000000000000000000000000000000000"
    "0000000000000000000000000000000000000b00000032303131303832395f4500")


def _polp_version(req):
    """Pull the client's current-version string out of a cmd-7 request."""
    if len(req) < 0x18:
        return b""
    return req[0x18:].split(b"\0", 1)[0]


def handle_patch(conn, addr):
    peer = f"{addr[0]}:{addr[1]}"
    try:
        conn.settimeout(15)
        req = b""
        # POLP frames are length-prefixed at offset 0 (u32 total_len).
        while len(req) < 4:
            chunk = conn.recv(4096)
            if not chunk:
                return
            req += chunk
        total = struct.unpack("<I", req[:4])[0]
        while len(req) < total:
            chunk = conn.recv(4096)
            if not chunk:
                break
            req += chunk
        magic = req[0x08:0x0c]
        cmd = struct.unpack("<I", req[0x0c:0x10])[0] if len(req) >= 0x10 else -1
        log("patch", f"{peer} req {len(req)}B magic={magic!r} cmd={cmd} "
                     f"ver={_polp_version(req)!r}\n" + hexdump(req))
        if magic != b"POLP":
            log("patch", f"{peer} not POLP; dropping")
            return
        # cmd 7 = version check -> reply canned cmd 8 (client is current -> no dl)
        conn.sendall(POLP_CMD8)
        log("patch", f"{peer} replied cmd-8 ({len(POLP_CMD8)}B, canned)")
    except Exception as e:
        log("patch", f"{peer} error: {e}")
    finally:
        conn.close()
