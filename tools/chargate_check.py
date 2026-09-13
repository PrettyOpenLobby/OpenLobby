"""Does our 1:3 reply actually open a game's Run button?

Runs the CLIENT's own two steps over our own payload, offline, with no client
and no DB:

  1. polcore's 1:3 record loop (0x37e247e..0x37e26d0): 104-byte records into the
     64-entry character table at 0x3bc3080, index = rec[0x00], plus the handle
     binding into 0x3bc5800.
  2. the launch gate `app.dll+0x199093`: true iff some table entry has the
     present bit AND a u16 at +0x02 equal to the content id asked about. Its two
     `al == 0` callers are the "You have no Content ID for %s" refusals
     (string 26073) at app.dll+0x1DEDBD / +0x1DEE21.
  3. FFXI's WORLD LOOKUP `FFXiMain FUN_100FFE00`, reached from char-select
     sub-state 14. Same table, a much stricter test -- and the one that decides
     whether the world socket is ever opened (POL-0001).

Run after any change to `_char_record` or `_db_chars`. Exits non-zero if a
linked title would still be refused. `smoke_chain.py` covers the chain; this
covers the one record whose bytes the smoke test cannot judge.
"""
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "services"))
os.environ.setdefault("POL_LOBBY_LIST_MODE", "1:3=chars")

import responders as R

TABLE = 104


def install(payload, count):
    """polcore's record loop. Returns {index: 104-byte table entry}."""
    table = {}
    handles = {}
    for i in range(count):
        rec = payload[8 + i * 0x68: 8 + (i + 1) * 0x68]
        idx = rec[0x00]
        if idx >= 0x40:                                  # cmp ebx,0x40 / jae
            continue
        e = table.setdefault(idx, bytearray(TABLE))
        e[0] |= 1                                        # or byte[...],1
        struct.pack_into("<H", e, 0x02, struct.unpack_from("<H", rec, 0x08)[0])
        e[0x04:0x08] = rec[0x0C:0x10]
        e[0x08:0x0C] = rec[0x10:0x14]
        e[0x0C:0x10] = rec[0x14:0x18]
        e[0x18:0x27] = rec[0x18:0x27]                    # 15-byte string
        packed = struct.unpack_from("<Q", e, 0x10)[0]
        packed = (packed & ~(0x3F << 16)) | ((rec[0x05] & 0x3F) << 16)
        packed = (packed & ~(1 << 15)) | ((rec[0x04] & 1) << 15)
        packed = (packed & ~(7 << 22)) | ((rec[0x05] & 7) << 22)
        struct.pack_into("<Q", e, 0x10, packed & 0xFFFFFFFFFFFFFFFF)
        if rec[0x04] != 0 and rec[0x05] < 0x40:          # the handle binding
            slot = handles.setdefault(rec[0x05], bytearray(40))
            old = slot[0x20 + rec[0x06]] | 1
            slot[0x20 + rec[0x06]] = ((idx & 0x3F) << 1) | (old & 0x81)
    return table, handles


def has_content_id(table, cid):
    """app.dll+0x199093."""
    for i in range(0x40):
        e = table.get(i)
        if e is None:
            continue
        if (e[0] & 1) and struct.unpack_from("<H", e, 0x02)[0] == cid:
            return True
    return False


def ffxi_world_lookup(table, ffxi_id, worldid, char_id24):
    """`FFXiMain FUN_100FFE00` -- the slot search that gates the world connect.

    Its three arguments come out of the `0x20` char-list record the lobby sent
    (which FFXI copies verbatim into its world context, 140-byte stride):
    `ffxi_id`, `worldid`, and the 24-bit `ffxi_id_world_tbl<<16 | ffxi_id_world`.
    Returns the matching slot, or -1 -- and -1 is FFXI writing -1 to
    `ctx+0x136AC` and aborting state 3 to 203, i.e. POL-0001.

    The `+0x04` comparand is transcribed shift-for-shift from the disassembly,
    overlap and all; see `pack_world_field` in the bridge.
    """
    id24 = char_id24 & 0xFFFFFF
    want = (((((worldid & 0xFFFF) << 8) | (id24 & 0xFFFF0000)) << 8)
            | (id24 & 0xFFFF)) & 0xFFFFFFFF
    for i in range(0x40):
        e = table.get(i)
        if e is None:
            continue
        if not (e[0] & 1):
            continue
        if struct.unpack_from("<H", e, 0x02)[0] != 1:
            continue
        if struct.unpack_from("<I", e, 0x08)[0] != ffxi_id:
            continue
        if struct.unpack_from("<I", e, 0x0C)[0] != 0:
            continue
        if struct.unpack_from("<I", e, 0x04)[0] != want:
            continue
        return i
    return -1


# Stand in for the DB: two handles, the first holding FFXI + Tetra Master +
# Fantasy Earth, the second holding Tetra Master only.
#
# TWO DIGIT WIDTHS, deliberately. Handle 0 carries the ALLOCATED 8-digit serials
# the mint issues (accounts.allocate_content_id); the other two links carry
# 10-digit values.
#
# WARNING: Read those 10-digit ones as a WIDTH TEST, not as live data: the legacy
# computed format was retired on 2026-08-23 (`tools/content_id_migrate.py`
# re-minted every row) and nothing issues one now. They stay because the record
# builder and the gate must be indifferent to how many digits an id has -- the
# id goes into a 15-byte string field and a u32 pair -- and a value WIDER than
# anything we currently mint is the stronger test of that. It is what makes the
# ceiling safe to move later.
#
# The fifth link is a handle's NINTH Content ID, served UNBOUND (`bind=False`).
# A handle's binding array is eight bytes at +0x20 and the record's position
# field is three bits, so a ninth BOUND record would not get a slot of its own --
# it would overwrite position 0 and take the first character out of the handle's
# Content ID list. Unbound is how an extra FFXI character is carried, and the
# claim that has to hold is that it is still PLAYABLE: the launch gate and the
# world lookup read the 64-slot table, which does not care about the binding.
# This file replays both, so that claim is tested here rather than argued from
# the disassembly. See responders._db_chars.
LINKS = [(0, 0, 1, "30000101", True), (0, 1, 2, "30000102", True),
         (0, 2, 11, "1000000111", True), (1, 0, 2, "1000000202", True),
         (0, 8, 1, "30000103", False)]
R._db_chars = lambda: LINKS

# ...and for the bridge's id map, whose real copy this container may not have.
# charid 0x010203 on world 0x20 exercises BOTH halves of the 24-bit id, which a
# small LSB charid (1) would not -- an implementation that dropped
# `ffxi_id_world_tbl` would still pass with charid 1 and fail in the field.
FFXI_CHARID = 0x010203
FFXI_WORLDID = 0x20
#: A SECOND character on the same handle -- the case the whole slot change is
#: for. Its own charid, so a lookup that matched it by accident (or matched the
#: first character's field) would show up as a wrong slot number below.
FFXI_CHARID2 = 0x040506


def _world_field(charid, worldid=None):
    worldid = FFXI_WORLDID if worldid is None else worldid
    return ((((worldid << 8) | (charid & 0xFF0000)) << 8)
            | (charid & 0xFFFF)) & 0xFFFFFFFF


R._ffxi_world_fields = lambda: {30000101: _world_field(FFXI_CHARID),
                                30000103: _world_field(FFXI_CHARID2)}

n = R._list_paylen(1, 3, R._list_count(1, 3))
count = R._list_count(1, 3)
payload = R._list_payload(1, 3, n)
print(f"count={count}  payload={len(payload)}B (expected {8 + count * 0x68})")
print("first record:", payload[8:8 + 0x20].hex(" "))

table, handles = install(payload, count)
print(f"installed {len(table)} character entries, {len(handles)} handle bindings")
for idx in sorted(table):
    e = table[idx]
    name = e[0x18:0x27].rstrip(b"\0").decode("cp932", "replace")
    print(f"  idx {idx}: present={e[0] & 1} content={struct.unpack_from('<H', e, 2)[0]}"
          f" str={name!r}")
for slot in sorted(handles):
    print(f"  handle {slot} binding: {handles[slot][0x20:0x28].hex(' ')}")

print()
ok = True
for cid, want in [(1, True), (2, True), (11, True), (3, False), (4, False)]:
    got = has_content_id(table, cid)
    flag = "OK " if got == want else "FAIL"
    if got != want:
        ok = False
    print(f"  {flag} hasContentId({cid:>2}) = {got}  (want {want})")
print()
# The bridge and this file each transcribe the SAME packing from the same
# disassembly, and they are the only two copies of it -- so the one failure this
# check exists to catch is the two drifting apart. Skipped, not failed, when the
# bridge is not importable (it lives outside the services image).
try:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    os.pardir, "lsb"))
    import ffxi_bridge as BR
except Exception as exc:                                   # pragma: no cover
    print(f"  --  bridge packing cross-check SKIPPED ({exc!r})")
else:
    for main_, world_, tbl_ in ((1, 0x20, 0), (0x0203, 0x20, 0x01),
                                (0xFFFF, 0, 0xFF), (0x1234, 0x0102, 0x7F)):
        id24 = (tbl_ << 16) | main_
        want = (((((world_ & 0xFFFF) << 8) | (id24 & 0xFFFF0000)) << 8)
                | (id24 & 0xFFFF)) & 0xFFFFFFFF
        got = BR.pack_world_field(main_, world_, tbl_)
        if got != want:
            ok = False
        print(f"  {'OK ' if got == want else 'FAIL'} bridge pack_world_field"
              f"({main_:#06x}, {world_:#04x}, {tbl_:#04x}) = 0x{got:08X}"
              f" (client computes 0x{want:08X})")

print()
# The world lookup. `ffxi_id` is the Content ID because the bridge rewrites the
# lobby's copy of it to exactly that; a mismatch here is POL-0001, not a refusal
# dialog -- the Run button works and the world connect never starts.
hit = ffxi_world_lookup(table, 30000101, FFXI_WORLDID, FFXI_CHARID)
print(f"  {'OK ' if hit >= 0 else 'FAIL'} FFXI world lookup"
      f"(ffxi_id=30000101, worldid=0x{FFXI_WORLDID:02X}, "
      f"char 0x{FFXI_CHARID:06X}) -> slot {hit}")
if hit < 0:
    ok = False
# Negative control: the same character on a DIFFERENT world must NOT match, or
# the check is passing on the present bit alone and proves nothing.
stray = ffxi_world_lookup(table, 30000101, FFXI_WORLDID ^ 1, FFXI_CHARID)
print(f"  {'OK ' if stray < 0 else 'FAIL'} ...and does not match world "
      f"0x{FFXI_WORLDID ^ 1:02X} (-> {stray})")
if stray >= 0:
    ok = False

# THE UNBOUND NINTH CONTENT ID. Everything below is the overflow design being
# tested rather than asserted: the record carries no handle binding, and it must
# still be found by BOTH gates.
hit2 = ffxi_world_lookup(table, 30000103, FFXI_WORLDID, FFXI_CHARID2)
print(f"  {'OK ' if hit2 >= 0 else 'FAIL'} an UNBOUND 9th Content ID is still "
      f"found by the world lookup (ffxi_id=30000103) -> slot {hit2}")
if hit2 < 0:
    ok = False
# ...and it must be a DIFFERENT table slot from the first character, or the two
# characters are sharing one entry and only one of them can ever be picked.
if hit2 >= 0 and hit2 == hit:
    ok = False
    print(f"  FAIL ...and it is its own table slot (got {hit2}, same as the first)")
elif hit2 >= 0:
    print(f"  OK  ...and it is its own table slot ({hit} vs {hit2})")
# The binding array is what it must NOT have touched. Position 0 still has to
# name character 0: if a future change sets +0x04 on the overflow record, this is
# the assertion that catches it -- the ninth id would land on position 0 and the
# player's FIRST character would vanish from the handle's Content ID list.
bind0 = handles.get(0, bytearray(40))[0x20:0x28]
print(f"  {'OK ' if (bind0[0] >> 1) == 0 else 'FAIL'} the unbound record left "
      f"handle 0's binding array alone (position 0 still names character "
      f"{bind0[0] >> 1}, want 0)")
if (bind0[0] >> 1) != 0:
    ok = False
bound_positions = sum(1 for b in bind0 if b)
print(f"  {'OK ' if bound_positions == 3 else 'FAIL'} ...and bound exactly the "
      f"three records that asked for it ({bound_positions})")
if bound_positions != 3:
    ok = False

print()
# THE +0x18 STRING IS THE CHARACTER NAME, not the Content ID digits -- confirmed
# against retail 2026-08-23, and it is what the Viewer's per-Content-ID sections
# under a handle render. `_char_display_name` owns the choice; these pin the two
# rules that are not free to change.
CASES = [
    # (content_code, content_id, handle_name, known-name map, expected, why)
    (1, "30000101", "Fox", {(30000101, 1): "Kupo"}, "Kupo",
     "FFXI takes the imported character name"),
    # WARNING: WAS "" AND THAT COST THE FFXI SLOT. SE serves an empty +0x18 only for a
    # Content ID with NO character; where one exists it sends the name. Serving
    # empty for every content we lack a name for made the client read them all
    # as characterless and drop the slot. Fall back, never blank.
    (1, "30000101", "Fox", {}, "Fox",
     "no character name known -> the HANDLE name, not a blank"),
    (2, "1000000102", "Fox", {}, "Fox",
     "Tetra Master falls back to the handle name"),
    # WARNING: THE REGRESSION THIS PINS, shipped and caught live the same day
    # (2026-08-23): tmrank's `cname` is filled from <CR> value[1], which is the
    # decimal CONTENT ID, not a name -- prod's pool is full of
    # {"cid": 30000046, "cname": "30000046"}. Preferring it put the digits back
    # in the Tetra Master slot, undoing the +0x18 fix from that morning. A
    # numeric pool name is the id echoed back and must not win over the handle
    # name.
    (2, "30000046", "Fox", {}, "Fox",
     "a NUMERIC pool cname is the Content ID, not a name -- handle name wins"),
    (2, "1000000102", "", {}, "1000000102",
     "...and to the DIGITS rather than empty: +0x18 is READ for the VS. COM "
     "seat banner, so an empty string there would be a fresh bug"),
    (3, "1000000003", "Fox", {}, "Fox",
     "same for Janhourou -- a nameless slot reads as 'no character'"),
    (3, "1000000003", "", {}, "1000000003",
     "...and with no handle name either, the digits: anything but empty"),
]
for code, cid, hname, names, want, why in CASES:
    got = R._char_display_name(code, cid, hname, names)
    if got != want:
        ok = False
    print("  %s name(content=%s, handle=%r, known=%s) = %r (want %r)"
          % ("OK " if got == want else "FAIL", code, hname, bool(names),
             got, want))
    print("         " + why)

# The kill switch has to restore the pre-2026-08-23 digits EVERYWHERE, because
# that is the only rollback if this reading is wrong on a title we cannot test.
os.environ["POL_CHAR_NAME"] = "0"
try:
    got = R._char_display_name(1, "30000101", "Fox", {(30000101, 1): "Kupo"})
    if got != "30000101":
        ok = False
    print("  %s POL_CHAR_NAME=0 restores the digits = %r"
          % ("OK " if got == "30000101" else "FAIL", got))
finally:
    os.environ.pop("POL_CHAR_NAME", None)

print("\nRESULT:", "gate opens for every linked title" if ok else "MISMATCH")
sys.exit(0 if ok else 1)
