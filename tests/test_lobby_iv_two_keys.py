"""One launch IV, two RSA keys: the lobby must still identify the frame.

2026-10-03: the Viewer and Tetra Master share a launch IV, but each hop
negotiates its own RSA key. Tetra Master's own hop recorded its key over the
Viewer's, while its lobby frames are encrypted with the VIEWER's key -- the
lobby tried that one wrong key, answered garbage, and the client sat on
"Updating online status..." until it timed out.

    python tests/test_lobby_iv_two_keys.py
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "services"))
os.environ.pop("POL_LOBBY_KEY", None)
os.environ.pop("POL_LOBBY_IV", None)

import sessioncrypt                                                # noqa: E402
from core import lobbybind, lobbysession                           # noqa: E402

FAILS = []


def chk(label, got, want=True):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label}: {got!r}")
    if not ok:
        FAILS.append(label)


def frame_under(key, iv):
    # a request header: type 0x02, opcode 04,05, LE u32 payload length 40
    pt = bytes([0x02, 0x04, 0x05, 0x00]) + struct.pack("<I", 40) + bytes(32) + bytes(40)
    P, S = sessioncrypt.bf_setkey(key)
    return sessioncrypt.ofb_apply(P, S, iv, pt)


def main():
    iv = bytes.fromhex("1122334455667788")
    viewer, tm = bytes.fromhex("aaaaaaaaaaaaaaaa"), bytes.fromhex("bbbbbbbbbbbbbbbb")
    lobbysession._session_put = lambda sid, **kw: lobbysession._SESSIONS.setdefault(sid, {}).update(kw)
    lobbysession._SESSIONS.clear()
    lobbysession._SESSIONS["u_launch"] = {"iv": iv, "member_id": 3}

    lobbybind._remember_iv_key("u_launch", iv, viewer)   # the Viewer's hop
    lobbybind._remember_iv_key("u_launch", iv, tm)       # then Tetra Master's own hop
    chk("both keys are kept for the IV, newest first",
        lobbysession._SESSIONS["u_launch"]["iv_keys"][iv.hex()], tm.hex() + "," + viewer.hex())

    f = frame_under(viewer, iv)
    chk("a frame under the OLDER (Viewer) key still validates",
        lobbybind._lobby_validates(f, iv, len(f), True))
    chk("...and that key is pinned for the connection", lobbybind._lobby_key_for_iv(iv), viewer)
    chk("the whole frame decrypts to the request header",
        lobbybind._lobby_crypt(f, iv)[:4], bytes([0x02, 0x04, 0x05, 0x00]))

    lobbybind._PIN.keys = {}
    g = frame_under(tm, iv)
    chk("a frame under the newer key validates too", lobbybind._lobby_validates(g, iv, len(g), True))
    chk("a frame under neither key does not",
        lobbybind._lobby_validates(frame_under(bytes(8)[:7] + b"\x01", iv), iv, 80, True), False)

    print("all ok" if not FAILS else f"FAILED: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
