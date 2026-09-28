"""Rooms as the lobby browses them: created, persistent, published and restored rooms."""
import json
import os
import struct
import time
import threading
import titles                   # the title-plugin seam (services/titles.py)  # noqa: E402
from srvcore import log
from . import handlelists, lobbymail, lobbysearch, lobbysession, roomregistry



#: ROOM SEARCH (opcode 05:03, tag 0x03E9) -- MEASURED ON SE 2026-08-15.
#:
#: The note below this one used to say 0x03E9 was "a status WRITE ... which must
#: keep their all-zero reply". It is not: it is the CHAT ROOM SEARCH, and
#: answering it with zeros is why every zone reported "Could not find any chat
#: rooms in this zone". SE's own client sends
#:
#:     UPPER(z_topic)=UPPER(Novice_Hall)      tag 0x03E9
#:     UPPER(z_up_name)=UPPER(Lex)            tag 0x03E8  (members)
#:
#: -- the same message with a different predicate field, which is also why its
#: length varies (52 / 84 / 124 / 132 bytes observed: the predicate is what
#: varies). The 52-byte form carries no predicate at all: "list this zone".
#:
#: The ROWS come back through 3:0 `u/s/select<slot>` exactly as member rows do.
#: One 160-byte record per room, captured live and used here as a TEMPLATE with
#: only the two strings and the two per-room numbers substituted -- the record is
#: a 0x03-tagged stream whose blobs are base-64 ('T' is that alphabet's zero), and
#: inventing those from scratch would be guessing:
#:
#:     +0x0A  room id, NUL-terminated, 'T'-padded to +0x38   ("ZYOTYU000003")
#:     +0x3E  u16   THE ZONE the room is in (0x044C = 1100 for both SE rooms)
#:     +0x41  u16   differs per room (100 / 102)   \ the obvious reading is
#:     +0x44  u16   differs per room (202 / 201)   / occupancy and capacity
#:     +0x4A  room name, NUL-terminated, 'T'-padded to +0x93 ("Novice_Hall")
#:
#: Everything else is byte-for-byte SE's. The two numbers are NOT confirmed to be
#: occupancy/capacity -- that is a guess a capture with a known headcount settles.
_ROOM_RECORD = 160
_ROOM_RECORD_TEMPLATE = bytes.fromhex(
    "0303230301034303500354545454545454545454545454545454545454545454"
    "5454545454545454545454545454545454545454545454540300000000034c04"
    "03640003ca00032d010354545454545454545454545454545454545454545454"
    "5454545454545454545454545454545454545454545454545454545454545454"
    "5454545454545454545454545454545454545403140000000300039540e76854"
)
_SEARCH_TAG_ROOM = 0x03E9

#: *** A ROOM BELONGS TO EXACTLY ONE ZONE, AND THE CLIENT SAYS WHICH ONE IT IS
#: ASKING ABOUT. *** Measured 2026-08-16 off SE's own frames plus our own logs.
#:
#: The room browser's `5:3` carries the wanted zone as TLV item **id 0x06** (see
#: `_search_zone`), and the room record echoes it back at **+0x3E**. In SE's live
#: session the account holder browsed two zones and SE answered them differently:
#:
#:     zone 0x044C (1100)   6 hits   -- Novice_Hall & co, the PlayOnline Zone
#:     zone 0x044F (1103)   0 hits   -- until they CREATED `FOXROOM` in it,
#:                                      after which the same query returned 1
#:
#: So "list every room in every zone", which is what this used to do, is not a
#: cosmetic difference: SE serves an EMPTY list outside the PlayOnline Zone, and
#: the persistent rooms below exist in 1100 alone.
_ZONE_PLAYONLINE = 0x044C

#: *** THE PERSISTENT ROOMS, READ OUT OF SE'S OWN `3:0` REPLIES (2026-08-16). ***
#: Three replies in the 2026-08-15 capture, byte-identical to each other, each 960
#: bytes = six 160-byte records. All six are in zone 1100, and they are the SAME
#: THREE ROOMS IN TWO LANGUAGES -- the pairing is not a guess, the two unknown
#: numbers match across each pair and nothing else does:
#:
#:   #01CP ZYOTYU000003  Novice_Hall         100 202 301   <-> #00CP ZYOTYU000001
#:   #01CP ZYOTYU000004  Town_Square         102 201 301   <-> #00CP ZYOTYU000000
#:   #01CP ZYOTYU000005  Traveller's_Haven   100 200 301   <-> #00CP ZYOTYU000002
#:
#: Two fields fall out of that:
#:
#:   +0x47  301 on every English row, 300 on every Japanese one -- the LANGUAGE.
#:          Earlier notes had this as a constant `03 2d 01`; it is constant only within
#:          one language. The client shows three rooms, not six, so it is the
#:          client that filters -- serve all six, as SE does.
#:   +0x02/+0x04/+0x06/+0x08  the channel prefix, spelled out one field per
#:          character: '#', 01, 'C', 'P'. The Japanese rows carry 00 and the
#:          room the account holder CREATED carries 'U' (`#01CU`), which is
#:          exactly the two namespaces the IRC side already knew about.
#:
#: *** +0x41 AND +0x44 ARE NOT OCCUPANCY AND CAPACITY. *** That was floated as
#: "the obvious reading ... a guess a capture with a known headcount settles".
#: This is that capture and it settles it NEGATIVE: the created room `FOXROOM`,
#: which had at most two people in it, reports 101 and 203, and `#01CP` rooms
#: answer `MODE` with `+l 21`. A hundred-odd people do not fit in twenty-one
#: seats. They are served verbatim and nothing computes them.
#:
#: (zone, chan_prefix, room_id, name, f41, f44, lang)
_SE_ROOMS = (
    (1100, "#00CP", "ZYOTYU000000", "出会いの広場",
     102, 201, 300),                                  # "Meeting Plaza"
    (1100, "#00CP", "ZYOTYU000001", "初心者の館",
     100, 202, 300),                                  # "Novice Hall"
    (1100, "#00CP", "ZYOTYU000002", "旅立ちの部屋",
     100, 200, 300),                                  # "Room of Departure"
    (1100, "#01CP", "ZYOTYU000003", "Novice_Hall", 100, 202, 301),
    (1100, "#01CP", "ZYOTYU000004", "Town_Square", 102, 201, 301),
    (1100, "#01CP", "ZYOTYU000005", "Traveller's_Haven", 100, 200, 301),
)


#: *** PLAYER-CREATED ROOMS ARE LISTED TOO, AND THIS IS WHERE THEY LIVE. ***
#: Everything outside the six fixtures above is created by somebody and lasts as
#: long as they are in it. SE's row for the one in the capture:
#:
#:   #01CU IT41NUQCZETZJ55OBU4AECNNNNNNNNNNSQ3E3N1N  zone 1103  101 203 301  FOXROOM
#:
#: so the id is the 40-symbol token out of the channel name and the prefix carries
#: 'U' instead of 'P'. The 101/203/301 are its MEMBERS, PURPOSE and LANGUAGE, and
#: they are read from the creator's own TOPIC (see `_parse_room_topic`) rather than
#: copied from FOXROOM -- which is what they were, so every room anybody made came
#: out "Professionals Only / What's cool / English" whatever they had picked.
#:
#: chan -> row. Populated when the creator names the room with a TOPIC, which is
#: the first moment the room HAS a name; dropped when the room empties, because a
#: created room is exactly as alive as its occupants.
_CREATED_ROOMS = {}
_CREATED_ROOMS_LOCK = threading.RLock()

#: *** THE TOPIC IS THE ROOM'S SETTINGS, AND IT DECODES COMPLETELY. ***
#: Twelve POL-base64 symbols (= 9 bytes) followed by the plain name. Decoded
#: 2026-08-16 against the client's own option lists:
#:
#:     +0  u16  zone
#:     +2  u16  members    100 + index, 0 = "Not set"
#:     +4  u16  purpose    200 + index, 0 = "Not set"
#:     +6  u16  language   300 + index, 0 = "Not set"
#:     +8  u8   0
#:
#: and those three are exactly the record's +0x41 / +0x44 / +0x47. Five samples:
#:
#:   zT95ToiTK7IT Novice_Hall  1100  Beginners Welcome  Looking for advice  English
#:   zt9rTojTK7IT FOXROOM      1103  Professionals Only What's cool         English
#:   zt9rToiTKtIT CYNROOM      1103  Professionals Only Looking for advice  German
#:   zT7TTTTTTTTT WHAT (ours)  1100  Not set            Not set             Not set
#:   zt7TTTTTTTTT CoolChat     1103  Not set            Not set             Not set
#:
#: Two independent things vouch for it. The fixtures' settings describe the
#: fixtures -- Novice_Hall is "Beginners Welcome / Looking for advice",
#: Town_Square is "Everyone Welcome / Chitchat" -- and CYNROOM reads language
#: **303**, a value never seen anywhere else, which is German by the option
#: ordering alone. Our own two rooms read "Not set" throughout, which is how they
#: were made.
#:
#: THE ZONE COMES FROM HERE. It used to be inferred from whichever zone the
#: creator's session last browsed, which had to cross a container boundary to be
#: read at all; the client states it outright, one field into the topic it was
#: already sending. The inference stays only as a fallback for a zone of 0.
_ROOM_TOPIC_SETTINGS_LEN = 12
_ROOM_SETTING_BASES = {"f41": 100, "f44": 200, "lang": 300}

#: For logs, and for anyone reading a room row later. Order is the client's own,
#: from its create dialog; index 0 is the first entry under each heading.
_ROOM_MEMBERS_NAMES = ("Beginners Welcome", "Professionals Only",
                       "Everyone Welcome", "Regulars Only")
_ROOM_PURPOSE_NAMES = ("Coffee Break", "Chitchat", "Looking for advice",
                       "What's cool", "Discussion forum")
_ROOM_LANGUAGE_NAMES = ("Japanese", "English", "French", "German")


def _room_setting_name(names, base, value):
    """"Beginners Welcome" / "Not set" / "?107" -- never raises, it is for a log."""
    if not value:
        return "Not set"
    i = value - base
    return names[i] if 0 <= i < len(names) else f"?{value}"


def _parse_room_topic(topic):
    """(settings, name) from a TOPIC value. settings is None if it is not one."""
    text = topic.decode("cp932", "replace") if isinstance(
        topic, (bytes, bytearray)) else str(topic)
    name = text[_ROOM_TOPIC_SETTINGS_LEN:]
    blob = lobbymail._b64decode(text[:_ROOM_TOPIC_SETTINGS_LEN])
    if len(blob) < 8:
        return None, name
    zone, members, purpose, lang = struct.unpack_from("<HHHH", blob, 0)
    return {"zone": zone, "f41": members, "f44": purpose, "lang": lang}, name

#: *** THE ZONE HAS TO CROSS A PROCESS BOUNDARY, AND THAT IS THE WHOLE PROBLEM. ***
#: The browse (`5:3`) arrives on the LOBBY band, which is the `login` container;
#: the room is created over IRC, which is `authsess`. A module-level dict bridges
#: nothing between them -- it would read empty on the far side every single time,
#: and the only symptom would be a room that quietly never lists.
#:
#: So it rides the SESSION, which already exists to hand state between exactly
#: these two processes (`_SESSION_FILE`, merged rather than overwritten) and which
#: both bands of one launch agree on. `_BROWSE_ZONE` stays as a same-process
#: fallback for the tests and for a single-container deployment.
_BROWSE_ZONE = {}


def _note_browse_zone(zone):
    """Remember the zone this session is browsing, for both bands to see."""
    if zone is None:
        return
    sid = lobbysession._session_sid()
    if sid:
        lobbysession._session_put(sid, room_zone=int(zone))
    mid = lobbysession._session_member_id()
    if mid:
        _BROWSE_ZONE[int(mid)] = int(zone)


#: *** THE ZONE SUMMARY -- WHY EVERY ROOM AND USER COUNT READS ZERO. ***
#: `5:3` tag 0x03EA is a THIRD query on this opcode, and the note beside
#: `_SEARCH_TAG` calls it "a status WRITE ... which must keep their all-zero
#: reply". It is not a write. SE answers it with ONE row and then serves a
#: 16-byte record through the usual `3:0 u/s/select<slot>`:
#:
#:   5:3  tag 0x03EA, 0 items, 52-byte body   ->  slot=2 count=1
#:   3:0  u/s/select2                         ->  20B payload, 16B record + cksum
#:
#:        03 03 4c 04 | 03 06 00 00 00 | 03 00 00 00 00 | 00 71
#:           ^u16 zone     ^u32 rooms       ^u32 users     ^see below
#:
#: 1100 is the zone, 6 is exactly how many rooms SE served in it, and 0 users is
#: right for both samples -- the account holder was out of every room at each. Two
#: samples, both zone 1100.
#:
#: TWO THINGS ARE NOT ESTABLISHED, and both are visible if wrong rather than
#: silent. The last two bytes differ between the samples (`00 71` / `40 34`) with
#: no reading that fits, so they are served as zero. And the request carries NO
#: zone, so which zone SE is summarising is inferred from session state -- we use
#: the last zone this session browsed. If the client turns out to ask once per
#: zone while drawing the list, every row will show the same numbers, which is a
#: diagnosis rather than a mystery.
_SEARCH_TAG_ZONE = 0x03EA
_ZONE_SUMMARY_RECORD = 16


def _zone_summary_row(zone):
    """{zone, rooms, users} for the zone this session is looking at."""
    rooms = lobbysearch._room_search_rows([], zone)
    users = sum(_room_members(_room_chan(r)) for r in rooms)
    return {"zone": zone, "rooms": len(rooms), "users": users,
            "handle_name": f"zone {zone}"}


def _zone_summary_record(row):
    """SE's 16-byte zone-summary record, field for field."""
    r = bytearray(_ZONE_SUMMARY_RECORD)
    r[0:2] = b"\x03\x03"
    struct.pack_into("<H", r, 2, row["zone"] & 0xFFFF)
    r[4] = 0x03
    struct.pack_into("<I", r, 5, row["rooms"] & 0xFFFFFFFF)
    r[9] = 0x03
    struct.pack_into("<I", r, 10, row["users"] & 0xFFFFFFFF)
    return bytes(r)                       # +0x0E..0x0F left zero -- see the note


def _created_room_zone(sess):
    """The zone a room this session creates belongs in, or None if we cannot say.

    Deliberately refuses to guess with two clients up, the same rule (and for the
    same reason) as `_session_get`: filing somebody's room in a stranger's zone is
    worse than not listing it, because the room still works and only the browser
    lies.
    """
    zone = lobbysession._session_get("room_zone")
    if zone is not None:
        return int(zone)
    mid = getattr(sess, "member", None) if sess is not None else None
    if mid is not None and int(mid) in _BROWSE_ZONE:
        return _BROWSE_ZONE[int(mid)]
    if len(_BROWSE_ZONE) == 1:
        return next(iter(_BROWSE_ZONE.values()))
    return None


def _register_created_room(chan, topic, sess):
    """List a room the client just named, in the zone its creator was browsing."""
    if not chan.startswith(b"#01CU") or not topic:
        return
    settings, name = _parse_room_topic(topic)
    if not name:
        return
    settings = settings or {}
    # The client STATES the zone, one field into the topic. The session-based guess
    # is only for a topic that carries no zone at all.
    zone = settings.get("zone") or _created_room_zone(sess)
    if zone is None:
        log("authserv", f"  room {chan.decode('latin1')}: created as {name!r} but "
                        "NO zone is known for its creator, so it is not listed. "
                        "It still works; it just will not appear in the browser.")
        return
    # The creator sets `MODE +l N` moments after the TOPIC, so the capacity may not
    # be in yet on this pass; `_created_room_list` re-reads it every browse.
    with _CREATED_ROOMS_LOCK:
        _CREATED_ROOMS[chan] = {
            "zone": zone, "prefix": "#01CU",
            "room_id": chan[len(b"#01CU"):].decode("latin1"),
            "handle_name": name,
            "f41": settings.get("f41", 0),
            "f44": settings.get("f44", 0),
            "lang": settings.get("lang", 0),
            "created": True, "created_at": int(time.time()),
        }
    roomregistry.ROOMS._publish()          # the browser lives in another container
    log("authserv", "  room settings: members=%s purpose=%s language=%s" % (
        _room_setting_name(_ROOM_MEMBERS_NAMES, 100, settings.get("f41", 0)),
        _room_setting_name(_ROOM_PURPOSE_NAMES, 200, settings.get("f44", 0)),
        _room_setting_name(_ROOM_LANGUAGE_NAMES, 300, settings.get("lang", 0))))
    log("authserv", f"  room {chan.decode('latin1')}: LISTED as {name!r} in zone "
                    f"{zone} -- it will now show in that zone's browser")


#: *** ROOM STATE CROSSES A CONTAINER BOUNDARY, EXACTLY LIKE THE BROWSE ZONE. ***
#: `ROOMS` and `_CREATED_ROOMS` are populated over IRC, in `authsess`. The room
#: browser, the per-room headcount and the zone summary are all served over the
#: lobby band, in `login`, where both are permanently empty -- so a created room
#: never listed and every count read zero, with nothing logged to say why. The
#: registration itself was working the whole time; `authserv.log` said
#: `LISTED as 'HI' in zone 1100` while `lobby.log` served six rooms.
#:
#: Only the process that MUTATES the registry writes this file (`_ROOMS_OWNER`,
#: set by `RoomRegistry._publish`). A reader can therefore never overwrite it with
#: its own empty view -- which is precisely how `_SESSION_FILE` got broken once
#: before, and it cost a POL-0010.
_ROOMS_FILE = os.environ.get(
    "POL_ROOMS_FILE", os.path.join(os.environ.get("POL_DATA_DIR", "/data"),
                                   "rooms-live.json"))
_ROOMS_OWNER = [False]
_ROOMS_CACHE = {"mtime": -1.0, "data": {}}


def _rooms_local_state():
    """{chan: {"members": n, "room": row|None}} from THIS process's own memory."""
    out = {}
    for chan, members in roomregistry.ROOMS.snapshot().items():
        # `who` -- WHO, not just HOW MANY. Added 2026-08-18 for the PS2 title's
        # lobby lists: `b/g/PTL`'s member block draws a NAME per row
        # (roomwin__ZoneListPage_0033e2c0 reads record +0x28), and that record is
        # built in the `login` container, which can only see this file. A count
        # cannot be turned back into people, so the identity has to cross here.
        # Every existing reader takes `members` and is unaffected.
        out[chan] = {"members": len(members), "room": None,
                     "who": _room_roster(chan)}
    # A CREATED ROOM DIES WITH ITS LAST OCCUPANT. Dropping it here rather than
    # merely hiding it is what stops the published table filling with rooms nobody
    # is in -- they were filtered from the browser but never actually let go.
    with _CREATED_ROOMS_LOCK:
        for chan in [c for c in _CREATED_ROOMS
                     if c.decode("latin1") not in out]:
            row = _CREATED_ROOMS.pop(chan)
            log("authserv", f"  room {chan.decode('latin1')}: empty, so "
                            f"{row['handle_name']!r} leaves the browser")
    with _CREATED_ROOMS_LOCK:
        for chan, row in _CREATED_ROOMS.items():
            key = chan.decode("latin1")
            # The capacity is read HERE and not frozen at registration: the client
            # sends `MODE +l N` after the TOPIC that names the room, so at
            # registration time there is nothing to record yet. The key travels
            # the same way (`MODE +k` follows the TOPIC too), so "does the door
            # need a password" is also a browse-time read.
            modes = roomregistry.ROOMS.modes(chan)
            limit = modes.get("l")
            out.setdefault(key, {"members": 0, "room": None})["room"] = dict(
                row, capacity=int(limit) if limit else lobbysearch._ROOM_CAPACITY_CREATED,
                keyed=bool(modes.get("k")))
    # THE REGISTRATION RIDES ALONG so the NEXT process can restore what memory
    # loses in a restart: owner, topic, modes (the +l/+k a fresh registry would
    # otherwise re-default -- the exact `+l 1` that bounced a joiner on
    # 2026-08-19) and the member nicks that become ghosts. Readers of the old
    # shape are unaffected: "reg" is a new sibling of the chan keys' dict.
    out["__reg__"] = roomregistry.ROOMS.registration()
    return out


#: member id -> (handle name, when read), so a publish does not re-query the DB
#: once per occupant per JOIN. Names change rarely and a stale one is a cosmetic
#: fault; a query storm on the join path is not.
#:
#: BUT NOT FOREVER. Handles can be renamed by another process
#: (accounts.rename_handle), and an entry that never expired would keep drawing
#: the old name until authsess restarted. A miss ("" -- no such member, or a DB
#: error) expires too, so one failed read cannot blank a player for the life of
#: the process.
_ROOM_NAME_CACHE = {}
_ROOM_NAME_TTL = 60.0


def _room_roster(chan):
    """`[{"nick", "member_id", "name"}]` for one channel, for publication.

    A session with no member bound still appears, with member_id 0 and whatever
    name we have -- being in the room is a fact about the socket, not about the
    account, and dropping the row would under-count the room.
    """
    key = chan.encode("latin1") if isinstance(chan, str) else chan
    out = []
    for m in roomregistry.ROOMS.members(key):
        mid = 0
        row = getattr(m, "member", None)
        if row is not None:
            try:
                mid = int(row["id"])
            except (KeyError, TypeError, ValueError, IndexError):
                mid = 0
        out.append({"nick": m.nick.decode("latin1", "replace"),
                    "member_id": mid, "name": handlelists._member_display_name(mid)})
    # ghosts count: a restart survivor is still in the room from every client's
    # point of view, and a browser count that drops them reads "empty" for a
    # room somebody is sitting in
    live = {r["nick"] for r in out}
    for nk in roomregistry.ROOMS.ghost_nicks(key):
        g = nk.decode("latin1", "replace")
        if g not in live:
            out.append({"nick": g, "member_id": 0, "name": None})
    return out


def _publish_rooms():
    """Write the registry's state where the other container can read it."""
    state = _rooms_local_state()
    # A ROOM ARRIVAL IS A ROSTER EVENT. Done here rather than on the JOIN path
    # because this is the ONE function every registry mutation goes through, and
    # a delta stream that misses a join is worse than no delta stream at all.
    titles.rooms_changed(state)
    os.makedirs(os.path.dirname(_ROOMS_FILE), exist_ok=True)
    tmp = _ROOMS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, _ROOMS_FILE)              # atomic
    _ROOMS_CACHE["mtime"] = -1.0              # our own next read re-stats


def _live_rooms():
    """Room state from wherever it is actually known.

    The owning process answers from memory -- it IS the truth, and an empty
    registry there means empty, not stale. Everyone else reads the file, re-read
    only when its mtime moves.
    """
    if _ROOMS_OWNER[0]:
        state = _rooms_local_state()
        state.pop("__reg__", None)            # bookkeeping, not a room
        return state
    try:
        mtime = os.stat(_ROOMS_FILE).st_mtime
    except OSError:
        return {}
    if mtime != _ROOMS_CACHE["mtime"]:
        try:
            with open(_ROOMS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            data.pop("__reg__", None)         # bookkeeping, not a room
            _ROOMS_CACHE["data"] = data
            _ROOMS_CACHE["mtime"] = mtime
        except (OSError, ValueError):
            return _ROOMS_CACHE["data"]       # torn write: last good view stands
    return _ROOMS_CACHE["data"]


_ROOMS_RESTORED = [False]
_ROOMS_RESTORE_LOCK = threading.Lock()


def _restore_rooms_once():
    """Load the previous process's room registrations, exactly once.

    rooms.json was publish-only until 2026-08-19: a deploy restart wiped every
    room while authrelay kept the clients' sessions alive, so a survivor sat in
    a room the server had forgotten (their PART arrived for an unknown channel)
    and the next joiner was made @owner of an empty ghost and bounced off the
    fresh-room `+l 1`. Restoring returns owner/topic/modes to the registry and
    the members as GHOSTS that re-attach on their next touch or expire
    (POL_ROOM_GHOST_TTL, default 900 s).

    Only a RECENT file restores (POL_ROOM_RESTORE_WINDOW, default 6 h): rooms
    die with their last occupant, so resurrecting last week's registry would
    fill the browser with rooms nobody is in.
    """
    if _ROOMS_RESTORED[0]:
        return
    with _ROOMS_RESTORE_LOCK:
        if _ROOMS_RESTORED[0]:
            return
        _ROOMS_RESTORED[0] = True
        try:
            st = os.stat(_ROOMS_FILE)
            window = float(os.environ.get("POL_ROOM_RESTORE_WINDOW", "21600"))
            if time.time() - st.st_mtime > window:
                log("authserv", f"room restore: {_ROOMS_FILE} is "
                                f"{time.time() - st.st_mtime:.0f}s old "
                                f"(> {window:.0f}); starting empty")
                return
            with open(_ROOMS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except (OSError, ValueError):
            return                            # no file / torn: a fresh start
        reg = data.get("__reg__") or {}
        ttl = float(os.environ.get("POL_ROOM_GHOST_TTL", "900"))
        n = roomregistry.ROOMS.restore_state(reg, ttl) if reg else 0
        # the room BROWSER registrations ride the same file: without these a
        # restored room has modes and ghosts but no listing, which reads as
        # "the room vanished" in the browser even though its door still works
        restored_rows = 0
        with _CREATED_ROOMS_LOCK:
            for key, entry in data.items():
                if key == "__reg__" or not isinstance(entry, dict):
                    continue
                row = entry.get("room")
                if not row:
                    continue
                chan = key.encode("latin1")
                if chan not in _CREATED_ROOMS:
                    row = dict(row)
                    row.pop("capacity", None)  # re-derived from modes at publish
                    _CREATED_ROOMS[chan] = row
                    restored_rows += 1
        if n or restored_rows:
            log("authserv", f"room restore: {n} registration(s), "
                            f"{restored_rows} browser row(s), ghosts expire in "
                            f"{ttl:.0f}s")
            # WARNING: AND REPUBLISH, OR EVERY COUNT KEEPS SHOWING THE OLD WORLD.
            # `_publish_rooms` runs on registry MUTATIONS, and a restart is not
            # one -- so `rooms-live.json` kept the last snapshot the PREVIOUS
            # process wrote, live member rows and all, until somebody happened to
            # join something. Reported 2026-08-20T19:56: the zone screen said
            # "1 player in Mermaid's Dreamworld" with nobody online, and the file
            # backing it was stamped 19:50:50 -- three minutes before the restart
            # that was serving it.
            #
            # The restored state is the honest one: ghosts carry `member_id 0`
            # and `snapshot()` counts LIVE SESSIONS, so a room nobody has
            # re-attached to republishes as 0. A survivor who is still connected
            # is put back by `adopt_ghosts` on their next touch, which publishes
            # again -- so the only window is between the restart and their first
            # message, and erring toward "nobody is there" is the right way round
            # for a number on a screen.
            _publish_rooms()


def _room_members(chan):
    """How many people are in a channel, asked from either container."""
    entry = _live_rooms().get(
        chan.decode("latin1") if isinstance(chan, (bytes, bytearray)) else chan)
    return int(entry.get("members", 0)) if entry else 0


def _created_room_list():
    """Created rooms that still have somebody in them.

    Liveness comes from the same snapshot as everything else, so there is no
    second place for "is this room still there" to be wrong; a room whose last
    member left is reaped by the registry and drops out here on the next browse.
    """
    out = []
    for _chan, entry in _live_rooms().items():
        row = entry.get("room")
        if row and int(entry.get("members", 0)) > 0:
            out.append(dict(row))
    return out


def _persistent_rooms():
    """The fixtures, as dicts. POL_ROOMS replaces the SE set entirely.

    The override spelling is "zone:id:name" (or the older "id:name", which means
    the PlayOnline Zone). An overridden room takes the English prefix and SE's
    Novice_Hall numbers, because those fields are not understood well enough to
    invent values for -- see the note above.
    """
    env = os.environ.get("POL_ROOMS")
    if not env:
        return [{"zone": z, "prefix": p, "room_id": i, "handle_name": n,
                 "f41": a, "f44": b, "lang": g}
                for z, p, i, n, a, b, g in _SE_ROOMS]
    out = []
    for item in env.split(","):
        item = item.strip()
        if not item:
            continue
        parts = [p.strip() for p in item.split(":")]
        if len(parts) == 2:
            zone, rid, name = _ZONE_PLAYONLINE, parts[0], parts[1]
        elif len(parts) >= 3:
            try:
                zone = int(parts[0], 0)
            except ValueError:
                log("lobby", f"  rooms: bad zone in POL_ROOMS entry {item!r} -- skipped")
                continue
            rid, name = parts[1], ":".join(parts[2:])
        else:
            continue
        if rid and name:
            out.append({"zone": zone, "prefix": "#01CP", "room_id": rid,
                        "handle_name": name, "f41": 100, "f44": 202, "lang": 301})
    return out


def _room_list():
    """Every room the browser can show: the fixtures plus the live player rooms."""
    return _persistent_rooms() + _created_room_list()


def _room_chan(room):
    """The IRC channel a room row points at: its prefix plus its id."""
    return (room["prefix"] + room["room_id"]).encode("cp932", "replace")


def _room_record(room):
    """One 160-byte room row, from SE's captured template.

    Every field written here was measured in SE's own records; everything else
    stays as the template, which is SE's bytes.
    """
    r = bytearray(_ROOM_RECORD_TEMPLATE)

    def put(off, end, text):
        raw = str(text).encode("cp932", "replace")[:end - off - 1] + b"\x00"
        r[off:off + len(raw)] = raw          # the rest stays 'T', as SE pads

    prefix = room.get("prefix", "#01CP")
    r[0x02] = ord(prefix[0])                             # '#'
    r[0x04] = int(prefix[1:3])                           # 01 English / 00 Japanese
    r[0x06] = ord(prefix[3])                             # 'C'
    r[0x08] = ord(prefix[4])                             # 'P' listed / 'U' created
    put(0x0A, 0x38, room["room_id"])
    put(0x4A, 0x93, room["handle_name"])
    # *** +0x39 IS z_npers, THE HEADCOUNT the browser draws as `N/`. *** CONFIRMED
    # live 2026-08-19 by the POL_ROOM_NPERS_OFF sweep: with the probe on 0x39 the
    # server served room IMADETHIS with one occupant and the row read `1/10`. It
    # had been mislabeled the "created" flag (and briefly `keyed`) -- but
    # created-ness is the PREFIX at +0x08 ('U' created / 'P' listed), not this
    # field. Writing the created flag here is exactly why every created room's
    # count was pinned at 1 and every fixture at 0: a constant masquerading as a
    # broken count. It carries the real live headcount now.
    struct.pack_into("<I", r, 0x39, _room_members(_room_chan(room)) & 0xFFFFFFFF)
    struct.pack_into("<H", r, 0x3E, room["zone"] & 0xFFFF)
    struct.pack_into("<H", r, 0x41, room.get("f41", 100) & 0xFFFF)
    struct.pack_into("<H", r, 0x44, room.get("f44", 202) & 0xFFFF)
    struct.pack_into("<H", r, 0x47, room.get("lang", 301) & 0xFFFF)
    # +0x94 = z_capa (capacity); the `MODE +l N` value shows as the `/N`. +0x99 =
    # z_chlock (the PADLOCK) -- confirmed 2026-08-19: writing the headcount here
    # (the old "occupancy" reading, ambiguous because SE's FOXROOM sample was ALSO
    # keyed) minted a lock on every occupied room. It carries the real key state.
    struct.pack_into("<I", r, 0x94, room.get("capacity", lobbysearch._ROOM_CAPACITY) & 0xFFFFFFFF)
    r[0x99] = 1 if room.get("keyed") else 0
    if room.get("created_at"):
        struct.pack_into("<I", r, 0x9B, int(room["created_at"]) & 0xFFFFFFFF)
    return bytes(r)
