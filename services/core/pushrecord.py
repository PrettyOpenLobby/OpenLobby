"""The field-list push record that paints a friend's picture, and presence broadcasts."""
import hashlib
import os
import struct
import time
from srvcore import log
from authtoken import TOKEN_ALPHABET
from .deps import accounts
from . import friendlist, friendroster, handlelists, logingate, memberstatus, presence, pushchannel, pushspool, roomregistry, titlezone



# --------------------------------------------------------------------------- #
# THE LONG (FIELD-LIST) PUSH RECORD -- what actually paints a friend's PICTURE
# --------------------------------------------------------------------------- #
# DECODED 2026-08-16 out of polcore's own dispatcher and validated against every
# long record in the 2026-08-15 SE capture. This is the record long marked "do not
# guess"; it no longer has to be guessed.
#
# A push LINE is not one base64 blob. It is TWO, concatenated:
#
#     <96 chars: the 72-byte main record><N chars: an OPTIONAL-FIELD chunk>
#
# `cft_0336_b64` reads exactly 0x60 chars for the main record, and the third
# branch of `polcore_irc_dispatch` (0x037db6f0) decodes the REST from `line+0x60`
# with `+0x38` as its length. That is why a "102/105-byte record" looked like the
# icon had moved to +0x60: the icon had not moved, a 16-byte field had appeared
# in front of it. Decoding the line as one stream hides the structure completely.
#
# The chunk is: 8 header bytes (byte 0 = a FIELD BITMASK) then the present fields
# back to back, each with a fixed width:
#
#     0x01  -- the main record's own +0x10 block (the presence/status action)
#     0x02  0x10 B  an object id + the subject's member guid (see below)
#     0x04  0x08 B  ** THE FACE ICON **, u32 + 4 pad
#     0x08  0x10 B
#     0x10  0x10 B  the display NAME, 15 chars + NUL
#     0x20  0x68 B  the COMMENT, 50 UTF-16LE chars + terminator
#     0x40  rest    a trailing field
#
# and the consumer is `FUN_037e50f0(block, slot, seq, ts, ICON, f08, NAME,
# COMMENT, f40)`, which writes each present field into the friend table and
# returns "something changed" so the UI repaints:
#
#     slot          = &0x038740d8 + slot*0xB0      the 2:3 friend slot
#     ICON          -> 0x03bbc920 + slot*0x84      <- the friend row's PICTURE
#     NAME          -> 0x03874178 + slot*0xB0
#     COMMENT       -> 0x03bbc93c + slot*0x84
#     seq/ts        -> slot+0x10 / slot+0x14
#
# WARNING: EVERY FIELD IS OPTIONAL AND ABSENT MEANS "LEAVE ALONE" -- each is applied
# under `if (param_N != 0)`. That is why we send the ICON alone and do NOT set
# bit 0x01: the +0x10 block is the presence action, and including it would
# rewrite the online/away bits we already serve at 2:3 +0x09.
#
# Routing (all four are load-bearing; miss one and the record is silently
# dropped, which is what "the client accepts it and does nothing" looks like):
#
#     +0x42 bit 0   SET      or the record goes down the short-push branch
#     +0x3e &0x0f80 == 0xf80 the gate/field-list class (SE sends 0xcf80)
#     +0x1b == 0, +0x1a == 0 or it is read as a login gate / a text event
#     +0x19 bit 0   SET      the field-list branch's own gate
#     +0x38 < 0x158          the chunk-length cap
#
# and, once the friend table is ready (`DAT_0387ca80`, i.e. after 2:3 lands), an
# IDENTITY check: +0x1c must be the friend's 2:3 slot, +0x18 must equal that
# slot's handle-table bits `(slot+0x9c >> 13) & 0x3f`, the guid must match, and
# (ts, seq) must be strictly NEWER than what the slot already holds.
#
# The guid is masked exactly as the short form masks it, and the runtime key K
# cancels for the same reason: `cft_0336_b64` unmasks with MASK **and** K, while
# the 2:3 record-copy stored the slot guid as `served ^ K`. Verified numerically
# against the capture -- SE's record for `Yui` unmasks to 008c002c1e04bc91, which
# is byte for byte the guid their 2:3 row carried at +0x10.

_PUSH_F_BLOCK, _PUSH_F_OBJECT, _PUSH_F_ICON = 0x01, 0x02, 0x04
_PUSH_F_F08, _PUSH_F_NAME, _PUSH_F_COMMENT, _PUSH_F_TAIL = 0x08, 0x10, 0x20, 0x40

#: Field widths, in the order the parser advances its cursor. 0x01 has no bytes
#: of its own (it selects the main record's +0x10 block) and 0x40 takes the rest.
_PUSH_FIELD_WIDTH = ((_PUSH_F_OBJECT, 0x10), (_PUSH_F_ICON, 0x08),
                     (_PUSH_F_F08, 0x10), (_PUSH_F_NAME, 0x10),
                     (_PUSH_F_COMMENT, 0x68))

#: The class word at +0x3e. `& 0x0f80 == 0x0f80` is what selects this branch;
#: the 0xc000 bits are SE's and are reproduced rather than tidied away.
_PUSH_FIELD_CLASS = 0xCF80

#: The chunk-length cap the parser enforces at 0x37DB99E.
_PUSH_CHUNK_MAX = 0x158


def _b64encode_exact(data):
    """POL-base64 WITHOUT padding a partial final group.

    `_b64encode` rounds every group up to 4 characters, which is right for the
    72-byte main record (24 whole groups) and wrong for the chunk: SE encodes a
    16-byte chunk as **22** characters, not 24, and `+0x38` counts characters.
    A padded chunk therefore disagrees with its own declared length.
    """
    out = []
    for i in range(0, len(data), 3):
        c = data[i:i + 3]
        n = len(c)
        v = int.from_bytes(c + bytes(3 - n), "big")
        out += [logingate._B64[(v >> 18) & 63], logingate._B64[(v >> 12) & 63],
                logingate._B64[(v >> 6) & 63], logingate._B64[v & 63]][:n + 1]
    return "".join(out)


#: `+0x11` in the +0x10 block -- the PRESENCE STATE, on SE's own presence pushes.
#:
#: Measured 2026-08-16. SE's presence records are BLOCK-ONLY long records (field
#: flags `0x48` == 0x01, no icon/name/comment chunk), 81 bytes, and they carry
#:
#:     +0x10 = 0x01      "apply the state below"
#:     +0x11 = 1 | 2 | 3  the state
#:
#: against the ICON pushes, which are the same block with `+0x10 = 0x00` and
#: `+0x11 = 0x01` -- i.e. 0 at +0x10 reads as "block present, leave status
#: alone", which is why our icon pushes have never disturbed presence.
#:
#: THE MAPPING, SETTLED BY A SECOND FIELD. A first pass guessed 1 = online from
#: raw frequency and that was BACKWARDS. `+0x14` decides it -- across all thirteen
#: of SE's presence pushes it tracks `+0x11` exactly:
#:
#:     +0x11 = 1  ->  +0x14 = 0x0000              5 records
#:     +0x11 = 2  ->  +0x14 = 0x03E8              2 records
#:     +0x11 = 3  ->  +0x14 = 0x03E8 / 0x03E9     6 records
#:
#: 0x03E8/0x03E9 are 1000/1001 -- ZONE ids, the same space the room browser's
#: `zone 1100` lives in. A friend who is somewhere has a zone; a friend who is
#: nowhere is offline. So state 1 (the only one with no zone) is OFFLINE, and 3 --
#: the most common, with a zone -- is ONLINE. 2 keeps a zone, which is exactly
#: right for AWAY: still connected, still somewhere, just flagged.
#:
#: This also says where "which game is my friend playing" most likely lives. The
#: icon the account holder describes needs a per-friend value that changes when
#: they launch a title, and `+0x14` is a location field already varying (1000 vs
#: 1001) while both accounts sat in the Viewer. Nobody launched a game during
#: either capture, so the value a TITLE writes here is not in this material -- but
#: this is the field to watch, not a new one to find.
#:
#: POL_PRESENCE_STATE_MAP="online=1,away=2,offline=3" re-orders it live if the
#: reading is still wrong; `_PRESENCE_STATES` names every state the callers use.
_PRESENCE_FIELD_STATE = {"online": 3, "back": 3, "away": 2, "offline": 1}


def _presence_zone(member_id, state):
    """`+0x14` for one subject: nowhere, a TITLE, the Viewer, or a chat room."""
    if state == _PRESENCE_FIELD_STATE["offline"]:
        return 0
    # A LAUNCHED TITLE WINS. The client reports it on 4:5 (see `_title_zone`),
    # and it is the more specific answer: someone inside Tetra Master is not in
    # the Viewer, and a title has no `#01C` room to find below either.
    in_title = titlezone._title_zone(member_id)
    if in_title is not None:
        return in_title
    try:
        for ts in presence.PRESENCE.sessions_for(int(member_id)):
            for chan in roomregistry.ROOMS.rooms_of(ts):
                c = chan.decode("latin1", "replace") if isinstance(chan, bytes) \
                    else str(chan)
                if c.startswith("#01C"):        # a player room, not #XXL group chat
                    return memberstatus._PRESENCE_ZONE_CHATROOM
    except Exception as exc:
        log("authserv", f"presence: zone lookup failed ({exc!r})")
    return memberstatus._PRESENCE_ZONE_VIEWER


def _presence_field_state(state):
    """`+0x11` for a state name, honouring POL_PRESENCE_STATE_MAP."""
    table = dict(_PRESENCE_FIELD_STATE)
    raw = os.environ.get("POL_PRESENCE_STATE_MAP", "")
    for pair in raw.split(","):
        if "=" in pair:
            k, _, v = pair.partition("=")
            try:
                table[k.strip()] = int(v.strip(), 0)
            except ValueError:
                pass
    return table.get(str(state), None)


def build_field_push_record(subject_guid, slot, icon=None, name=None,
                            comment=None, seq=0, when=None, hslot=0,
                            block=False, state=None, zone=None, group=None,
                            gpacked=None):
    """A long push record: `<main 96 chars><field chunk>`, ready for a NOTICE.

    `slot` is the friend's 2:3 record index (the same number that record carries
    at +0x08); `hslot` is their HANDLE-table slot, which is a different number
    and is what +0x18 is checked against. Both default to what our 2:3 currently
    serves, i.e. 0, and both must agree with it or the client drops the record.

    Only the fields you pass travel, because absent means "leave alone".

    WARNING: `block=True` sets field bit 0x01, which hands the main record's +0x10 block
    to `FUN_037dee40` -- and that writes the friend slot's STATUS word at +0x08,
    the same online/away bits our 2:3 `+0x09` byte sets at login. SE sends it;
    we do not, because the action byte's meaning inside THIS block is not pinned
    (the mapping we have was read off a different caller) and a wrong value would
    silently flip a friend to offline as the price of drawing their picture. The
    icon, name and comment are applied independently of it, so leaving it out
    costs nothing. It exists so `push_test.py` can reproduce SE's exact bytes.
    """
    r = bytearray(72)
    struct.pack_into("<Q", r, 0x00, (int(subject_guid) ^ pushchannel._PUSH_GUID_MASK)
                     & 0xFFFFFFFFFFFFFFFF)
    # +0x08 is RAW ZERO in every captured long record (it unmasks to the mask
    # itself). The short form puts a second guid here; this one does not.
    r[0x11] = 1                                   # SE's, inside the +0x10 block
    if state is not None:
        # A PRESENCE push. `+0x10 = 1` is what makes the block's status word take
        # effect -- SE's icon pushes leave it 0 and their presence pushes set it,
        # which is the only difference between the two record shapes besides the
        # absent field chunk. Implies block, since the block IS the payload.
        r[0x10] = 1
        r[0x11] = int(state) & 0xFF
        # The zone travels with the state, and only for a subject who HAS one --
        # SE sends 0 here on every offline push and a zone on every other. See
        # _PRESENCE_FIELD_STATE: these two bytes are read together. The caller
        # resolves WHICH zone (`_presence_zone`); falling back to the Viewer keeps
        # a caller that does not care from accidentally saying "offline".
        if zone is None:
            zone = (0 if int(state) == _PRESENCE_FIELD_STATE["offline"]
                    else memberstatus._PRESENCE_ZONE_VIEWER)
        struct.pack_into("<H", r, 0x14, int(zone) & 0xFFFF)
        block = True
    r[0x18] = int(hslot) & 0x3F                   # vs (slot+0x9c >> 13) & 0x3f
    r[0x19] = 1                                   # the field-list branch's gate
    r[0x1c] = int(slot) & 0xFF                    # the friend's 2:3 slot, < 0xC8
    if gpacked is not None and not block:
        # THE GROUP-MEMBER SHAPE. Measured off SE's ev=(group<<8|sub) pushes
        # (auth429364.pkl lines 77679/81407, decoded 2026-08-18): +0x10..0x13 is
        # ALL ZERO -- unlike every friend-shape record, which carries at least
        # the r[0x11]=1 byte. The rest of the main record matches the friend
        # shape byte for byte (gate +0x19, class 0xcf80, +0x42=1), so this is
        # the only main-record delta the group family has.
        r[0x11] = 0

    chunk = bytearray(8)
    flags = _PUSH_F_BLOCK if block else 0
    if group is not None:
        # THE GROUP-SCOPED COMPANION. SE sends TWO records for every presence
        # change, in this order (measured, polshim-se.429364.log):
        #
        #   105094  flags 0x03  BLOCK|OBJECT  +0x50 = 184173 (0x2CF6D, the group)
        #   105121  flags 0x01  BLOCK         the friend-list record
        #
        # -- 19 pairs across the log, never one without the other. The OBJECT
        # field is 0x10 bytes and its first dword is the group id; the rest is
        # zero in every sample. We were sending only the second of the pair.
        #
        # The state and zone ride the SAME block bytes as the friend record, so
        # this is the identical event addressed to a group instead of a row.
        flags |= _PUSH_F_OBJECT
        if gpacked is not None:
            # THE GROUP-MEMBER OBJECT FIELD IS NOT ZERO-PADDED. In the presence
            # companion the 16B field is `u32 group + 12 zero`; in the group
            # ROSTER pushes (ev = group_id<<8 | sub) the same field is two u64s:
            # the group id, then the member's PACKED word -- guid low32, class
            # @bit50, slot @bit53, the identical packing the 7:12 member record
            # carries at +0x08. Measured: chunk mask 0x16 = OBJECT|ICON|NAME,
            # 48 bytes total, e.g. `6dcf02..|9d50270000000c00|f502..|597569..`
            # = QUIET CORNER / Yui / class 3 / icon 757 / "Yui". The class bits
            # step 2->3->4 across SE's narrated role changes, which is what
            # pinned the field.
            chunk += struct.pack("<QQ", int(group) & 0xFFFFFFFFFFFFFFFF,
                                 int(gpacked) & 0xFFFFFFFFFFFFFFFF)
        else:
            chunk += struct.pack("<I", int(group) & 0xFFFFFFFF) + bytes(0x0C)
    if icon is not None:
        flags |= _PUSH_F_ICON
        chunk += struct.pack("<II", int(icon) & 0xFFFFFFFF, 0)
    if name is not None:
        flags |= _PUSH_F_NAME
        raw = name.encode("cp932", "replace") if isinstance(name, str) else name
        chunk += raw[:15].ljust(0x10, b"\x00")
    if comment is not None:
        flags |= _PUSH_F_COMMENT
        raw = (comment.encode("utf-16-le") if isinstance(comment, str)
               else comment)
        chunk += raw[:0x66].ljust(0x68, b"\x00")
    chunk[0] = flags
    body = _b64encode_exact(bytes(chunk))
    # +0x38 counts the chunk's CHARACTERS PLUS ONE -- 22 chars are declared as
    # 23, 64 as 65, on every long record in the capture. Read as a
    # NUL-terminated string length, which is what the caller's cap (0x158)
    # suggests it is.
    struct.pack_into("<I", r, 0x30, int(seq) & 0xFFFFFFFF)
    struct.pack_into("<I", r, 0x34, int(when if when is not None else time.time())
                     & 0xFFFFFFFF)
    struct.pack_into("<I", r, 0x38, len(body) + 1)
    struct.pack_into("<H", r, 0x3e, _PUSH_FIELD_CLASS)
    struct.pack_into("<H", r, 0x42, 0x0001)
    if len(body) + 1 >= _PUSH_CHUNK_MAX:
        raise ValueError(f"field chunk {len(body)} chars exceeds the client's "
                         f"{_PUSH_CHUNK_MAX} cap")
    return logingate._b64encode(bytes(r)) + body


def field_push_lines(watcher_nick, subject_guid, slot, **kw):
    """The NOTICE line delivering one field-list record to ONE session.

    Raises ValueError for a slot at or past the PC friend-table cap -- the last
    line of defence for `_friend_push_slot_ok`; every caller already catches
    ValueError and skips the record."""
    if not friendroster._friend_push_slot_ok(slot):
        raise ValueError(f"friend slot {slot} is past the PC table's "
                         f"{handlelists._LOBBY_LIST[(0x02, 0x03)][2]} rows -- not pushed")
    rec = build_field_push_record(subject_guid, slot, **kw)
    if isinstance(watcher_nick, str):
        watcher_nick = watcher_nick.encode()
    return [b":" + _push_nick(rec).encode() + b"!~x@ NOTICE " + watcher_nick +
            b" :" + rec.encode()]


def _push_nick(record_b64):
    """A per-record pseudo-nick in SE's shape (`PMY4QWC1V`).

    SE derives these from the record's object id -- the same token family that
    addresses `O/m/<path>` objects in 3:0/3:1/3:2 -- so a stable hash of the
    record reproduces the OBSERVABLE property (one distinct nick per record)
    without pretending to know their id allocator.

    WARNING: **THE FIRST CHARACTER MUST BE A LETTER.** `TOKEN_ALPHABET` has ten digits
    in it, so a plain hash put a digit first on 19% of records -- and a nick
    cannot begin with a digit (RFC 1459 `nick = letter *8(letter/number/special)`,
    and every one of SE's own push nicks in the capture begins `PM`). Nothing
    caught this because until 2026-08-16 no push had ever reached a live client:
    `push=0` in presence.ctl meant the short form was only ever exercised against
    FakeSessions in push_test.py, which do not parse IRC.
    """
    h = hashlib.md5(record_b64.encode("ascii", "replace")).digest()
    letters = [c for c in TOKEN_ALPHABET if c.isalpha()]
    return (letters[h[0] % len(letters)] +
            "".join(TOKEN_ALPHABET[b & 31] for b in h[1:9]))


def push_lines(watcher_nick, subject_guid, peer_guid, name, text, event,
               when=None):
    """The NOTICE line(s) delivering one push event to ONE watcher session."""
    rec = pushchannel.build_push_record(subject_guid, peer_guid, name, text, event, when)
    if isinstance(watcher_nick, str):
        watcher_nick = watcher_nick.encode()
    # `!~x@` with an EMPTY host, exactly as captured.
    return [b":" + _push_nick(rec).encode() + b"!~x@ NOTICE " + watcher_nick +
            b" :" + rec.encode()]


def _presence_lines(watcher_nick, srv, subject_nick, subject_guid, state,
                    slot=None, seq=None, subject_comment=None):
    """The session-channel line(s) that tell ONE watcher their friend
    `subject` is now in `state` ('online'|'offline'|'away'|'back'). Returns a list
    of raw IRC lines (before checksum/OFB -- ChatSession.encode does that), or []
    to send nothing.

    DISABLED by default (`POL_PRESENCE_PUSH` unset/"0" -> []).

    `POL_PRESENCE_FMT`:
      none   -- DEFAULT SINCE 2026-08-16, and the right answer for presence. The
                long FIELD record `_broadcast_presence` sends alongside this call
                is what repaints the row; this short line adds nothing and, as
                `push`, actively harms. See below.
      push   -- WARNING: **THE 72-BYTE RECORD IS A MESSAGE-ARRIVAL ANNOUNCEMENT, NOT A
                PRESENCE ONE.** Decoding every push in the 2026-08-15 capture
                sorts them into exactly two families, and this class is the wrong
                one for presence:

                    +0x3e=0x8000 +0x42=0x0003   72B  ev 3,4,5,7
                    +0x3e=<mail kind> +0x42=0x1003  75B  ev 0,1,2
                    +0x3e=0xcf80 +0x42=0x0001   75-249B  the FIELD-LIST record

                Every 0x8000 record SE sends carries a real subject at +0x20 and
                a real OBJECT LENGTH at +0x38 -- "Ahoy!" (0x0e), "RE: Ahoy!"
                (0x12), the friend request "o/" (0x08). There is **no presence
                record of this class anywhere in the capture**; presence rides
                0xcf80, which is what `field_push_lines` already builds.

                `build_push_record` hardcodes +0x38 = 8 and +0x42 = 0x03, so a
                presence flip went out as "a message arrived, 8-byte object" with
                +0x00 and +0x08 BOTH set to the subject's guid. Reported live
                2026-08-16: every online/offline produced an inbox entry reading
                *To: <player> / From: <same player> / No subject / empty body* --
                the client faithfully rendering a message arrival we invented,
                for an object that does not exist. It also explains the
                unresolved note that "presence pushes caused message entries to
                appear ... six pushes, six client 03:02 writes": those were the
                client acknowledging the phantoms.

                The reason this was switched on -- that a friend's COMMENT had no
                other route -- no longer holds: the row push carries the comment
                alongside the icon since `rowpush=1` was fixed the same day.
      xxl    -- SUPERSEDED, and known wrong: a `PRIVMSG #XXL<zoneid> :<payload>` whose
                substituted-base64 payload is byte-mapped from the client's handler
                `0x037db6f0` (see `_presence_xxl_payload`). Needs the friend's `slot`
                and `subject_guid`. The remaining live-tunables are the zoneid and
                the prefix (POL_PRESENCE_ZONE / POL_PRESENCE_PREFIX) and whether the
                guid2 / seq guesses hold -- confirm with `POL_PRESENCE_PUSH=log`,
                then `=1` and watch the client.
      away   -- LEGACY PROBE, known WRONG (superseded by the #XXL finding).
      notice -- LEGACY PROBE, also superseded.
    """
    mode = presence._presence_cfg("push", "POL_PRESENCE_PUSH", "0")
    if mode not in ("1", "log"):
        return []
    if isinstance(srv, str):
        srv = srv.encode()
    fmt = presence._presence_cfg("fmt", "POL_PRESENCE_FMT", "none")
    away = state in ("offline", "away")
    if fmt in ("none", "off", "0"):
        # The long field record in `_broadcast_presence` carries the state. This
        # short line is deliberately silent -- see the docstring.
        return []
    if fmt == "push":
        if subject_guid is None:
            return []
        # WARNING: WHICH EVENT CODE PRESENCE USES IS NOT PINNED. The capture gave codes
        # 7 (friend request), 13 (Message arrived) and 0 (profile change); a
        # presence flip was watched happening live ("their row changes
        # immediately and says offline") but its record was not isolated from
        # the 38 on disk. 0 is the reasoned default -- presence reads as another
        # profile field, and 0 is the code whose text field carries "the new
        # value", which is the shape a state name fits. POL_PRESENCE_EVENT
        # overrides it without a rebuild so the right answer costs one login,
        # not a code change.
        ev = int(presence._presence_cfg("event", "POL_PRESENCE_EVENT",
                               str(pushchannel._PUSH_EV_PROFILE)))
        return push_lines(watcher_nick, subject_guid, subject_guid,
                          subject_nick or b"", state, ev, seq)
    if fmt == "xxl":
        if slot is None or subject_guid is None:
            return []                                  # can't address the friend
        action = presence._PRESENCE_ACTION.get(state)
        if action is None:
            return []
        if seq is None:
            seq = int(time.time())                     # monotonic-ish unix seconds
        # `hslot` is the friend's HANDLE-table slot, which is what +0x18 is
        # compared against -- NOT `slot`, which is the 2:3 record index that
        # +0x1c uses. They are different numbers (record index 0..N vs handle
        # slot 4..) and conflating them is what made the old sweep meaningless.
        hslot = friendlist._friend_handle_slots().get(int(subject_guid))
        payload = presence._presence_xxl_payload(subject_guid, slot, action, seq,
                                        comment=subject_comment,
                                        hslot=hslot).encode()
        # zoneid: the factory only checks the "#XXL" prefix, so a fixed 16-hex is a
        # fine first guess; the prefix (sender) follows the game-notice convention.
        zone = presence._presence_cfg("zone", "POL_PRESENCE_ZONE", "0000000000000000").encode()
        pfx = presence._presence_cfg("prefix", "POL_PRESENCE_PREFIX", "").strip()
        prefix = (b":" + pfx.encode() + b" ") if pfx else b""
        return [prefix + b"PRIVMSG #XXL" + zone + b" :" + payload]
    if fmt == "away":                                  # legacy probe, superseded
        num = b"306" if away else b"305"
        return [b":" + srv + b" " + num + b" " + watcher_nick + b" " +
                subject_nick + b" :presence"]
    if fmt == "notice":                                # legacy probe, superseded
        body = b"P" + (b"\x02" if away else b"\x01") + subject_nick
        return [b"NOTICE " + watcher_nick + b" :" + body]
    return []


def _push_identity_guid(db, subject_handle):
    """The guid the client keys this friend's ROW by -- so a presence/field push
    stamps the SAME identity the 2:3 row served.

    The client matches a pushed record to a friend slot by the guid it stored
    from that slot's 2:3 record (+0x10); a push carrying a different guid fails
    the identity compare and is dropped SILENTLY. This mirrors, gated on the SAME
    flag, exactly how `_db_friends` chooses the row guid: `handle_guid`, overridden
    by the peer's learned `client_guid` when `POL_FRIEND_GUID_CLIENT` is on and we
    have one. Coupled to one source so the row and the push cannot drift apart --
    the drift that broke presence on 2026-08-22 (see `_broadcast_presence`).
    """
    guid = accounts.handle_guid(int(subject_handle))
    if os.environ.get("POL_FRIEND_GUID_CLIENT", "1") == "1":
        try:
            cg = db.execute("SELECT client_guid FROM handle WHERE id = ?",
                            (int(subject_handle),)).fetchone()
            if cg and cg["client_guid"]:
                guid = int(cg["client_guid"])
        except Exception:
            pass                      # a missing client_guid = fall back, not fail
    return guid


def _broadcast_presence(subject_member_id, state, subject_name=None, seq=None):
    """Push `subject`'s presence change to every online friend that watches them.

    Three modes via `POL_PRESENCE_PUSH`:
      unset/"0"  fully off -- no DB work, no log, nothing. The default.
      "log"      DRY RUN: compute the watchers and the exact line that WOULD be
                 sent, log it, but do NOT write to any socket. This validates the
                 whole fan-out (who is watching, who is online, what the line looks
                 like) against real client logins without risking an unvalidated
                 byte on the wire -- the safe way to confirm the plumbing before
                 the inbound format is settled. Returns 0 (nothing sent).
      "1"        LIVE: actually send. Only flip to this once the wire format is
                 confirmed AND a real client is on hand to watch it land.

    Returns the number of watcher sessions actually WRITTEN to (0 in log mode).
    """
    mode = presence._presence_cfg("push", "POL_PRESENCE_PUSH", "0")
    if accounts is None or mode not in ("1", "log"):
        return 0
    if state not in presence._PRESENCE_STATES:
        return 0
    dry = mode == "log"
    sent = 0
    try:
        db = accounts.connect(os.environ.get("POL_ACCOUNTS_DB", accounts.DEFAULT_DB))
    except Exception as exc:
        log("authserv", f"presence: db open failed ({exc!r})")
        return 0
    try:
        try:
            watchers = accounts.friend_watchers(db, int(subject_member_id))
        except Exception as exc:
            log("authserv", f"presence: watcher lookup failed ({exc!r})")
            return 0
        # THE NAME A WATCHER SEES IS THE HANDLE NAME, and nothing else. The two
        # call sites used to supply whatever they had in scope -- the login hop
        # passed the IRC NICK and the logout path passed `login_name` -- so a
        # push could carry `URZ82TPPK` into a field the client renders. That was
        # seen live on 2026-08-15. Resolve it here instead, where the DB is
        # already open, and let callers pass nothing.
        if not subject_name:
            try:
                row = db.execute(
                    "SELECT handle_name FROM handle WHERE member_id = ? "
                    "ORDER BY is_primary DESC, id ASC LIMIT 1",
                    (int(subject_member_id),)).fetchone()
                if row:
                    subject_name = row["handle_name"]
            except Exception:
                pass                      # a missing name is cosmetic, not fatal
        online_watchers = 0
        pushed_slots, field_state, field_zone = [], None, None
        for watcher_member, watcher_handle, subject_handle in watchers:
            targets = presence.PRESENCE.sessions_for(watcher_member)
            if not targets:
                continue                  # that friend is not logged in -- nothing to push
            # The friend's slot in THIS watcher's 2:3 list -- the presence update
            # addresses the friend by that slot, so it must match what we served.
            slot = friendroster._friend_slot(db, watcher_handle, subject_handle)
            if slot is None:
                continue                  # subject not in this watcher's rendered list
            online_watchers += 1
            pushed_slots.append(slot)
            # THE PUSH IDENTITY MUST MATCH THE ROW IDENTITY, OR THE CLIENT DROPS
            # THE PUSH SILENTLY -- and the online icon is painted ONLY by this
            # push (RE 2026-08-23: the 2:3 record-copy 0x37deeb0 never writes the
            # slot+0x08 bits 11/13..15 the renderer 0x0488efc5 tests; no wire row
            # byte can set online, so a fetched list can only ever render offline
            # and the presence push is the SOLE painter). The client keys a friend
            # by the (guid_lo, guid_hi, handle_slot) triple; when the push guid
            # disagrees with the row's +0x10 guid the apply path's identity
            # compare fails and nothing repaints. That is exactly what broke on
            # 2026-08-22: POL_FRIEND_GUID_CLIENT defaulted ON (9bd29667/2ad043b4),
            # flipping the 2:3 ROW to the peer's client_guid, while this push kept
            # stamping handle_guid -- so presence stopped landing while the icon
            # row push (which carries the SERVED guid) kept working. The account
            # holder's "it worked great until today" is what isolated the flip.
            # Derive the guid the SAME WAY, gated on the SAME flag, as `_db_friends`
            # serves the row -- coupled to one source so they cannot drift again.
            guid = _push_identity_guid(db, subject_handle)
            name = (subject_name or "").encode("cp932", "replace") if isinstance(
                subject_name, str) else (subject_name or b"")
            # THE ROW ITSELF IS REPAINTED BY THE LONG RECORD, not by this line.
            # `_presence_lines` is the short/session-channel form; SE's presence
            # pushes are BLOCK-ONLY LONG records (see _PRESENCE_FIELD_STATE), and
            # the block is what writes the friend slot's status word. Sending only
            # the short form is why "X is now online" never changed the list --
            # the same wrong-carrier mistake that hid comments. Send both.
            field_state = _presence_field_state(state)
            # Resolved for the SUBJECT, not the watcher -- this says where the
            # friend is, and it is what draws the Viewer/chat-room icon on the row.
            field_zone = (_presence_zone(subject_member_id, field_state)
                          if field_state is not None else None)
            for ts in targets:
                lines = _presence_lines(ts.nick, ts.srv, name, guid, state,
                                        slot=slot, seq=seq)
                if field_state is not None:
                    try:
                        # SE's ORDER: the group-scoped record(s) first, then the
                        # friend-list one. 19 pairs in the capture, group always
                        # ahead. One per group the two of us share -- the record
                        # names the group, so a subject in three groups is three
                        # records, exactly as a subject in one is one.
                        lines = list(lines or [])
                        for g in pushspool._shared_groups(db, watcher_handle,
                                                subject_handle):
                            lines += field_push_lines(
                                ts.nick, guid, slot, state=field_state,
                                zone=field_zone, group=g,
                                seq=next(pushspool._ROW_PUSH_SEQ))
                        lines += field_push_lines(
                            ts.nick, guid, slot, state=field_state,
                            zone=field_zone, seq=next(pushspool._ROW_PUSH_SEQ))
                    except Exception as exc:
                        log("authserv", f"presence: row record failed "
                                        f"(slot {slot}, state {state}): {exc!r}")
                if not lines:
                    continue
                if dry:
                    log("authserv", f"presence[dry]: would tell {ts.nick!r} that "
                                    f"{subject_name or subject_member_id} is {state} "
                                    f"(slot {slot}, +0x11={field_state}): "
                                    + "; ".join(repr(l) for l in lines))
                elif ts.send(lines):
                    sent += 1
        if dry and online_watchers:
            log("authserv", f"presence[dry]: {subject_name or subject_member_id} -> "
                            f"{state}, {online_watchers} online watcher(s) (nothing sent)")
        elif sent:
            # SAY WHICH SLOT, AND WITH WHAT. "pushed to N sessions" is true and
            # useless: it proved the record left us while the account holder was
            # reporting presence not working, which narrows nothing. The client
            # validates a row push against the SLOT and the guid and drops it
            # silently on a mismatch -- and the row spool (icons/comments, which
            # DO render) resolves its slot a different way, from what we actually
            # served, while this path re-derives it. If those two disagree, icons
            # land and presence does not, which is exactly the symptom.
            log("authserv", f"presence: {subject_name or subject_member_id} -> {state}, "
                            f"pushed to {sent} watcher session(s) "
                            f"[slots {sorted(set(pushed_slots))}, +0x11={field_state}, "
                            f"zone={field_zone}]")
        return sent
    finally:
        try:
            db.close()
        except Exception:
            pass
