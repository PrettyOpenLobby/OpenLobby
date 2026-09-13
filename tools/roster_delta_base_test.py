"""A `b/g/PTL` we could not build must not claim a live sequence -- and a client
that echoes 0 must be replayed, not refused.

THE BUG IT PINS, measured on prod 2026-08-20. Two faults, one screen:

  1. `_PTL_LIVE_BASE` was a plain module dict recording "we served this member a
     live roster", written by the LOGIN container (which serves `b/g/PTL`) and
     read by AUTHSESS (which answers `<DR>`). Two containers, two processes, so
     the read side saw an empty dict for every member for ever and refused every
     delta. Prod counters for the session: 17,547 `<DR>` in, **0** deltas out,
     **0** "CURRENT -- silent", 20-60 full `b/g/PTL` re-fetches PER MINUTE. The
     re-fetch storm 11u removed was back at full rate.

  2. The race that gate was written for is real, but the cure was not: the
     shipped fixture carries `+0x40 = 1`, so a client served the TEMPLATE
     truthfully echoed `<DR>(1)` -- and 1 was also the room's live sequence the
     moment the first player's `<DE>` landed. It was told it was current about
     nobody.

So the fix is in the DATA: a blob we serve without placing its fetcher gets
`+0x40 = 0`, a sequence a populated room is never at, and `deltas_after` replays
the whole log to anyone reporting it. That path already worked in the wild --
`<DR>(0) -> 4 delta(s) ... up to sequence 4` then `<DR>(4) is CURRENT`, prod
00:20:25 -- which is what makes the gate unnecessary as well as impossible.

Everything below is asserted against `responders`' real functions, because the
serial is one 4-byte field and the only thing that keeps it honest is a test that
reads it back out of the bytes we would have put on the wire.
"""
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "services"))

TMP = tempfile.mkdtemp(prefix="roster-delta-base-")
os.environ["POL_ACCOUNTS_DB"] = os.path.join(TMP, "accounts.db")
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_LOG_DIR"] = TMP
os.environ["POL_TM_ROSTER_FILE"] = os.path.join(TMP, "tm-roster.json")

import polpro                                                      # noqa: E402
import responders as R                                             # noqa: E402
import tmroom                                                      # noqa: E402

FAILS = []
CHAN = "#TM0R001"


def check(ok, label, detail=""):
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                           "  --  " + detail if detail else ""))
    if not ok:
        FAILS.append(label)


def serial(blob):
    return struct.unpack_from("<I", blob, tmroom.SERIAL_OFF)[0]


def template():
    """The blob the fetch path starts from, exactly as the tree ships it."""
    blob = R._tm_template_blob("b/g/PTL")
    assert blob and len(blob) >= tmroom.TOTAL, "the b/g/PTL fixture must ship"
    return blob


def seed(members=((6, "LaptopTest2"), (9, "PCTest"))):
    for d in (tmroom._RECORDS, tmroom._ROOMS_SEQ, tmroom._DELTAS,
              tmroom._NAMES, tmroom._GUIDS, tmroom._POLIDS, tmroom._TABLES):
        d.clear()
    tmroom._OWNER[0] = True
    for mid, name in members:
        tmroom.note_member(mid, tmroom.synth_member(CHAN, mid, name), CHAN)


def as_member(mid, in_room=True):
    """Pretend to be `mid`, optionally visible in the room registry."""
    R._session_get = lambda key, _m=mid: _m if key == "member_id" else None
    who = [{"member_id": mid, "name": "T%d" % mid, "nick": "N%d" % mid}]
    R._live_rooms = lambda: ({CHAN: {"who": who}} if in_room else {})


# --------------------------------------------------------------------------- #
# THE FIXTURE ITSELF. This is the constant that collided with a live sequence;
# if it is ever 0 the collision is gone at the source, but the serving path must
# not depend on that -- a fix that lives only in a template comes back.
tpl = template()
print("\nshipped b/g/PTL fixture: +0x40 = %d, members = %d"
      % (serial(tpl), struct.unpack_from("<i", tpl, tmroom.MEMBER_COUNT_OFF)[0]))

# --------------------------------------------------------------------------- #
# ARM 1: no session at all.
seed()
R._session_get = lambda key: None
R._live_rooms = lambda: {}
out = R._ptl_with_live_roster("b/g/PTL", tpl)
check(serial(out) == 0, "a blob served with NO session claims sequence 0",
      "+0x40 = %d" % serial(out))

# ARM 2: a member we cannot place in any room -- the LaptopTest2 race, i.e. a
# fetch that beat its own <DE> in. The ROOM is populated (member 9 is standing
# in it); what is missing is anything that names 6, which is exactly the live
# ordering: the fetch landed one second before the record.
seed(members=((9, "PCTest"),))
as_member(6, in_room=False)
out = R._ptl_with_live_roster("b/g/PTL", tpl)
check(serial(out) == 0, "a fetch that beat its <DE> claims sequence 0",
      "+0x40 = %d" % serial(out))
check(serial(out) != serial(tpl) or serial(tpl) == 0,
      "the fixture's authored serial never reaches an unplaced client",
      "fixture=%d served=%d" % (serial(tpl), serial(out)))

# ARM 3: a room the roster knows nothing about -- build_ptl bails and returns
# its input, which is still the authored fixture.
for d in (tmroom._RECORDS, tmroom._ROOMS_SEQ, tmroom._DELTAS, tmroom._TABLES):
    d.clear()
as_member(6, in_room=True)
out = R._ptl_with_live_roster("b/g/PTL", tpl)
check(serial(out) == 0, "an empty room's blob claims sequence 0",
      "+0x40 = %d" % serial(out))

# --------------------------------------------------------------------------- #
# THE LIVE ARM still stamps the room's real sequence -- that equality is what
# makes `<DR>` mean anything, so breaking it would be worse than the bug.
seed()
as_member(6, in_room=True)
out = R._ptl_with_live_roster("b/g/PTL", tpl)
n = struct.unpack_from("<i", out, tmroom.MEMBER_COUNT_OFF)[0]
check(serial(out) == tmroom.sequence(CHAN) and n == 2,
      "a live build stamps the room's own sequence and carries its members",
      "+0x40 = %d, seq = %d, members = %d"
      % (serial(out), tmroom.sequence(CHAN), n))


# --------------------------------------------------------------------------- #
# THE REPLY. `<DR>(0)` from a template holder must be REPLAYED, not refused:
# this is the exact case the retracted gate turned into a reload storm.
def dr(have, mid=6):
    as_member(mid, in_room=True)
    return R._roster_delta_reply(polpro.build([("DR", [str(have)])]))


seed()
reply, handled = dr(0)
check(handled and reply, "<DR>(0) is answered with deltas, not a reload",
      "handled=%s bytes=%d" % (handled, len(reply or b"")))
if reply:
    groups = polpro.parse(reply)
    tags = [g[0] for g in groups]
    pds = [g for g in groups if g[0] == "PD"]
    check(tags[0] == "DD" and "DN" in tags, "the reply is a <DD>/<DN> pair list",
          "tags=%s" % (tags[:4],))
    check(len(pds) == 2, "every member in the room is replayed",
          "%d PD group(s)" % len(pds))

# CURRENT is silence. `handled` True with no reply is the arm that stops the
# storm; `handled` False would fall through to <DO> = reload.
reply, handled = dr(tmroom.sequence(CHAN))
check(handled and reply is None, "a client at the room's sequence gets silence",
      "handled=%s reply=%r" % (handled, reply))

# A sequence we never issued is still a reload -- deltas_after is the authority
# the deleted gate tried to be, and it already covers a stale blob.
reply, handled = dr(tmroom.sequence(CHAN) + 99)
check(not handled and reply is None,
      "a sequence from the future falls through to the template, i.e. a reload")

# --------------------------------------------------------------------------- #
# TOO LONG TO SEND IS A RELOAD, NOT A PREFIX. One <DD> is one IRC NOTICE, and a
# rejoin at 0 can owe the whole DELTA_WINDOW. Truncating looks convergent and is
# not -- measured on prod 2026-08-20T19:07-19:08, two clients handed the same
# 16-delta prefix twelve times each and neither moved off sequence 0. So when the
# chain does not fit we say nothing and let the snapshot reload carry it, which
# is the same answer a client past RELOAD_BEHIND already gets.
seed()
for i in range(30):
    tmroom.note_member(100 + i, tmroom.synth_member(CHAN, 100 + i, "P%d" % i),
                       CHAN)
os.environ["POL_TM_DELTA_CHUNK"] = "16"
reply, handled = dr(0)
check(not handled and reply is None,
      "a replay longer than one notice falls through to a RELOAD",
      "%d owed" % tmroom.sequence(CHAN))

# ...and the DEFAULT cap is 4, because 16 is MEASURED not to apply: a complete
# 16-delta chain (len == chunk, so the old >16 guard never fired) was re-sent
# identically every 3 s for half a minute on 2026-08-20T22:33 while the client
# sat at sequence 0 -- seen on screen as a room-exit timeout. 1 and 4 are the
# only counts ever measured to apply.
os.environ.pop("POL_TM_DELTA_CHUNK", None)
reply, handled = dr(0)
check(not handled and reply is None,
      "by DEFAULT a chain of 16 is a RELOAD too -- 16 is measured not to apply")

# The prefix behaviour is still reachable for whoever wants to measure WHY the
# client refuses it -- but only behind its own switch, never by default.
os.environ["POL_TM_DELTA_PREFIX"] = "1"
os.environ["POL_TM_DELTA_CHUNK"] = "16"
reply, handled = dr(0)
count = len([g for g in polpro.parse(reply) if g[0] == "PD"]) if reply else 0
check(handled and count == 16, "POL_TM_DELTA_PREFIX=1 restores the prefix",
      "%d PD group(s)" % count)
os.environ.pop("POL_TM_DELTA_PREFIX", None)
os.environ["POL_TM_DELTA_CHUNK"] = "0"

os.environ["POL_TM_DELTA_CHUNK"] = "0"
reply, _ = dr(0)
count = len([g for g in polpro.parse(reply) if g[0] == "PD"]) if reply else 0
check(count == min(tmroom.sequence(CHAN), tmroom.DELTA_WINDOW),
      "POL_TM_DELTA_CHUNK=0 sends the whole chain", "%d PD group(s)" % count)
os.environ.pop("POL_TM_DELTA_CHUNK", None)

# --------------------------------------------------------------------------- #
# NO CROSS-BAND MEMORY MAY COME BACK. `b/g/PTL` is served by one container and
# `<DR>` answered by another, so a module-scope dict recording one for the other
# is unreadable by construction. Assert the names are gone, not just unused.
gone = [n for n in ("_PTL_LIVE_BASE", "_ptl_note_live_base",
                    "_ptl_has_live_base", "_ptl_warn_once")
        if hasattr(R, n)]
check(not gone, "no in-process 'we served this member a live base' map exists",
      "still present: %s" % (gone,))

print()
print("FAILED: " + ", ".join(FAILS) if FAILS else "all checks passed")
sys.exit(1 if FAILS else 0)
