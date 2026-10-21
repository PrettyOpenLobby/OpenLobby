#!/usr/bin/env python3
"""4:5 KChangeMyStatus never echoes the client's bytes 0..15 back.

    python tests/test_status_echo_zero_key.py

Those 16 bytes of the reply are the content auth value a title is keyed with.
The PS2 and PC send zeros there; an Xbox 360 sends 01 02 00 00 00 00 00 00
followed by its XUID, and echoing that made it the 360's FFXI world key prefix,
which never matched the bridge's zero key (FFXI-3001 on every 360). The rest of
the state (status, zone) is still echoed.
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))

from core import memberstatus  # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


# No content auth, no presence side effects: only the echo is under test.
memberstatus.contentauth = None
memberstatus.titlezone._publish_title_zone = lambda *a, **k: None
memberstatus._publish_member_status = lambda *a, **k: False


def request(first16):
    # 40-byte header, then a 40-byte payload: 16 key bytes, 8 zeros, then the state
    # (in_title at +0x13, zone u16 at +0x14, status at +0x16) and a handle.
    payload = bytearray(40)
    payload[0:16] = first16
    payload[0x13] = 1
    struct.pack_into("<H", payload, 0x14, 1)      # FFXI
    payload[0x16] = 1                            # online
    payload[0x18:0x1d] = b"Hndl1"
    hdr = bytearray(40)
    hdr[0], hdr[1], hdr[2] = 0x02, 0x04, 0x05
    struct.pack_into("<I", hdr, 4, len(payload))
    return bytes(hdr + payload)


print("Xbox 360: 01 02 00.. + XUID in bytes 0..15")
x360 = bytes.fromhex("0102000000000000") + bytes.fromhex("1122334455660900")
out = memberstatus.payload_change_status(32, request(x360))
chk("reply bytes 0..15 are zero", out[:16], bytes(16))
chk("in_title still echoed", out[0x13], 1)
chk("zone still echoed", struct.unpack_from("<H", out, 0x14)[0], 1)
chk("status still echoed", out[0x16], 1)
chk("reply length", len(out), 32)

print("PS2 / PC: zeros in bytes 0..15 (unchanged behaviour)")
out = memberstatus.payload_change_status(32, request(bytes(16)))
chk("reply bytes 0..15 are zero", out[:16], bytes(16))
chk("zone still echoed", struct.unpack_from("<H", out, 0x14)[0], 1)

print("short request still padded")
out = memberstatus.payload_change_status(32, request(x360)[:50])
chk("reply length", len(out), 32)
chk("reply bytes 0..15 are zero", out[:16], bytes(16))

print("FAIL: %d" % bad if bad else "all ok")
sys.exit(1 if bad else 0)
