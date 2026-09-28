"""Member status (online, away, invisible) as published across processes and framed in pushes."""
import json
import os
import struct
import time
from srvcore import log
from .deps import contentauth
from . import lobbysession, titlezone


#: `+0x14` -- WHERE THE SUBJECT IS. Not a constant, and not cosmetic: it is half
#: of what distinguishes online from offline above, and it is the field that drives
#: the friend-row activity icon.
#:
#: SE's two values are pinned by correlating every presence push against the IRC
#: JOIN/PART traffic in the same log:
#:
#:   push 91348  zone 1001  ->  friend JOINs #01CPZYOTYU000003 (Novice_Hall)
#:   part 100968            ->  push 101076 drops to zone 1000
#:   push 105121 zone 1001  ->  friend JOINs a created room
#:   push 120344 zone 0                     friend goes offline
#:
#: So 1000 = in the Viewer, 1001 = in a player chat room, 0 = nowhere. Confirmed
#: independently by the account holder, who reports the row icon changing between
#: the Viewer and a chat-room glyph.
#:
#: WARNING: GROUP CHAT IS NOT A ZONE CHANGE. The `#XXL...` join/part run at 86571-89006
#: sits between two pushes that are both 1000. Only the `#01C...` namespace (a
#: player room) moves this field, which is why `_presence_zone` tests for it
#: specifically rather than "is in any channel".
#:
#: A TITLE presumably writes a further value here -- that is the "which game is
#: my friend playing" icon -- but nobody launched one during either capture, so
#: that value is unmeasured. Do not guess it: one launch with the shim running
#: names it the same way the JOINs named these two.
_PRESENCE_ZONE_VIEWER = 1000

#: WARNING: **A 4:5 FRAME THAT NAMES A TITLE ZONE IS NOT AN EXIT, WHATEVER BYTE 19
#: SAYS.** Reported 2026-08-17 by the account holder -- a friend sitting in
#: Tetra Master showed the plain Viewer icon -- and then measured out of the two
#: logs, because the fault is only visible by crossing them.
#:
#: Tetra Master, and ONLY Tetra Master, sends a SECOND 4:5 about nine seconds
#: after the one that announces it, carrying `in_title=0` while still naming
#: `zone=2` (byte 17/18 go `00 00` -> `01 01`, which STATUS already recorded as
#: "TM-exit" without knowing what it meant). Every zone=2 entry in `lobby.log`
#: has one, at 9-12 s, eight times over:
#:
#:     22:27:10  in_title=1 zone=2      the announcement
#:     22:27:19  in_title=0 zone=2      <- read as "left", key DELETED
#:     22:28:14..22:42:31               title traffic @ShReq= @Buy= ... @Pong=
#:
#: The player was in the card shop for a quarter of an hour after we recorded
#: them as gone. The most recent case is sharper still: the exit frame at
#: 23:56:38 is followed by title traffic through 23:59:06.
#:
#: **What it actually is:** in Tetra Master the TITLE reports the player's
#: status, so the Viewer's own "I am inside a title" flag goes false while the
#: zone it names stays 2. It is a hand-off, not a departure. No other title does
#: this -- FMO (4) and Fantasy Earth (11) never send `in_title=0` with their own
#: zone at all; they return by announcing zone 1000.
#:
#: So the zone is what is load-bearing and byte 19 is not: a named title latches,
#: and only the Viewer's own zone 1000 clears it (plus logout and the 12 h TTL,
#: which are unchanged). POL_STATUS_ZONE_LATCH=0 restores the old reading.
_STATUS_ZONE_LATCH = os.environ.get("POL_STATUS_ZONE_LATCH", "1") == "1"


#: **4:5 CARRIES A PRESENCE STATUS BYTE, AND WE USED TO READ PAST IT.**
#: Decoded off a live retail capture 2026-08-19 (`polshim.647064.log`, Lex
#: toggling Online -> Away -> Invisible).
#: The 40-byte 4:5 body is mostly zero and the status code is ONE BYTE:
#:
#:     body[0x14..] = E8 03 <STATUS> 00 00 01 00 00 ...
#:                    \___/  \____/
#:                    zone    the presence code
#:
#: -- i.e. the `E8 03` at +0x14 is the same u16 zone `_status_frame_zone` already
#: reads (1000 = the Viewer), and the byte immediately after it is the state the
#: user picked. Measured: **01 Online, 02 Away, 05 Invisible**. 03/04 were not
#: exercised and are therefore NOT in this table -- they are most likely Busy and
#: Offline, but naming them from the gap between 02 and 05 would be a guess with
#: a constant's name on it. Capture them to finish the enum.
#:
#: Each change fires 4:5 TWICE, each on a fresh lobby connection, so the store
#: below is written twice per toggle with the same value. That is why it is a
#: latch and not an event.
_STATUS_ONLINE = 0x01
_STATUS_AWAY = 0x02
_STATUS_INVISIBLE = 0x05
#: Where the code sits in the 4:5 BODY (frame 0x28 onward), i.e. two bytes past
#: the zone `_status_frame_zone` reads at +20.
_STATUS_CODE_AT = 0x16

#: Codes that mean "not here" for the purposes of the in-room WHO letter. 01 is
#: the only measured value that means present, so the rule is stated that way
#: round: anything ELSE that we have actually been told is away-ish. An absent
#: record (0) is NOT away -- it means the client never told us, and inventing a
#: `G` for someone who is simply sitting in a room is worse than the `H` we
#: served before this existed.
def _status_is_away(code):
    """Does this 4:5 status code render as `G` (gone) rather than `H` (here)?"""
    try:
        code = int(code or 0)
    except (TypeError, ValueError):
        return False
    return code not in (0, _STATUS_ONLINE)


#: WHERE THE STATUS CROSSES THE CONTAINER BOUNDARY. Exactly the problem
#: `_TITLE_ZONE_FILE` solves and for exactly the same reason: the 4:5 request
#: lands on the LOBBY (`login`) while the in-room WHO and the friend push are
#: served by `authsess`. One writer, an atomic replace, an mtime-cached read.
#:
#: Kept as its OWN file rather than a key in title-zone.json: that file is
#: written by `_publish_title_zone`, which DELETES a member's key when they leave
#: a title, and a presence status must not evaporate because somebody quit Tetra
#: Master.
_MEMBER_STATUS_FILE = os.environ.get(
    "POL_MEMBER_STATUS_FILE",
    os.path.join(os.environ.get("POL_DATA_DIR", "/data"), "member-status.json"))
_MEMBER_STATUS_CACHE = {"mtime": -1.0, "data": {}}
#: Same backstop as the title zone, and for the same case: a client that dies
#: without logging out. Long enough never to expire a real session.
_MEMBER_STATUS_TTL = 12 * 3600


def _live_member_status():
    """Per-member 4:5 status, re-read only when the file's mtime moves."""
    try:
        mtime = os.stat(_MEMBER_STATUS_FILE).st_mtime
    except OSError:
        return {}
    if mtime != _MEMBER_STATUS_CACHE["mtime"]:
        try:
            with open(_MEMBER_STATUS_FILE, "r", encoding="utf-8") as f:
                _MEMBER_STATUS_CACHE["data"] = json.load(f) or {}
            _MEMBER_STATUS_CACHE["mtime"] = mtime
        except (OSError, ValueError):
            return _MEMBER_STATUS_CACHE["data"]   # torn write: last good view
    return _MEMBER_STATUS_CACHE["data"]


def _publish_member_status(member_id, code):
    """Record this member's 4:5 status. `None` clears it (logout).

    Writes only when the value actually MOVES. 4:5 arrives twice per toggle and
    the client re-sends its status on every fresh lobby connection, so an
    unconditional write would rewrite this file -- which two containers
    read-modify-write -- several times a minute for no change at all.
    """
    if member_id is None:
        return False
    try:
        key = str(int(member_id))
    except (TypeError, ValueError):
        return False
    have = (_live_member_status() or {}).get(key) or {}
    if code is None:
        if not have:
            return False                      # nothing to clear, nothing to write
    elif int(have.get("code", -1)) == int(code):
        return False
    try:
        state = dict(_live_member_status())
        if code is None:
            state.pop(key, None)
        else:
            state[key] = {"code": int(code), "at": time.time()}
        os.makedirs(os.path.dirname(_MEMBER_STATUS_FILE), exist_ok=True)
        tmp = _MEMBER_STATUS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, _MEMBER_STATUS_FILE)  # atomic
        _MEMBER_STATUS_CACHE["mtime"] = -1.0  # our own next read re-stats
        return True
    except (OSError, ValueError) as exc:
        log("lobby", f"  4:5 status: cannot publish ({exc})")
        return False


def _member_status(member_id):
    """This member's last 4:5 status code, or 0 if we have never been told."""
    if os.environ.get("POL_MEMBER_STATUS", "1") != "1":
        return 0
    try:
        row = _live_member_status().get(str(int(member_id))) or {}
    except (TypeError, ValueError):
        return 0
    if time.time() - row.get("at", 0) > _MEMBER_STATUS_TTL:
        return 0
    try:
        return int(row.get("code") or 0)
    except (TypeError, ValueError):
        return 0


def _status_frame_code(state):
    """The presence code in a 4:5 body, or None if the body is too short."""
    if state is None or len(state) <= _STATUS_CODE_AT:
        return None
    return state[_STATUS_CODE_AT]


def _status_frame_zone(state):
    """Read a 4:5 KChangeMyStatus body as (zone, in_title), or None if too short.

    Kept out of the handler so the decision can be tested against real captured
    bytes -- see `tools/titlezone_test.py`. Byte 19 is returned nowhere: it is
    logged for the record and deliberately does not decide anything, for the
    reason in `_STATUS_ZONE_LATCH` above.
    """
    if state is None or len(state) < 22:
        return None
    zone = struct.unpack_from("<H", state, 20)[0]
    named = zone != _PRESENCE_ZONE_VIEWER
    return zone, (named if _STATUS_ZONE_LATCH else (state[19] == 1 and named))
_PRESENCE_ZONE_CHATROOM = 1001


def payload_change_status(n, req_pt):
    """4:5 **KChangeMyStatus** -- "put me online" (lobby opcode table).

    Until 2026-08-12 this had no builder at all and fell through to 32 zero
    bytes. The Viewer tolerates that (it sends 4:5 twice and carries on
    regardless), but the standalone Friend List does NOT: it tears down its
    session channel and every lobby socket 0.7 s after the reply and sits on
    "connecting" forever, which is what a -5314 "no data" looks like from the
    outside.

    The request says exactly what it wants to be told back. Decrypted, its
    40-byte payload is 24 zero bytes, then the new state, then the handle:
        ... 00 00 00 01 0e 00 00 00 00 01 00 ...   status=1, content 0x0e=14
        ... 4c 65 78 ...                           handle "Lex"
    so we echo the state back and drop the trailing handle+checksum, which
    is the natural shape for a "your status is now X" acknowledgement and
    the only one the request itself justifies.

    EXPERIMENT, not a captured truth: SE's own 4:5 reply has never been
    captured. POL_STATUS_ECHO=0 restores the zero-fill if this regresses a
    client -- check the Viewer still logs in after changing this.
    """
    if req_pt is None or os.environ.get("POL_STATUS_ECHO", "1") != "1":
        return None
    body = req_pt[40:]
    echo = body[:n]
    if len(echo) < n:
        echo += b"\x00" * (n - len(echo))
    log("lobby", f"  4:5 KChangeMyStatus: echoing {n}B of the requested state "
                 f"(was {n} zero bytes); POL_STATUS_ECHO=0 to revert")
    # DUMP THE STATE (2026-08-16, TM track). This blob is how the client says
    # WHICH TITLE it is in -- the comment above already reads a content id out
    # of it (0x0e=14 for the Viewer), and Tetra Master is content id 2. It is
    # the candidate carrier for "friends see me in Tetra Master", so log it
    # rather than inferring the change from the reply checksum. Cheap, and it
    # is the only place the decrypted state is in hand.
    log("lobby", f"  4:5 state: {echo.hex()}")
    # THE CONTENT AUTH VALUE (contentauth.py). SE's reply carries
    # a fresh 16-byte random value in its first 16 bytes -- the bytes this
    # echo has always filled with the request's zeros -- and polcore keys the
    # title with it (FMO: TCP RC4 key = value + our 0x0322 tail). Minting one
    # per login is what lets FMO name the member from the key instead of the
    # address. Only zones in POL_CONTENT_AUTH_ZONES; every other title gets
    # the unchanged echo.
    _zf = _status_frame_zone(echo)
    if (contentauth is not None and _zf is not None
            and _zf[0] in contentauth.ZONES):
        _mid = lobbysession._session_get("member_id")
        if _mid is None:
            log("lobby", f"  4:5 content auth: zone {_zf[0]} but NO member "
                         f"bound to this session -- echoing zeros; the title "
                         f"falls back to the address")
        elif not contentauth.enabled_for(_mid):
            pass        # rollout gate (content-auth-members.txt): unchanged echo
        else:
            _cv = contentauth.mint()
            try:
                contentauth.publish(_mid, lobbysession._session_get("peer_ip"), _zf[0], _cv)
                echo = _cv + echo[contentauth.LEN:]
                log("lobby", f"  4:5 content auth: zone {_zf[0]} member {_mid} "
                             f"-> value {_cv.hex()[:8]}.. in reply bytes 0..15")
            except (OSError, ValueError) as exc:
                log("lobby", f"  4:5 content auth: cannot publish ({exc}) -- "
                             f"echoing zeros so the key stays one FMO can read")
    # AND ACT ON IT: +19 is "inside a title", u16 at +20 the content id
    # (1000 = the Viewer). This is the only place we learn it, and the
    # presence push in the other container reads it back via _title_zone.
    if len(echo) >= 22:
        # WARNING: THE ZONE DECIDES, NOT BYTE 19 -- see _STATUS_ZONE_LATCH. Tetra
        # Master sends in_title=0 while still naming zone 2 nine seconds
        # after entering, and taking that as an exit is what showed a friend
        # in TM as sitting in the Viewer.
        zone, in_title = _status_frame_zone(echo)
        titlezone._publish_title_zone(lobbysession._session_get("member_id"), zone, in_title)
        log("lobby", f"  4:5 zone={zone} in_title={in_title} "
                     f"(flag={echo[19]})")
    # AND THE PRESENCE STATUS, which is the OTHER field in this body and had
    # never been read (2026-08-19, `pol-presence-status-protocol`). One byte
    # at +0x16: 01 Online, 02 Away, 05 Invisible. It is what SE reflects as
    # the `G@`/`H@` letter in the in-room 352 WHO -- we hardcoded `H`, so an
    # AFK member showed as present -- and it is the state a friend-list
    # presence push should carry. Stored here because this is the only place
    # the client ever says it; `_member_status_watcher` in the OTHER
    # container turns a change into the push (see `_publish_member_status`
    # for why it crosses through a file).
    code = _status_frame_code(echo)
    if code is not None:
        named = {_STATUS_ONLINE: "online", _STATUS_AWAY: "away",
                 _STATUS_INVISIBLE: "invisible"}.get(code)
        if _publish_member_status(lobbysession._session_get("member_id"), code):
            log("lobby", f"  4:5 status={code:#04x}"
                         + (f" ({named})" if named
                            else " -- UNMEASURED code; 03/04 are the two we "
                                 "have never seen, capture what the client "
                                 "was showing and name it")
                         + f"; WHO will now say "
                           f"{'G' if _status_is_away(code) else 'H'} for this "
                           "member")
    return echo
