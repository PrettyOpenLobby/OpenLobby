"""A connection that dies without a PART must retire its Tetra Master record.

THE BUG IT PINS, measured live 2026-08-20. Observed in live testing: **one** client in
`#TM0R001` and `tm-roster.json` held **two** `stat 2` records: PCTest was gone
from the IRC registry and still standing in the game roster. The dead-socket path
in `responders` did the IRC half of the cleanup -- `ROOMS.drop`, a broadcast
`QUIT :Connection closed`, `PRESENCE.unregister` -- and never told `tmroom`, so
records only ever retired on the two CLEAN exits: an IRC `PART` and class-L
`<PC>`.

WHY A TIMEOUT WOULD HAVE BEEN WRONG, since that was the first instinct. `<PD>` is
event-driven, not periodic: measured across one live session, the gaps between a
member's own record re-sends run 2 s, 5 s, three minutes, ninety minutes. There
is no heartbeat on that channel, so an expiry has nothing to work against and SE
cannot have used one either. The connection IS the mechanism.

AND THE ONE IT MUST NOT BECOME. Judging presence from current IRC membership on
every read was already tried and reverted, because auth-session churn blinked
people out of each other's lists (`tmroom.note_member`'s banner). This retires
ONCE, on an actual close, which is the same shape as the `PART` the module
already trusts -- so the seat-ordering and the `PC` delta have to come out
identical to the PART path, and that equivalence is asserted below.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "services"))

TMP = tempfile.mkdtemp(prefix="roster-retire-")
os.environ["POL_ACCOUNTS_DB"] = os.path.join(TMP, "accounts.db")
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_LOG_DIR"] = TMP
os.environ["POL_TM_ROSTER_FILE"] = os.path.join(TMP, "tm-roster.json")

import responders as R                                             # noqa: E402
import tmroom                                                      # noqa: E402

FAILS = []


def check(ok, label, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  --  {detail}"
                                                       if detail else ""))
    if not ok:
        FAILS.append(label)


def seed(chan="#TM0R001"):
    """Two members standing in one room, as `<DE>` would have left them."""
    for d in (tmroom._RECORDS, tmroom._ROOMS_SEQ, tmroom._DELTAS,
              tmroom._NAMES, tmroom._GUIDS, tmroom._POLIDS):
        d.clear()
    tmroom._OWNER[0] = True
    for mid, name in ((6, "LaptopTest2"), (9, "PCTest")):
        tmroom.note_member(mid, tmroom.synth_member(chan, mid, name), chan)
    return chan


chan = seed()
check(len(tmroom.members(chan)) == 2, "two members seeded",
      f"{len(tmroom.members(chan))} in {chan}")

# --------------------------------------------------------------------------- #
# The close.
before_seq = tmroom.sequence(chan)
R._roster_retire_on_close(9)

left = [m for m, _ in tmroom.members(chan)]
check(left == [6], "the dropped member is retired, the other one stands",
      f"remaining={left}")
check(tmroom.room_of(9) is None, "the retired member is in no room")

# The survivors have to be TOLD, or a client holding the row keeps drawing it --
# that is the ghost this whole fix is about, one layer further out.
after = tmroom.deltas_after(chan, before_seq)
tags = [d[1] for d in after]
check("PC" in tags, "a PC delta is queued for everyone still in the room",
      f"deltas={tags}")

# --------------------------------------------------------------------------- #
# It must be a no-op for anyone who is not in a room, because this runs on EVERY
# socket close in the service -- lobby-only sessions, redirect hops, POP3, the
# lot. A close-retire that raised, or that churned the roster file, would be paid
# for by every disconnect on the server.
seed()
before = dict(tmroom._RECORDS)
R._roster_retire_on_close(4242)                 # never heard of them
check(tmroom._RECORDS == before, "a member in no room changes nothing")
R._roster_retire_on_close(0)                    # a falsy id
check(tmroom._RECORDS == before, "member id 0 changes nothing")

# --------------------------------------------------------------------------- #
# The knob, because a live rollback must not need a deploy of new code.
seed()
os.environ["POL_TM_RETIRE_ON_CLOSE"] = "0"
try:
    R._roster_retire_on_close(9)
    check(len(tmroom.members(chan)) == 2,
          "POL_TM_RETIRE_ON_CLOSE=0 leaves the record alone")
finally:
    os.environ.pop("POL_TM_RETIRE_ON_CLOSE", None)

# --------------------------------------------------------------------------- #
# EQUIVALENCE WITH THE PART PATH. A dirty close and a clean PART must leave the
# roster in the same state -- if they diverge, one of the two is wrong and the
# next person will not know which.
seed()
R._roster_retire_on_close(9)
dirty = (sorted(m for m, _ in tmroom.members(chan)), tmroom.room_of(9))

seed()
tmroom.forget_member(9)                         # what the PART path calls
clean = (sorted(m for m, _ in tmroom.members(chan)), tmroom.room_of(9))

check(dirty == clean, "a dirty close leaves the same roster as a clean PART",
      f"close={dirty} part={clean}")

# --------------------------------------------------------------------------- #
# THE LAST-SESSION GUARD. Shipping the retire without this cost a live match:
# member 9 was DROPPED three times while playing, because a launch holds several
# connections and ANY of them closing retired him. The roster then served to the
# other player was missing himself.
class _Sess:
    def __init__(self, mid):
        self.member_id = mid


check(R._member_still_present([_Sess(9), _Sess(6)], 9) is True,
      "a member with another session in the room is still present")
check(R._member_still_present([_Sess(6)], 9) is False,
      "a member whose last session went is not present")
check(R._member_still_present([], 9) is False, "an empty room is not present")
check(R._member_still_present([_Sess("9")], 9) is True,
      "the id compares by value, not by type")
check(R._member_still_present([_Sess(None), _Sess(9)], 9) is True,
      "a session with no member id does not mask a real one")
check(R._member_still_present([_Sess(9)], None) is False,
      "a caller with no member id retires nobody")

# --------------------------------------------------------------------------- #
# POL-IDS MUST SURVIVE A RESTART. `_publish` dumps the in-memory maps and takes
# ownership, and a fresh process starts empty -- so without a seed the first
# publish after a bounce writes an empty `polids` over the good data. Records
# survive because clients re-send <DE> constantly; a POL-ID does not, because
# @Init=/NN= arrives once per LAUNCH. Measured 2026-08-20: member 9's id was
# captured at 15:47, lost to a bounce, and at 17:32 both players were served a
# roster naming only member 6 -- so the one listed player was listed to himself
# and his client hid the row as "me". An empty list, from an id we already knew.
seed()
tmroom.note_pol_id(6, "AB12CD56EB0F5932")
tmroom.note_pol_id(9, "AB12CD1EEEAE9C3B")
check(tmroom.pol_id_of(9) == "AB12CD1EEEAE9C3B", "both POL-IDs stored")

# the bounce: memory gone, the published file still on disk
tmroom._POLIDS.clear()
tmroom._POLIDS_SEEDED[0] = False
tmroom._OWNER[0] = False
tmroom._CACHE["mtime"] = -1.0

check(tmroom.pol_id_of(9) == "AB12CD1EEEAE9C3B",
      "a POL-ID survives a restart", "recovered from the published file")

# ...and a live capture must beat the file, never the other way round
tmroom._POLIDS.clear()
tmroom._POLIDS_SEEDED[0] = False
tmroom.note_pol_id(9, "AAAABBBBCCCCDDDD")     # this process just learned better
check(tmroom.pol_id_of(9) == "AAAABBBBCCCCDDDD",
      "a live capture wins over the published file")

print()
print("roster-retire: " + ("OK" if not FAILS else f"FAILED ({len(FAILS)})"))
for f in FAILS:
    print("   - " + f)
raise SystemExit(1 if FAILS else 0)
