#!/usr/bin/env python3
"""A windowed save (FFXI's macros) is stored piece by piece and read back by window.

    python tests/test_windowed_save.py

2026-10-18, FFXI on the Xbox 360: "save macros to the server" wrote
`U/g/0001_00.bak` as a 03:02 delete, a 292-byte header at offset 0, then a
4986-byte ZIP at offset 292, and the next load read offset 0 length 292. We
acknowledged all of it and stored nothing, so every load came back empty. The
frames below have the captured layout (offset at +0x1B8, length at +0x1BC, the
window at +0x1C0, a checksum behind it) with made-up contents.
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import pgtest  # noqa: E402
pgtest.use_fresh_database()

import responders  # noqa: E402,F401
import titles  # noqa: E402
from core import lobbysession, paylen, resourcestore as rs  # noqa: E402
from core.deps import accounts  # noqa: E402
accounts.connect().close()                 # migrates the fresh database

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s%s" % ("ok  " if ok else "FAIL", what,
                         "" if ok else ": %r  (want %r)" % (got, want)))


PATH = "U/g/0001_00.bak"
SUBJECT = bytes.fromhex("4ae1cc4aee000000")


def frame(op, off=None, data=b"", junk=False):
    pt = bytearray(b"\x02\x03" + bytes([op]) + b"\x00" + b"\x00" * (rs._MAIL_OBJ_OFF - 4))
    pt[0x30:0x38] = SUBJECT
    pt[0x38:0x38 + len(PATH) + 1] = PATH.encode() + b"\x00"
    if junk:                       # a 03:02: heap junk where the window would be
        pt[0x1A0:0x1C0] = bytes(range(0x60, 0x80))
        pt = pt[:0x1C0]
    else:
        struct.pack_into("<I", pt, rs._WINDOW_OFF, off)
        pt += struct.pack("<I", len(data)) + data + b"\xc7\xaf\x58\x55"
    struct.pack_into("<I", pt, 4, len(pt) - 0x28)
    return bytes(pt)


def read(off, n):
    """A 03:00 of the captured 456-byte shape: offset, length, nothing more."""
    pt = bytearray(frame(0x00, off, b"")[:rs._MAIL_OBJ_OFF])
    pt += struct.pack("<I", n) + b"\x00" * 4 + b"\xa2\xd4\xff\xd9"
    struct.pack_into("<I", pt, 4, len(pt) - 0x28)
    pt = bytes(pt)
    want = paylen.paylen_fetch(pt)
    return want, rs.payload_fetch(want, pt)


def save(header, body):
    rs.capture_write(frame(0x02, junk=True))
    rs.capture_write(frame(0x01, 0, header))
    rs.capture_write(frame(0x01, len(header), body))


# What boot does with a title that declares the prefix.
class XI(titles.Title):
    tag = b"XI0"
    resource_windowed = ("U/g/0001_",)


titles.register(XI())
rs._RESOURCE_WINDOWED = titles.resource_windowed()
rs._RESOURCE_WRITE_PATHS += rs._RESOURCE_WINDOWED
chk("a title's prefix reaches the store", rs._RESOURCE_WINDOWED, ("U/g/0001_",))

member = {"v": 71}
lobbysession._session_get = lambda field: member["v"] if field == "member_id" else None

HEADER = b"\x00\x00\x02\x00" + bytes(range(1, 255)) + b"\x7a\x13" + b"\x00" * 32
BODY = b"\x04\x03KP" + bytes((i * 7) & 0xFF for i in range(4982))
chk("the header is the captured 292 bytes", len(HEADER), 292)

print("first load, nothing stored")
n, pay = read(0, 292)
chk("the header window is asked for at 292 + trailer", n, 296)
chk("and served as zeros", pay, b"\x00" * 296)

print("save: delete, header at 0, ZIP at 292")
save(HEADER, BODY)
chk("the file is header then ZIP", rs._res_read(rs._resource_file(PATH)), HEADER + BODY)

print("next load")
n, pay = read(0, 292)
chk("header window length", n, 296)
chk("header window is the header, trailer bytes left to the signer",
    pay, HEADER + b"\x00" * 4)
n, pay = read(292, len(BODY))
chk("body window length", n, len(BODY) + 4)
chk("body window is the ZIP", pay[:-4], BODY)

print("a later, SHORTER save")
small = BODY[:1000]
save(HEADER, small)
chk("the delete truncated: no old tail behind the new ZIP",
    rs._res_read(rs._resource_file(PATH)), HEADER + small)
chk("the previous save is kept beside it",
    rs._res_read(rs._resource_file(PATH) + ".prev"), HEADER + BODY)

print("scope")
member["v"] = 72
chk("another member reads nothing", read(0, 292)[1], b"\x00" * 296)
member["v"] = 71

print("malformed")
before = rs._res_read(rs._resource_file(PATH))
bogus = bytearray(frame(0x01, 0, HEADER))
struct.pack_into("<I", bogus, rs._MAIL_OBJ_OFF, 9999)        # length past the frame
rs.capture_write(bytes(bogus))
rs.capture_write(frame(0x01, rs._WINDOW_MAX, b"x"))         # offset past the cap
chk("a window that does not fit stores nothing",
    rs._res_read(rs._resource_file(PATH)), before)

print("other paths are untouched")
chk("a non-windowed save path is still refused",
    rs._windowed("U/g/MJSUserData"), False)

print("FAILED" if bad else "windowed save round trip intact")
sys.exit(1 if bad else 0)
