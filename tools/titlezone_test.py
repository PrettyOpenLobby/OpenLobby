#!/usr/bin/env python3
"""Prove the title-zone LEASE: it renews by itself, and it always lets go.

    python titlezone_test.py

WHY A LEASE AND NOT A LATCH. `_publish_title_zone` records what the client said
on 4:5, and the client also reports its exit -- so that entry is a latch and its
12-hour TTL is only a backstop for a console that died. **Janhourou never sends
4:5 at all**, in either direction: measured 2026-08-17, content id 3 appears zero
times across four lobby log generations against 10,361 MJS messages. So the
server infers the title from game traffic, and an inferred entry MUST expire --
a latch set from traffic would never be cleared and the friend list would read
"in Janhourou" forever, including after the player is back at the Viewer.

That makes "it lets go" the property worth testing, in all three ways it can:
the Viewer reporting its return, the lease lapsing, and a switch to another
title. The first and third are instant; the second is the only one covering
"quit the game but stayed logged in", which for Janhourou is the common case.

THE EXPIRY IS DRIVEN BY REWRITING `at`, NOT BY SLEEPING. A test that waited out a
real lease would take 15 minutes and would still only prove the default.
"""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))

LOG_DIR = tempfile.mkdtemp(prefix="tzone-")
DATA_DIR = tempfile.mkdtemp(prefix="tzdata-")
os.environ["POL_LOG_DIR"] = LOG_DIR
os.environ["POL_TITLE_ZONE_FILE"] = os.path.join(DATA_DIR, "title-zone.json")
os.environ["POL_TITLE_LEASE_TTL"] = "60"

import responders as R                              # noqa: E402

#: A title's content id, and therefore its presence zone. The lease is
#: generic; 3 is the zone the traffic inference was first built for.
ZONE = 3

ok = True


def check(label, got, want):
    global ok
    good = got == want
    ok = good and ok
    print(f"  {'OK  ' if good else 'FAIL'} {label}: {got!r}"
          + ("" if good else f"  (want {want!r})"))


def age(member, seconds):
    """Backdate an entry's `at`, which is how every expiry below is driven."""
    state = json.load(open(R._TITLE_ZONE_FILE, encoding="utf-8"))
    state[str(member)]["at"] = time.time() - seconds
    with open(R._TITLE_ZONE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)
    R._TITLE_ZONE_CACHE["mtime"] = -1.0


print("a lease is visible, renews itself, and expires")

R._title_zone_lease(7, ZONE)
check("a leased zone reads back", R._title_zone(7), 3)

# RENEWAL IS THROTTLED, or the file would be rewritten once per game message --
# Janhourou alone logged 10,361 of them, and two containers read-modify-write it.
first = json.load(open(R._TITLE_ZONE_FILE, encoding="utf-8"))["7"]["at"]
R._title_zone_lease(7, ZONE)
same = json.load(open(R._TITLE_ZONE_FILE, encoding="utf-8"))["7"]["at"]
check("an immediate re-lease does not rewrite the file", same, first)

# ...but once past the refresh point, traffic renews it. This is the whole
# mechanism: a live session holds its own lease open by being played.
age(7, R._TITLE_ZONE_REFRESH + 1)
R._title_zone_lease(7, ZONE)
renewed = json.load(open(R._TITLE_ZONE_FILE, encoding="utf-8"))["7"]["at"]
check("past the refresh point, traffic renews the lease", renewed > first, True)
check("and the zone is still live", R._title_zone(7), 3)

# THE POINT OF THE WHOLE DESIGN: silence lets go.
age(7, 61)
check("a lapsed lease reads as no title", R._title_zone(7), None)
check("...and the watcher's view drops it too",
      R._title_zone_expired(
          json.load(open(R._TITLE_ZONE_FILE, encoding="utf-8"))["7"]), True)

print("\nthe fast clears beat the lease, and a latch is NOT shortened by it")

# 1. the Viewer reporting its return -- instant, and it deletes the key outright.
R._title_zone_lease(8, ZONE)
check("leased before returning to the Viewer", R._title_zone(8), 3)
R._publish_title_zone(8, R._PRESENCE_ZONE_VIEWER, False)
check("the Viewer's own 4:5 clears it at once", R._title_zone(8), None)

# 2. switching titles must not wait for the refresh throttle -- the friend list
#    should follow a player from one game to another immediately.
R._title_zone_lease(9, ZONE)
R._title_zone_lease(9, 2)
check("a different zone writes through the throttle", R._title_zone(9), 2)

# 3. a CLIENT-REPORTED zone keeps the long backstop. The short lease TTL must not
#    leak onto it, or Tetra Master -- which does report -- would start expiring
#    out from under a legitimately long session.
R._publish_title_zone(10, 2, True)
age(10, R._TITLE_ZONE_LEASE_TTL + 60)
check("a latched zone outlives the LEASE ttl", R._title_zone(10), 2)
age(10, R._TITLE_ZONE_TTL + 60)
check("...but still dies at its own 12h backstop", R._title_zone(10), None)

# 4. THE 4:5 FRAME ITSELF, against bytes captured off the wire. Tetra Master
#    sends a SECOND frame ~9 s after entering with byte 19 CLEAR while STILL
#    naming zone 2 -- the title taking over status reporting, not an exit. We
#    read it as "left" and deleted the entry, so a friend in Tetra Master drew
#    the plain Viewer icon for the whole session bar the first nine seconds.
#    No other title sends such a frame; FMO and FE return via zone 1000.
print()
print("the 4:5 frame: a NAMED title is not an exit, whatever byte 19 says")
FRAMES = {                      # verbatim from logs/lobby.log, 2026-08-17
    "TM enter":   "0000000000000000000000000000000000000001020002000001000000000000",
    "TM handoff": "0000000000000000000000000000000000010100020002000001000000000000",
    "FMO enter":  "0000000000000000000000000000000000010301040001000001000000000000",
    "FE enter":   "00000000000000000000000000000000000000010b0001000001000000000000",
    "Viewer":     "0000000000000000000000000000000000000000e80301000001000000000000",
}
F = {k: bytes.fromhex(v) for k, v in FRAMES.items()}
check("TM enter reads zone 2, in title", R._status_frame_zone(F["TM enter"]), (2, True))
check("TM HAND-OFF still reads zone 2, in title",
      R._status_frame_zone(F["TM handoff"]), (2, True))
check("byte 19 really is clear on that frame", F["TM handoff"][19], 0)
check("FMO enter reads zone 4, in title", R._status_frame_zone(F["FMO enter"]), (4, True))
check("FE enter reads zone 11, in title", R._status_frame_zone(F["FE enter"]), (11, True))
check("the Viewer's own frame is NOT a title",
      R._status_frame_zone(F["Viewer"]), (R._PRESENCE_ZONE_VIEWER, False))
check("a short frame is refused rather than misread",
      R._status_frame_zone(bytes(21)), None)
check("and None does not raise", R._status_frame_zone(None), None)

# ...and end to end: the hand-off must not clear a published zone, while the
# Viewer's own frame must. This is the reported bug, in two lines.
for label in ("TM enter", "TM handoff"):
    z, in_title = R._status_frame_zone(F[label])
    R._publish_title_zone(11, z, in_title)
    check("after the %r frame the friend list says Tetra Master" % label,
          R._title_zone(11), 2)
z, in_title = R._status_frame_zone(F["Viewer"])
R._publish_title_zone(11, z, in_title)
check("and the Viewer's own frame is what clears it", R._title_zone(11), None)

print("\nbad input cannot take the game path down with it")
for bad in (None, "", "nope", object()):
    R._title_zone_lease(bad, ZONE)         # must not raise
check("a junk member id is ignored, not raised on", True, True)
check("and wrote nothing",
      set(json.load(open(R._TITLE_ZONE_FILE, encoding="utf-8"))) - {"7", "9", "10", "11"},
      set())

print()
print("titlezone_test: OK" if ok else "titlezone_test: FAILED")
sys.exit(0 if ok else 1)
