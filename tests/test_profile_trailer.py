#!/usr/bin/env python3
"""The 0xB0 profile trailer behind POL_PROFILE_TRAILER (default off).

    python tests/test_profile_trailer.py

Every profile record ends in the subject's 168-byte friend row plus an 8-byte
status block, placed at z_phead = align8(1 + sum(len + 1)) -- the shape
Project Crystal Server writes, checked here against the shape of SE's own FFXI
records (a retail profile capture, with the handle's name and guid replaced by
placeholders). What this pins:

  * the arithmetic reproduces every per-title record length in _CONTENT_SCHEMAS;
  * SE's content record really carries the handle record's trailer bytes;
  * knob OFF: the records are what they were (no trailer, unaligned z_phead);
  * knob ON: only z_phead's value and the trailer bytes move, and the status
    block follows the four SE samples (self / offline / online / away).
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))

tmp = tempfile.mkdtemp(prefix="proftrailer-")
os.environ["POL_ACCOUNTS_DB"] = os.path.join(tmp, "accounts.db")
os.environ["POL_STAMP_FILE"] = os.path.join(tmp, "stamps.json")
os.environ["POL_DATA_DIR"] = tmp
os.environ["POL_LOG_DIR"] = tmp
os.environ["POL_RESOURCE_DIR"] = os.path.join(tmp, "resources")
os.environ["POL_MEMBER_STATUS_FILE"] = os.path.join(tmp, "member-status.json")
os.environ["POL_TITLE_ZONE_FILE"] = os.path.join(tmp, "title-zone.json")
os.environ.pop("POL_PROFILE_TRAILER", None)

import accounts  # noqa: E402
import responders as R  # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


def align8(n):
    return (n + 7) & ~7


print("the arithmetic, against every schema the Viewer ships")
for code, (schema, rec_len, measured) in sorted(R._CONTENT_SCHEMAS.items()):
    used = 1 + sum(ln + 1 for _i, _n, ln, _t in schema)
    chk("code %d: align8(%d) + 0xB0" % (code, used),
        align8(used) + R._PROFILE_TRAILER_LEN, rec_len)
    if measured is not None:
        chk("code %d: the measured z_phead is the aligned one" % code,
            measured, align8(used))
handle_used = 1 + sum(1 + f[2] for f in R._PROFILE_SCHEMA)
chk("handle record: 424 + 0xB0 = 600", handle_used + R._PROFILE_TRAILER_LEN, 600)
chk("status block sits at 0x250", R._PROFILE_NOTIFY_AT, 0x250)

print("SE's own record shape (retail FFXI profile capture)")
# Handle record 0x1A0..0x258 and content record 0x60..0x118, transcribed with
# the handle's guid and name replaced by placeholders (the same in both).
SE_HANDLE_TAIL = bytes.fromhex(
    "0375C4806A03000340A08E230A000000" "00000000000000000011223344556677"
    "466F7800000000000000000000000000" "01000100BD80650AF5C16A0100000000"
    + "00" * 16 * 7 + "0101000000000000")
SE_CONTENT_TAIL = bytes.fromhex(
    "010003050097E24840A08E230A000000" "00000000000000000011223344556677"
    "466F7800000000000000000000000000" "01000100BD80650AF5C16A0100000000"
    + "00" * 16 * 7 + "0101000000000000")
chk("content +0x68..+0x118 == handle +0x1A8..+0x258",
    SE_CONTENT_TAIL[8:] == SE_HANDLE_TAIL[8:], True)
chk("SE's trailer is 0xB0 long", len(SE_HANDLE_TAIL[8:]), 0xB0)

print("fixture")
db = accounts.connect(os.environ["POL_ACCOUNTS_DB"])
me = accounts.ensure_member(db, "TRAILME")
them = accounts.ensure_member(db, "TRAILTHEM")
my_h = db.execute("SELECT * FROM handle WHERE member_id = ?",
                  (me["id"],)).fetchone()
their_h = db.execute("SELECT * FROM handle WHERE member_id = ?",
                     (them["id"],)).fetchone()
accounts.set_handle_profile(db, int(their_h["id"]), {17: 4})
for code in (3, 10):
    accounts.link_content_to_handle(db, int(their_h["id"]), code,
                                    str(40000000 + code))
db.commit()
cids = {}
for link in accounts.handle_content_list(db, int(their_h["id"])):
    if link["content_id"]:
        cids[int(link["content_code"])] = int(link["content_id"])
db.close()
chk("their handle owns a Content ID for every schema",
    sorted(c for c in R._CONTENT_SCHEMAS if c in cids),
    sorted(R._CONTENT_SCHEMAS))

R._session_member_id = lambda: int(me["id"])
online = {"on": False}
accounts.handle_online = lambda conn, hid: online["on"]
status = {"code": 0}
R._member_status = lambda member: status["code"]


def item(fid, value):
    return bytes([fid, 0x01, 8, 0, 0, 0, 0, 0]) + int(value).to_bytes(8, "little")


def request(*items):
    return b"\x00" * R._TLV_START + b"".join(item(i, v) for i, v in items) \
        + b"\x00" * 4


def content(code):
    rec_len = R._CONTENT_SCHEMAS[code][1]
    return R._content_profile_record(rec_len, request((1, cids[code]),
                                                      (2, cids[code])))


def handle(hrow):
    return R._profile_record(600, request((2, accounts.handle_guid(int(hrow["id"])))))


def phead_value(rec):
    return int.from_bytes(rec[2:10], "little")


print("knob OFF: what the records were")
off_content = {c: content(c) for c in sorted(R._CONTENT_SCHEMAS)}
off_handle = handle(their_h)
for code, rec in off_content.items():
    schema, rec_len, measured = R._CONTENT_SCHEMAS[code]
    used = 1 + sum(ln + 1 for _i, _n, ln, _t in schema)
    chk("code %d z_phead unaligned (or measured)" % code, phead_value(rec),
        measured if measured is not None else used)
    at = align8(used)
    chk("code %d trailer region all zero" % code,
        rec[at:at + 0xB0] == bytes(0xB0), True)
chk("handle 0x250 status block zero", off_handle[0x250:0x258], bytes(8))

print("knob ON")
os.environ["POL_PROFILE_TRAILER"] = "1"
db = accounts.connect(os.environ["POL_ACCOUNTS_DB"])
their_row = db.execute("SELECT * FROM handle WHERE id = ?",
                       (int(their_h["id"]),)).fetchone()
idrec = R._profile_identity_record(db, their_row)
db.close()
for code in sorted(R._CONTENT_SCHEMAS):
    rec = content(code)
    old = off_content[code]
    schema = R._CONTENT_SCHEMAS[code][0]
    at = align8(1 + sum(ln + 1 for _i, _n, ln, _t in schema))
    chk("code %d z_phead = aligned %d" % (code, at), phead_value(rec), at)
    chk("code %d trailer row = the owner's friend row" % code,
        rec[at:at + 168] == idrec, True)
    chk("code %d status block (offline, purpose 4)" % code,
        rec[at + 168:at + 176].hex(), "0001000400000000")
    diff = [i for i in range(len(rec)) if rec[i] != old[i]
            and not (2 <= i < 10) and not (at <= i < at + 0xB0)]
    chk("code %d nothing else moved" % code, diff, [])

on_handle = handle(their_h)
chk("handle: only 0x250..0x258 moved",
    [i for i in range(600) if on_handle[i] != off_handle[i]
     and not (0x250 <= i < 0x258)], [])
chk("handle, offline member", on_handle[0x250:0x258].hex(), "0001000400000000")
online["on"] = True
chk("handle, online in the Viewer", handle(their_h)[0x250:0x258].hex(),
    "01030004e8030000")
status["code"] = R._STATUS_AWAY
chk("handle, away", handle(their_h)[0x250:0x258].hex(), "01020004e8030000")
status["code"] = 0
chk("handle, own profile (SE: 01 01 00 00, zone 0)",
    handle(my_h)[0x250:0x258].hex(), "0101000000000000")

os.environ.pop("POL_PROFILE_TRAILER")
online["on"] = False
chk("knob back OFF restores the old handle record",
    handle(their_h) == off_handle, True)
chk("knob back OFF restores the old content record",
    content(10) == off_content[10], True)

print("FAILED: %d" % bad if bad else "all ok")
sys.exit(1 if bad else 0)
