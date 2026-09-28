"""Handle, character and list payloads served on the lobby band."""
import os
import struct
import time
from srvcore import log
from .deps import accounts
import titles
from . import characters, contentprofiles, friendgroups, friendlist, lobbyrooms, lobbysession, profilerecord, pushspool



def _member_primary_handle(db, member_id):
    row = db.execute("SELECT id FROM handle WHERE member_id = %s"
                     " ORDER BY is_primary DESC, id ASC LIMIT 1",
                     (int(member_id),)).fetchone()
    return int(row["id"]) if row else 0


#: The four LIST opcodes, resolved statically from polcore
#: (2026-08-11). Each reads an 8-byte count block and
#: then `count` fixed-size records -- and then STOPS. Unlike the mail list (3:3)
#: there is no 4-byte trailer, so the payload is exactly 8 + count*record.
#:
#:   opcode -> (record size, count field width, per-read record cap)
#:
#: The cap is the client's own `cmp <len>,0x800` clamp; it re-enters the read
#: state until the count is exhausted, so a payload longer than one cap is fine.
_LOBBY_LIST = {
    (0x00, 0x07): (0xA0, 1, 12),    # PS2-ONLY handle list. This client never sends
                                    # 0:9 -- it has no call site in pol.pex at all --
                                    # and asks here instead. Read off the client, not
                                    # guessed: the call site at va 0x0014a098 passes
                                    # (type 0, opcode 7, len 0), the consumer reads an
                                    # 8-byte count block with verify=0 and count in
                                    # byte 0 (`lbu a0,0(v1)`), then
                                    #     addiu s3, zero, 160    <- RECORD SIZE
                                    #     addiu v1, zero, 1920   <- per-read clamp
                                    #     mult v0, s3 ; divu s0, s3
                                    # so records are 0xA0, not the 0x88 of the PC's
                                    # 0:9, and the cap is 1920/160 = 12. Serving 0x88
                                    # here truncates the frame and the client hangs
                                    # waiting for the remainder (POL-0010).
                                    # The 160-byte LAYOUT is still unverified; the
                                    # PC's field offsets are reused. A wrong layout
                                    # shows a wrong name, not a hang.
    (0x00, 0x09): (0x88, 1, 15),    # 136B records, count = BYTE 0
    (0x01, 0x03): (0x68, 1, 19),    # 104B records, count = BYTE 0
    (0x02, 0x03): (0xA8, 4, 0x40),  # 168B records, count = U32. 0x40, NOT 12:
                                    # polcore's 2:3 reader (0x037e38c9..0x037e3a57)
                                    # takes the U32 count, then reads chunks of
                                    # min(remaining*168, 0x7e0) = up to 12 records
                                    # per socket read, files each at slot byte
                                    # +0x08 (a FULL byte, table of 200), subtracts,
                                    # and re-enters the read (state 5) until the
                                    # count is spent. 12 was that chunk size read as
                                    # a list cap (2026-08-13), which silently
                                    # dropped every friend past the 12th (a member
                                    # with 19 friends never saw 7 of them,
                                    # 2026-09-28).
                                    # 0x40 because the push records' slot byte
                                    # (+0x1c) and the slot maps are `< 0x40`; 200
                                    # once those are lifted.
    (0x07, 0x0C): (0x88, 1, 4),     # 136B records, count = BYTE 0, VALIDATED:
                                    # byte 0 <= 4 and bytes 1..4 each <= 0x40 or
                                    # the client fails with -5133. THE CAP IS 4,
                                    # not the 15 that stood here: 15 is the
                                    # client's 0x7f8 per-read clamp, but this
                                    # opcode validates the count byte itself, so
                                    # a 5th group would have shipped byte 0 = 5
                                    # and drawn -5133 instead of a longer list.
                                    # (`KGetGroupList` -- see _group_record.)
}


#: `sub_037debd0` -- the precondition helper at the head of 26 request builders --
#: rejects any slot index outside 0..3, so an account has at most four handles.
_HANDLE_SLOTS = 4

#: Size of polcore's character table at 0x3bc3080 (stride 104). The 1:3 record
#: loop drops any record whose index byte is >= 0x40 (`cmp ebx,0x40; jae`).
_CHAR_SLOTS = 0x40

#: Content IDs per handle. The binding the loop writes is one byte per position
#: in the 8 bytes at handle_slot+0x20, and SE's own limit is the same eight
#: ("You can link up to eight Content IDs to a handle", string 26069).
_CHAR_PER_HANDLE = 8


def _list_mode(op1, op2):
    """The per-opcode record flavour. POL_LOBBY_LIST_MODE="0:9=handles"."""
    for item in os.environ.get("POL_LOBBY_LIST_MODE", "").split(","):
        key, _, val = item.partition("=")
        a, _, b = key.strip().partition(":")
        try:
            if (int(a, 0), int(b, 0)) == (op1, op2):
                return val.strip().lower()
        except ValueError:
            continue
    return ""


def _int_field(fields, fid, default=0):
    """One profile field as an int. Text or missing values read as `default`."""
    try:
        return int(fields.get(fid, default))
    except (TypeError, ValueError):
        return default


def _db_handles():
    """The member's handles, primary first, capped at the client's four slots.

    Returns `[(handle_id, handle_name, {profile field_id: value}), ...]`. The id
    and the profile come along because the 0:9 record needs both: the id becomes
    the handle's on-wire guid (`accounts.handle_guid`) and the profile supplies
    its face icon. Fetching them per handle here is what makes the handle list,
    the badge and the profile screen agree on WHICH handle they are showing.
    """
    if accounts is None:
        return []
    try:
        db = accounts.connect()
        try:
            mid = lobbysession._session_member_id()
            row = db.execute("SELECT id FROM member WHERE id = %s",
                             (mid,)).fetchone() if mid else None
            if row is None:
                return []
            rows = db.execute(
                "SELECT id, handle_name FROM handle WHERE member_id = %s"
                " ORDER BY is_primary DESC, id ASC LIMIT %s",
                (row["id"], _HANDLE_SLOTS)).fetchall()
            return [(int(r["id"]), r["handle_name"],
                     accounts.get_handle_profile(db, int(r["id"])))
                    for r in rows]
        finally:
            db.close()
    except Exception as exc:
        log("lobby", f"handle list lookup failed ({exc!r}); serving none")
        return []


def _handle_list_entries():
    """The 0:9 handle list: OWN handles first (slots 0..N-1), then FRIENDS
    (slots N..) when POL_LOBBY_FRIEND_HANDLES is on.

    Returns `[(guid, name, face_icon), ...]`; the record's index is its handle-table
    slot (0x3bc5800). WHY FRIENDS BELONG HERE (RE'd 2026-08-13, app.dll base
    0x04850000): "view friend profile" builds the 05:04 `z_hid` from the handle
    table via `GetHandleWord(id)` = polcore 0x37dcc40 = `memcpy(out, 0x3bc5800 +
    id*0x28, 0x28)`. That table is filled by 0:9, NOT by the 2:3 friend list, and a
    friend with no handle-table slot falls to id 0 = SELF -> every friend's profile
    showed the viewer's own (the "profile bleed"). Serving each friend as a 0:9
    record at a slot past the four own-handle slots gives it a table entry with its
    real guid, so the friend row resolves to the friend. Friends land at slots
    >= _HANDLE_SLOTS(4), which the handle SWITCHER ignores (sub_037debd0 accepts
    only 0..3), so they are "known handles" for lookups, not selectable own ones.
    Guid MUST equal what 2:3 serves for the same friend (both `handle_guid`), or the
    client cannot link the friend row to its handle entry.
    """
    # (slot, guid, name, face_icon). Own handles keep their natural slots 0..N-1
    # (the switcher's range); friends go at slots >= _HANDLE_SLOTS so they never
    # show up as selectable own handles.
    own = [(i, accounts.handle_guid(hid), name, _int_field(prof, friendgroups._PROFILE_FICON))
           for i, (hid, name, prof) in enumerate(_db_handles())]
    if os.environ.get("POL_LOBBY_FRIEND_HANDLES", "0") != "1" or accounts is None:
        return own
    cap = _LOBBY_LIST[(0x00, 0x09)][2]                  # client per-read record cap
    slot = _HANDLE_SLOTS                                # first free non-own slot (4)
    # A friend's FACE ICON, not a hardcoded 0. Own handles take theirs from
    # their profile (field _PROFILE_FICON) three lines up; friends were getting
    # 0, which is "no badge" -- so every friend rendered blank whether or not
    # they had ever picked a portrait. Their icon lives on THEIR handle's
    # profile, so it is one query for the lot rather than one per friend.
    icons = _face_icons_by_handle()
    for guid, name, _kind, peer_handle, _status, _rid, _lbl in friendlist._db_friends(
            kinds=(accounts.KIND_FRIEND,)):
        if len(own) >= cap or slot >= 0x40:
            break
        own.append((slot, int(guid), name, icons.get(peer_handle, 0)))
        slot += 1
    return own


def _face_icons_by_handle():
    """`{handle_id: face_icon}` for every handle that has picked one.

    One query, because the caller needs it for a whole list. A handle with no
    stored portrait is simply absent, and the caller falls back to 0.
    """
    if accounts is None:
        return {}
    try:
        db = accounts.connect()
        try:
            return {int(r[0]): int(r[1]) for r in db.execute(
                "SELECT handle_id, val_int FROM handle_profile "
                "WHERE field_id = %s AND val_int IS NOT NULL",
                (friendgroups._PROFILE_FICON,))}
        finally:
            db.close()
    except Exception as exc:
        log("lobby", f"  face icons: lookup failed ({exc!r})")
        return {}


def _comments_by_handle():
    """`{handle_id: comment}` for every handle that has written one.

    The twin of `_face_icons_by_handle`, and it exists for the same reason: the
    friend-row spool needs the whole list at once, so this is one query rather
    than one per friend.

    WHY THE ROW SPOOL NEEDS IT AT ALL. A friend's comment has no PULL carrier --
    searched every lobby message in both SE captures for the known comment
    plaintexts and there is none, while your OWN comment has two (`4:6` +0x10 and
    the `0:9` record +0x20). It arrives only on this push, and SE sends it in the
    SAME record as the face icon: field flags 0x25 = BLOCK|ICON|COMMENT, icon at
    +0x50 and the comment at +0x58 (measured, `polshim-se.429364.log` line 146535).
    Pushing the icon without it is why pictures appeared and comments did not.

    Stored as `val_text`, unlike the icon's `val_int` -- same table, different
    column, which is the one thing to get right when copying the query above.
    """
    if accounts is None:
        return {}
    try:
        db = accounts.connect()
        try:
            return {int(r[0]): str(r[1]) for r in db.execute(
                "SELECT handle_id, val_text FROM handle_profile "
                "WHERE field_id = %s AND val_text IS NOT NULL AND val_text <> ''",
                (profilerecord._COMMENT_FIELD,))}
        finally:
            db.close()
    except Exception as exc:
        log("lobby", f"  row comments: lookup failed ({exc!r})")
        return {}


def _db_chars():
    """The session's CHARACTERS -- one per (handle, Content ID) link.

    Returns `[(handle_slot, position, content_code, content_id, bind), ...]` in
    the order the records go on the wire; the record's own index is its position
    in that list. `handle_slot` is the index into `_db_handles()`, i.e. exactly
    the slot number 0:9 gives the same handle -- the two lists MUST agree or the
    binding lands on the wrong handle (see `_char_record`).

    WARNING: **NEVER SERVE A HANDLE A NINTH RECORD. `_CHAR_PER_HANDLE` IS A HARD
    CEILING, AND THIS TRUNCATION IS THE ONLY THING ENFORCING IT.** A handle's
    binding array is eight bytes at handle_slot+0x20 and the record's position
    field is three bits, so only eight of a handle's Content IDs can be bound --
    SE's own limit, string 26069. This function used to serve the rest UNBOUND
    (`bind=False`) on the reasoning that the two things which gate play (the
    launch gate at app.dll+0x199093 and FFXI's world lookup FUN_100FFE00) read
    the 64-slot table and never the binding. Both really do. Both are irrelevant,
    because a THIRD consumer decides it: the Viewer reads a present-but-unbound
    Content ID as one that still needs a handle and opens the assign-a-handle
    flow, which dead-ends on the same ceiling -- 26069 "The handle "%s" is
    already linked to 8 Content IDs.", then 15083 "Which handle should this be
    copied to?", and the title is unreachable from that handle. Every record
    this returns is BOUND.

    WARNING: Which ones are dropped is chosen, not incidental. Positions go to every
    game's **slot 0 first**, in content-code order, and only then to a game's
    extra slots, so what falls off the end is an extra character slot of a
    title that issues one Content ID per character, and never a whole TITLE. It
    should not happen at all -- `titles.Title.content_slots` defaults to 1 and
    `accounts.ensure_content_slots` clamps the mint to eight per handle -- so
    the drop is LOGGED: a handle that reaches here over the ceiling has an
    id no client can see, and if a character is bound to it that character is
    POL-0001 with no explanation.
    """
    handles = _db_handles()
    if not handles or accounts is None:
        return []
    out = []
    try:
        db = accounts.connect()
        try:
            for slot, (hid, _name, _prof) in enumerate(handles[:_HANDLE_SLOTS]):
                links = accounts.handle_content_list(db, hid)
                primary = [l for l in links if int(l.get("slot", 0)) == 0]
                extra = [l for l in links if int(l.get("slot", 0)) != 0]
                order = primary + extra
                if len(order) > _CHAR_PER_HANDLE:
                    dropped = [(int(l["content_code"]), int(l.get("slot", 0)))
                               for l in order[_CHAR_PER_HANDLE:]]
                    log("lobby", f"  handle {hid} holds {len(order)} Content IDs,"
                                 f" over the ceiling of {_CHAR_PER_HANDLE}:"
                                 f" dropping (content_code, slot) {dropped}")
                    order = order[:_CHAR_PER_HANDLE]
                for pos, link in enumerate(order):
                    out.append((slot, pos, int(link["content_code"]),
                                link["content_id"] or "", True))
        finally:
            db.close()
    except Exception as exc:
        log("lobby", f"character list lookup failed ({exc!r}); serving none")
        return []
    return out[:_CHAR_SLOTS]


#: THE MOBILE FRIEND-LIST MARKER. The PC Viewer's 2:3
#: KGetFriendList request carries an EMPTY payload (40-byte header, +4 length
#: 0), and the reply is capped at 12 rows because that is the PC client's
#: per-read record cap. A mobile client has no such cap and wants
#: the whole list, so it sends these 4 ASCII bytes as the FIRST bytes of its
#: 2:3 payload, i.e. decrypted message offset 0x28..0x2B (header is 0x28 bytes;
#: payload +0x00). No PC/PS2 client sends a non-empty 2:3 payload, so the marker
#: cannot appear by accident. Anything after it (a cksum32 trailer, padding) is
#: ignored -- the server does not check request trailers.
_FRIENDS_MOBILE_MARKER = b"MOB1"
_LOBBY_REQ_HDR_LEN = 0x28


def _friends_mobile_request(req_pt):
    """True when `req_pt` is a 2:3 carrying the mobile marker and the marker is
    enabled (POL_FRIENDS_MOBILE=0 disables it: a marked request is then served
    exactly like the PC's)."""
    if req_pt is None or len(req_pt) < _LOBBY_REQ_HDR_LEN + 4:
        return False
    if (req_pt[1], req_pt[2]) != (0x02, 0x03):
        return False
    if os.environ.get("POL_FRIENDS_MOBILE", "1") == "0":
        return False
    head = bytes(req_pt[_LOBBY_REQ_HDR_LEN:_LOBBY_REQ_HDR_LEN + 4])
    return head == _FRIENDS_MOBILE_MARKER


def _friends_cap(req_pt=None):
    """The 2:3 row cap for this request: `_LOBBY_LIST`'s 0x40 for everyone, or
    POL_FRIENDS_MOBILE_CAP (default 64) for a marked mobile request.

    Rows are served in the same `_db_friends` order with the same slot numbers
    either way, so slot i is identical for a PC and a phone. Clamped to 0x40:
    the presence/friend-load paths skip slots >= 0x40, and never below the
    list cap. (The PC's cap was 12 until 2026-09-28 -- a misread of polcore's
    12-record READ CHUNK as a list limit; see `_LOBBY_LIST`.)"""
    cap = _LOBBY_LIST[(0x02, 0x03)][2]
    if not _friends_mobile_request(req_pt):
        return cap
    try:
        want = int(os.environ.get("POL_FRIENDS_MOBILE_CAP", "64"), 0)
    except ValueError:
        want = 64
    return max(cap, min(want, 0x40))


def _list_count(op1, op2, req_pt=None):
    """How many records to serve for a list opcode. POL_LOBBY_LIST="0:9=2,2:3=1".

    In `handles` mode the count comes from the DB instead, so registering a handle
    grows the list on the next login without touching the config.

    Defaults to 0, i.e. the 8-byte empty answer we have always sent.

    `req_pt` is the decrypted REQUEST message. Only 2:3 reads it, for the
    mobile marker (see `_friends_cap`); every other list ignores it, and
    omitting it gives exactly the old behaviour.
    """
    if (op1, op2) not in _LOBBY_LIST:
        return 0
    if _list_mode(op1, op2) in ("handles", "handlesz"):
        # Own handles are capped at the four switcher slots; friend handles (when
        # enabled) extend the list up to the client's 0:9 per-read record cap.
        if os.environ.get("POL_LOBBY_FRIEND_HANDLES", "0") == "1":
            return min(len(_handle_list_entries()), _LOBBY_LIST[(op1, op2)][2])
        return min(len(_db_handles()), _HANDLE_SLOTS)
    if _list_mode(op1, op2) == "chars":
        return min(len(_db_chars()), _LOBBY_LIST[(op1, op2)][2])
    if _list_mode(op1, op2) == "friends":
        # Count comes from the DB, so a friend added between logins appears with
        # no config change -- same contract as `handles`. `_LOBBY_LIST[..][2]`
        # bounds it (0x40 for 2:3; the client reads any count in 12-record
        # chunks, see the note there).
        return min(len(friendlist._db_friends(kinds=(accounts.KIND_FRIEND,))),
                   _friends_cap(req_pt) if (op1, op2) == (0x02, 0x03)
                   else _LOBBY_LIST[(op1, op2)][2])
    if _list_mode(op1, op2) == "groups" and accounts is not None:
        # *** COUNT FROM THE SAME LIST THE REPLY IS BUILT FROM. ***
        #
        # This asked `_db_friends(KIND_GROUP)` -- groups this handle OWNS -- while
        # the records, the length and the roster all come from `_groups_for_list`,
        # which also includes groups you are a MEMBER of. For an invitee the two
        # disagreed at the worst possible place: count 0, so the reply was the
        # 8-byte empty one and the record loop never ran. Making groups visible to
        # members therefore changed NOTHING for them, and could not have -- the
        # decision was taken here, before any of that code was reached, and it is
        # silent (an empty list logs nothing, which is why the log showed one
        # `7:12 groups:` line for the owner and none at all for the other two).
        #
        # Any future group-visibility rule belongs in `_groups_for_list` alone.
        return min(len(friendgroups._groups_for_list(_LOBBY_LIST[(op1, op2)][2])),
                   _LOBBY_LIST[(op1, op2)][2])
    for item in os.environ.get("POL_LOBBY_LIST", "").split(","):
        key, _, val = item.partition("=")
        a, _, b = key.strip().partition(":")
        try:
            if (int(a, 0), int(b, 0)) == (op1, op2):
                return max(0, int(val, 0))
        except ValueError:
            continue
    return 0


#: One 07:12 group MEMBER record, appended after the group headers.
_GROUP_MEMBER_REC = 0x20


#: Live-tunable 7:12 knobs, same control-file idiom as `_presence_cfg` -- the
#: client re-fetches the whole group list EVERY time the window opens (101 fetches
#: in one log), so a sweep needs no container recreate and no client restart:
#: edit `$POL_LOG_DIR/group.ctl`, reopen the list, read the result off the screen.
#: Clear the file when done -- a stale value here breaks every later group fetch,
#: exactly as `search-calib.txt` does for member search.
_GROUP_CTL_FILE = os.environ.get(
    "POL_GROUP_CTL", os.path.join(os.environ.get("POL_LOG_DIR", "/logs"),
                                  "group.ctl"))
_GROUP_CTL = {"mtime": None, "vals": {}}


def _list_paylen(op1, op2, count):
    rec = _LOBBY_LIST[(op1, op2)][0]
    n = 8 + count * rec
    if (op1, op2) == (0x07, 0x0C) and count \
            and os.environ.get("POL_GROUP_MEMBERS", "1") == "1":
        # The SAME group list, truncated the SAME way, as the record loop in
        # `_list_payload` -- it serves `friends[:count]`. Deriving the length
        # from a differently-shaped list is how a reply comes out declaring more
        # or fewer member records than it carries.
        n += sum(len(m) for m in friendgroups._group_members(friendgroups._groups_for_list(count))) \
            * _GROUP_MEMBER_REC
    return n


def _handle_record(slot, name, rec, base_bytes=None, guid=0, face_icon=0,
                   comment=None):
    """One 0:9 record: **a HANDLE**, in slot `slot`, named `name`.

    CONFIRMED LIVE 2026-08-11. 0:9 was served as a guessed content list and the
    client rendered the two records as the user's HANDLES -- the handle shown in
    the top-left became the record's name string, the "00-0" badge above it became
    "00-1" (i.e. record[0x00] is the slot number the badge displays), and the
    second record appeared as a second selectable handle. So 0:9 is the handle
    list, NOT the content list, and it is the missing half of handle persistence:
    the client re-prompted on every login because this list always came back empty.

    Corroboration: SE's own handle rule is "maximum 15 characters" (client string
    0x3D1D, `accounts.HANDLE_MAX`), which is exactly the width of the +0x10 field;
    `sub_037debd0` rejects slot indices outside 0..3, i.e. four handles per
    account; and 0:8 (handle registration) writes back into the very same 64-entry
    table at 0x3bc5800 that this record feeds.

    Layout from the consumer (polcore state 6 @0x37de235), which walks the block
    at stride 0x88 and scatters each record into two fixed tables indexed by
    record[0x00]:

        +0x00  u8   slot index, MUST be < 0x40   -> shown as the "00-N" badge
        +0x01  u8   6-bit field  -> slot+0x18 bits 2..7
        +0x02  u8   2-bit field  -> slot+0x18 bits 0..1
        +0x04  u32  FACE ICON    -> slot+0x1c
        +0x08  u32  packed 64-bit field with +0x0C (bit 0 used)
        +0x0C  u32  masked 0xfff -- a 12-bit field
        +0x10  15B  short code   (memcpy 0x0f -> slot+0x08)
        +0x20  50B  display name (memcpy 0x32 -> 0x386ab28 + idx*104)

    Mind the base register when re-deriving these: through the loop body `edi`
    points at record+8, not at the record, so the `lea eax,[edi+0x18]` feeding the
    50-byte copy is record+0x20. Reading it as +0x18 overlaps the 15-byte code.

    +0x08 AND +0x0C ARE THE HANDLE'S GUID, SPLIT -- and the record does NOT hold
    the packed form the client eventually reads. polcore's consumer
    (0x37de29a) does the packing itself:

        edx = record[+0x0C] & 0xFFF          ; the high 12 bits, masked
        eax = record[+0x08]                  ; the low 32 bits
        __allshl(eax:edx, 1)                 ; shift the 44-bit value up by one
        slot[0] = lo | (slot[0] & 1)         ; bit 0 stays the client's flag
        slot[4] = hi | (slot[4] & 0xFFFFE000)

    so we write the guid RAW, 32 bits at +0x08 and 12 at +0x0C, and let polcore
    shift it. Writing the already-shifted 64-bit word here (which is what the
    first cut of this did, reasoning from app.dll's reader alone) doubles every
    guid and drops the top bit off the 12-bit half.

    The SLOT INDEX is not in this word either. polcore takes it from record[0x00]
    and installs it at bits 45..50 itself (0x37de2c0: `eax = ebp & 0x3f;
    shl eax,0xd; slot[4] = eax | (slot[4] & 0xFFF81FFF)`). Both facts matter
    because app.dll then reads the packed result back as

        guid = (word >> 1)  & 0xFFF_FFFFFFFF     bits  1..44
        slot = (word >> 45) & 0x3F               bits 45..50

    and it was the ZEROES here that caused the visible bugs. Every record used to
    carry a flat `1`:

    * A zero SLOT meant every row claimed slot 0. `dHandleList+0x44` is that slot
      number and it is the argument the client passes to its own
      `GetHandleRecord()` (app.dll 0x4878592), so handle #2 asked for slot 0 and
      was answered with handle #1's name, face icon and content list. That is the
      "every handle shows the same profile" bug, and it lived in the CLIENT's
      lookup, not in our DB.
    * A zero GUID meant no handle could be named. The client hands this value
      back as the profile record's `z_hid` to say whose profile it wants, so
      every request arrived asking about handle 0.

    THE FACE ICON AT +0x04 reaches the main-interface badge, and only from here:
    the badge reads global 0x4CDE610, which is written from `slot+0x1C` at
    app.dll 0x488fc8e -- it never looks at the 05:04 profile record, which is why
    the portrait rendered in the handle editor and nowhere else. The value is
    `sheet * 8 + tile`, where sheet is the number in `data/icon/download/
    hnf%03d.png`; an unknown sheet resolves to 0 and 0 is the placeholder
    (app.dll 0x4a7d199). `work/pc/proface.py` decodes SE's catalogue of them.
    """
    out = bytearray(base_bytes if base_bytes is not None else bytes(rec))
    out[0x00] = slot & 0x3F
    out[0x01] = 1                                  # status guess: active
    out[0x02] = 1
    struct.pack_into("<I", out, 0x04, int(face_icon) & 0xFFFFFFFF)
    guid = int(guid) & friendgroups._HANDLE_GUID_BITS
    struct.pack_into("<I", out, 0x08, guid & 0xFFFFFFFF)
    struct.pack_into("<I", out, 0x0C, (guid >> 32) & 0xFFF)
    raw = name.encode("ascii", "replace")
    # +0x10 is memcpy'd with a hardcoded 0x0f and SE caps handles at 15 characters
    # (accounts.HANDLE_MAX), so a full-length handle fills the field with no NUL.
    # Shorter ones are NUL-padded, which is what the SJIS converter stops on.
    #
    # In probe mode (base_bytes = an offset-marker record) write only the value
    # plus ONE terminator and leave the rest of the marker intact: the client
    # stops at the NUL, so the name still renders correctly, while every byte
    # past it keeps naming its own offset for whatever OTHER field reads it.
    # +0x20 IS THE COMMENT, AND IT IS UTF-16LE. Measured on SE's LIVE service
    # 2026-08-15 (the 0:9 handle record): one record carried
    #     +0x10  "Examplemember" 00 00 00      8-BIT, the handle
    #     +0x20  48 00 61 00 69 00 …           UTF-16LE, the member comment
    # Two encodings sixteen bytes apart in one record. We used to write the
    # ASCII HANDLE at +0x20 as well, so the client decoded "Examplemember" as
    # UTF-16 and drew mojibake -- that is the long-standing "user comment comes
    # out as garbage" report, and it was our bytes, not the client.
    #
    # With no comment to serve we write ZEROS, not the name: an empty comment is
    # what SE sends for an empty comment, and blank beats garbage.
    cmt = ("" if comment is None else str(comment)).encode("utf-16-le", "replace")
    cmt = cmt[:48] + b"\x00\x00"                       # 50B field, always terminated
    if base_bytes is None:
        out[0x10:0x10 + 15] = raw[:15].ljust(15, b"\x00")
        out[0x20:0x20 + 50] = cmt.ljust(50, b"\x00")
    else:
        v = raw[:14] + b"\x00"
        out[0x10:0x10 + len(v)] = v
        out[0x20:0x20 + len(cmt)] = cmt
    return bytes(out)




def _list_payload(op1, op2, n, req_pt=None):
    """The count block plus `count` records.

    Default record content is an offset marker naming offsets WITHIN the record
    (`R00`, `R10`, ... NUL-terminated per 16 bytes) plus the record index, the
    same probe shape that mapped the account record and the mail list.

    POL_LOBBY_LIST_MODE="0:9=content" serves real content ids instead, taken from
    POL_LOBBY_CONTENT_IDS. That is the Tetra Master entitlement test.
    """
    rec, width, _cap = _LOBBY_LIST[(op1, op2)]
    count = _list_count(op1, op2, req_pt)
    if (op1, op2) == (0x02, 0x03) and _friends_mobile_request(req_pt):
        log("lobby", f"  2:3 MOBILE marker: cap {_friends_cap(req_pt)}, "
                     f"serving {count} row(s)")
    out = bytearray(n)
    if width == 1:
        out[0] = count & 0xFF
    else:
        struct.pack_into("<I", out, 0, count)

    # 7:12's count block is not just a count. The client validates byte 0 <= 4 and
    # bytes 1..4 <= 0x40 each, and those two limits are exactly the sizes of the
    # structures the reply fills: FOUR group slots (0x3bb06c0, stride 0x3098,
    # `cmp ecx,4`) and SIXTY-FOUR member slots per group (obj+0x30, stride 0xC0,
    # `mov ecx,0x40`). So **bytes 1..4 are the per-group MEMBER COUNTS**, and the
    # installer takes that count as its third argument, walking a **32-byte**
    # member record (`add edi,0x20` @0x37e898f) out of the array at session+0x328.
    #
    # THEY ARE NOW FILLED (2026-08-13). Serving them as zero meant every group
    # arrived with no members, and the client showed NO GROUPS AT ALL -- they
    # "disappeared" on every reboot even though 7:1 stored them and 7:12 served
    # them (measured: a 304-byte reply carrying two well-formed headers). Disband
    # then failed with -7202 raised locally, with no request on the wire, because
    # the client had no group to disband. `_group_members` supplies the owner as
    # the one member we can state truthfully; POL_GROUP_MEMBERS=0 restores the
    # old empty-membership behaviour for A/B.

    mode = _list_mode(op1, op2)
    # Once per reply -- see _friend_handle_slots on why this is not in the loop.
    fslots = friendlist._friend_handle_slots() if mode in ("friends", "groups") else {}
    # One query for the whole list, never one per record -- this DB is WAL on a
    # Windows bind mount and per-record connections are the churn that has
    # corrupted it before (see accounts-db-wal-hazard).
    ficons = _face_icons_by_handle() if mode in ("friends", "groups") else {}
    # Same rule, same reason -- and only for `friends`, since the row spool is the
    # only consumer and groups have no comment.
    rcomments = _comments_by_handle() if mode == "friends" else {}
    # POL_FRIEND_BITFIELD only: each row's stored low byte (ignore + level bits,
    # from the client's own last 2:6), one query for the whole list.
    flows = friendlist._friend_flags_low_by_row() \
        if mode in ("friends", "groups") and friendlist._friend_bitfield_mode() else {}
    #: (slot, guid, icon) per friend that HAS a picture -- filled by the record
    #: loop, pushed once the reply is built. See push_friend_icons.
    icon_rows = []
    #: (slot, guid, friend handle id) per friend served ONLINE -- the INITIAL
    #: PRESENCE BURST. The online icon is painted ONLY by the push (the 2:3 row
    #: cannot set it -- see _push_identity_guid, measured 2026-08-23), so a
    #: freshly fetched list stays grey until each friend TRANSITIONS. Retail
    #: cannot have worked that way; push current state for every online friend
    #: right after the serve. The guid here is the SAME variable the row was
    #: built from, so the push identity matches the row BY CONSTRUCTION.
    burst_rows = []
    #: (slot, name, db row id) per friend record ACTUALLY EMITTED -- filled by the
    #: record loop, published once the reply is built. This is the numbering a
    #: later 2:6 delete indexes; see `_friend_slots_publish`.
    served_slots = []
    served_online = {}          # slot -> the online flag the row was served with
    # Own handles + (optionally) friend handles, so friend profiles resolve -- see
    # _handle_list_entries. Falls back to own-only when the knob is off.
    # Each handle entry is (table_slot, name, profile-fields, guid). The table_slot
    # is the record's +0x00 (its slot in the 0x3bc5800 table), decoupled from the
    # record's position in the list so friends can sit at slots 4+.
    if mode in ("handles", "handlesz"):
        if os.environ.get("POL_LOBBY_FRIEND_HANDLES", "0") == "1":
            handles = [(slot, name, {friendgroups._PROFILE_FICON: fi}, guid)
                       for slot, guid, name, fi in _handle_list_entries()]
        else:
            handles = [(i, name, prof, accounts.handle_guid(hid))
                       for i, (hid, name, prof) in enumerate(_db_handles())]
    else:
        handles = []
    chars = _db_chars() if mode == "chars" else []
    # ONE query for the whole list, not one per character: this loop runs up to
    # 64 times and the names all come out of the same small table.
    char_names = characters._character_names() if mode == "chars" else {}
    # The +0x18 string doubles as the client's stored character name (CN) for
    # Tetra Master -- the VS. COM name banner -- so the bound handle's name
    # rides along; see _char_record. Indexed by handle SLOT, same numbering
    # `_db_chars` uses.
    char_handle_names = ([name for _hid, name, _prof in _db_handles()]
                         if mode == "chars" else [])
    if mode == "chars":
        log("lobby", "  1:3 characters: " + (", ".join(
            f"idx{i}=content {c} on handle {s} pos {p}"
            + ("" if b else " UNBOUND")
            for i, (s, p, c, _cid, b) in enumerate(chars[:count])) or "none"))
        # IDENTITY CAPTURE (2026-08-21, tm-member-sidebar). TM's member menu
        # resolves its target through polcore cft +0x2c4, which reads THIS
        # list's table entry and extracts a packed field (+0x40/+0x44,
        # `shrd ..,0x10` = >>16). Per the packing documented on _char_record
        # (UNMEASURED against the getter -- treat as hypothesis), log what that
        # extraction would yield for each record we serve, so the live
        # readself.py table dump can be compared field for field.
        for i, (s, p, c, _cid, b) in enumerate(chars[:count]):
            # Bit 15 is the BIND flag, so it follows `bind` here for the same
            # reason `_char_record` clears +0x04: this line has to describe the
            # record we actually serve, not a bound one.
            packed = ((1 << 15) if b else 0) | ((s & 0x3F) << 16) | ((s & 7) << 22)
            ext = packed >> 16
            log("lobby", f"  1:3 idx{i}: packed(+0x40)={packed:#x} -> "
                         f"cft+0x2c4 id lo={ext & 0xFFFFFFFF:#x} "
                         f"hi={(ext >> 32) & 0xFFFFFFFF:#x} (hypothesis)")
        # STAMP THE PRE-LAUNCH MOMENT for the FFXI bridge.
        #
        # 1:3 fills the 64-entry character table, which is the launch gate AND
        # the table FFXI checks before opening its world socket -- so a client
        # about to start a game has just asked for this. The bridge cannot see a
        # POL identity on FFXI's own wire (0x26 is version, 0x1F a bare poke, and
        # the first Content ID arrives at 0x07, after the character list is
        # built), so it has to be told, and this is the moment worth telling it
        # about.
        #
        # Deliberately the SESSION id -- the client's per-launch USER token --
        # and not the peer address. See the JOIN KEY note above _SESSIONS: keying
        # identity on an address was the worst bug in this file, because under
        # the dev compose every client is one address.
        sid = lobbysession._session_sid()
        if sid:
            lobbysession._session_put(sid, chars_at=time.time())
    friends = []
    if accounts is not None and mode in ("friends", "groups"):
        # 2:3 (`KGetFriendList`) carries PEOPLE; 7:12 (`KGetGroupList`) carries
        # GROUPS. Groups used to be tried on 1:3 on the strength of its 104-byte
        # record having a name at +0x18 -- that was a guess, it did nothing, and
        # the RTTI map since named 1:3 as `KGetChrList` (characters). The group
        # list had a dedicated opcode all along.
        # GROUPS COME FROM `_groups_for_list`, NOT `_db_friends`. It is the one
        # definition the reply LENGTH was already computed from, and it is the
        # only one that includes groups this handle is merely a member of. Using
        # a second query here is what would let the header count and the member
        # block disagree -- the failure that walks the client off the end of one
        # group's members into the next.
        friends = (friendlist._db_friends(kinds=(accounts.KIND_FRIEND,)) if mode == "friends"
                   else friendgroups._groups_for_list(count))
    # THE PER-CONTENT BLOCK, for the FRIEND rows. The 168-byte 2:3 record is the
    # same shape as the profile identity row, and SE's layout carries the 8x16
    # content block at +0x28 of both (`_IDREC_CONTENT_AT` -- the 05:04 diff that
    # found it is at profile offset 0x1D0 = idrec+0x28). We served it as zero
    # here, which is why Tetra Master's OWN friend list greys "View Profile"
    # with "no Tetra Master profile available" for everyone: TM builds its
    # friend cache from these rows, and a row that
    # advertises no TM content gives it no z_ctid to request a profile with --
    # the same downstream-of-an-empty-list failure the Viewer's content section
    # had, one surface over. Same builder (`_identity_content_entries`), same
    # agreement rule with 1:3, one connection for the whole list (the
    # accounts-db WAL hazard rule). SE's only 2:3 samples carry ZERO here (both
    # friends offline; SE seems to fill it transiently for a content in
    # session) -- we fill it unconditionally for the same reason the profile
    # block does: a section that works is what is wanted.
    # POL_FRIEND_CONTENTS=0 reverts to the all-zero tail.
    cblocks = {}
    if (mode == "friends" and friends and accounts is not None
            and os.environ.get("POL_FRIEND_CONTENTS", "1") == "1"):
        try:
            _cdb = accounts.connect()
            try:
                for _row in friends:
                    _fh = _row[3]
                    if _fh:
                        cblocks[int(_fh)] = contentprofiles._identity_content_entries(
                            _cdb, int(_fh))
            finally:
                _cdb.close()
        except Exception as exc:
            log("lobby", f"  2:3 content blocks skipped ({exc!r})")

    for i in range(count):
        base = 8 + i * rec
        if base + rec > n:
            break
        if mode == "handles":
            hslot, hname, prof, hguid = handles[i]
            out[base:base + rec] = _handle_record(
                hslot, hname, rec, guid=hguid,
                face_icon=_int_field(prof, friendgroups._PROFILE_FICON),
                comment=prof.get(profilerecord._COMMENT_FIELD))
            continue
        if mode == "chars":
            slot, pos, code, cid, bind = chars[i]
            hname = (char_handle_names[slot]
                     if slot < len(char_handle_names) else "")
            out[base:base + rec] = characters._char_record(rec, i, slot, pos, code, cid,
                                                handle_name=hname,
                                                names=char_names, bind=bind)
            if cid:
                # Serve-time observability for a title with a world identity:
                # WHAT world identity this 1:3 actually carried, and WHEN. A
                # re-fetch that logs a real field and still fails to connect
                # rules the server side out entirely.
                try:
                    _wf = titles.character_world(code, int(str(cid).strip() or 0))
                except ValueError:
                    _wf = None
                if _wf is not None:
                    log("lobby", f"1:3 slot {i}: code {code} Content ID {cid} world_field "
                                 f"0x{_wf:08X}"
                                 + ("" if bind else "  [unbound: past this handle's"
                                                   " 8 binding slots, playable but"
                                                   " not shown in the profile view]"))
            continue
        if mode == "groups" and (op1, op2) == (0x07, 0x0C):
            # 7:12's 136-byte record has its OWN layout, read off polcore
            # 0x37e86c0 -- see _group_record. It is not 0:9's, despite the shared
            # size, and the name is UTF-16LE at +0x08.
            out[base:base + rec] = friendgroups._group_record(
                rec, i, friends[i][1], guid=friends[i][0])
            if i == count - 1 and os.environ.get("POL_GROUP_MEMBERS", "1") == "1":
                # After the LAST header, append every group's members in group
                # order and record each group's count in the count block. Both
                # halves have to agree or the client walks off the end of one
                # group's members into the next group's.
                members = friendgroups._group_members(friends[:count])
                # Built ONCE per reply, not per record -- it opens the database.
                selfguids = friendgroups._client_guid_map()
                # CLIENT_GUID GOES ON THE REQUESTER'S OWN ROW ONLY (2026-08-22).
                # +0x00's one measured consumer is the SELF-match
                # (`cft_0355(member+0x00) == own_id`, see _group_member_record),
                # and only the VIEWER's row can ever be self -- yet this passed
                # every member's client_guid, so a FRIEND's group row carried an
                # id the friend array (keyed by the 2:3 handle_guid) could never
                # match, and the client offered "Add as friend" on an existing
                # friend (live 2026-08-22, Fox on Ironbadger's group row; SE
                # never offers it). SE's own capture has ONE id in both records
                # -- Cyn's 0x91bc041e2c008c00 at 7:12 +0x00 and 2:3 +0x10 -- so
                # peers get THE SAME identity their friend row serves. Chat
                # attribution is safe: in-room speakers are named by IRC nick,
                # not this field (group-chat-retail-decode).
                #
                # TIED TO THE FRIEND-LIST IDENTITY, NOT HARDCODED (2026-08-22).
                # The friend-recognition here compares a peer's +0x00 against the
                # client's friend array, which is keyed by whatever 2:3 served for
                # that peer. When 2:3 served handle_guid this field had to be
                # handle_guid; the TM room-menu fix flipped 2:3 to client_guid
                # (POL_FRIEND_GUID_CLIENT, tm-member-sidebar-identity), so this
                # field must now follow. Defaulting peer_cg to that same knob
                # means the group row and the friend row can never disagree again
                # -- and _group_member_record already falls back to handle_guid
                # for a peer whose client_guid we have never learned, exactly as
                # _db_friends does, so the two stay in lockstep per-peer.
                # POL_GROUP_PEER_CLIENTGUID overrides the default explicitly.
                own_name = lobbysession._session_handle_name()
                peer_cg = os.environ.get(
                    "POL_GROUP_PEER_CLIENTGUID",
                    os.environ.get("POL_FRIEND_GUID_CLIENT", "1")) == "1"
                off = 8 + count * rec
                truncated = []
                for g, mems in enumerate(members):
                    # The reply was SIZED from this same list, so a record that
                    # will not fit means the two disagreed -- serve the count we
                    # can actually back with records rather than the count we
                    # wanted, because a count block promising records that are
                    # not there is the walk-off-the-end failure.
                    # 0x40 IS CLAMPED ONCE, HERE, SO BOTH HALVES USE IT. It used
                    # to be applied to the count block alone (`min(fits, 0x40)`)
                    # while the loop below still wrote `fits` records -- so a
                    # 65-member roster declared 64 and emitted 65, and the client
                    # walked 32 bytes into the next group's members. `_group_members`
                    # now caps before this, which makes the two agree at the
                    # source; this stays as the invariant that cannot drift.
                    fits = max(0, min(len(mems), (n - off) // _GROUP_MEMBER_REC,
                                      0x40))            # client validates <= 0x40
                    if fits < len(mems):
                        truncated.append((friends[g][1], len(mems), fits))
                    out[1 + g] = fits
                    for guid, nm, cls in mems[:fits]:
                        out[off:off + _GROUP_MEMBER_REC] = \
                            friendgroups._group_member_record(
                                guid, nm, cls=cls,
                                self_guid=selfguids.get(nm)
                                if (peer_cg or nm == own_name) else None)
                        off += _GROUP_MEMBER_REC
                log("lobby", "  7:12 groups: " + ", ".join(
                    f"{friends[g][1]!r}={out[1 + g]} member(s)"
                    for g in range(count)))
                if truncated:
                    log("lobby", "  7:12 groups: REPLY TOO SHORT for the "
                                 "membership -- " + ", ".join(
                                     f"{nm!r} {had}->{got}"
                                     for nm, had, got in truncated)
                                 + f" (payload {n}B); the length and the record "
                                   "loop disagreed, which should be impossible")
                # THE PICTURES DO NOT TRAVEL IN THE REPLY. The 32-byte member
                # record is full (8 id + 8 packed + 15 name + NUL, measured
                # against SE's CRAZY PEOPLE replies), so each member's face
                # icon and live role arrive as ev=(group<<8) pushes on the auth
                # band -- see push_group_rosters. Guarded like every push
                # issued from a reply handler: an exception here must not
                # abort the 7:12 (the POL-0008 lesson).
                if friendgroups._group_cfg("rowpush",
                              os.environ.get("POL_GROUP_ROWPUSH", "1")) == "1":
                    try:
                        mid = lobbysession._session_member_id()
                        if mid:
                            pushspool.push_group_rosters(None, int(mid), [
                                [int(friends[g][0]),
                                 [[int(m[0]), str(m[1]), int(m[2])]
                                  for m in members[g]]]
                                for g in range(count) if friends[g][0]])
                    except Exception as exc:
                        log("lobby", f"  7:12 group push skipped ({exc!r})")
            continue
        if mode in ("friends", "groups"):
            g, nm, kd, fh, st, rid, lbl = friends[i]
            # BOTH unsettled flavours render as SE's captured pending word: an
            # outgoing request ('pending') and an incoming one ('invited') are the
            # same state on the wire. 0900005A is the only pending value we have
            # ever observed, so a second one would be invention.
            icon = ficons.get(int(fh)) if fh else 0
            # WHAT THE ROW IS CALLED ON *THIS* ACCOUNT'S LIST. A rename writes a
            # caption into the same field the name occupies (see
            # `_friend_put_text`), so the caption is what belongs in the record
            # the client renders. `nm` stays the real name everywhere else --
            # the slot bookkeeping below, and every lookup keyed on it.
            #
            # WARNING: SERVING IT BACK IS INFERRED, NOT CAPTURED. What is measured is
            # that the client SENDS the caption in this field and that SE echoes
            # it in the 2:6 reply; SE's own later 2:3 for a renamed friend was
            # not captured. It is the only reading under which a rename survives
            # a relog, so it is the default -- POL_FRIEND_RENAME_SERVE=0 serves
            # the real name and keeps the label stored, if a client disagrees.
            shown = nm
            if lbl and os.environ.get("POL_FRIEND_RENAME_SERVE", "1") == "1":
                shown = lbl
            # 🔬 probe hook -- inert unless `guidmap=` is set in presence.ctl.
            # Applied to the record AND to the icon push below, or the push's
            # identity check (which matches on the guid) rejects the row.
            g = friendlist._friend_guid_probe(nm, g)
            # Hoisted so the serve log can SAY what was written. The 2026-08-23
            # presence incident ("friends show offline on both surfaces") was
            # diagnosable only by re-running this computation after the fact:
            # prod runs POL_LOG_HEX=0, so nothing recorded whether a row went
            # out online or offline. Now every 2:3 serve names it per slot.
            onl = friendlist._peer_online(fh)
            out[base:base + rec] = friendlist._friend_list_record(
                rec, g, shown, kd, fh,
                status=1 if st in accounts.STATUS_UNSETTLED else 0,
                online=onl, index=i,
                handle_slot=fslots.get(int(g)),
                face_icon=icon, row_id=rid, flags_low=flows.get(int(rid)))
            # The content block -- see the banner where `cblocks` is built. The
            # 128 bytes end exactly at the record's 0xA8, so the size check is
            # an identity today and a guard if the record ever shrinks.
            cblk = cblocks.get(int(fh)) if fh else None
            if cblk and rec >= profilerecord._IDREC_CONTENT_AT + len(cblk):
                out[base + profilerecord._IDREC_CONTENT_AT:
                    base + profilerecord._IDREC_CONTENT_AT + len(cblk)] = cblk
            if mode == "friends":
                served_online[int(i)] = bool(onl)
                # ALL friends ride the burst, offline included -- measured off
                # SE's own burst (2026-08-23, auth429364.pkl): every friend gets
                # a presence assertion, an offline one being `state 01, zone 0`.
                # Current state is resolved at DELIVERY time, so the offline
                # assertion also covers a friend who logs out inside the spool
                # delay.
                if fh:
                    burst_rows.append((i, int(g), int(fh)))
            # The picture does NOT travel in this record -- SE does not put one
            # here either. Remember which slot wants which face and push them
            # once the list is composed. `i` is the slot the record lands in
            # (its +0x08), and `g` is the guid the client will compare against.
            #
            # THE COMMENT RIDES ALONG. It has no pull carrier at all, and SE puts
            # it in this same record (flags 0x25 = BLOCK|ICON|COMMENT). So the row
            # is worth pushing when EITHER half is present -- gating on `icon`
            # alone silently drops the comment of any friend who has not picked a
            # picture.
            rcmt = rcomments.get(int(fh)) if fh else None
            if mode == "friends" and (icon or rcmt):
                icon_rows.append((i, int(g), int(icon or 0), rcmt))
            if mode == "friends":
                # `i` is the record's +0x08, i.e. the slot the client files this
                # row under and the number a delete of it will send back.
                served_slots.append((i, nm, int(rid)))
            continue
        if mode == "zeros":
            # A record that is PRESENT but entirely zero. The point is to separate
            # two things the marker probe cannot: "the client consumes this list"
            # from "our marker bytes are illegal values". Serving 1:3/2:3 markers
            # turned a silent hang into POL-7192 (未実装の認証を行った -- an
            # unimplemented authentication type), which is the expected reaction to
            # garbage in a type field and says nothing about the real layout.
            # Zeros keep the count and the record framing while making every field
            # the lowest legal-looking value.
            continue                       # `out` is already zero-filled
        for row in range(0, rec, 16):
            row_end = min(row + 16, rec)
            tag = f"R{row:02X}#{i}".encode("ascii")[:row_end - row - 1]
            out[base + row:base + row + len(tag)] = tag
            out[base + row_end - 1] = 0
        if mode == "handlesz":
            # Offset markers EVERYWHERE except the fields we already know, so one
            # login keeps the handle working while naming whichever unmapped field
            # feeds a UI element. This is how 3:0's `acct` probe found its strings:
            # a marker that renders tells you its own offset. Each 16-byte row is
            # "R<off>#<record>" + NUL, so a string field started anywhere in the
            # record comes out readable instead of running to the end.
            hslot, hname, prof, hguid = handles[i]
            out[base:base + rec] = _handle_record(
                hslot, hname, rec, base_bytes=bytes(out[base:base + rec]),
                guid=hguid, face_icon=_int_field(prof, friendgroups._PROFILE_FICON),
                comment=prof.get(profilerecord._COMMENT_FIELD))
    if mode in ("handles", "handlesz"):
        shown = [(s, n, g, _int_field(p, friendgroups._PROFILE_FICON))
                 for s, n, p, g in handles[:count]]
        log("lobby", f"  list {op1:x}:{op2:x} mode={mode} count={count} rec={rec} "
                     f"handles(id,name,guid,ficon)={shown}")
    if mode == "friends":
        # REMEMBER THE NUMBERING WE JUST HANDED OUT. Every 2:6 delete for the rest
        # of this session indexes THIS reply and nothing else -- the client does
        # not re-read the list and does not renumber around a row it has hidden.
        friendlist._friend_slots_publish(served_slots)
        log("lobby", "  2:3 friends served: " + (", ".join(
            f"slot {s}={nm!r} "
            f"{'ONLINE' if served_online.get(int(s)) else 'offline'}"
            for s, nm, _rid in served_slots) or "none")
            + " -- this is the numbering a later 2:6 delete names, and the "
              "online flag is what the row's presence byte carried")
    if icon_rows:
        mid = lobbysession._session_member_id()
        if mid:
            # Say what actually happened. The first cut logged "-> push"
            # unconditionally, which read as a delivery even with the push
            # switched off -- exactly the wrong thing while bisecting one.
            on = pushspool._row_push_enabled()
            log("lobby", "  2:3 row spool: "
                         f"{[(s, i, c) for s, _g, i, c in icon_rows]}"
                         f" (member {mid}) -> "
                         + ("push" if on else "NOT pushed (rowpush off)"))
            if on:
                # NEVER let the spool take the reply down with it. This runs
                # inside the 2:3 handler, so an exception here aborts the friend
                # list mid-flight and the client reports POL-0008 -- a dropped
                # connection, which reads like a network fault and sends you
                # looking at DNS. It cost one login to learn that (2026-08-16, a
                # 3-tuple unpack left over from adding the comment). A picture
                # that fails to arrive is a cosmetic loss; a friend list that
                # fails to arrive is the login.
                try:
                    pushspool.push_friend_icons(None, mid, icon_rows)
                except Exception as exc:
                    log("lobby", f"  2:3 row spool: NOT queued ({exc!r}) -- "
                                 "reply unaffected")
    if burst_rows:
        mid = lobbysession._session_member_id()
        if mid:
            # THE INITIAL PRESENCE BURST. Same POL-0008 rule as the icon spool
            # above: this runs inside the 2:3 handler, so nothing here may take
            # the reply down -- a burst that fails to queue costs an icon that
            # stays grey until the friend transitions, not the login.
            try:
                n = pushspool.push_presence_burst(None, mid, burst_rows)
                log("lobby", "  2:3 presence burst: "
                             f"{[(s, f) for s, _g, f in burst_rows]} "
                             f"(member {mid}) -> "
                             + ("queued" if n else
                                "NOT queued (burst off or spool unavailable)"))
            except Exception as exc:
                log("lobby", f"  2:3 presence burst: NOT queued ({exc!r}) -- "
                             "reply unaffected")
    return bytes(out)


#: The 0:8 reply is 64 slot entries of 8 bytes (512), then 4 spare and the 4-byte
#: checksum -- exactly the 0x208 the client reads. Its consumer (polcore state 7
#: @0x37dd161) walks them against the SAME 64-entry table at 0x3bc5800 that 0:9
#: fills, and applies an entry only when `(dword1 >> 12) & 3 == 1`:
#:
#:     ecx = reply[i*8 + 4]; ecx = (ecx >> 12) & 3
#:     if cl != 1: skip
#:     eax:edx = __allshl(reply[i*8], 0, cl)      ; i.e. value << 1
#:     slot[0] = eax | (slot[0] & 1)              ; bit 0 kept as a flag
#:     slot[4] = edx | (slot[4] & 0xffffe000)
#:
#: 520 zeros therefore match NO slot: the client applies nothing back from a
#: registration, every time. That is a candidate root cause for "nothing
#: persists", and it costs one env var to test.
_HANDLEREG_SLOTS = 64
_HANDLEREG_APPLY = 1 << 12          # (dword1 >> 12) & 3 == 1


def _handlereg_payload(n):
    """0:8's reply with the per-slot APPLY marker set, so the client actually
    commits the 64-slot table instead of skipping every entry.

        POL_LOBBY_HANDLEREG="apply"      value 0 in every slot, marker set
        POL_LOBBY_HANDLEREG="apply:5"    value 5 in every slot
        POL_LOBBY_HANDLEREG=""           (default) the historical 520 zeros

    The VALUE's meaning is still unknown -- it is shifted left by 1 into a packed
    field whose bit 0 is reserved as a flag, the same shape 0:9 writes from its
    record+0x08/+0x0C. Setting the marker with value 0 is the minimal change that
    makes the client take the reply at all, which is the thing worth testing first.
    """
    spec = os.environ.get("POL_LOBBY_HANDLEREG", "").strip().lower()
    if not spec.startswith("apply"):
        return b""
    _, _, val = spec.partition(":")
    try:
        value = int(val, 0) if val else 0
    except ValueError:
        value = 0
    out = bytearray(n)
    for i in range(_HANDLEREG_SLOTS):
        base = i * 8
        if base + 8 > n:
            break
        struct.pack_into("<II", out, base, value & 0xFFFFFFFF, _HANDLEREG_APPLY)
    log("lobby", f"  0:8 handle-reg reply: APPLY marker on {_HANDLEREG_SLOTS} "
                 f"slots, value={value}")
    return bytes(out)


def _handle_reg_payload(n):
    """The 0:8 handle-registration reply.

    Captured from a real SE session 2026-08-11: SE's reply is 520 bytes with just
    **6 non-zero**:

        +0x00  81 00 00 00        a status word
        +0x14  6F A5 7B 6A        a unix timestamp (that day)

    This overturned the theory that had stood for weeks -- that
    our all-zero reply "matches no slot, so nothing is ever committed back". SE
    sends essentially zeros too. The difference is those two fields, and the second
    is what makes the client APPLY a slot: its documented rule is

        ecx = reply[i*8 + 4]; if ((ecx >> 12) & 3) != 1: skip

    and 0x6A7BA56F >> 12 = 0x6A7BA5, & 3 == 1. So the dword at +0x14 -- entry 2's
    second word -- is the one entry SE marks as applicable, and a timestamp
    naturally satisfies the test (any value whose bits 12-13 are 01).

    POL_HANDLE_ACK=0 restores the all-zero reply for A/B.
    """
    out = bytearray(n)
    if n >= 0x18:
        struct.pack_into("<I", out, 0x00,
                         int(os.environ.get("POL_HANDLE_ACK_STATUS", "0x81"), 0))
        ts = int(time.time()) & 0xFFFFFFFF
        # Keep the slot test satisfied even if a future timestamp's bits differ:
        # the client needs (v >> 12) & 3 == 1, so force those two bits.
        ts = (ts & ~(3 << 12)) | (1 << 12)
        struct.pack_into("<I", out, 0x14, ts)
        log("lobby", f"  handle-reg 0:8 ack: status=0x{out[0]:02x} ts={ts:#x} "
                     f"(slot test (ts>>12)&3 = {(ts >> 12) & 3})")
    return bytes(out)


def _member_display_name(member_id):
    """The handle name a person is KNOWN by, or "".

    The room registry knows a scrambled login nick (`UELRIS73E`) and, when the
    session bound one, a member row. Neither is what the player calls themselves
    -- the handle is, and it is what the badge, the friend list and the sign-up
    wizard all show.
    """
    try:
        mid = int(member_id)
    except (TypeError, ValueError):
        return ""
    if mid <= 0:
        return ""
    hit = lobbyrooms._ROOM_NAME_CACHE.get(mid)
    if hit is not None and time.monotonic() - hit[1] < lobbyrooms._ROOM_NAME_TTL:
        return hit[0]
    name = ""
    if accounts is not None:
        try:
            db = accounts.connect()
            try:
                row = accounts.primary_handle_row(db, mid)
                name = str(row["handle_name"]) if row else ""
            finally:
                db.close()
        except Exception as exc:
            log("authserv", f"  room roster: cannot name member {mid} ({exc!r})")
    lobbyrooms._ROOM_NAME_CACHE[mid] = (name, time.monotonic())
    return name


def payload_handle_ack(n, req_pt):
    """0:8 handle registration (lobby opcode table): the acknowledgement
    record. POL_HANDLE_ACK=0 falls through to the generic path, where
    `_handlereg_payload` answers instead."""
    if os.environ.get("POL_HANDLE_ACK", "1") != "1":
        return None
    return _handle_reg_payload(n)
