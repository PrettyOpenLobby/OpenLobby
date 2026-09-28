#!/usr/bin/env python3
"""The character a friend is playing, in the friend-status record.

    python tests/test_presence_character.py

A title plugin that knows which character a member plays
(`titles.Title.presence_character`) has it put in the record's 0x08 field.
The layout comes from LandSandBoat's xi_profile, not from our own capture, so
this checks our bytes against their struct and nothing more:

  1. RECORD: with a character, +0x12 = 1, chunk flag 0x08 set, and the 16
     bytes after the block are `u16 1 | u16 content code | u32 sub id |
     u64 user id`, placed before NAME as the flag order requires.
  2. TWIN: with no character the record is byte-identical to one built
     without the argument.
  3. DISPATCH: the core asks only the title whose content code is the zone,
     and a title that answers nothing gives no character.
  4. WATCH: titles.playing_characters merges every title's map.
"""
import os
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))
TMP = tempfile.mkdtemp(prefix="presence-character-")
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_LOG_DIR"] = TMP

import responders as R  # noqa: E402
import titles  # noqa: E402

FAILS = []


def check(ok, label, detail=""):
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                           "  --  " + detail if detail else ""))
    if not ok:
        FAILS.append(label)


def b64decode(s):
    inv = {c: i for i, c in enumerate(R._B64)}
    out = bytearray()
    for i in range(0, len(s), 4):
        v = 0
        for c in s[i:i + 4]:
            v = (v << 6) | inv[c]
        v <<= 6 * (4 - len(s[i:i + 4]))
        out += bytes([(v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF])
    return bytes(out)


print("1. record with a character")
char = (1, 2097153, 1000000101)
enc = R.build_field_push_record(0x1122334455667788, 3, state=3, zone=1,
                                character=char, name="Lex", seq=5, when=1)
main, chunk = b64decode(enc[:96]), b64decode(enc[96:])
check(main[0x12] == 1, "+0x12 playing bit", f"{main[0x12]:#x}")
check(struct.unpack_from("<H", main, 0x14)[0] == 1, "+0x14 zone is the title")
check(bool(chunk[0] & 0x08 and chunk[0] & 0x01 and chunk[0] & 0x10),
      "flags BLOCK|F08|NAME", f"{chunk[0]:#x}")
prim = chunk[8:24]
check(prim == struct.pack("<HHIQ", 1, 1, 2097153, 1000000101),
      "0x08 field = sqPolCharacterPrimitive", prim.hex())
check(chunk[24:24 + 3] == b"Lex", "NAME follows the character field")

print("2. twin: no character changes nothing")
a = R.build_field_push_record(0x1122334455667788, 3, state=3, zone=1000,
                              seq=5, when=1)
b = R.build_field_push_record(0x1122334455667788, 3, state=3, zone=1000,
                              character=None, seq=5, when=1)
check(a == b, "character=None is byte-identical")
check(b64decode(a[:96])[0x12] == 0 and not b64decode(a[96:])[0] & 0x08,
      "and sets neither +0x12 nor 0x08")


class Playing(titles.Title):
    tag = b"TS1"
    content_code = 77

    def presence_character(self, member_id):
        return (0x2A, 900 + member_id) if member_id == 5 else None

    def playing_characters(self):
        return {5: 905}


class Quiet(titles.Title):
    tag = b"TS2"
    content_code = 78


print("3. dispatch by zone")
titles.register(Playing())
titles.register(Quiet())
check(R._presence_character(5, 77) == (77, 0x2A, 905),
      "the title of the zone answers, with its content code")
check(R._presence_character(6, 77) is None, "a member it does not know: none")
check(R._presence_character(5, 78) is None, "another title's zone: none")
check(R._presence_character(5, 1000) is None, "in the Viewer: none")
check(R._presence_character(5, None) is None, "no zone: none")

print("4. what the watcher reads")
check(titles.playing_characters() == {(77, 5): 905}, "merged by (code, member)",
      repr(titles.playing_characters()))

print("FAILED: " + ", ".join(FAILS) if FAILS else "all passed")
sys.exit(1 if FAILS else 0)
