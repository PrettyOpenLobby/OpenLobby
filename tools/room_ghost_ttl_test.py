"""A restart must not renew a ghost's lease, and the counts must not show one.

REPORTED LIVE 2026-08-20T19:56: the Tetra Master zone screen said "1 player in
Mermaids' Dreamworld" with nobody online at all. Two faults behind it, and they
compound:

  1. `rooms-live.json` IS NEVER REWRITTEN BY A RESTART. `_publish_rooms` runs on
     registry MUTATIONS, and coming back up is not one -- so the file kept the
     last snapshot the PREVIOUS process wrote, live member rows and all, until
     somebody happened to join something. The file backing that "1 player" was
     stamped 19:50:50; the process serving it started at 19:53:47.

  2. GHOST LAUNDERING. `registration()` writes live members and ghosts into one
     undifferentiated `who` list, and `restore_state` gave every restored nick a
     fresh `now + ttl`. So each restart re-ghosted the previous restart's ghosts
     with a brand-new 900 s promise and published them again for the next restart
     to read. Deploys ran at 19:39, 19:44, 19:48, 19:50 and 19:53 -- none more
     than 900 s apart -- so a player who had long since quit was carried through
     all five and could never expire. **A TTL that resets on restart is not a
     TTL.**

The fix is a saved absolute deadline per ghost, and a publish on the way up.
A nick with NO saved deadline is a live member from the previous process and
still gets the full grace -- that is the case the ghost feature exists for.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "services"))

TMP = tempfile.mkdtemp(prefix="room-ghost-")
os.environ["POL_ACCOUNTS_DB"] = os.path.join(TMP, "accounts.db")
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_LOG_DIR"] = TMP
os.environ["POL_TM_ROSTER_FILE"] = os.path.join(TMP, "tm-roster.json")

import responders as R                                             # noqa: E402

FAILS = []
CHAN = "#TM0R001"
TTL = 900.0


def check(ok, label, detail=""):
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                           "  --  " + detail if detail else ""))
    if not ok:
        FAILS.append(label)


def fresh_registry():
    """A process with an empty registry, as one that just started up has."""
    R.ROOMS._rooms.clear()
    R.ROOMS._modes.clear()
    R.ROOMS._topics.clear()
    R.ROOMS._ghosts.clear()


def ghosts_of(chan=CHAN):
    return {nk.decode("latin1"): when
            for nk, when in R.ROOMS._ghosts.get(chan.encode("latin1"),
                                                {}).items()}


# --------------------------------------------------------------------------- #
# RESTART 1: a live member from the previous process. No saved deadline, so they
# get the full grace -- this is what the feature is for.
fresh_registry()
R.ROOMS.restore_state({CHAN: {"owner": "UF8TOQDTX", "topic": None, "modes": {},
                              "who": ["UF8TOQDTX"]}}, TTL)
first = ghosts_of()
check(list(first) == ["UF8TOQDTX"], "a previous process's member comes back as a "
                                    "ghost", "%s" % (list(first),))
check(abs(first["UF8TOQDTX"] - (time.time() + TTL)) < 5,
      "a member with no saved deadline gets the FULL grace",
      "%.0fs from now" % (first["UF8TOQDTX"] - time.time()))

# What that process would publish for the NEXT restart to read.
reg = R.ROOMS.registration()
check(reg[CHAN].get("ghosts", {}).get("UF8TOQDTX") == first["UF8TOQDTX"],
      "the deadline is written out with the ghost",
      "%s" % (reg[CHAN].get("ghosts"),))
check("UF8TOQDTX" in reg[CHAN]["who"],
      "...and the flat `who` list still carries them, for every existing reader")

# --------------------------------------------------------------------------- #
# RESTART 2, 3, 4: the laundering case. Each restore reads what the last one
# wrote, and the deadline must NOT move.
deadline = first["UF8TOQDTX"]
for i in range(2, 5):
    fresh_registry()
    R.ROOMS.restore_state(reg, TTL)
    reg = R.ROOMS.registration()
check(ghosts_of().get("UF8TOQDTX") == deadline,
      "four restarts in a row do NOT extend the lease",
      "still %.0fs from now" % (deadline - time.time()))

# --------------------------------------------------------------------------- #
# AND IT ACTUALLY EXPIRES. A ghost whose deadline passed while the server was
# down is not resurrected -- previously it came back with a fresh 900 s.
fresh_registry()
stale = {CHAN: {"owner": "UF8TOQDTX", "topic": None, "modes": {},
                "who": ["UF8TOQDTX"],
                "ghosts": {"UF8TOQDTX": time.time() - 1}}}
R.ROOMS.restore_state(stale, TTL)
check(not ghosts_of(), "a ghost whose lease ran out while down is NOT restored",
      "%s" % (ghosts_of(),))
check(CHAN.encode("latin1") not in R.ROOMS._rooms,
      "...and its room is not resurrected around it either")

# A mix: one expired, one live. Only the live one comes back.
fresh_registry()
alive = time.time() + 300
R.ROOMS.restore_state({CHAN: {"owner": "UA4XX8PKP", "topic": None, "modes": {},
                              "who": ["UF8TOQDTX", "UA4XX8PKP"],
                              "ghosts": {"UF8TOQDTX": time.time() - 1,
                                         "UA4XX8PKP": alive}}}, TTL)
check(list(ghosts_of()) == ["UA4XX8PKP"],
      "an expired ghost drops out and a live one stays", "%s" % (ghosts_of(),))
check(ghosts_of().get("UA4XX8PKP") == alive,
      "...keeping its own remaining time, not a fresh TTL",
      "%.0fs left" % (alive - time.time()))

# --------------------------------------------------------------------------- #
# A GHOST IS NOT A HEADCOUNT. `_room_roster` publishes ghosts with `member_id 0`
# on purpose -- a restart survivor is still in the room from every client's point
# of view -- but the zone/room COUNT is taken from live sessions, so a room that
# is only ghosts publishes 0 and the screen says nobody is there.
fresh_registry()
R.ROOMS.restore_state({CHAN: {"owner": "UF8TOQDTX", "topic": None, "modes": {},
                              "who": ["UF8TOQDTX"]}}, TTL)
state = R._rooms_local_state()
entry = state.get(CHAN) or {}
check(entry.get("members") == 0,
      "a room holding only ghosts publishes a headcount of ZERO",
      "members=%s who=%s" % (entry.get("members"), entry.get("who")))
check([r for r in (entry.get("who") or []) if r.get("member_id")] == [],
      "...and no ghost row carries a member id for a counter to pick up",
      "%s" % (entry.get("who"),))

# --------------------------------------------------------------------------- #
# ADOPTION NEEDS EVIDENCE ABOUT THE GHOST, NOT ABOUT WHOEVER TOUCHED THE ROOM.
# Measured 2026-08-20T20:08:29: `UF8TOQDTX` had been carried as a ghost with NO
# JOIN for #TM0R001 anywhere in the log, and a DIFFERENT player's join promoted
# him to a full member off nothing but an open socket. He was on the auction
# screen. The room counted 2 with one real player in it.
class _Sess:
    """The parts of ChatSession adoption looks at."""

    def __init__(self, nick, polling, alive=True):
        self.nick = nick
        self.alive = alive
        # not polling = seen on that band an hour ago, i.e. navigated away
        self.last_room_heard = time.time() - (0 if polling else 3600)
        self._polling = polling

    def in_room_recently(self, within=None):
        return self._polling

    def send(self, lines):
        return True


def with_session(sess):
    R.PRESENCE.sessions_by_nick = lambda nk: (
        [sess] if sess and nk == sess.nick else [])


NICK = b"UF8TOQDTX"

# The reported case: connected, but not on a room screen.
fresh_registry()
R.ROOMS.restore_state({CHAN: {"owner": "UF8TOQDTX", "topic": None, "modes": {},
                              "who": ["UF8TOQDTX"]}}, TTL)
with_session(_Sess(NICK, polling=False))
check(R.ROOMS.adopt_ghosts(CHAN.encode("latin1")) == [],
      "a live session that is NOT polling the room band does not adopt")
check(list(ghosts_of()) == ["UF8TOQDTX"],
      "...and the ghost is kept, not evicted -- it can still re-attach",
      "%s" % (list(ghosts_of()),))
state = R._rooms_local_state()
check((state.get(CHAN) or {}).get("members") == 0,
      "...so the room still counts ZERO, which is what the zone screen shows",
      "members=%s" % ((state.get(CHAN) or {}).get("members"),))

# The case the feature exists for: a survivor really sitting on the room screen.
fresh_registry()
R.ROOMS.restore_state({CHAN: {"owner": "UF8TOQDTX", "topic": None, "modes": {},
                              "who": ["UF8TOQDTX"]}}, TTL)
with_session(_Sess(NICK, polling=True))
check(len(R.ROOMS.adopt_ghosts(CHAN.encode("latin1"))) == 1,
      "a survivor polling the room band IS adopted")
check(not ghosts_of(), "...and stops being a ghost")

# WARNING: THE CASE THAT KILLED THE FIRST VERSION OF THIS FIX. A session that has
# NEVER been seen on the room band was given the benefit of the doubt -- and
# immediately after a restart that is EVERY session, which is precisely when
# ghosts get adopted. Fox had sent auction traffic and no class-L poll at all in
# the process that adopted him, so the exception would have swallowed the rule.
fresh_registry()
R.ROOMS.restore_state({CHAN: {"owner": "UF8TOQDTX", "topic": None, "modes": {},
                              "who": ["UF8TOQDTX"]}}, TTL)
unmeasured = _Sess(NICK, polling=False)
unmeasured.last_room_heard = 0.0
with_session(unmeasured)
check(R.ROOMS.adopt_ghosts(CHAN.encode("latin1")) == [],
      "a session never seen on the room band is NOT adopted either")

# ...but something with no such signal at all -- a relay stub, not a client --
# still is. Absence of a field is not absence of a player.
fresh_registry()
R.ROOMS.restore_state({CHAN: {"owner": "UF8TOQDTX", "topic": None, "modes": {},
                              "who": ["UF8TOQDTX"]}}, TTL)


class _Stub:
    nick, alive = NICK, True

    def send(self, lines):
        return True


with_session(_Stub())
check(len(R.ROOMS.adopt_ghosts(CHAN.encode("latin1"))) == 1,
      "a session with no room-band signal at all keeps the benefit of the doubt")

# The knob, for a live rollback.
fresh_registry()
R.ROOMS.restore_state({CHAN: {"owner": "UF8TOQDTX", "topic": None, "modes": {},
                              "who": ["UF8TOQDTX"]}}, TTL)
with_session(_Sess(NICK, polling=False))
os.environ["POL_ROOM_ADOPT_NEEDS_POLL"] = "0"
try:
    check(len(R.ROOMS.adopt_ghosts(CHAN.encode("latin1"))) == 1,
          "POL_ROOM_ADOPT_NEEDS_POLL=0 restores the old blanket adoption")
finally:
    os.environ.pop("POL_ROOM_ADOPT_NEEDS_POLL", None)

# --------------------------------------------------------------------------- #
# ONE PLAYER LEAVING MUST NOT RE-MATERIALISE ANOTHER. Measured 2026-08-20
# T21:00:22 and it is the hole in the in_room_recently gate:
#
#     PART :#TM0R003
#     room #TM0R003: ghost UA4XX8PKP re-attached to a live session
#
# `part()` drops the PARTING nick's ghost correctly -- but the PART is then
# BROADCAST, the broadcast swept the channel, and it adopted somebody else's.
# The gate did not stop it because that session really was on a room screen:
# A DIFFERENT ROOM'S. `in_room_recently` says "this client is in a room", never
# "in THIS room". Reported as "it says all three users are in that room" with
# two of them gone.
OTHER = b"UA4XX8PKP"


def two_ghosts():
    fresh_registry()
    R.ROOMS.restore_state({CHAN: {"owner": "UF8TOQDTX", "topic": None,
                                  "modes": {},
                                  "who": ["UF8TOQDTX", "UA4XX8PKP"]}}, TTL)


def sessions(*sess):
    by = {s.nick: s for s in sess}
    R.PRESENCE.sessions_by_nick = lambda nk: ([by[nk]] if nk in by else [])


# Both are polling a room screen -- OTHER is just polling a DIFFERENT room's.
two_ghosts()
sessions(_Sess(NICK, polling=True), _Sess(OTHER, polling=True))
check(R.ROOMS.adopt_ghosts(CHAN.encode("latin1"), only=NICK) == []
      or list(ghosts_of()) == ["UA4XX8PKP"],
      "adopting one nick leaves every OTHER ghost alone",
      "%s" % (list(ghosts_of()),))
check("UA4XX8PKP" in ghosts_of(),
      "...specifically, a bystander is NOT dragged back in",
      "%s" % (list(ghosts_of()),))

# And a broadcast -- which every PART causes -- must adopt nobody at all.
two_ghosts()
sessions(_Sess(NICK, polling=True), _Sess(OTHER, polling=True))
before = set(ghosts_of())
R.ROOMS.broadcast(CHAN.encode("latin1"), [b"PART"])
check(set(ghosts_of()) == before,
      "a broadcast adopts NOBODY -- a PART cannot put people back in the room",
      "%s -> %s" % (sorted(before), sorted(ghosts_of())))

print()
print("FAILED: " + ", ".join(FAILS) if FAILS else "all checks passed")
sys.exit(1 if FAILS else 0)
