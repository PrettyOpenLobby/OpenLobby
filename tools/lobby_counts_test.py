"""The zone and room screens must show the people who are actually there.

TWO FAULTS, MEASURED ON PROD 2026-08-20, and the first is why the second went
unnoticed for a day:

  1. `b/g/ZL` WAS NEVER PATCHED AT ALL. The zone branch resolved each zone's room
     list by `open()`ing the STORED resource, and prod's `data/resources/` holds
     no `b_g_RL*.bin` -- the room lists are server-authored fixtures that ship in
     `services/tmdata/`. So `rls` came out empty, `patch_zone_list` found no room
     list for any row, and every zone kept its authored placeholder. Five `b/g/ZL`
     serves in one session, not one count line, while `b/g/RL000` on the very
     same fetches logged "2 player(s) across 1 room(s)".

     This is the FOURTH time a server-authored fixture has been read from a
     dev-only stored path (`b/g/ZL`, `b/g/PTL`, `U/g/TM0_RANKLIST`, and now the
     zone list's view of `b/g/RL`).

  2. IT FAILED SILENTLY, because the log line was gated on `out != data` --
     "patched nothing" and "nothing needed patching" printed identically, which
     is to say neither printed anything.

  3. And the headcount came from the registry's `members` field, which counts
     ROWS. The registry holds a duplicate row after a reconnect (measured: 3 for
     two people), and `build_ptl` was made immune to that while this was not --
     so the room list could say 3 while Table Members showed 2, from one
     registry.

Asserted against `responders._lobby_counts_live` itself, reading the counts back
out of the bytes that would have gone on the wire.
"""
import os
import struct
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "services"))

TMP = tempfile.mkdtemp(prefix="lobby-counts-")
os.environ["POL_ACCOUNTS_DB"] = os.path.join(TMP, "accounts.db")
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_LOG_DIR"] = TMP
os.environ["POL_TM_ROSTER_FILE"] = os.path.join(TMP, "tm-roster.json")

import responders as R                                             # noqa: E402
import tmroom                                                      # noqa: E402

FAILS = []


def check(ok, label, detail=""):
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                           "  --  " + detail if detail else ""))
    if not ok:
        FAILS.append(label)


def zone_rows(blob):
    """`[(zone id, players, rooms)]` as the client reads them."""
    n = min(struct.unpack_from("<I", blob, tmroom.ZL_COUNT_OFF)[0],
            (tmroom.ZL_TOTAL - tmroom.ZL_HDR) // tmroom.ZL_REC)
    out = []
    for i in range(n):
        o = tmroom.ZL_HDR + i * tmroom.ZL_REC
        out.append((blob[o + tmroom.ZL_F_ZONEID],
                    struct.unpack_from("<I", blob, o + tmroom.ZL_F_PLAYERS)[0],
                    struct.unpack_from("<I", blob, o + tmroom.ZL_F_ROOMS)[0]))
    return out


def room_rows(blob):
    """`[(channel, players)]` as the client reads them."""
    out = []
    for i, chan in tmroom._room_channels(blob):
        o = tmroom.RL_HDR + i * tmroom.RL_REC + tmroom.RL_F_PLAYERS
        out.append((chan, struct.unpack_from("<I", blob, o)[0]))
    return out


def registry(who_by_chan, members_by_chan=None):
    """Stand in for the room registry the login container publishes."""
    members_by_chan = members_by_chan or {}
    live = {}
    for chan, ids in who_by_chan.items():
        live[chan] = {"who": [{"member_id": m, "name": "M%s" % m} for m in ids],
                      "members": members_by_chan.get(chan, len(ids))}
    R._live_rooms = lambda: live


ZL = R._tm_template_blob("b/g/ZL")
RL = R._tm_template_blob("b/g/RL000")
assert ZL and RL, "the zone and room fixtures must ship"
CHANS = [c for _i, c in tmroom._room_channels(RL)]
print("\nshipped fixtures: b/g/ZL %dB, b/g/RL000 %dB naming %d room(s): %s"
      % (len(ZL), len(RL), len(CHANS), ", ".join(CHANS[:4])))
FIRST = CHANS[0]

# --------------------------------------------------------------------------- #
# THE ROOM LIST. Two people in one room is two, once.
registry({FIRST: [3, 6]})
out = R._lobby_counts_live("b/g/RL000", RL)
rows = dict(room_rows(out))
check(rows.get(FIRST) == 2, "the room list carries the live headcount",
      "%s = %s" % (FIRST, rows.get(FIRST)))

# THE DUPLICATE. A reconnect leaves the same person in the registry twice and
# `members` counts rows -- the snapshot is immune to that and so must this be.
registry({FIRST: [3, 6, 6]}, {FIRST: 3})
out = R._lobby_counts_live("b/g/RL000", RL)
rows = dict(room_rows(out))
check(rows.get(FIRST) == 2, "a duplicated registry row is counted ONCE",
      "%s = %s (registry said members=3)" % (FIRST, rows.get(FIRST)))

# ...but a channel whose rows have not bound an id yet is NOT empty.
registry({FIRST: [0, 0]}, {FIRST: 2})
out = R._lobby_counts_live("b/g/RL000", RL)
rows = dict(room_rows(out))
check(rows.get(FIRST) == 2, "rows with no member id yet keep the registry count",
      "%s = %s" % (FIRST, rows.get(FIRST)))

# --------------------------------------------------------------------------- #
# THE TABLE COLUMNS. Reported 2026-08-20: the room list draws 15 tables for the
# first room and 16 for the rest, which is neither of the authored values -- so
# the fields were never written and the offsets are NOT confirmed on TM. What is
# asserted here is what we CAN assert: the numbers we write are the room's real
# tables, from the same rows `build_ptl` serves, with the card shop excluded.
import tmroom as _tm                                                # noqa: E402

os.environ["POL_TM_ROOM_TABLE_COUNTS"] = "1"   # default is OFF, see below
# The expected count is DERIVED from the shipped fixture, not hardcoded: it was
# 3 when this test was written and 16 the same evening (89deadd4 authored the
# SE-accurate room), and a literal here turns every authoring change into a
# false failure. Only #TM0T### rows are tables -- #TM0CARD and #TM0COM are the
# shop and the VS. COM endpoint, and counting them would advertise seats nobody
# can take.
PTL = R._tm_template_blob("b/g/PTL")
ntab = sum(1 for i in range(struct.unpack_from("<i", PTL, _tm.TABLE_COUNT_OFF)[0])
           if _tm.decode_table(PTL[_tm.TABLE_OFF + i * _tm.TABLE_REC:
                                   _tm.TABLE_OFF + (i + 1) * _tm.TABLE_REC]
                               )[0].startswith("#TM0T0"))
registry({FIRST: [3, 6]})
out = R._lobby_counts_live("b/g/RL000", RL)
o = _tm.RL_HDR + _tm.RL_F_TABLES
total = struct.unpack_from("<I", out, o)[0]
free = out[_tm.RL_HDR + _tm.RL_F_OPENTABLES] - 0x20
check((total, free) == (ntab, ntab),
      "the room's table columns are its REAL tables, shop/COM excluded",
      "total=%d open=%d (fixture holds %d #TM0T rows)" % (total, free, ntab))

# An occupied table drops out of the OPEN column but not the total.
seated = dict(_tm.tables(FIRST))
_tm._OWNER[0] = True
_tm._TABLES["0x%016X" % _tm.room_id_for(FIRST)] = {
    "#TM0T001": ["#TM0T001", "1", "0x1", "1", "8", "1", "BAAAAB" + "A" * 43]}
out = R._lobby_counts_live("b/g/RL000", RL)
free = out[_tm.RL_HDR + _tm.RL_F_OPENTABLES] - 0x20
total = struct.unpack_from("<I", out, _tm.RL_HDR + _tm.RL_F_TABLES)[0]
check((total, free) == (ntab, ntab - 1),
      "a seated table leaves the total and lowers OPEN",
      "total=%d open=%d" % (total, free))
_tm._TABLES.clear()

# ...and OFF is the DEFAULT, because writing these two offsets made the room
# list "horrifically off" on screen (observed in live testing, 2026-08-20). That is a result: the
# client reads at least one of them, and they do not mean what janlobby's names
# say for THIS title. A known-wrong placeholder beats an unknown-wrong number.
os.environ.pop("POL_TM_ROOM_TABLE_COUNTS", None)
out = R._lobby_counts_live("b/g/RL000", RL)
check(struct.unpack_from("<I", out, _tm.RL_HDR + _tm.RL_F_TABLES)[0] == 1,
      "by DEFAULT the authored table bytes are left alone",
      "room 1's authored TableNum is 1")

# --------------------------------------------------------------------------- #
# THE ZONE LIST -- the half that was never patched at all. There is no stored
# `b/g/RL###` here, exactly as on prod, so this only passes if the shipped
# fixture is used as the fallback.
check(not os.path.exists(os.path.join(TMP, "resources")),
      "no stored resources exist -- the fixture fallback is the ONLY source")

registry({FIRST: [3, 6]})
out = R._lobby_counts_live("b/g/ZL", ZL)
rows = zone_rows(out)
check(out != ZL, "the zone list is patched at all", "%d row(s)" % len(rows))
check(any(p for _z, p, _r in rows), "some zone carries the live population",
      "%s" % (rows,))
check(sum(p for _z, p, _r in rows) == 2,
      "the population across the whole list is the number of real players",
      "%s" % (rows,))
check(all(r for _z, _p, r in rows), "every zone carries a room count",
      "%s" % (rows,))

# ONE PERSON, ONE ZONE. Tetra Master's rows both claim zone 0, so without the
# claim guard one person entering raised EVERY zone by one.
check(len([1 for _z, p, _r in rows if p]) == 1,
      "a player is counted by exactly ONE zone row, never every row",
      "%s" % (rows,))

# Somebody standing on the room list has joined no channel and is in no room --
# they still have to appear in their zone's population.
# `_ZONE_OF` is what `_note_zone_presence` fills when a client fetches a room
# list -- that fetch IS the zone-entry event. Seeded directly here so the case
# under test is the counting, not the event plumbing.
R._ZONE_OF.clear()
registry({FIRST: [3]})
R._ZONE_OF[9] = [0, time.monotonic()]
out = R._lobby_counts_live("b/g/ZL", ZL)
check(sum(p for _z, p, _r in zone_rows(out)) == 2,
      "a player standing IN a zone but in no room is counted",
      "%s" % (zone_rows(out),))

# ...and not twice, if they then walk into one of that zone's rooms.
R._ZONE_OF.clear()
registry({FIRST: [3, 9]})
R._ZONE_OF[9] = [0, time.monotonic()]
out = R._lobby_counts_live("b/g/ZL", ZL)
check(sum(p for _z, p, _r in zone_rows(out)) == 2,
      "...and NOT twice once they are in a room of that zone",
      "%s" % (zone_rows(out),))

# --------------------------------------------------------------------------- #
# THE KNOB.
R._ZONE_OF.clear()
registry({FIRST: [3, 6]})
os.environ["POL_TM_LOBBY_COUNTS"] = "0"
try:
    check(R._lobby_counts_live("b/g/ZL", ZL) is ZL
          and R._lobby_counts_live("b/g/RL000", RL) is RL,
          "POL_TM_LOBBY_COUNTS=0 serves the authored blobs untouched")
finally:
    os.environ.pop("POL_TM_LOBBY_COUNTS", None)

print()
print("FAILED: " + ", ".join(FAILS) if FAILS else "all checks passed")
sys.exit(1 if FAILS else 0)
