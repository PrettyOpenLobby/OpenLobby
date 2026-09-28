"""The member profile record and profile TLV writes."""
import json
import os
import struct
import time
from srvcore import hexdump, log, save_capture
from .deps import accounts
from . import (contentprofiles, friendlist, lobbyrefuse, lobbyreply, lobbysession, memberstatus,
               paylen, pfc, pushchannel, pushrecord, pushspool)



#: The 05:04 Handle Profile record schema, read LIVE out of polcore by the
#: pol-shim `profSchema` probe (polcore RVA 0x20DA0, the parser sub_037E0DA0).
#: The client's own descriptor array carries ASCII NAMES in its first 8 bytes,
#: so this is the client's schema, not a reconstruction: 32 entries of 0x18
#: bytes each, `word length` at +0x10 and `byte type` at +0x12.
#:
#: (index, name, length, type). The INDEX IS THE WRITE-SIDE FIELD ID -- 05:01's
#: TLV ids line up one for one, confirmed on sixteen names against
#: accounts.HANDLE_PROFILE_FIELDS (0x03 age -> z_age, 0x05 sex -> z_sex,
#: 0x12 mail_address -> z_mail, 0x13 portrait -> z_ficon, ...) and on two live
#: writes (18 -> the user's mail address, 19 -> 2512).
#:
#: Types come from the dispatcher sub_037E04D0 (jump table 0x37E06F8):
#:   1 = string, fixed width, NUL padded      2 = single byte
#:   4 = 2-byte LE                            5, 6 = 4-byte LE
#:   8, 9 = 8-byte blob
_PROFILE_SCHEMA = (
    (0,  "z_phead",   8, 9), (1,  "z_name",   16, 1), (2,  "z_hid",     8, 8),
    (3,  "z_age",     4, 5), (4,  "z_age_f",   1, 2), (5,  "z_sex",     1, 2),
    (6,  "z_area",    1, 2), (7,  "z_cntry",   1, 2), (8,  "z_wide",    1, 2),
    (9,  "z_local",   1, 2), (10, "z_lang0",   1, 2), (11, "z_lang1",   1, 2),
    (12, "z_lang2",   1, 2), (13, "z_job",     1, 2), (14, "z_fav0",    2, 4),
    (15, "z_fav1",    2, 4), (16, "z_fav2",    2, 4), (17, "z_purp",    1, 2),
    (18, "z_mail",  320, 1), (19, "z_ficon",   4, 6), (20, "z_rlang",   1, 2),
    (21, "z_aa_mt",   1, 2), (22, "z_aa_dt",   1, 2), (23, "z_aa_t",    1, 2),
    (24, "z_aa_m",    1, 2), (25, "z_aa_c",    1, 2), (26, "z_aa_w",    1, 2),
    (27, "z_aa_d",    1, 2), (28, "z_aa_e",    1, 2), (29, "z_utime",   4, 6),
    (30, "z_pnum",    1, 2), (31, "z_up_nam",  0, 1),
)

#: KEY: **THE 168 BYTES AFTER THE FIELDS ARE THE SUBJECT'S OWN 2:3 FRIEND ROW.**
#:
#: The packed fields end at 424 (0x1A8) -- z_phead says so on SE's records and on
#: ours, and `tools/verify_profile.py` pins it. What follows is NOT slack: SE's
#: `u/s/select1` reply (polshim-se.429364.log line 72342) carries a complete
#: 168-byte friend record at +0x1A8, and the record ends at 0x250 = 592, leaving
#: the 8 bytes of real slack SE fills with `01 03 00 00 e8 03 00 00`.
#:
#: Its contents, field for field, are a 2:3 row:
#:
#:     +0x00  40 a0 13 ea               head word (0xEA13A040)
#:     +0x04  04 00 00 00               status (4 = offline)
#:     +0x10  91 bc 04 1e 2c 00 8c 00   guid
#:     +0x18  "Yui"                     name
#:
#: -- which is why the 2026-08-11 note called +0x1B8 "the handle GUID" and +0x1C0
#: "the handle name": those are `idrec + 0x10` and `idrec + 0x18` exactly. Same
#: bytes, right frame at last.
#:
#: WHY IT MATTERS, and it is the whole *"X is not waiting for friend
#: registration"* bug: app.dll keys a person by the TRIPLE (guid_lo, guid_hi,
#: handle_slot) -- `0x488c6fc` builds it, `0x488ddba` looks it up in the friend
#: array at `0x4d34bc4` (entries 0..199 friends, 200..299 ignore). Reading a
#: `Friend registration accepted` message runs that lookup on the message's own
#: (+0x00/+0x04 guid, +0x3C slot) at `0x4aadaf3`, and a MISS in BOTH arrays is
#: precisely what raises string 18161, *"%s is not waiting for friend
#: registration. Resend "Let's be friends!" request?"* with its Send/Cancel pair.
#:
#: A member added from a SEARCH is the case that breaks, and it breaks every
#: time: the 2:6 PUT carries only a NAME -- no guid anywhere in the record -- so
#: this block is the only place the client can learn who it just added. We sent
#: zeros, so the asker's row was anonymous, so the acceptance matched nothing and
#: the client offered to ask again. After a relog the row comes from 2:3 with a
#: real guid and the same accept works, which is the tell.
#:
#: POL_PROFILE_IDREC=0 restores the empty tail.
_PROFILE_IDREC_AT = 0x1A8
_PROFILE_IDREC_LEN = 168

#: KEY: **THE IDENTITY ROW'S LAST 128 BYTES ARE THE SUBJECT'S CONTENT LIST -- and
#: that block is what the per-Content-ID profile SECTION is built from.**
#:
#: MEASURED 2026-08-24 by diffing SE's own 05:04 replies for the SAME account
#: (Lex, same handle, same guid, same 0:9 and 1:3) across two retail sessions --
#: one where the content section worked and one where it did not. The two
#: records are byte-identical apart from the privacy level, z_utime, SE's
#: uninitialised z_mail slack, and SIXTEEN BYTES at record +0x1D0:
#:
#:     polshim-se.429364.log:43503     no section; 8x 5:4 outlen=36, no 52
#:       1D0  00000000 00000000 00000000 00000000
#:
#:     work/pc/prof-ffxi-retail-20260823.log:9321   section works; 5:4 outlen=52
#:       1D0  0100 0100 BD80650A F5C16A01 00000000
#:
#: and `BD80650A` / `F5C16A01` are EXACTLY the two values that session's 52-byte
#: content request then carried (item id 1 z_ctsid = 0x0A6580BD, item id 2
#: z_ctid = 0x016AC1F5), so this block is where the client reads them from. They
#: are also byte-for-byte the pair SE's 1:3 char record puts at +0x0C and +0x10
#: for the same character: two independent carriers, one identity.
#:
#: WHY THIS IS THE GATE AND THE CLIENT NEVER WAS. The client builds its
#: selectable content list with ZERO lobby traffic between the handle profile
#: and the content request (measured), so the ids must come from something it
#: already holds. The 1:3 char table was made byte-identical to SE's, carrying a
#: real z_ctsid, and the section still did not appear -- which eliminates that
#: cache and leaves this reply. Every client-side gate hunted before this
#: (`0x800`, `0x224ABC`, the handle guid, `0:8`) was retracted in turn; they were
#: all downstream of a list with nothing in it.
#:
#: 0x1D0 is idrec +0x28 and the idrec ends at 0x250, so the block is exactly
#: 128 bytes = **8 entries of 16**, which is also POL's per-handle content
#: ceiling (`_CHAR_PER_HANDLE`). This is the same +0x28 the friend-record probes
#: kept sweeping for a comment field and a face icon and never found either in.
#: Entry layout, from the one populated sample:
#:
#:     +0x00  u16  content CODE       1 = FFXI
#:     +0x02  u16  ???                1 in the only sample there is
#:     +0x04  u32  z_ctsid            the 1:3 record's +0x0C
#:     +0x08  u32  z_ctid             the Content ID, 1:3's +0x10
#:     +0x0C  u32  0
#:
#: VERIFIED: **+0x02 IS A SECOND COPY OF THE CONTENT CODE -- named LIVE 2026-08-24.**
#: SE's only sample is FFXI, whose code is 1, so "1" and "the code" were
#: indistinguishable in it; serving a non-FFXI content is what separated them,
#: exactly as predicted. With +0x02 pinned to 1 on all eight links the profile
#: screen listed FOUR ENTRIES ALL LABELLED FFXI for eight different codes, so
#: the field is read as the title. It now mirrors the entry's own code;
#: `POL_PROFILE_CONTENT_F02=<int>` forces a constant for bisecting.
#: WARNING: Still open: why FOUR rows for eight entries. Stride 16 is CONFIRMED (the
#: client's second content request carried `z_ctid` 30000001 = entry 1's exact
#: value, which a 32-byte stride cannot produce), so the count is capped or
#: filtered somewhere else, not a layout error.
#:
#: WARNING: SE's 2:3 friend rows carry ZERO here (polshim-se.369852.log:3307, Mirabel
#: and Gazlo, both offline), which fits the section being TRANSIENT on retail --
#: SE appears to fill the block only for a content that is, or recently was, in
#: session. We fill it from `handle_content` unconditionally. That is a
#: DELIBERATE divergence and it is the one guess in this change: we cannot
#: observe "recently played", and a section that stays is the behaviour being
#: asked for. If a populated block turns out to be refused where an empty one is
#: not, that is the knob to turn first.
#:
#: POL_PROFILE_CONTENTS=0 restores the zero block.
_IDREC_CONTENT_AT = 0x28
_IDREC_CONTENT_STRIDE = 16
_IDREC_CONTENT_MAX = 8


#: 🔬 **POL_PROFILE_TRAILER=1 -- EVERY PROFILE RECORD ENDS IN A 0xB0 TRAILER.
#: DEFAULT OFF, NOT YET CHECKED AGAINST A CLIENT.**
#:
#: The trailer is the subject's 168-byte friend row (FriendData) followed by an
#: 8-byte status block (NotifyStatusData), and it sits at z_phead, which is
#: 1 + sum(len + 1) ROUNDED UP TO 8. Project Crystal Server's profile table
#: writer (PolDbTable / ProfileHeader) builds records that way. It matches every
#: record length the Viewer's own `prof_<NNN>.pib` files declare:
#:
#:     FFXI  101 -> 104 + 176 = 280     TM / jan  251 -> 256 + 176 = 432
#:     DoC    61 ->  64 + 176 = 240     FE        285 -> 288 + 176 = 464
#:     FMO   102 -> 104 + 176 = 280     handle    424       + 176 = 600
#:
#: That also accounts for the "3-byte discrepancy" on FFXI's z_phead (104, not
#: 101) noted at `_CONTENT_SCHEMAS`: the 104 is the aligned value.
#:
#: CHECKED AGAINST SE'S OWN BYTES (work/pc/prof-ffxi-retail-20260823.log):
#:   * the 280-byte FFXI content record (line 10525) has, at +0x68, the same
#:     168-byte row the handle record carries at 0x1A8 (head 0x238EA040, guid,
#:     "Lex", the content block) and then `01 01 00 00 00 00 00 00` at +0x110;
#:   * the handle records carry 8 bytes at 0x250: `01 01 00 00 00000000` on the
#:     account's OWN profile, `00 01 00 04 ...` and `00 01 00 05 ...` on two
#:     offline members (line 11843/11950), and polshim-se.429364's select1 for
#:     an online friend has `01 03 00 00 e8 03 00 00`.
#:
#: The status block, read with Crystal's field names and checked on those four:
#:     +0 login flag     1 online (or self), 0 offline
#:     +1 status         the presence push's +0x11 value: 3 online, 2 away,
#:                       1 offline -- and 1 on the account's own record
#:     +2 active character bits (0 in every sample; left 0)
#:     +3 purpose        the subject's z_purp. The two offline members carry
#:                       purpose 4 and 5 in their field 17 and 04/05 here; Crystal
#:                       always writes 0, SE does not.
#:     +4 u32 zone       the presence zone (1000 = the Viewer), 0 offline / self
#:
#: The SELF case is copied from the one sample there is (own profile, logged in,
#: yet status 1 / zone 0); the others follow the presence push we already
#: serve, so a friend's record and their push agree.
#:
#: What changes with the knob ON: content records use the aligned z_phead
#: (DoC/TM/jan/FMO/FE -- FFXI's measured 104 is unchanged) and carry the owner
#: handle's friend row + status block there instead of zeros; the handle record
#: gains the status block at 0x250. With it OFF the bytes are what they were.
#: A/B: open a TM/FE/DoC/FMO content profile and a friend's handle profile with
#: it on and off; watch for the section rendering and for any refusal.
_PROFILE_NOTIFY_AT = _PROFILE_IDREC_AT + _PROFILE_IDREC_LEN     # 0x250
_PROFILE_NOTIFY_LEN = 8
_PROFILE_TRAILER_LEN = _PROFILE_IDREC_LEN + _PROFILE_NOTIFY_LEN  # 0xB0


def _profile_trailer_on():
    """Is POL_PROFILE_TRAILER on? Read per call so a test can flip it."""
    return os.environ.get("POL_PROFILE_TRAILER", "0") == "1"


def _profile_notify_status(db, h, purpose=None):
    """The 8-byte status block of a profile trailer for handle row `h`.

    See `_PROFILE_NOTIFY_AT` for the layout and the four SE samples it is
    checked against. Best effort: any failure gives the offline shape.
    """
    purp = 0
    try:
        purp = int(purpose or 0) & 0xFF
    except (TypeError, ValueError):
        purp = 0
    offline = pushrecord._PRESENCE_FIELD_STATE["offline"]
    try:
        member = int(h["member_id"])
        if lobbysession._session_member_id() == member:
            # The account's own record: SE sent status 1, zone 0 while online.
            return bytes([1, offline, 0, purp]) + bytes(4)
        if not accounts.handle_online(db, int(h["id"])):
            return bytes([0, offline, 0, purp]) + bytes(4)
        away = memberstatus._status_is_away(memberstatus._member_status(member))
        state = pushrecord._presence_field_state("away" if away else "online")
        if state is None:
            state = pushrecord._PRESENCE_FIELD_STATE["away" if away else "online"]
        zone = pushrecord._presence_zone(member, state)
        return bytes([1, int(state) & 0xFF, 0, purp]) + struct.pack(
            "<I", int(zone or 0) & 0xFFFFFFFF)
    except Exception as exc:
        log("lobby", f"profile trailer: status block failed ({exc!r}); offline")
        return bytes([0, offline, 0, purp]) + bytes(4)


#: The parser consumes ONE leading byte (stored at dest+4) and then, per field,
#: a PRESENCE byte followed by `length` value bytes -- the walk step is
#: `edi += length + 1`. So the used span is 1 + sum(length + 1) = 424 bytes of
#: the 600 the client memcpys; the tail is slack and stays zero.
_PROFILE_LEAD = 1

#: field id -> true byte width, straight from the schema. Used to mask the
#: uninitialised junk out of 05:01 writes (see _parse_profile_tlv).
_PROFILE_WIDTH = {f[0]: f[2] for f in _PROFILE_SCHEMA}

#: Storage key for the profile's VISIBILITY level. The 05:01 write carries it at
#: payload offset 0x2A (values 1 and 2 both observed live), and we used to only log
#: it -- so the user's Public/Friends/Private choice was captured on the wire and
#: then thrown away, while the read-back served a constant. That is why the setting
#: never persisted: every profile fetch overwrote the choice. 0x100 is outside the
#: 0..31 schema range, so it cannot collide with a real field id and the record
#: builder's schema loop ignores it.
_PROFILE_VIS_KEY = 0x100

#: *** WHERE THE LEVEL GOES IN THE REPLY -- MEASURED OFF SE'S OWN WIRE. ***
#:
#: Storing the choice was only half of it. The read-back still put the level on
#: the WRONG BYTES, and the byte that decides "can anyone else see this profile"
#: was a hardcoded 1 -- the most restrictive value there is. So a second account
#: searching for the first got "Selected profile is hidden" no matter what the
#: owner chose, and the owner's own editor loaded that same 1 back as "Private"
#: and wrote it straight out again: the setting looked like it saved and did
#: nothing, every time.
#:
#: SE's four real profile records (polshim-se.429364.log, decoded with
#: pol-shim/tools/lobbydec.py) settle the layout outright:
#:
#:     record            +0x00 lead   +0x01 z_phead vis   fields 1..31 vis
#:     5:4  'Lex'  (own)       2                   2                    3
#:     3:0  'YUI'  (search)    3                   3                    3
#:     3:0  'Yui'  (search)    3                   3                    3
#:
#: Two things follow, and they are the whole fix:
#:
#:   1. The PER-FIELD visibility byte is a CONSTANT 3 on every field SE serves,
#:      in all three records, including the one whose head says 2. It is not the
#:      user's privacy dial. We were writing the stored level onto all 31 fields.
#:   2. The leading byte and z_phead's visibility byte MOVE TOGETHER and carry the
#:      profile's disclosure LEVEL -- 2 on the account holder's own restricted
#:      profile, 3 on the two profiles that were openly viewable. That is the same
#:      1/2/3 scale the 05:01 write sends at payload +0x2A, so the value the user
#:      picks belongs here and nowhere else.
#:
#: With a stored level of 3 our record is now byte-identical to SE's 'Yui' record
#: in these three positions, which is the strongest check available short of a
#: capture of SE refusing a hidden profile (we have none -- no SE profile in the
#: capture was set below 2).
_PROFILE_FIELD_VIS = 3
_PROFILE_LEVEL_DEFAULT = 3


#: Extra bytes some field types occupy BEYOND their descriptor length.
#:
#: Measured, not derived. pol-shim's `profParsed` probe resolves the value
#: pointer the client computes for every entry; diffing those against the bytes
#: we actually write showed a CONSTANT +2 shift appearing between field 0 and
#: field 1 and holding thereafter (entry1 +13 vs our +11, entry4 +42 vs +40,
#: entry18 +73 vs +71 -- same +2, no accumulation). Exactly one field is
#: mis-sized: z_phead, type 9, whose descriptor says 8 but whose slot in the
#: stream is 10.
#:
#: The symptom was ugly and would have been very hard to reason to: from field
#: 13 onward the client read ASCII out of the mail address AS the visibility
#: bytes of later fields.
#:
#: WITHDRAWN -- the +2 was an ARTEFACT OF MY OWN ARITHMETIC, not the client's.
#:
#: I anchored every offset on entry 0's reported value. Entry 0 is type 9, and
#: type 9's handler stores the RECORD BASE, not the `edi+1` value pointer the
#: other types store -- provable from the parser itself: the entry array lives
#: at dest+0xC and the record is memcpy'd to `entries + count*0x10`, which is
#: exactly the address entry 0 reports. Anchoring on it shifted every computed
#: offset by 2 and invented a drift that was never there.
#:
#: Re-anchored on entry 1 (type 1, which really does store edi+1), the packing
#: `1 lead byte + per field (1 visibility byte + length)` is EXACTLY right:
#: entry 1 wants record+11 and gets 11; entry 18 wants record+73 and gets 73.
#: Adding 2 broke both.
#:
#: Kept as an empty dict so the step logic stays in one place if a genuine
#: per-type adjustment ever turns up -- but do not add one without anchoring on
#: a type whose handler is known to store a value pointer.
_PROFILE_STEP_EXTRA = {}


def _profile_identity_record(db, h, tagged=False):
    """The 168-byte 2:3 row that rides in profile field 31, for handle row `h`.

    It MUST agree with what `_db_friends` -> `_friend_list_record` serves for the
    same person, because those are the two places the client can learn a friend's
    identity and it compares the two. `_friend_list_record` is therefore the
    builder here as well rather than a second, drifting copy: same head word
    (so the same bits 7..12 handle slot), same `handle_guid`, same name.

    Presence is read live for the same reason it is on the 2:3 row -- a search
    hit that renders offline while the person is online is the same defect there.
    Best-effort: any failure returns zeros, which is the pre-2026-08-17 behaviour.

    `tagged` rewrites the head word to SE's SEARCH-hit shape: `(rid << 8) | 0x40`
    with rid = hid << 5, so `0x200000 | (head >> 13)` is exactly the z_hid the
    same record's profile serves. SE keeps ONE id space across the profile and
    the friend row -- Yui's select1 identity row heads 0xEA13A040 and her profile
    z_hid is 0x27509D == 0x200000 | (0xEA13A040 >> 13) -- and reconciling the two
    is what lets the client link the added row back to the search hit and post
    the "Let's be friends!" 3:1 itself. Only the head word changes: the guid at
    +0x10 and name at +0x18 stay, and the status dword's low byte is already
    SE's 04.
    """
    try:
        hid = int(h["id"])
        rec = friendlist._friend_list_record(
            _PROFILE_IDREC_LEN, accounts.handle_guid(hid), h["handle_name"],
            kind=accounts.KIND_FRIEND, hid=hid,
            online=bool(accounts.handle_online(db, hid)), index=0)
        if tagged and friendlist._friend_bitfield_mode():
            # POL_FRIEND_BITFIELD: SE's search-hit head is the same u64 with the
            # low 13 bits replaced by 0x40 (no valid bit, level 2) -- Yui's
            # 0xEA13A040 -- so the handle id stays where the row put it.
            rec = bytearray(rec)
            word = struct.unpack_from("<Q", rec, 0x00)[0]
            struct.pack_into("<Q", rec, 0x00, (word & ~0x1FFF) | 0x40)
            rec = bytes(rec)
        elif tagged:
            rec = bytearray(rec)
            struct.pack_into("<I", rec, 0x00,
                             (((hid & 0x7FFFF) << 5) << 8) | 0x40)
            rec = bytes(rec)
        # THE CONTENT LIST, where SE puts it -- see `_IDREC_CONTENT_AT`. This is
        # the block the profile screen's selectable content section is built
        # from, and it was zero on every record we have ever served.
        span = _IDREC_CONTENT_AT + _IDREC_CONTENT_STRIDE * _IDREC_CONTENT_MAX
        if (os.environ.get("POL_PROFILE_CONTENTS", "1") == "1"
                and len(rec) >= span):
            block = contentprofiles._identity_content_entries(db, hid)
            rec = bytearray(rec)
            rec[_IDREC_CONTENT_AT:_IDREC_CONTENT_AT + len(block)] = block
            rec = bytes(rec)
        return rec
    except Exception as exc:
        log("lobby", f"profile record: identity row failed ({exc!r}); left empty")
        return bytes(_PROFILE_IDREC_LEN)


def _profile_record(size, req_pt=None, force_hid=None):
    """The 05:04 profile record, built from `handle_profile`.

    `size` is the memcpy length the client uses (600); the 4-byte checksum the
    caller appends lives beyond it, which is why SE's payload is 604.

    The byte in front of each value is NOT a boolean. The parser stores it RAW
    at the consumer's `entry+5` and only derives "is set" from whether it is
    nonzero (`setne` into bit 0 of `entry+4`), so it carries a VALUE -- the UI's
    Public/Friends/Private level.

    *** ITS VALUE IS 3. Captured 2026-08-11 from a REAL Square Enix session ***
    (MITM capture + the profSchema raw-record dump). SE's record reads:

        leading byte 01
        field  0 z_phead  vis 1   value 424   <- 0x1A8, the record's own used length
        field  1 z_name   vis 3   "Examplemember"
        field  3 z_age    vis 3   26
        field 13 z_job    vis 3   20
        ... every remaining field vis 3

    So EVERY field is 3 except field 0, which is 1. This supersedes the earlier
    note that "serving 1 or 2 showed nothing at all;
    255 works" -- that experiment was run against a record which changed underneath
    it (its three settings were flagged as unverified for exactly this
    reason). 255 presumably also "works" only because it clears every threshold
    the renderer compares against; 3 is what the real service sends.

    *** CORRECTED 2026-08-16: FIELD 0'S 1 IS NOT A CONSTANT. *** Reading "1" off a
    single record made it look like part of the layout. Three more of SE's records
    show the lead byte and field 0's visibility byte taking 2 and 3 as well, always
    as a pair, while every OTHER field stays 3 in all of them -- so that pair is the
    profile's disclosure LEVEL and field 0 was never a fixed header. See the
    measurement table at `_PROFILE_FIELD_VIS`. Serving the hardcoded 1 there is what
    made every profile read "hidden" to everybody else.

    The packing rule is now CONFIRMED rather than inferred: 1 leading byte, then
    per field `1 visibility byte + <len> value bytes` in schema order, ending at
    1 + sum(len+1) = 424 = 0x1A8.

    POL_PROFILE_VIS (fields 1..31) and POL_PROFILE_LEVEL (the lead byte and field
    0, i.e. the privacy dial) override them for A/B without a rebuild.
    """
    vis = _PROFILE_FIELD_VIS                    # SE's value, measured
    try:
        vis = int(os.environ.get("POL_PROFILE_VIS",
                                 str(_PROFILE_FIELD_VIS)), 0) & 0xFF
    except ValueError:
        pass

    # The privacy dial. Default open, for the same reason unset fields are served
    # visible rather than Private: a level nobody has chosen must not be one the
    # user cannot climb out of from inside the client.
    level = _PROFILE_LEVEL_DEFAULT
    try:
        level = int(os.environ.get("POL_PROFILE_LEVEL",
                                   str(_PROFILE_LEVEL_DEFAULT)), 0) & 0xFF
    except ValueError:
        pass
    level_env = "POL_PROFILE_LEVEL" in os.environ  # explicit override wins over stored

    # WHOSE profile is this? The 05:04 request names the subject handle in its TLV
    # (field 2 = z_hid). Resolving it here rather than after the DB read is the
    # whole point: viewing a FRIEND's profile previously returned the logged-in
    # user's own record with the friend's z_hid stamped on top, so every profile in
    # the game looked like yours.
    asked = {}
    if req_pt:
        try:
            asked = _parse_profile_tlv(req_pt) or {}
        except Exception as exc:
            log("lobby", f"profile record: request TLV did not parse ({exc!r})")
    subject_hid = asked.get(2)
    vis_all = os.environ.get("POL_PROFILE_VIS_ALL", "1") != "0"
    out = bytearray(size)
    fields = {}
    idrec = None                       # the trailing 2:3 row, see _PROFILE_IDREC_AT
    notify = None                      # the status block, see _PROFILE_NOTIFY_AT
    if accounts is not None:
        try:
            db = accounts.connect()
            try:
                # z_hid is a WIRE GUID, not our row id -- `accounts.handle_guid`
                # mints it and the client only ever repeats back what we put in
                # the 0:9 record. Matching it against `handle.id` (as this did
                # until 2026-08-12) could only ever succeed by coincidence.
                h = None
                # `force_hid` names the subject by ROW ID, bypassing the request
                # TLV entirely. The member-search result rows need it: those rows
                # are profiles of whoever matched, and that request carries no
                # z_hid to resolve -- the subject comes from the search, not from
                # the client. See the `profile` mode in _search_result_payload.
                if force_hid:
                    h = db.execute("SELECT * FROM handle WHERE id = %s",
                                   (int(force_hid),)).fetchone()
                    if h is None:
                        log("lobby", f"profile record: forced handle "
                                     f"{force_hid!r} does not exist")
                if h is None and subject_hid:
                    h = accounts.handle_by_guid(db, subject_hid)
                if h is None and subject_hid:
                    # A friend served under their CLIENT guid
                    # (POL_FRIEND_GUID_CLIENT, see _db_friends) echoes THAT back as
                    # the profile z_hid. Resolve it the same way the mail path does,
                    # or their profile falls through to the viewer's own -- the
                    # profile-bleed this whole block exists to prevent.
                    h = accounts.handle_by_client_guid(db, int(subject_hid))
                if h is None and subject_hid and \
                        (int(subject_hid) >> 19) in (0x04, 0x09) and \
                        (int(subject_hid) & 0x7FFFF):
                    # A SUBJECT PICKED OUT OF THE FRIEND ROW, not a wire guid.
                    # The client builds it as `(state byte << 19) | (head >> 13)`
                    # from the 2:3 record -- see the head-word note in
                    # `_friend_list_record` -- so the low 19 bits are the handle
                    # id we put there, and this is the friend whose row was
                    # clicked. The `0x200000` "tag" this branch used to test was
                    # never a constant: it is the ORDINARY-FRIEND state byte 04
                    # shifted (4 << 19 == 0x200000), which is why it held for
                    # every settled row (SE's Yui: 0x27509D = (4 << 19) |
                    # 0x7509D, state word 0x...04) and silently missed the
                    # PENDING class. Measured live 2026-08-22T03:51:29: View
                    # Profile on a pending row (AmicableElm, handle 5, state
                    # word 0x5A000009) sent z_hid 0x480005 = (9 << 19) | 5,
                    # fell through to "serving the active one", and drew the
                    # VIEWER's own profile. Only the two state bytes we
                    # actually serve are accepted (04 settled, 09 pending) --
                    # a wider mask would swallow subjects of classes we have
                    # never observed.
                    fid = int(subject_hid) & 0x7FFFF
                    h = db.execute("SELECT * FROM handle WHERE id = %s",
                                   (fid,)).fetchone()
                    log("lobby", f"profile record: z_hid {int(subject_hid):#x} is a "
                                 f"friend-row subject (state "
                                 f"{int(subject_hid) >> 19:#04x}) -> handle {fid}"
                                 + (f" ({h['handle_name']!r})" if h else
                                    " -- WHICH WE DO NOT HAVE"))
                if h is None and subject_hid and friendlist._friend_bitfield_mode() \
                        and 0 < int(subject_hid) < 0x80000:
                    # POL_FRIEND_BITFIELD: the friend row carries the plain
                    # handle id in bits 13-50, so the client's subject is that id
                    # with no "state" bits above it (see `_friend_bitfield_mode`).
                    fid = int(subject_hid)
                    h = db.execute("SELECT * FROM handle WHERE id = %s",
                                   (fid,)).fetchone()
                    log("lobby", f"profile record: z_hid {fid:#x} is a plain "
                                 f"handle id (POL_FRIEND_BITFIELD) -> handle {fid}"
                                 + (f" ({h['handle_name']!r})" if h else
                                    " -- WHICH WE DO NOT HAVE"))
                if h is None and subject_hid and \
                        accounts.looks_like_content_id(subject_hid):
                    # A SUBJECT NAMED BY CONTENT ID. The chat-room member list
                    # identifies a member by the Content ID they are logged in
                    # under, and "View profile" sends it as the z_hid. Measured
                    # live on prod 2026-08-19T00:15: a tester viewed THEMSELF
                    # from a room's member list, z_hid arrived as 0x3b9acbf5 =
                    # 1000000501 = our own minted id (member 5, content 1 --
                    # the then-current computed mint, 1e9 + member*100 + code),
                    # the resolver fell through to "serving the active one",
                    # and the client crashed on the mismatched reply.
                    #
                    # MATCHED AS A NUMBER (2026-08-23). This used to test
                    # `1000000000 <= z_hid <= 1999999999` and look the value up
                    # as `f"{n:010d}"` -- the RETRACTED computed mint's shape
                    # written into the resolver. Content IDs are allocated
                    # 8-digit serials now (accounts.allocate_content_id) and the
                    # 10-digit ones have been migrated away, so the window is
                    # the 8-digit one and the lookup compares numerically rather
                    # than formatting a fixed width.
                    h = accounts.handle_by_content_id(db, subject_hid)
                    log("lobby", f"profile record: z_hid {int(subject_hid):#x} "
                                 f"({int(subject_hid)}) is a CONTENT-ID subject"
                                 + (f" -> handle {h['id']} "
                                    f"({h['handle_name']!r})" if h is not None
                                    else " -- no handle_content row matches"))
                if h is None and subject_hid and                         accounts.looks_like_retired_content_id(subject_hid):
                    # A RETIRED 10-DIGIT CONTENT ID. Not "meaningless" -- it is
                    # a real id we issued before 2026-08-23 and migrated away
                    # (`tools/content_id_migrate.py`). The client caches the
                    # content table from `1:3` at LOGIN, so a player who was
                    # connected across the migration keeps sending the old value
                    # until they relog. Named explicitly, because "names no
                    # handle of ours" would send the next reader looking for a
                    # bug that is really a stale session.
                    log("lobby", f"profile record: z_hid {int(subject_hid):#x} "
                                 f"({int(subject_hid)}) is a RETIRED 10-digit "
                                 f"Content ID -- this client has not relogged "
                                 f"since the migration; serving the active one")
                if h is None and subject_hid and not force_hid and \
                        req_pt is not None and \
                        os.environ.get("POL_PROFILE_NOT_FOUND", "1") == "1":
                    # SOMEBODY ELSE'S PROFILE THAT WE DO NOT HAVE. Serving the
                    # viewer's own record here is the "profile bleed" this
                    # function exists to prevent: the screen draws the wrong
                    # person, and a mismatched record once crashed the client
                    # (the content-id case above). Project Crystal Server
                    # answers "no profile found" (0x7C, POL-5324) instead.
                    # POL_PROFILE_NOT_FOUND=0 serves the active one as before.
                    lobbyrefuse._lobby_refuse(
                        lobbyrefuse.LOBBY_ERR_NO_PROFILE,
                        f"profile record: subject z_hid {int(subject_hid):#x} "
                        f"names no handle of ours", req_pt)
                    return bytes(size)
                if h is None and subject_hid:
                    log("lobby", f"profile record: subject z_hid "
                                 f"{int(subject_hid):#x} names no handle of "
                                 f"ours; serving the active one")
                if h is None:
                    # z_hid 0 means "mine". Which one is mine depends on which
                    # handle the client logged in as -- 4:7 told us.
                    hid = lobbysession._session_handle_id(db)
                    if hid:
                        h = db.execute("SELECT * FROM handle WHERE id = %s",
                                       (hid,)).fetchone()
                if h is not None:
                    fields = accounts.get_handle_profile(db, int(h["id"]))
                    # z_name (field 1) and z_hid (field 2) are IDENTITY, not
                    # profile edits: the client only ever writes them via 0:8
                    # registration, never via a 05:01 profile save. So they are
                    # never in handle_profile and the record went out nameless --
                    # while SE's real record has z_name = "Examplemember" and a
                    # matching z_hid. Fill them from the handle, letting a stored
                    # value win if one somehow exists.
                    fields.setdefault(1, h["handle_name"])
                    fields.setdefault(2, accounts.handle_guid(int(h["id"])))
                    # *** SEARCH HITS SERVE THE TAGGED ROW ID, NOT THE WIRE
                    # GUID. *** SE keeps ONE id space across the profile and the
                    # friend row: its select1 z_hid for Yui is 0x27509D ==
                    # 0x200000 | (identity-row head 0xEA13A040 >> 13) -- the
                    # tagged friend-row subject, not a guid. We served the
                    # 44-bit handle_guid here and a zeroed identity-row rid, two
                    # id spaces that never meet, and the client never posted the
                    # "Let's be friends!" 3:1 after a search-based 2:6 add
                    # (live 2026-08-22, Lex -> Stonefinch: add stored, real guid
                    # in the request, no mail). Scoped to the SEARCH/select
                    # serve (`force_hid`) only -- what SE puts in the OWN-profile
                    # 5:4 z_hid is not yet decoded, so that path is unchanged.
                    # POL_SEARCH_ZHID_TAGGED=0 reverts.
                    tagged = bool(force_hid) and os.environ.get(
                        "POL_SEARCH_ZHID_TAGGED", "1") == "1"
                    if tagged and friendlist._friend_bitfield_mode():
                        # POL_FRIEND_BITFIELD: the id row carries the plain
                        # handle id in bits 13-50, so z_hid is that id.
                        fields[2] = int(h["id"]) & friendlist._FRIEND_HID_BITS
                        log("lobby", f"profile record: search hit z_hid "
                                     f"{int(fields[2]):#x} (handle {h['id']}, "
                                     f"POL_FRIEND_BITFIELD)")
                    elif tagged:
                        fields[2] = friendlist._FRIEND_HID_TAG | (int(h["id"]) & 0x7FFFF)
                        log("lobby", f"profile record: search hit z_hid "
                                     f"TAGGED {int(fields[2]):#x} "
                                     f"(handle {h['id']})")
                    # THE SUBJECT'S OWN FRIEND ROW, for the trailing block --
                    # see `_PROFILE_IDREC_AT`. Built here because this is where
                    # the subject handle is resolved and the database is open.
                    idrec = _profile_identity_record(db, h, tagged=tagged)
                    if _profile_trailer_on():
                        notify = _profile_notify_status(db, h, fields.get(17))
                    log("lobby", f"profile record: serving handle {h['id']} "
                                 f"({h['handle_name']!r}), "
                                 f"{len(fields)} field(s)")
                    # z_pnum (field 30) -- HOW MANY CONTENT PROFILES THIS HANDLE
                    # HAS? WARNING: THAT READING IS A GUESS FROM THE NAME AND NOTHING
                    # ELSE, which is why it is OFF by default.
                    #
                    # The question it exists to answer: the Viewer's handle
                    # screen shows a ROW OF CONTENT SLOTS and, on retail, the
                    # owned ones are clickable and open that content's profile
                    # section. On our server no section appears, and until one
                    # does the client never sends the 284-byte content-profile
                    # request we need to identify. `z_pnum` is the only field in
                    # either schema that looks like it could gate that: one byte,
                    # named "p num", sitting beside `z_utime` in the handle
                    # record and absent from the content record.
                    #
                    # Set POL_PROFILE_PNUM=1 and it carries the count of ACTIVE
                    # Content IDs the handle owns. If the sections then appear,
                    # the guess was right and this becomes measured; if they do
                    # not, we have eliminated the only candidate we had and the
                    # gate is somewhere else. Either outcome is worth one launch,
                    # which is the whole reason to ship it as a switch rather
                    # than as a belief.
                    if os.environ.get("POL_PROFILE_PNUM", "0") == "1":
                        try:
                            n_content = len(accounts.handle_content_list(
                                db, int(h["id"]), active_only=True) or [])
                        except Exception:
                            n_content = 0
                        fields[30] = n_content & 0xFF
                        log("lobby", f"profile record: z_pnum={n_content} "
                                     f"(POL_PROFILE_PNUM=1 -- UNMEASURED guess "
                                     f"that this gates the content sections)")
                    # Serve back the visibility LEVEL the user actually chose,
                    # instead of a constant that silently reverted it each fetch.
                    # It goes on the lead byte and field 0 -- NOT on fields 1..31,
                    # which SE holds at 3 whatever the level is.
                    stored_vis = fields.pop(_PROFILE_VIS_KEY, None)
                    if stored_vis and not level_env:
                        level = int(stored_vis) & 0xFF
            finally:
                db.close()
        except Exception as exc:
            log("lobby", f"profile record: db read failed ({exc!r}); sending empty")
            fields = {}

    # ECHO THE SUBJECT BACK. The 05:04 request is a TLV in the same encoding as
    # the 05:01 write, and it carries exactly one item: id 2, length 8 -- which
    # is schema field 2, `z_hid`, the handle whose profile is being asked for.
    # A reply that leaves z_hid unset is a record for nobody, so the client has
    # nothing to match against the handle it asked about. Anything the request
    # names is echoed, not just z_hid, since the same rule should hold for any
    # subject key the client sends.
    skip = accounts.HANDLE_PROFILE_SKIP if accounts else frozenset()
    echoed = []
    for fid, val in asked.items():
        if fid in skip:
            continue
        if fid not in fields:            # stored data wins over the echo
            fields[fid] = val
            echoed.append(fid)
    if asked:
        # The RAW z_hid, because the two failure modes are both silent without
        # it: z_hid 0 (client had no identity for the clicked member) and
        # z_hid == the viewer's OWN guid (handle-table miss fell to slot 0 =
        # self) both resolve to the active handle with nothing logged, and
        # 2026-08-19T01:05 live they were indistinguishable from a genuine
        # self-view -- the client then refused the mismatched reply as "You do
        # not have access to this profile" (string 11514).
        _zh = asked.get(2)
        log("lobby", f"profile record: request asked for {sorted(asked)}, "
                     f"echoed {sorted(echoed)}"
                     + (f"; z_hid={int(_zh):#x}" if _zh is not None else
                        "; z_hid ABSENT"))

    # The leading byte is not 00 (captured 2026-08-11; the profParsed probe reads
    # it back as `dest+4 = 0x00000001`) -- and it is not a constant 01 either: SE
    # moves it in lockstep with field 0's visibility byte, 1/2/3, as the profile's
    # disclosure level. Both now carry `level`.
    out[0] = level
    off = _PROFILE_LEAD
    served = []
    for idx, name, ln, ty in _PROFILE_SCHEMA:
        step = 1 + ln + _PROFILE_STEP_EXTRA.get(ty, 0)
        if off + step > size:
            log("lobby", f"profile record: {name} would overrun {size}B; stopping")
            break
        # Set the visibility byte for EVERY field, not just the ones we have a
        # value for. A zero byte renders "Private" (app.dll +0x2238CE compares
        # it against the required level with `jl`), and that created a trap the
        # user could not escape from inside the client: unset fields came back
        # Private, the editor loaded that state, and saving wrote Private
        # straight back -- so "make my profile visible to everyone" could never
        # take. An unset field is now visible-but-empty instead of hidden.
        # POL_PROFILE_VIS_ALL=0 restores the old behaviour for A/B.
        val = fields.get(idx)
        # z_phead (field 0) is not stored profile data: SE puts 424 = 0x1A8 there,
        # which is exactly `1 + sum(len+1)`, the record's own used length. Derive it
        # rather than leaving it zero, and let a stored value still win if one is
        # ever captured that disagrees.
        if idx == 0 and val is None:
            val = _PROFILE_LEAD + sum(1 + f[2] for f in _PROFILE_SCHEMA)
        if vis_all or val is not None:
            # Field 0 (z_phead) is the exception in SE's record, but not by holding
            # a constant: its value is 424 = the record's own used length, so it
            # reads like a header, and its visibility byte carries the PROFILE's
            # disclosure level rather than a field's.
            out[off] = level if idx == 0 else vis
        if val is None:
            off += step
            continue
        try:
            if ty == 1:
                raw = str(val).encode("cp932", "replace")[:max(0, ln - 1)]
                blob = raw + b"\x00" * (ln - len(raw))
            elif ty in (2, 4, 5, 6):
                # MASK ON READ TOO, not just on write. Masking the incoming TLV
                # only fixes values stored from now on; the DB still holds the
                # junk written before that fix, and those came back as
                # "z_fav0 value 1835336448 rejected (int too big to convert)" --
                # so the field stayed unset and interests still looked unsaved.
                # A stored value wider than its field is junk from the client's
                # uninitialised stack, and the low bytes are the real value.
                blob = (int(val) & ((1 << (8 * ln)) - 1)).to_bytes(
                    ln, "little", signed=False)
            else:                     # 8/9: opaque, accept hex or int
                blob = (int(val).to_bytes(ln, "little", signed=False)
                        if isinstance(val, int)
                        else bytes.fromhex(str(val))[:ln].ljust(ln, b"\x00"))
        except (ValueError, OverflowError) as exc:
            log("lobby", f"profile record: {name} value {val!r} rejected ({exc}); "
                         "left unset")
            off += step
            continue
        out[off] = level if idx == 0 else vis   # level on z_phead, visibility on the rest
        out[off + 1:off + 1 + ln] = blob
        served.append(name)
        off += step

    # The client's walk and this packing disagreed by 2 bytes from field 1
    # onward (seen via pol-shim's `profParsed` probe: entries 13-17 came back
    # with ASCII from the mail address as their visibility bytes). Dump the head
    # so the offsets we WRITE can be compared against the pointers the client
    # RESOLVES, instead of reasoning about it.
    # *** THE "TAIL IDENTITY BLOCK" WAS FIELD 31 ALL ALONG, AND WRITING IT BY HAND
    # WAS CORRUPTING IT. RETIRED 2026-08-17. ***
    #
    # The 2026-08-11 capture note read the bytes right and the frame wrong:
    #
    #     0x1B8  54DC2CE9 6201BD01   "the handle GUID"
    #     0x1C0  "Examplemember"     "the handle name, 16 bytes NUL-padded"
    #
    # With field 31 packed at its real length (168), its value starts at 0x1A8 --
    # so 0x1B8 is `idrec + 0x10` and 0x1C0 is `idrec + 0x18`, which are exactly
    # the GUID and NAME slots of a 2:3 friend record. The observation was an
    # independent confirmation of `_PROFILE_IDREC_FIELD`, not a separate block.
    #
    # It would ALSO have been harmful had it been on: the guid it stamps is
    # `0x5400000000000000 | hid`, the obsolete format retired when `handle_guid`
    # moved to the 0x800000000xx shape (see the guid note in `_db_friends`), so
    # it would hand every searched member an identity resolving to nobody. Both
    # composes have carried `POL_PROFILE_TAIL: "0"` though, so **it was not the
    # live cause**: in production those bytes were simply ZERO, which is the same
    # failure by omission. The default moves to off so code and deployment agree,
    # and the whole block is superseded by the real record below.
    if os.environ.get("POL_PROFILE_TAIL", "0") == "1" and size >= 0x1D0:
        hid = fields.get(2)
        name = fields.get(1)
        if name:
            struct.pack_into("<Q", out, 0x1B8,
                             (0x5400000000000000 | int(hid or 0))
                             & 0xFFFFFFFFFFFFFFFF)
            raw = str(name).encode("cp932", "replace")[:15]
            out[0x1C0:0x1C0 + len(raw)] = raw
            log("lobby", "profile tail: LEGACY hand-written identity pair at "
                         f"0x1B8/0x1C0 for {name!r}")
    elif idrec and os.environ.get("POL_PROFILE_IDREC", "1") == "1" \
            and size >= _PROFILE_IDREC_AT + _PROFILE_IDREC_LEN:
        # THE SUBJECT'S 2:3 ROW, whole, where SE puts it. Written after the field
        # walk rather than inside it because the fields genuinely END at 0x1A8 --
        # z_phead says 424 on SE's records and on ours, so this is a trailing
        # block and not a 32nd field. See `_PROFILE_IDREC_AT` for why it is the
        # difference between a friend the client can name and one it cannot.
        out[_PROFILE_IDREC_AT:_PROFILE_IDREC_AT + _PROFILE_IDREC_LEN] = idrec
        log("lobby", f"profile tail: identity row at {_PROFILE_IDREC_AT:#x} "
                     f"({_PROFILE_IDREC_LEN}B) -- guid at "
                     f"{_PROFILE_IDREC_AT + 0x10:#x}, name at "
                     f"{_PROFILE_IDREC_AT + 0x18:#x}")
    if notify is not None \
            and size >= _PROFILE_NOTIFY_AT + _PROFILE_NOTIFY_LEN:
        # POL_PROFILE_TRAILER=1 only -- see `_PROFILE_NOTIFY_AT`.
        out[_PROFILE_NOTIFY_AT:_PROFILE_NOTIFY_AT + _PROFILE_NOTIFY_LEN] = notify
        log("lobby", f"profile tail: status block at {_PROFILE_NOTIFY_AT:#x} = "
                     f"{notify.hex()} (POL_PROFILE_TRAILER)")

    log("lobby", "profile record head:\n" + hexdump(bytes(out[:96])))
    log("lobby", f"profile record: {len(served)}/{len(_PROFILE_SCHEMA)} fields set "
                 f"({', '.join(served) if served else 'none'}); vis={vis}"
                 f"{' on ALL fields' if vis_all else ' on set fields only'}; "
                 f"level={level} (lead byte + z_phead: 3 = anyone may view); "
                 f"{off}B used of {size}")
    return bytes(out)


#: A handle/friend entry is 32 bytes. Captured from a real SE session 2026-08-11:
#:
#:     +0x00  u64  GUID          e.g. 54DC2CE9 6201BD01
#:     +0x08  u32  handle id     == the profile record's z_hid (cross-checked:
#:                               0x4B7885 = 4946053 appeared in BOTH)
#:     +0x0C  u32  type flags    0x0800 friend | 0x1400 self | 0x0001 group
#:     +0x10  16B  name, NUL-padded
#:
#: SE's list read: Mirabel(0x800), TombArrington(0x800), Examplemember(0x1400,
#: the account's own handle), LexGroup(0x0001).
FRIEND_ENTRY = 32
FRIEND_SELF = 0x1400
FRIEND_FRIEND = 0x0800
FRIEND_GROUP = 0x0001


def _parse_profile_tlv(pt):
    """{field_id: int|str} from a decrypted 05:01, or None if it does not parse.

    Item = u8 id, u8 JUNK, u16 len, u32 pad, value[len], padded up to 8. The id
    is a BYTE -- read as a u16 it looks random, because the next byte is
    uninitialised client stack. The same junk is inside len=8 values, so those
    are taken as a u32 and the caller should not trust the high bytes of fields
    whose real width is smaller.

    KEY: **A NARROW ITEM (1..4 BYTES) HAS NO VALUE SLOT -- IT IS INLINE IN THE
    HEADER'S SECOND DWORD, AND THE ITEM IS 8 BYTES, NOT 16.** Measured
    2026-09-12 on the first CONTENT-profile write ever captured
    (`lobby-profile-write-unparsed-2026-09-12T175202Z.bin`, a live user
    changing a content profile's Purpose):

        off    id len  value
        0x38    4   8  01 4c 65 78 ..   z_purp = 1  (+ "Lex" stack junk)
        0x48    3   2  --               z_name, empty        <-- 8 BYTES
        0x50    2   8  0x01C9C382       z_ctid
        0x60    1   8  0x01C9C382       z_ctsid
        0x70    0   0  --               terminator

    Padding every length up to 8 stepped 16 from 0x48, landed mid-value at
    0x58, read `len=457` there and bailed -- which is why **no 84-byte 05:01
    has ever parsed** and why that Purpose change never saved.

    WARNING: **THE CHANGE IS SCOPED TO `1 <= len <= 4` AND NOTHING ELSE.** For len 0,
    5..8 and above, `8 + roundup(len, 8)` and this rule give the identical step,
    so the HANDLE form -- which parses today and must go on parsing -- is not
    touched. That is not a hope: this file's own note records a 2-byte handle
    field (`z_fav0`) arriving with **len=8** and four bytes of stack junk behind
    it, so the handle client sends 8 for every narrow scalar and never enters
    this branch.

    WARNING: The INLINE VALUE's position (the dword at `off+4`) is inferred from ONE
    sample, in which it is zero -- so "empty name" and "no value at all" are not
    separated by this capture. The STEP is not inferred: it is the only value
    that lands the walk on the trailer, and the trailer check below is what
    makes that a measurement rather than a preference.

    Returns None unless the walk lands EXACTLY on the checksum trailer. That is
    the only cheap check that the framing held, and a misparse here would write
    garbage into the account, so a failed walk stores nothing.
    """
    # KEY: **TWO STEP RULES, TRIED IN THE ORDER OF THEIR EVIDENCE.** `narrow=False`
    # is the rule every handle write on this server has parsed under since
    # 2026-08-10, so it goes FIRST and nothing that works today can change.
    # `narrow=True` is the one the content form needs (above). A walk is only
    # accepted when it lands EXACTLY on the trailer, so "try both" is a
    # measurement, not a preference -- an 8-byte error cannot land there by
    # chance across a whole message. The caller is told which rule matched.
    for narrow in (False, True):
        out = _walk_profile_tlv(pt, narrow)
        if out is not None:
            if narrow:
                log("lobby", "05:01/05:04 TLV: parsed under the NARROW-ITEM rule "
                             "(a 1..4-byte item is 8 bytes, value inline) -- the "
                             "CONTENT-profile form")
            return out
    return None


def _walk_profile_tlv(pt, narrow):
    """One walk of the TLV under one step rule, or None if it does not land
    exactly on the checksum trailer. See `_parse_profile_tlv`."""
    end = len(pt) - 4
    off, out = lobbyreply._TLV_START, {}
    while off + 8 <= end:
        if struct.unpack_from("<H", pt, off)[0] == lobbyreply._TLV_TAG:   # a section header
            off += 8
            continue
        fid = pt[off]
        ln = struct.unpack_from("<H", pt, off + 2)[0]
        # A narrow item carries its value in the header and occupies 8 bytes; a
        # wide one gets its own 8-aligned slot after it.
        inline = narrow and 1 <= ln <= 4
        if not inline and off + 8 + ln > end:
            return None
        val = pt[off + 4:off + 4 + ln] if inline else pt[off + 8:off + 8 + ln]
        if ln == 8:
            # MASK TO THE FIELD'S REAL WIDTH. Every len=8 item carries 4 bytes of
            # uninitialised client stack after the value, and for narrow fields the
            # junk reaches into the u32 itself. Live 2026-08-11: a 2-byte interest
            # field (z_fav0, type 4) was stored as `interest_1=1835336448`
            # (0x6D670000) -- pure garbage, which then went back out in the 05:04
            # record and is why interests never appeared to save.
            #
            # The TLV id IS the schema index (established as the "Rosetta stone"),
            # so the schema's length is authoritative here. Unknown ids keep the
            # full u32 rather than being dropped -- the id map still has holes.
            #
            # A field whose schema width really IS 8 keeps all 8 bytes: z_hid is
            # the one that matters, and it is a 44-bit handle guid whose top bit
            # sits at 2**43 (a live subject id reads 0x0000_0800_0035_5000). Read
            # as a u32 it truncated to 0x355000 and matched no handle at all.
            width = _PROFILE_WIDTH.get(fid)
            if width == 8:
                v = struct.unpack_from("<Q", val)[0]
            else:
                v = struct.unpack_from("<I", val)[0]
                if width and width < 4:
                    v &= (1 << (8 * width)) - 1
            out[fid] = v
        else:
            out[fid] = val.split(b"\x00")[0].decode("cp932", "replace")
        off += 8 if inline else 8 + ln + (-ln % 8)
    return out if off == end else None


#: Field id the free-text profile COMMENT is stored under in `handle_profile`.
#: Deliberately outside the 0..31 range the 05:04 profile schema uses, because it
#: does NOT come from that record -- it arrives on its own opcode and there is no
#: slot for it in the 32-field schema.
_COMMENT_FIELD = 100

#: 04:03 `KPutMyCommentForFriend`, decoded from the first one ever sent
#: (2026-08-13, the user typing a profile comment):
#:
#:   0000  02 04 03 00 70 00 00 00 ...       type 2, op 4:3, paylen 0x70 = 112
#:   0018  <16-byte session token>
#:   0028  4C 00 4F 00 4C 00 20 00 ...       "LOL This is my comment." UTF-16LE
#:   008E  01 00                             a flag; 1 on the only sample
#:   0094  ed 03 f6 03                       the payload checksum
#:
#: TWO CORRECTIONS TO THE TABLE. The payload is **112 bytes, not the 520** the
#: length table claims (that entry was never measured), and the text is
#: **UTF-16LE** -- every other string in this protocol is cp932, so decoding it
#: the usual way yields "L\0O\0L\0", which is exactly the kind of thing that
#: renders as a stray glyph.
_COMMENT_OFF = 0x28
_COMMENT_MAX = 0x8E - 0x28          # 102 bytes = 51 UTF-16 code units


def _capture_comment(pt):
    """Persist the free-text comment from a 04:03 write.

    Without this the comment was acknowledged and discarded: nothing in this file
    had ever seen a 4:3 (it is absent from the whole log history until now), so
    the client saved a comment, we dropped it, and the profile screen then drew
    an unfilled buffer -- reported as "a hamburger icon and sometimes a random
    letter".
    """
    if accounts is None or len(pt) < _COMMENT_OFF + 2:
        return
    raw = bytes(pt[_COMMENT_OFF:_COMMENT_OFF + _COMMENT_MAX])
    try:
        text = raw.decode("utf-16-le").split("\x00")[0].strip()
    except UnicodeDecodeError:
        log("lobby", "  4:3 comment: not decodable as UTF-16LE; not stored")
        return
    try:
        db = accounts.connect()
        try:
            hid = lobbysession._session_handle_id(db)
            if not hid:
                log("lobby", "  4:3 comment: no handle for this session")
                return
            accounts.set_handle_profile(db, int(hid), {_COMMENT_FIELD: text})
            log("lobby", f"  4:3 comment: stored {text!r} for handle {hid}")
            # SE pushes this to watching friends immediately -- measured live:
            # the account holder saw a friend's comment change to "banana" and
            # back with no refresh. Event 0 carries the NEW VALUE as its text.
            pushspool.broadcast_event(db, int(hid), pushchannel._PUSH_EV_PROFILE, text)
        finally:
            db.close()
    except Exception as exc:
        log("lobby", f"  4:3 comment: store failed ({exc!r})")


def _store_content_profile_write(fields, pt):
    """Persist a CONTENT-form 05:01 -- the Viewer editing one game character.

    Keyed by Content ID, beside the game's own `<GR>` write, because that is
    what the profile IS (`_content_profile_note`). The two are different
    CHANNELS for the same record -- `<GR>` is the game telling us about itself
    on the PolPro band, this is the player editing it in the Viewer -- so both
    are kept and the NEWER one wins at read time.

    WARNING: Only ids this content's own schema declares are stored, and never the
    three we author (`z_phead`, `z_ctsid`, `z_ctid`): the subject is the
    request's own operands and must be echoed, not remembered. An id outside
    the schema is logged and dropped rather than kept "just in case" -- it
    would be served back into a slot whose meaning we do not know.
    """
    cid = fields.get(2)
    code = contentprofiles._content_code_for_cid(cid) if cid else None
    if not cid or code is None:
        log("lobby", f"05:01 CONTENT profile write for {cid!r}: no content "
                     f"code (nothing links that Content ID) -- not stored")
        return
    schema, _rl, _ph, measured = contentprofiles._content_schema_for(code)
    known = {i for i, _n, _l, _t in schema} - {0, 1, 2}
    keep = {f: v for f, v in fields.items()
            if f not in (0, 1, 2) and measured and f in known}
    dropped = [f for f in fields
               if f not in (0, 1, 2) and f not in keep]
    if not keep:
        log("lobby", f"05:01 CONTENT profile write for {cid}: nothing storable "
                     f"(ids {sorted(dropped)} are not in content {code}'s "
                     f"schema) -- not stored")
        return
    try:
        data = pfc._content_profiles()
        rec = dict(data.get(str(int(cid))) or {})
        # WARNING: MERGE, do not replace. The client sends only the screen it just
        # saved, so a write that carries Purpose alone must not erase a name
        # an earlier write set -- and `<GR>`'s groups have to survive it.
        merged = dict(rec.get("fields") or {})
        merged.update({str(f): v for f, v in keep.items()})
        rec["fields"] = merged
        rec["fields_seen"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        data[str(int(cid))] = rec
        path = pfc._content_profile_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
        names = {i: n for i, n, _l, _t in schema}
        log("lobby", f"05:01 CONTENT profile write STORED for Content ID {cid} "
                     f"(content {code}): " + ", ".join(
                         f"{names.get(f, f)}={v!r}" for f, v in sorted(keep.items()))
                     + (f"; dropped {sorted(dropped)} (not in this schema)"
                        if dropped else ""))
    except Exception as exc:
        log("lobby", f"05:01 CONTENT profile write for {cid}: not stored ({exc!r})")


def _capture_profile_write(pt):
    """Persist a 05:01 profile write. Read-only with respect to the protocol.

    WARNING: **A 05:01 HAS TWO FORMS, THE SAME WAY 05:04 DOES, AND ONLY ONE OF THEM
    BELONGS IN `handle_profile`.** Measured on prod 2026-09-12: editing a
    CONTENT profile's Purpose in the Viewer sends a **84-byte-payload 05:01**
    (124B decrypted) which this parser refuses -- `05:01 profile write did not
    parse; nothing stored`, the live report's "I changed Purpose for the game and it
    didn't save". The same 84-byte form appears on 2026-09-06T23:32 from a
    different client, and **no 84-byte 05:01 has ever parsed**; the handle form
    (612B and 252B payloads in the same logs) parses fine. So the discriminator
    is the form, not a corrupt frame.

    WARNING: **AND THE PARSE FAILURE HAS BEEN PROTECTING US.** The TLV ids are schema
    field indices OF THE RECORD BEING WRITTEN, so in the CONTENT form id 1 is
    `z_ctsid`, 2 is `z_ctid` and 4 is `z_purp` -- while in the HANDLE schema
    those same ids are `z_name`, `z_hid` and `z_age_f`. Had it parsed, this
    function would have written a Content ID into the handle's name, a
    content-service id into `z_hid`, and the purpose into the age flag. Refuse
    the content form explicitly rather than relying on a parser bug to do it:
    `_is_content_profile_request` is the same (ids 1 AND 2 present) test 05:04
    already uses.

    The 84 bytes themselves are still UNREAD -- prod's capture ring
    (`POL_CAPTURE_KEEP`) had rotated both of them out before anyone looked -- so
    an unparsable write now SAVES ITS DECRYPTED FRAME under its own capture
    name, which is the same "fixed for next time, not solved" the unknown `1:10`
    arm uses. Until those bytes are read, the content form cannot be stored:
    guessing the framing is how a wrong field silently lands on a screen.
    """
    fields = _parse_profile_tlv(pt)
    if not fields:
        cap = save_capture("lobby-profile-write-unparsed", bytes(pt))
        log("lobby", f"05:01 profile write did not parse; nothing stored "
                     f"(payload {max(0, len(pt) - 40)}B, decrypted {len(pt)}B) "
                     f"-- frame saved as {cap}. WARNING: 84B is the CONTENT-profile "
                     f"form (see this function's docstring); read the capture "
                     f"with tools/lobby_tlv.py before adding a branch for it")
        return
    if contentprofiles._is_content_profile_request(pt):
        _store_content_profile_write(fields, pt)
        return
    try:
        db = accounts.connect()
        try:
            # THE ACTIVE HANDLE, not "the first member". A 05:01 names no handle
            # -- SE scopes it to whoever you are logged in as -- so this used to
            # be `SELECT id FROM member ORDER BY id LIMIT 1`, which sent every
            # account's every profile edit to member 1's single row.
            hid = lobbysession._session_handle_id(db)
            if hid is None:
                log("lobby", "05:01 profile write: no active handle; nothing stored")
                return
            if len(pt) > 0x2A and pt[0x2A]:
                fields[_PROFILE_VIS_KEY] = int(pt[0x2A])
            accounts.set_handle_profile(db, hid, fields)
            row = db.execute("SELECT handle_name FROM handle WHERE id = %s",
                             (hid,)).fetchone()
            named = ", ".join(
                f"{accounts.HANDLE_PROFILE_FIELDS.get(k, hex(k))}={v!r}"
                for k, v in sorted(fields.items())
                if k not in accounts.HANDLE_PROFILE_SKIP)
            log("lobby", f"profile write CAPTURED for handle {hid} "
                         f"({row['handle_name'] if row else '?'}, "
                         f"visibility={pt[0x2A]}): {named}")
            # Same live push as the 4:3 comment. A 5:1 can carry several fields
            # at once; the capture only ever showed ONE value in the text field,
            # so send the comment if this write touched it and otherwise say
            # which field moved rather than inventing a multi-field record.
            if _COMMENT_FIELD in fields:
                pushspool.broadcast_event(db, hid, pushchannel._PUSH_EV_PROFILE,
                                str(fields[_COMMENT_FIELD]))
            elif fields:
                changed = accounts.HANDLE_PROFILE_FIELDS.get(
                    sorted(fields)[0], "profile")
                pushspool.broadcast_event(db, hid, pushchannel._PUSH_EV_PROFILE, str(changed))
        finally:
            db.close()
    except Exception as exc:
        log("lobby", f"profile capture failed: {exc!r}")


#: Where the 4:7 request carries the active handle: one flag byte, then the name.
#: 0x28 is the start of the PAYLOAD proper (the same 0x28 the 07:01 note derives),
#: so the whole 4:7 payload is `<flag><handle name>` and nothing else.
_ACTIVE_HANDLE_OFF = 0x28


# --------------------------------------------------------------------------- #
# The lobby opcode table's entries for the profile (see lobbyops.py)
# --------------------------------------------------------------------------- #
def capture_profile_write(pt):
    """5:1 profile write."""
    _capture_profile_write(pt)
    return True


def capture_comment(pt):
    """4:3 KPutMyCommentForFriend."""
    _capture_comment(pt)
    return True


def paylen_profile(req_pt):
    """5:4 SERVES TWO DIFFERENT RECORDS AND THE LENGTH DEPENDS ON THE REQUEST,
    which a fixed table entry cannot express -- so the table's 604 is right for
    the handle profile and wrong in KIND for the other one. Measured on retail:
    a one-item TLV gets 604 (600 record + 4), a two-item TLV gets 284 (280 + 4).
    Declaring 604 to a content request is a 320-byte overrun of what the client
    will read, i.e. exactly the shape of the bug the 604 entry itself fixed.

    AND THE CONTENT RECORD'S LENGTH IS PER TITLE, not a constant 280.
    `prof_<code>.pib` carries it: 280 FFXI, 432 TM, 432 code 3, 280 FMO,
    240 (code 10), 464 FE. Serving 284 to a title that reads 436 is the
    same class of defect as the 604-to-a-content-request above, one layer
    down. See `_CONTENT_SCHEMAS`. The POL_LOBBY_PAYLEN override still wins.
    """
    if not contentprofiles._is_content_profile_request(req_pt):
        return None
    forced = paylen.paylen_override(0x05, 0x04)
    if forced is not None:
        return forced
    _s, rec_len, _p, _m = contentprofiles._content_schema_for(
        contentprofiles._content_code_for_request(req_pt))
    return rec_len + 4


def payload_profile(n, req_pt):
    """5:4 profile read-back: the CONTENT profile when the request is the
    two-item TLV (see `_is_content_profile_request`; `n` is 284 = 280 record
    + checksum), else the handle profile served unconditionally from the DB.
    This is normal operation, not a probe, so it is deliberately NOT behind
    POL_LOBBY_TAIL. `n` is 604 = 600 record + the 4-byte checksum the caller
    appends, so the record itself is n-4."""
    if contentprofiles._is_content_profile_request(req_pt):
        return contentprofiles._content_profile_record(n - 4, req_pt)
    return _profile_record(n - 4, req_pt)
