#!/usr/bin/env python3
"""Lobby 1:11, the character bind query, gets the 48 bytes the console reads.

    python tests/test_lobby_char_bind.py

A PS2 in FFXI or in Dirge of Cerberus sends 1:11. When the server had no
length for it, it answered the 8-byte default and the console waited out the
180 s idle timer. The console's pump (US 1.15.05
pol.pex 0x001405a0) reads exactly 0x30 payload bytes with the checksum on,
then a guid at +0x00, a 15-byte handle name at +0x10 and a found flag at
+0x20. The request here has the captured layout with made-up values.
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import pgtest  # noqa: E402
pgtest.use_fresh_database()
os.environ.pop("POL_LOBBY_PAYLEN", None)

import responders  # noqa: E402,F401  (loads the core package the way the server does)
from core import characters, lobbybind, lobbyops, lobbyreply, paylen  # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


def request(chara_no, code, slot=0):
    pt = bytearray(0x28 + 0x18)
    pt[1], pt[2] = 0x01, 0x0B
    body = bytearray(0x18)
    struct.pack_into("<I", body, 0x08, chara_no)
    struct.pack_into("<H", body, 0x0C, code)
    body[0x0E] = slot
    struct.pack_into("<I", body, 0x14, lobbybind._lobby_cksum(bytes(body[:0x14])))
    pt[0x28:] = body
    return bytes(pt)


print("the length")
chk("1:11 has a table row", paylen._LOBBY_PAYLEN.get((0x01, 0x0B)), 48)
chk("and the dispatcher returns it", paylen._lobby_paylen(0x01, 0x0B, request(1, 10)), 48)
chk("the opcode table names it", lobbyops.lookup(0x01, 0x0B).name, "character bind query")

print("the answer")
handles = [(7, "Alpha", {}), (9, "Bravo", {})]
chars = [(0, 0, 2, "1000000002", True), (1, 0, 10, "1000000123", True),
         (1, 1, 3, "1000000003", True)]
guid = lambda hid: 0x0100000000000000 | hid           # noqa: E731
rep = characters.char_bind_reply(1000000123 & 0xFFFFFFFF, 10, 1, chars, handles, guid)
chk("48 bytes", len(rep), 48)
chk("found", rep[0x20], 1)
chk("the holder's guid at +0x00", struct.unpack_from("<Q", rep, 0)[0], guid(9))
chk("the holder's name at +0x10", rep[0x10:0x1F].rstrip(b"\0"), b"Bravo")
chk("+0x21 echoes the request slot", rep[0x21], 1)
chk("trailer left for the signer", rep[0x2C:], b"\0\0\0\0")
rep = characters.char_bind_reply(0x00200033, 10, 0, chars, handles, guid)
chk("an unmatched number on a title one handle holds still names it",
    rep[0x10:0x15], b"Bravo")
rep = characters.char_bind_reply(5, 1, 0, chars, handles, guid)
chk("a title we serve nobody is not found (all zero)", rep, bytes(48))
two = chars + [(0, 1, 10, "1000000999", True)]
rep = characters.char_bind_reply(5, 10, 0, two, handles, guid)
chk("an unmatched number on a title two handles hold is not found", rep[0x20], 0)

print("somebody else's character (Tobin's 360 asks for Mira's)")
# On a title WITH world ids the number is a character the asker is looking at
# in the game. It was answered with the asker's own handle (the one-handle
# guess above), so every message and friend request sent from inside FFXI
# went back to its sender.
chars_xi = [(0, 0, 1, "30000101", True)]
handles_xi = [(89, "Tobin", {})]
saved_world = characters.titles.character_world
characters.titles.character_world = (
    lambda code, cid: 0x00200058 if (code, cid) == (1, 30000101) else None)
try:
    rep = characters.char_bind_reply(0x00200058, 1, 0, chars_xi, handles_xi, guid)
    chk("the asker's own character still names the asker",
        rep[0x10:0x15], b"Tobin")
    rep = characters.char_bind_reply(0x00200056, 1, 0, chars_xi, handles_xi, guid)
    chk("another member's character is NOT guessed as the asker's own",
        rep[0x20], 0)
    other = lambda code, no: ((34, "Arden")             # noqa: E731
                              if (code, no) == (1, 0x00200056) else None)
    rep = characters.char_bind_reply(0x00200056, 1, 0, chars_xi, handles_xi,
                                     guid, resolve=other)
    chk("...it is resolved across members", rep[0x10:0x15], b"Arden")
    chk("with that handle's guid", struct.unpack_from("<Q", rep, 0)[0], guid(34))
    chk("found", rep[0x20], 1)
    rep = characters.char_bind_reply(0x00200099, 1, 0, chars_xi, handles_xi,
                                     guid, resolve=other)
    chk("a character nobody holds is not found (all zero)", rep, bytes(48))
    rep = characters.char_bind_reply(0x00200033, 10, 0, chars, handles, guid,
                                     resolve=other)
    chk("a title with no world ids keeps the one-handle guess",
        rep[0x10:0x15], b"Bravo")
finally:
    characters.titles.character_world = saved_world

print("the resolver: world number -> Content ID -> the handle it is linked to")
saved_by_world = characters.titles.character_by_world
saved_by_cid = characters.accounts.handle_by_content_id
characters.titles.character_by_world = (
    lambda code, world: 30000202 if (code, world) == (1, 0x00200056) else None)
characters.accounts.handle_by_content_id = (
    lambda db, cid, active_only=False: ({"id": 34, "handle_name": "Arden"}
                                        if int(cid) == 30000202 else None))
try:
    chk("a known number names its holder", characters._bind_resolve(1, 0x00200056),
        (34, "Arden"))
    chk("an unknown number is None", characters._bind_resolve(1, 0x00200099), None)
    chk("a title with no map is None", characters._bind_resolve(10, 0x00200056), None)
finally:
    characters.titles.character_by_world = saved_by_world
    characters.accounts.handle_by_content_id = saved_by_cid

print("the reply on the wire")
characters.handlelists._db_chars = lambda: chars
characters.handlelists._db_handles = lambda: handles
pt = lobbyreply._build_lobby_reply_pt(request(1000000123, 10, 1), "127.0.0.1")
n = struct.unpack_from("<I", pt, 4)[0]
chk("declared length", n, 48)
pay = pt[24:24 + n]
chk("signed: trailer is the checksum of the first 44",
    struct.unpack_from("<I", pay, 44)[0], lobbybind._lobby_cksum(bytes(pay[:44])))
chk("found flag on the wire", pay[0x20], 1)

print()
print("test_lobby_char_bind: " + ("OK" if not bad else "FAILED (%d)" % bad))
sys.exit(1 if bad else 0)
