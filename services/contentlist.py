#!/usr/bin/env python3
"""PlayOnline free-contents (games-menu) list codec -- the SERVER side of the
games menu.

The games menu is server-driven, not a client filter (see the pol-games-menu
memory, "RESOLVED"): during login the server sends **command code 1** carrying a
**192-byte (0xC0) big-endian** content-list block, which polcore.dll caches at
its content global and app.dll renders verbatim. To show Tetra Master we simply
include an entry with content id 2 in that block -- no client patch.

Block layout (from RE of the reader polcore 0x040d5670 / writer 0x040d62f4, all
big-endian = network order):

    +0x00  u16 flags        1 = valid / received
    +0x04  u16 entry_count  number of entries the UI shows (0..8)
    +0x10  entry[0]         8-byte entries, stride 8, up to 8
           ...              entry: u16 content_id then 6 bytes UNCONFIRMED
    +0xa0  tail             `26 59 4d 54 a2 e7 59 4a` x4, not read by the UI

Content ids (polcore resolver table @ RVA 0x9accc):
    1 = FinalFantasyXI   2 = TetraMaster   3 = Janhourou   >=4 -> "Contents%04d"
Ids beyond the resolver table are still perfectly valid on the wire (the reader
accepts 1..1023); 4 = FRONT MISSION ONLINE and 11 = FANTASY EARTH are both
installed locally and listed in in01.pml, which supplies their display names.

CAVEAT, and it is probably PERMANENT rather than pending. Every live capture has
had entry_count == 0: SE's surviving service sends an empty list and the menu
collapses to the FFXI primary. So the 8-byte per-entry layout beyond the leading
u16 id is not confirmed against a real count>0 example, and `build_block` fills
the id and ZEROES the other six bytes.

WARNING: Re-checked 2026-08-18 (stopgap audit) and the honest reading changed. The note
here used to say "revisit once a decoded lobby/login capture yields a populated
list", which implies a capture that is coming. It is not: the only service that
could produce one stopped populating the list before any capture in this project
was taken, so waiting is not a plan. What is left is a period screenshot or an
archived capture from when the menu still listed more than one title -- the §10r
route -- or nothing.

Meanwhile the zeros are load-bearing in the good way: the reader accepts them,
the menu renders every id we send, and the six bytes have never been observed to
matter. That is a well-tested unknown rather than an open bug, and it should not
be filed as work that a future capture will do for us.

This module is the single source of truth for the format -- lobbydec.py imports
it when reachable.
"""
import re
import struct

#: 1..3 are the client's own resolver table (polcore RVA 0x9accc). 4 and 11 are
#: OURS: the client renders any id >= 4 as "Contents%04d", so these names never
#: reach the UI -- the menu labels come from in01.pml -- but they keep our logs
#: and `accounts.py list` readable. Both titles are installed locally and
#: registered in the PlayOnlineUS hive (see pol-regional-registry-hives).
CONTENT_NAMES = {1: "FinalFantasyXI", 2: "TetraMaster", 3: "Janhourou",
                 4: "FrontMissionOnline", 10: "DirgeOfCerberus",
                 11: "FantasyEarth", 14: "PolFriendList",
                 15: "FinalFantasyXITest"}
BLOCK_LEN = 0xC0                                   # 192
ENTRY_OFF = 0x10
ENTRY_STRIDE = 8
MAX_ENTRIES = 8
TAIL_OFF = 0xA0
# The tail SE's reader ignores; kept so a round-trip reproduces a real block.
DEFAULT_TAIL = bytes.fromhex("26594d54a2e7594a") * 4


#: The same titles, spelled the way a PLAYER should see them. The table above is
#: for the wire and the logs -- `FrontMissionOnline` is an identifier, and it is
#: deliberately one word so it greps and tabulates. Anything shown in the client
#: or the admin panel wants this one instead: registration's completion screen
#: was printing "FantasyEarth, FrontMissionOnline" because it fell through to
#: the identifier table.
#:
#: Keep the two keyed alike. `content_title` splits an unlisted identifier on its
#: capitals rather than showing it glued, so a new id degrades to "Some Title"
#: instead of "SomeTitle".
CONTENT_TITLES = {1: "FINAL FANTASY XI",      # SE sets it in caps everywhere
                  2: "Tetra Master",
                  3: "JongHoLow",
                  4: "Front Mission Online",
                  10: "DIRGE of CERBERUS -FINAL FANTASY VII-",
                  11: "Fantasy Earth",
                  14: "PlayOnline Friend List",
                  15: "FINAL FANTASY XI Test Server"}


def content_name(cid):
    """The identifier for this content id -- for logs, tables and the wire."""
    return CONTENT_NAMES.get(cid, f"Contents{cid:04d}")


def content_title(cid):
    """The title for this content id, as a player should read it."""
    if cid in CONTENT_TITLES:
        return CONTENT_TITLES[cid]
    name = CONTENT_NAMES.get(cid)
    if not name:
        return "Content %d" % cid
    # CamelCase -> spaced, keeping runs of capitals together (XI, PolFL).
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)


def parse_block(block):
    """Parse a 192-byte content-list block. Returns a dict; tolerant of short
    input (returns what it can) so it is safe to run on speculative decodes."""
    b = bytes(block)
    flags = struct.unpack_from(">H", b, 0)[0] if len(b) >= 2 else None
    count = struct.unpack_from(">H", b, 4)[0] if len(b) >= 6 else None
    entries = []
    n = count if (count is not None and 0 <= count <= MAX_ENTRIES) else MAX_ENTRIES
    for k in range(n):
        off = ENTRY_OFF + k * ENTRY_STRIDE
        if off + 2 > len(b):
            break
        cid = struct.unpack_from(">H", b, off)[0]
        raw = b[off:off + ENTRY_STRIDE]
        entries.append({"id": cid, "name": content_name(cid), "raw": raw.hex()})
    return {"flags": flags, "count": count, "entries": entries,
            "len": len(b)}


def looks_like_block(block):
    """Heuristic: a plausible content-list block (valid flags + sane count +
    known/sane ids). Used by the scanner to reject coincidental 0xC0 windows."""
    p = parse_block(block)
    if p["flags"] not in (0, 1) or p["count"] is None or not (0 <= p["count"] <= MAX_ENTRIES):
        return False
    # if it claims entries, their ids should be in the valid 1..1023 range
    for e in p["entries"][: p["count"]]:
        if not (1 <= e["id"] <= 1023):
            return False
    return True


def build_block(ids=(1, 2), flags=1, tail=DEFAULT_TAIL, content_ids=None):
    """Build a 192-byte content-list block listing `ids` (default FFXI + Tetra
    Master).

    Each 8-byte entryA is { u16 content_id, u16 fieldB, u32 fieldC }. fieldC is
    the per-game CONTENT ID: the app.dll populator (0x4aa9f6c) reads entry+4,
    computes `fieldC - 0x6270`, and displays it -- and a zero here is what makes
    the client refuse to launch with "You have no content id for <game>". So
    `content_ids` (a {content_id: number} map) fills fieldC; a game with no entry
    gets 0 (the old behaviour = unlaunchable). fieldB is left 0 (plain label)."""
    ids = list(ids)[:MAX_ENTRIES]
    content_ids = content_ids or {}
    b = bytearray(BLOCK_LEN)
    struct.pack_into(">H", b, 0, flags & 0xFFFF)
    struct.pack_into(">H", b, 4, len(ids))
    for k, cid in enumerate(ids):
        off = ENTRY_OFF + k * ENTRY_STRIDE
        struct.pack_into(">H", b, off, cid & 0xFFFF)
        struct.pack_into(">I", b, off + 4, int(content_ids.get(cid, 0)) & 0xFFFFFFFF)
    b[TAIL_OFF:TAIL_OFF + len(tail)] = tail[:BLOCK_LEN - TAIL_OFF]
    return bytes(b)


def scan(plaintext):
    """Find content-list blocks in a decrypted stream. Looks for command code 1
    (as a leading u32-BE or u16-LE marker) immediately followed by a 0xC0 window
    that passes `looks_like_block`. Returns [(offset, parsed), ...]."""
    pt = bytes(plaintext)
    hits = []
    for i in range(len(pt) - 4):
        cand = None
        if pt[i:i + 4] == b"\x00\x00\x00\x01":            # u32-BE command 1
            cand = i + 4
        elif pt[i] == 1 and pt[i + 1] == 0:               # u16-LE command 1
            cand = i + 2
        if cand is not None and cand + BLOCK_LEN <= len(pt):
            blk = pt[cand:cand + BLOCK_LEN]
            if looks_like_block(blk):
                hits.append((cand, parse_block(blk)))
    return hits


if __name__ == "__main__":
    # Self-check: build FFXI+TetraMaster, round-trip through parse.
    blk = build_block([1, 2])
    assert len(blk) == BLOCK_LEN
    p = parse_block(blk)
    assert p["count"] == 2 and [e["id"] for e in p["entries"]] == [1, 2], p
    assert looks_like_block(blk)
    print("build_block([1,2]) ->", blk.hex())
    print("parse ->", p)
