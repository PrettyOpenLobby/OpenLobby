#!/usr/bin/env python3
"""Pin how the CLIENT identifies a person, across the two records that carry it.

WHY THIS EXISTS. app.dll does not match people by name. It builds a THREE-DWORD
key -- `0x488c6fc(out, guid_lo, guid_hi, slot)`, storing `guid_lo ^ 0x12345678`,
`guid_hi`, `slot` -- and looks it up with `0x488ddba(which, key)` in the array at
`0x4d34bc4` (stride 0xF0; entries 0..199 are FRIENDS, 200..299 the IGNORE list).
Two places fill that key and they have to agree:

    a FRIEND    `0x488f0f0` -> polcore cft 371 `GetFriend` -> guid at slot+0x00,
                slot = (slot+0x9c >> 13) & 0x3F, i.e. bits 7..12 of the 2:3
                record's head word
    a MESSAGE   `0x4aadaf3` -> guid at record +0x00/+0x04, slot at record +0x3C

When the message misses BOTH arrays the client raises StringTable 18161,
*"%s is not waiting for friend registration. Resend "Let's be friends!"
request?"* -- which is what marking a `Friend registration accepted` message as
read was doing, every time, for a friend added from a search.

SE's own wire settles the slot half, 24 message records against two friend rows:

    2:3 head 0xEA13A021 -> bits 7..12 = 0   Cyn     22 records, +0x3C = 0
    2:3 head 0x048CE0A1 -> bits 7..12 = 1   Yatih    2 records, +0x3C = 1

and Cyn and Yatih share ONE guid (`91bc041e2c008c00`) -- two handles of one
member -- which is exactly why the guid alone is not the key.

Run: python tools/mailident_test.py
"""
from __future__ import annotations

import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "services"))
os.environ.setdefault("POL_ACCOUNTS_DB", "/nonexistent/mailident.db")

import responders as R  # noqa: E402

FAILED = []


def check(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label
          + ("" if ok else f": {got!r}  (want {want!r})"))
    if not ok:
        FAILED.append(label)


def head_slot(head):
    """The handle slot the client reads out of a 2:3 head word: bits 7..12."""
    return (head >> 7) & 0x3F


print("\nSE's own bytes: the head word and +0x3C are the same field")
# Read off polshim-se.429364.log: the 2:3 reply at line 155287 and the 24 `O/m/`
# tokens in the same capture. Structure, not account data.
check("Cyn's 2:3 head 0xEA13A021 -> slot 0", head_slot(0xEA13A021), 0)
check("Yatih's 2:3 head 0x048CE0A1 -> slot 1", head_slot(0x048CE0A1), 1)
check("our replayed head keeps the low 13 bits SE sends",
      0x1EC96021 & 0x1FFF, 0x0021)
check("...so an untagged friend is slot 0, as SE serves",
      head_slot(0x1EC96021), 0)

print("\nthe 2:3 row we serve and the message we mint name the same slot")
row = R._friend_list_record(168, 0x8000000000D, "PS2Tester", hid=13)
row_head = struct.unpack_from("<I", row, 0x00)[0]
check("our own 2:3 row lands on a slot the client can key on",
      head_slot(row_head), 0)

rec = bytearray(0x48)
struct.pack_into("<H", rec, 0x3C, 0)
check("a minted record's +0x3C matches that row", rec[0x3C], head_slot(row_head))

print("\nthe profile record carries the subject's own 2:3 row in its tail")
# The packed fields end at 424 (z_phead says so, and verify_profile.py pins it);
# SE puts a whole 168-byte friend record immediately after, ending at 592.
check("the identity row starts where the fields end", R._PROFILE_IDREC_AT, 0x1A8)
check("it is a whole 2:3 record", R._PROFILE_IDREC_LEN, 168)
check("its guid lands on the byte the 2026-08-11 capture called 'the handle GUID'",
      R._PROFILE_IDREC_AT + 0x10, 0x1B8)
check("its name lands on the byte that capture called 'the handle name'",
      R._PROFILE_IDREC_AT + 0x18, 0x1C0)
check("and it ends inside the 600-byte record",
      R._PROFILE_IDREC_AT + R._PROFILE_IDREC_LEN <= 600, True)

print("\n+0x42 is a STATE, so it must not key the stored file")
# One message wears three values there: 0x00 as a client writes it, 0x02 in every
# stored path, 0x03 in the arrival push. Measured 2026-08-17: a client wrote its
# own acceptance with 0x00 and read it back asking for 0x02, so the object was
# served as 34 zero bytes and its 3:2 found "nothing to retire".
base = bytearray(0x48)
struct.pack_into("<Q", base, 0x00, 0x8000000000D ^ R._PUSH_GUID_MASK)
struct.pack_into("<Q", base, 0x08, 0x860FB3E2A2 ^ R._PUSH_GUID_MASK)
base[0x10:0x14] = b"Cyn\x00"
struct.pack_into("<H", base, 0x3E, R.MAIL_KIND_FRIEND_ACCEPTED)

names = {}
for state in (0x00, 0x02, 0x03):
    rec = bytearray(base)
    struct.pack_into("<I", rec, 0x40, 0x100003E8 | (state << 16))
    assert rec[0x42] == state, rec[0x42]
    path = R._MAIL_PATH_PREFIX + R._b64encode(bytes(rec))
    names[state] = R._mail_name(path)

check("a client-written 0x00 and a stored 0x02 are ONE file",
      names[0x00], names[0x02])
check("the pushed 0x03 is that same file", names[0x03], names[0x02])
check("the name is still a real canonical mail name",
      bool(names[0x02] and names[0x02].startswith(R._MAIL_FILE_PREFIX)), True)
check("and it still decodes back to a path",
      bool(R._mail_path_of(names[0x02])), True)

os.environ["POL_MAIL_STATE_CANON"] = "0"
try:
    rec = bytearray(base)
    struct.pack_into("<I", rec, 0x40, 0x100003E8)
    off = R._mail_name(R._MAIL_PATH_PREFIX + R._b64encode(bytes(rec)))
    check("POL_MAIL_STATE_CANON=0 restores the literal-token name",
          off != names[0x02], True)
finally:
    os.environ["POL_MAIL_STATE_CANON"] = "1"

print("\nfields the canonicalisation must NOT disturb")
rec = bytearray(base)
struct.pack_into("<I", rec, 0x40, 0x100003E8)
meta = R._mail_meta(R._MAIL_PATH_PREFIX
                    + R._b64encode(bytes(R._b64decode(
                        R._mail_canon_token(R._b64encode(bytes(rec)))))))
check("the kind survives", meta and meta["kind"], R.MAIL_KIND_FRIEND_ACCEPTED)
check("the sender survives", meta and meta["sender"], "Cyn")
check("the recipient survives", meta and meta["recipient_guid"], 0x860FB3E2A2)

print()
if FAILED:
    print(f"FAILED ({len(FAILED)} failure{'s' if len(FAILED) > 1 else ''})")
    sys.exit(1)
print("PASS")
