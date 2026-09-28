#!/usr/bin/env python3
"""The friend row's u64 bitfield behind POL_FRIEND_BITFIELD (default off).

    python tests/test_friend_bitfield.py

Project Crystal Server reads friend row +0x00 as one u64 (valid, ignore, level,
handle slot, handle id in bits 13-50, group, pending). This pins that reading
against SE's own words, the served row with the knob off (unchanged) and on,
the profile resolver for the plain handle id the client then sends, and the
2:6 valid-bit delete reading against SE's frames.
"""
import os
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))

tmp = tempfile.mkdtemp(prefix="friendbits-")
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import pgtest  # noqa: E402
pgtest.use_fresh_database()
os.environ["POL_DATA_DIR"] = tmp
os.environ["POL_LOG_DIR"] = tmp
os.environ["POL_RESOURCE_DIR"] = os.path.join(tmp, "resources")
os.environ.pop("POL_FRIEND_BITFIELD", None)

import accounts  # noqa: E402
import responders as R  # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


def hid_of(word):
    return (word >> 13) & ((1 << 38) - 1)


print("SE's own words, read as Crystal's u64")
# (low dword, +0x04 dword, the id SE itself used for that person)
for lo, hi, want, who in ((0x238EA040, 0x0000000A, 0x511C75, "Lex z_hid"),
                          (0xEA13A021, 0x00000004, 0x27509D, "Yui z_hid"),
                          (0x1EC96021, 0x06000004, 0x20F64B, "Mirabel z_hid")):
    chk(who, hex(hid_of(lo | hi << 32)), hex(want))
word = 0x1EC96021 | 0x06000004 << 32
chk("settled word: group 3, not pending", ((word >> 57) & 0xF, word >> 62 & 1),
    (3, 0))
word = 0x1EC96021 | 0x5A000009 << 32
chk("TombArrington pending word: bit 62 set", word >> 62 & 1, 1)
chk("SE row 0x21: valid, level 1, slot 0", (0x21 & 1, 0x21 >> 5 & 3, 0x21 >> 7 & 0x3F),
    (1, 1, 0))
chk("SE ignore scope 0x31: ignore bit", 0x31 >> 4 & 1, 1)
chk("SE ignore scope 0x51: still ignored, level 2", (0x51 >> 4 & 1, 0x51 >> 5 & 3),
    (1, 2))
chk("SE delete 0xE2: valid bit clear", 0xE2 & 1, 0)

HID = 0x123


def row(**kw):
    args = dict(rec_size=168, guid=0x91BC041E2C008C00, name="Friend", hid=HID,
                index=3, handle_slot=2)
    args.update(kw)
    return R._friend_list_record(**args)


print("knob OFF: the row is what it was")
r = row()
chk("head", hex(struct.unpack_from("<I", r, 0)[0]),
    hex((0x1EC96021 & 0x1FFF & ~(0x3F << 7)) | (2 << 7) | (HID << 13)))
chk("+0x04 settled", hex(struct.unpack_from("<I", r, 4)[0]), hex(0x06000004))
chk("+0x08 / +0x09 (offline)", (r[8], r[9]), (3, 2))
r = row(status=1)
chk("+0x04 pending", hex(struct.unpack_from("<I", r, 4)[0]), hex(0x5A000009))
off_rows = {k: row(**kw) for k, kw in (
    ("plain", {}), ("pending", {"status": 1}), ("online", {"online": True}),
    ("flags", {"flags_low": 0x51}))}
chk("flags_low is ignored with the knob off",
    off_rows["flags"] == off_rows["plain"], True)

print("knob ON (=head)")
os.environ["POL_FRIEND_BITFIELD"] = "head"
r = row()
w = struct.unpack_from("<Q", r, 0)[0]
chk("u64", hex(w), hex(1 | 1 << 5 | 2 << 7 | HID << 13 | 3 << 57))
chk("the client's subject (bits 13-50) is the plain handle id", hid_of(w), HID)
chk("+0x08 / +0x09 untouched in head mode", (r[8], r[9]), (3, 2))
chk("bytes past +0x08 unchanged", r[8:] == off_rows["plain"][8:], True)
w = struct.unpack_from("<Q", row(status=1), 0)[0]
chk("pending: bit 62 + group 0xD", (w >> 62 & 1, w >> 57 & 0xF), (1, 0xD))
w = struct.unpack_from("<Q", row(flags_low=0x31), 0)[0]
chk("stored ignore 0x31 -> ignore bit, level 1", (w >> 4 & 1, w >> 5 & 3), (1, 1))
w = struct.unpack_from("<Q", row(flags_low=0x51), 0)[0]
chk("stored scope 0x51 -> ignore bit, level 2", (w >> 4 & 1, w >> 5 & 3), (1, 2))
big = 0x2512345678
w = struct.unpack_from("<Q", row(hid=big), 0)[0]
chk("a 38-bit id survives whole", hex(hid_of(w)), hex(big))

print("knob ON (=1)")
os.environ["POL_FRIEND_BITFIELD"] = "1"
r = row()
chk("+0x09 = +0x08 (positions)", (r[8], r[9]), (3, 3))

print("profile resolver")
db = accounts.connect()
me = accounts.ensure_member(db, "BITSME")
them = accounts.ensure_member(db, "BITSTHEM")
my_h = db.execute("SELECT * FROM handle WHERE member_id = %s", (me["id"],)).fetchone()
their_h = db.execute("SELECT * FROM handle WHERE member_id = %s",
                     (them["id"],)).fetchone()
db.close()
R._session_member_id = lambda: int(me["id"])
R._session_handle_id = lambda db: int(my_h["id"])


def item(fid, value):
    return bytes([fid, 0x01, 8, 0, 0, 0, 0, 0]) + int(value).to_bytes(8, "little")


def profile_name(zhid):
    req = b"\x00" * R._TLV_START + item(2, zhid) + b"\x00" * 4
    rec = R._profile_record(600, req)
    return rec[11:27].split(b"\x00")[0].decode("ascii")


chk("plain handle id resolves to that handle (knob on)",
    profile_name(int(their_h["id"])), their_h["handle_name"])
chk("the old tagged form still resolves (knob on)",
    profile_name(0x200000 | int(their_h["id"])), their_h["handle_name"])
os.environ.pop("POL_FRIEND_BITFIELD")
chk("knob off: a plain id falls back to the viewer (unchanged)",
    profile_name(int(their_h["id"])), my_h["handle_name"])

print("search-hit identity row")
db = accounts.connect()
their_row = db.execute("SELECT * FROM handle WHERE id = %s",
                       (int(their_h["id"]),)).fetchone()
off_tag = R._profile_identity_record(db, their_row, tagged=True)
os.environ["POL_FRIEND_BITFIELD"] = "1"
on_tag = R._profile_identity_record(db, their_row, tagged=True)
db.close()
w = struct.unpack_from("<Q", on_tag, 0)[0]
chk("knob on: low 13 bits 0x40, id = plain handle id",
    (w & 0x1FFF, hid_of(w)), (0x40, int(their_h["id"])))
w = struct.unpack_from("<I", off_tag, 0)[0] | struct.unpack_from("<I", off_tag, 4)[0] << 32
chk("knob off: tagged head unchanged (id reads 0x200000 | hid)",
    hex(hid_of(w)), hex(0x200000 | int(their_h["id"])))

print("2:6: the valid bit against SE's frames")


def frame(total, **blobs):
    buf = bytearray(total)
    for at, hx in blobs.items():
        raw = bytes.fromhex(hx)
        buf[int(at[1:], 16):int(at[1:], 16) + len(raw)] = raw
    return bytes(buf)


RENAME = frame(0x208, x150=(
    "606162630100965821a013ea0400000000000a00"
    "0000000091bc041e2c008c00436f6f6c20667269656e64203a3300a3"))
DELETE = frame(0x208, x150=(
    "6061626301002222e2c7e11908616e4102d70a00"
    "00000000b3531a0eaeed2c436676475bf9d5cb95bb880ee2695333a3"))
IGNORE_ADD = frame(0x2B0, x150=(
    "6061626302009658e2c7e11908616e4100d70a00"
    "00000000b3531a0eaeed2c436676475bf9d5cb95bb880ee2695333a3"),
    x200=("3100000000000034000044000000000091bc041e2c008c00"
          "59756900d595cf2d6c3f445462dc4c59"))
chk("header count at 0x154 (one record)", struct.unpack_from("<H", RENAME, 0x154)[0], 1)
chk("header count at 0x154 (two records)",
    struct.unpack_from("<H", IGNORE_ADD, 0x154)[0], 2)
for label, pt in (("RENAME", RENAME), ("DELETE", DELETE), ("IGNORE_ADD", IGNORE_ADD)):
    start, recs = R._friend_put_records(pt)
    heur = R._friend_put_deletes(pt)
    out, extra = R._friend_put_bitfield_deletes(pt, start, recs, heur)
    chk(label + ": the valid bit adds no delete", (len(out), extra),
        (len(heur), set()))
# A live-looking record (valid name) whose valid bit is CLEAR: only the bit
# reading calls it a delete.
CLEARED = bytearray(RENAME)
CLEARED[0x158] = 0x20
CLEARED[0x170:0x180] = b"Yui\x00" + bytes(12)
start, recs = R._friend_put_records(bytes(CLEARED))
heur = R._friend_put_deletes(bytes(CLEARED))
out, extra = R._friend_put_bitfield_deletes(bytes(CLEARED), start, recs, heur)
chk("bit-clear, named record: heuristic keeps it", len(heur), 0)
chk("bit-clear, named record: bit adds it as a delete", (len(out), len(extra)), (1, 1))
out, extra = R._friend_put_bitfield_deletes(bytes(CLEARED), start, recs, heur,
                                            renamed={bytes(recs[0])})
chk("...unless it was just claimed as a rename", (len(out), len(extra)), (0, 0))
os.environ.pop("POL_FRIEND_BITFIELD")

print("FAILED: %d" % bad if bad else "all ok")
sys.exit(1 if bad else 0)
