"""The friend list as served: records, slot map, database rows."""
import os
import struct
import time
import threading
from srvcore import log
from .deps import accounts
from . import handlelists, lobbymail, lobbysession, presence, profilerecord



def _peer_online(peer_handle):
    """Is this friend logged in right now? Presence = a live session row."""
    if accounts is None or not peer_handle:
        return False
    try:
        db = accounts.connect()
        try:
            return accounts.handle_online(db, int(peer_handle))
        finally:
            db.close()
    except Exception:
        return False


def _friend_handle_slots():
    """`{guid: handle-table slot}` from the SAME list 0:9 serves.

    Built ONCE per reply, not once per record: `_handle_list_entries()` opens
    the database (twice, and now three times with the face-icon lookup), so
    calling it inside the record loop made a 12-friend list do dozens of
    connections for one fetch -- and this database is WAL on a Windows bind
    mount, which is exactly the churn that has corrupted it before.

    A friend absent from the map has no handle-table entry (POL_LOBBY_FRIEND_HANDLES
    off), and the caller then leaves the record's slot bits alone.
    """
    try:
        return {int(g): int(slot) for slot, g, _name, _fi in handlelists._handle_list_entries()}
    except Exception:
        return {}          # a slot we cannot name is exactly the "leave it 0" case


#: The bit the client ORs into a profile subject it took out of a friend row.
#: `0x200000 | (head >> 13)`, measured -- see the head-word note in
#: `_friend_list_record`.
_FRIEND_HID_TAG = 0x200000


#: 🔬 **POL_FRIEND_BITFIELD -- THE FRIEND ROW'S FIRST 8 BYTES ARE ONE u64.
#: DEFAULT OFF, NOT YET CHECKED AGAINST A CLIENT.**
#:
#: Project Crystal Server reads the 168-byte friend row (FriendData) as
#:
#:     bit 0      valid (clear = a delete, in a 2:6)
#:     bit 4      ignore list
#:     bits 5-6   level (disclosure scope)
#:     bits 7-12  handle slot                    (what we already write there)
#:     bits 13-50 HANDLE ID, 38 bits
#:     bits 57-60 group
#:     bit 62     temporary (a pending request)
#:     +0x08 creation position, +0x09 custom position
#:
#: and SE's bytes agree without exception. What this file has called the
#: "state byte" at +0x04 is bits 32-39, i.e. handle-id bits 19-26: SE's ids are
#: 23-bit numbers, and the "0x200000 tag" is just their high bits.
#:   * Fox's profile row head 0x238EA040 with +0x04 = 0x0A gives
#:     (0x0A << 19) | (0x238EA040 >> 13) = 0x511C75 = the SAME record's z_hid
#:     (prof-ffxi-retail-20260823.log), and 0x511C75 is also Fox's group-member
#:     word in SE's 7:12.
#:   * Cyn 0x27509D = (4 << 19) | 0x7509D, Wiccaan 0x20F64B: +0x04 low byte 04.
#:   * JackAlexender's pending word 0x5A000009: byte 7 = 0x5A = bit 62 set
#:     (temporary) + group 0xD. The settled 0x06000004 is group 3, bit 62 clear.
#:   * SE's 2:6 frames (tools/friend_rename_test.py) have the header Crystal
#:     describes -- u16 count at frame 0x154 (1, and 2 in the two-record write) --
#:     and each row starting at 0x158: our "state dword in front of the grid" is
#:     the row's bitfield. The ignore-scope bytes 21/31/51 are valid+level 1,
#:     valid+ignore+level 1, valid+ignore+level 2; SE's deletes carry 0xE2 =
#:     bit 0 clear. Our own clients agree: all 78 logged 2:6 records in
#:     logs/lobby.log* (dev, 08-15..08-25) have bit 0 set on the 24 with a
#:     name and clear on the 54 with heap in the name field.
#:
#: With the knob: the served head is the full u64 -- the real handle id in bits
#: 13-50 (for our ids that zeroes the old "state byte"), level and ignore from
#: the bytes the client last wrote for the row (`friend.ignore_low`), group and
#: the pending bit kept from the SE words we replay today. So the client's
#: profile subject becomes the plain handle id, which `_profile_record` accepts
#: under the same knob; the 2:6 reply's fresh-row id and the search-hit id row
#: follow. `POL_FRIEND_BITFIELD=1` also serves +0x09 = +0x08 (SE's rows always
#: carry equal bytes there); `=head` changes only the u64. WARNING: Mirroring +0x09
#: cost a login once (POL-0008, 2026-08-16, cause never separated -- see
#: POL_FRIEND_ROWSLOT9), so A/B `head` first. Incoming 2:6: a row with bit 0
#: clear is ALSO taken as a delete, and every record where that and the
#: name-field heuristic disagree is logged.
#: A/B: relog with it on, open a friend's profile and a pending row's profile,
#: add, rename, ignore and delete a friend; compare the lobby log's resolver
#: and `2:6 bitfield` lines with the knob off.
_FRIEND_HID_BITS = (1 << 38) - 1


def _friend_bitfield_mode():
    """None (off), "full" (u64 + positions) or "head" (u64 only)."""
    v = os.environ.get("POL_FRIEND_BITFIELD", "0").strip().lower()
    if v in ("1", "full"):
        return "full"
    if v == "head":
        return "head"
    return None


def _friend_row_bitfield(hid, handle_slot, state_word, flags_low=None):
    """The u64 at friend row +0x00 -- see `_friend_bitfield_mode`.

    `state_word` is the +0x04 dword we would have replayed; its group bits and
    pending bit are kept, its low byte (old "state") is replaced by the real
    handle-id bits. `flags_low` is the row's stored low byte from the client's
    own 2:6, if any, for the ignore and level bits.
    """
    level = 1                                   # SE's settled rows: 0x21
    ignore = 0
    if flags_low is not None:
        level = (int(flags_low) >> 5) & 3
        ignore = (int(flags_low) >> 4) & 1
    word = 1 | (ignore << 4) | (level << 5)
    if handle_slot is not None:
        word |= (int(handle_slot) & 0x3F) << 7
    word |= (int(hid or 0) & _FRIEND_HID_BITS) << 13
    word |= (int(state_word) & 0xFE000000) << 32          # group, bit 62, bit 63
    return word & 0xFFFFFFFFFFFFFFFF


def _friend_flags_low_by_row():
    """`{friend row id: stored ignore_low}` for rows that have one."""
    if accounts is None:
        return {}
    try:
        db = accounts.connect()
        try:
            return {int(r["id"]): int(r["ignore_low"]) for r in db.execute(
                "SELECT id, ignore_low FROM friend WHERE ignore_low IS NOT NULL")}
        finally:
            db.close()
    except Exception:
        return {}


def _friend_row_handles(blob):
    """Every handle id a 02:06 frame echoes back in a friend-row head word.

    The head word we serve is `(SE's low 13 bits) | (handle_id << 13)`, and a
    DELETE hands part of its copy of the row back to us. Scanning for words whose
    low 13 bits match SE's is what turns that echo into "which friend", without
    needing an id the client minted.
    """
    out, low = [], 0x1EC96021 & 0x1FFF
    for at in range(0, len(blob) - 3):
        word = int.from_bytes(bytes(blob[at:at + 4]), "little")
        if (word & 0x1FFF) == low and (word >> 13):
            out.append(word >> 13)
    return out


#: KEY: **AN ACCEPTANCE HAS NO BODY, AND THAT IS WHAT MAKES IT AN ACCEPTANCE.**
#: The same structural rule the group notifications follow (see `_group_message`):
#: a REQUEST carries a body, an ACCEPTANCE carries none. Every sample in the
#: 2026-08-15 capture agrees, with no exceptions --
#:
#:     objlen 48   "Let's be friends!"            "Would you like to be friends?"
#:     objlen 48   "Let's be friends!"            "Would you like to be friends?"
#:     objlen 30   "Friend registration accepted" <EMPTY>
#:     objlen 30   "Friend registration accepted" <EMPTY>
#:     objlen 30   "Friend registration accepted" <EMPTY>
#:
#: WARNING: **WE MINTED A 69-BYTE ACCEPTANCE WITH A 39-BYTE BODY** (`"<name> accepted
#: your friend request."`), so it had the shape of a REQUEST. Keeping the body
#: empty is still right -- it is what SE sends, every sample.
#:
#: WARNING:WARNING: **BUT THE BODY WAS NOT WHY THE CLIENT OFFERED TO RESEND. RETRACTED
#: 2026-08-17.** This block used to claim the 39-byte body explained both the
#: "marking the acceptance as read offers to send the request again" report and
#: the older *"Fox is not waiting for friend registration"* one. It explains
#: neither, and the empty-body fix did not close them:
#:
#:   * the prompt is StringTable 18161 and it is raised by an IDENTITY lookup --
#:     `0x4aadaf3` keys the sender by (record +0x00/+0x04 guid, record +0x3C
#:     slot) and `0x488ddba` misses both the friend and ignore arrays. The
#:     OBJECT is never read on that path, so its length cannot reach the
#:     decision. See `_PROFILE_IDREC_AT` for the real cause and the fix.
#:   * and it was observed again AFTER this fix: lobby.log 2026-08-17T02:45
#:     mints a 30-byte empty-body acceptance and the report stands.
#:
#: `reconcile_pending`'s `skip` deferral is left in place -- it is still right to
#: let a client close its own loop when it can -- but neither of its stated
#: justifications survives.
#:
#: The wording is SE's own, verbatim. The subject is truncated to 15 bytes for the
#: record field at +0x20 by `_mail_mint`, exactly as SE's is; the OBJECT carries
#: it in full, which is why "Let's be friend" (the old value, already truncated at
#: the source) was wrong in the object too.
_FRIEND_REQ_SUBJECT = "Let's be friends!"
_FRIEND_REQ_BODY = "Would you like to be friends?"
_FRIEND_ACC_SUBJECT = "Friend registration accepted"
_FRIEND_ACC_BODY = ""


#: 🔬 THROWAWAY PROBE, NOT A FIX -- the 2:3 friend guid, overridden per name.
#:
#: WHY. Tetra Master's room member menu greys "Ask to become friends" from
#: `TM 0x166130`, which walks TM's cache of polcore's friend table and matches a
#: node whose key `+0x08/+0x0C` equals the ROOM ROW's POL-ID. Measured live
#: 2026-08-21 with a menu-gate probe: both sides are healthy and the
#: two ids are simply different numbers for one person --
#:
#:     LaptopTest2 in the friend list : 0x000008000000000D  (handle_guid(13))
#:     LaptopTest2 in the room roster : 0xAB12CD56EB0F5932  (their @Init=/NN=)
#:
#: The node key holds what polcore STORED, which is `served ^ K`, so the value
#: the compare wants is `POL_ID ^ K`. K is per-account (polcore 0x37DEA60 derives
#: it from the client's own id), so THIS CAN NEVER BE THE REAL FIX -- it is a
#: one-account, one-friend falsification test of the whole id-mapping
#: chain. Read the screen, then take it out.
#:
#: Set it WITHOUT a restart, in `logs/presence.ctl` (same reader as every other
#: live knob -- a new env var would need declaring in both composes and is
#: silently absent otherwise):
#:
#:     guidmap=LaptopTest2:AB12CD56809F33DF
#:
#: comma-separated for more than one; the name is the friend's REAL name (not a
#: rename caption), the value is hex, `0x` optional. Absent/empty = inert.
_FRIEND_GUIDMAP = {"raw": None, "map": {}}


def _friend_guid_probe(name, guid):
    """The 2:3 guid to serve for `name` -- the probe override, or `guid`."""
    raw = (presence._presence_cfg("guidmap", "POL_FRIEND_GUIDMAP", "") or "").strip()
    if raw != _FRIEND_GUIDMAP["raw"]:
        m = {}
        for tok in raw.split(","):
            tok = tok.strip()
            if not tok or ":" not in tok:
                continue
            k, v = tok.split(":", 1)
            try:
                m[k.strip()] = int(v.strip(), 16)
            except ValueError:
                log("lobby", f"  2:3 guidmap: cannot parse {tok!r} as name:hex")
        _FRIEND_GUIDMAP["raw"], _FRIEND_GUIDMAP["map"] = raw, m
        if m:
            log("lobby", "  2:3 guidmap ARMED (probe, not a fix): " + ", ".join(
                f"{k}->0x{v:016X}" for k, v in m.items()))
    over = _FRIEND_GUIDMAP["map"].get(str(name))
    if over is None or int(over) == int(guid):
        return guid
    log("lobby", f"  2:3 record: guid for {name!r} OVERRIDDEN "
                 f"0x{int(guid):016X} -> 0x{int(over):016X} (guidmap probe)")
    return int(over)


def _friend_list_record(rec_size, guid, name, kind=0x0800, hid=0, status=0,
                        online=False, index=0, handle_slot=None, face_icon=0,
                        row_id=0, flags_low=None):
    """One record for the 1:3 / 2:3 friend-list family.

    Layout read straight off a real SE capture (2026-08-11), 2:3 record:

        +0x00  16B  flags/ids -- values seen: 2160C91E 04000006 02020000 00000000
        +0x10  u64  GUID   (matches the same friend's guid in the 32-byte list)
        +0x18  16B  name, NUL-padded            e.g. "Wiccaan"

    The 104-byte (1:3) record carries a name at the SAME +0x18 with two dwords at
    +0x0C/+0x10 that also appear in the profile record's tail, so it reads as the
    HANDLE record while 2:3 is the FRIEND record. Only the fields we can name are
    written; the +0x00 block stays zero because its meaning is unknown and a wrong
    guess there is worse than an empty one.
    """
    r = bytearray(rec_size)
    if rec_size >= 0x28:
        # SLOTPROBE=2: pack the record index into the guid's SLOT BITS. 0:9's
        # +0x08 is documented as "a 44-bit guid + 6-bit slot", and every friend
        # guid we serve shares one base, so those bits are zero for all of them
        # -- which is what "only the last friend renders" looks like. Bit 0 of
        # +0x00 was NOT it (tried, still only the last), so this is the other
        # place a per-record slot can live.
        gout = guid & 0xFFFFFFFFFFFFFFFF
        if os.environ.get("POL_FRIEND_SLOTPROBE", "0") == "2":
            gout = (gout & ~(0x3F << 44)) | ((int(index) & 0x3F) << 44)
        struct.pack_into("<Q", r, 0x10, gout)
        raw = str(name).encode("cp932", "replace")[:15]
        r[0x18:0x18 + len(raw)] = raw
        # THE FRIEND'S HANDLE ID goes somewhere in the 16 bytes at +0x00, and we
        # do not know which dword. Evidence: opening a friend's profile makes the
        # client send 05:04 with z_hid = 0 -- it has no id for them, so the server
        # answers with the caller's own profile. SE fills this block
        # (2160C91E 04000006 02020000 00000000) and we zero it.
        #
        # POL_FRIEND_IDPROBE=1 writes a DIFFERENT tagged value into each of the
        # four dwords, so the next 05:04 request names the one the client read:
        #   +0x00 = 0xAA<<24 | hid    +0x08 = 0xCC<<24 | hid
        #   +0x04 = 0xBB<<24 | hid    +0x0C = 0xDD<<24 | hid
        # One login identifies the field outright instead of four guesses.
        # STATUS. SE's 168-byte records differ from each other at +0x04, and the
        # user's live ground truth named the states (2026-08-11):
        #     0900005A  friend request PENDING   (JackAlexender)
        #     04000006  OFFLINE                  (Wiccaan, Gazlo)
        # Both share a 2160-prefixed dword at +0x00. We do not know the field's
        # internal structure, so this replays the two OBSERVED values verbatim
        # rather than inventing an encoding -- an exact replay of a state we have
        # seen is defensible; a synthesised one is not. ONLINE is not yet known,
        # since every friend in the capture was offline or pending.
        # ONLINE is the one state never captured, so it is the ONE value here we
        # cannot replay -- POL_FRIEND_ONLINE makes it sweepable without a
        # rebuild, the same idea as POL_FRIEND_IDPROBE above. The client's own
        # presence enum is 1 = Online / 2 = Offline (read off FriendList.dll at
        # 0x100a2c20, which selects strings 23393/23397 for 1 and 23394/23398 for
        # 2), but that is the value AFTER this dword has been decoded, not the
        # dword itself -- so the mapping from +0x04 to it is still open.
        # Default keeps the observed OFFLINE bytes with the low byte set to 1,
        # which is the smallest change consistent with both captures having a
        # small enum in byte 0 (offline 04, pending 09).
        if rec_size >= 0x08:
            # THE RECORD INDEX. Confirmed live 2026-08-13: with 12 friends the
            # client showed only the 12th, with 6 only the 6th -- always the
            # LAST, whatever the status byte. Every record lands in one slot and
            # each overwrites the previous, which is exactly what an identical
            # per-record index does. 0:9 and 1:3 both carry that index in BYTE 0
            # of the record ("MUST be < 0x40"); this record has the same shape
            # and we were serving SE's captured byte (0x21) for all of them.
            # POL_FRIEND_SLOTPROBE=1 varies byte 0 by record index instead.
            head = 0x1EC96021
            # BITS 13..31 ARE THE FRIEND'S ID, AND THIS IS THE PROFILE BLEED.
            #
            # Solved 2026-08-16 by arithmetic on two numbers we already had. Every
            # 05:04 in every log, across three accounts and two installs, asks for
            # the same `z_hid 0x20F64B` -- and
            #
            #     0x200000 | (0x1EC96021 >> 13) == 0x20F64B
            #
            # exactly. `0x1EC96021` is SE's captured head word, which we replay
            # verbatim into EVERY friend row. So the client is not carrying a
            # stale cached id at all: it reads the subject out of this field, and
            # we hand every friend the same one. It then asks for a profile we
            # cannot resolve, and `_profile_record` falls back to the caller's --
            # which is the bleed, ours all along.
            #
            # Putting the friend's own handle id here gives each row a distinct,
            # resolvable subject. Bits 0..12 are left EXACTLY as SE sends them:
            # byte 0 is the record index and bits 7..12 are the handle slot below.
            if hid and os.environ.get("POL_FRIEND_ROW_TAG", "1") == "1":
                head = (head & 0x1FFF) | ((int(hid) & 0x7FFFF) << 13)
            if os.environ.get("POL_FRIEND_SLOTPROBE", "0") == "1":
                head = (head & 0xFFFFFF00) | (int(index) & 0x3F)
            # THE FRIEND'S HANDLE SLOT -- bits 7..12 of THIS dword. Read off the
            # two consumers, 2026-08-13, and cross-checked on three fields we
            # already knew (guid at +0x10, name at +0x18, presence at +0x09 all
            # land where this same function says they do):
            #
            #   polcore 0x37df001 (the 2:3 record-copy, dest = the 0xB0 friend
            #   slot):   dest+0x9c = ((record[0x00] & 0x1F80) << 6) | (old & ...)
            #   -- i.e. the dest 64-bit word at +0x98 takes record bits 7..12 into
            #   its OWN bits 45..50.
            #
            #   app.dll 0x48781c9 (dHandleList::fill) and 0x4894e14 (CFriendData):
            #   both do `slot = (word >> 45) & 0x3F` and keep it at +0x44 -- the
            #   `event[+0x44]` the row action feeds GetHandleWord/GetHandleRecord.
            #
            # So bits 7..12 here ARE +0x44. SE's captured head (0x1EC96021) has
            # them zero, which is why every row's slot read 0 = SELF. Only set
            # when the friend actually HAS a handle-table entry (0:9 must have
            # served it, i.e. POL_LOBBY_FRIEND_HANDLES=1) -- pointing a row at a
            # slot 0:9 never filled is strictly worse than leaving it at 0.
            if handle_slot is not None and \
                    os.environ.get("POL_FRIEND_HSLOT", "1") == "1":
                head = (head & ~(0x3F << 7)) | ((int(handle_slot) & 0x3F) << 7)
            struct.pack_into("<I", r, 0x00, head)
            # THE ONE-RECORD COLLAPSE, SOLVED 2026-08-13 by reading polcore's 2:3
            # reply parser (memimg 0x037e39ab..0x037e3a37). Each 168-byte record is
            # copied into slot `byte[+0x08]` of a fixed table (dest = base +
            # record[+0x08]*0xB0), so with +0x08 = 0 for every friend they all land
            # in slot 0 and each overwrites the last -- exactly "only the final
            # friend renders". The slot lives at +0x08, NOT byte 0 (SLOTPROBE=1) and
            # NOT the guid's slot bits (SLOTPROBE=2); both earlier probes missed
            # because they never touched +0x08. The record index (0..count-1, and
            # the list is capped at 12) gives each friend a distinct slot.
            #     037e39ca  mov al, byte ptr [ebp + 8]   ; ebp = record base
            #     037e39cf  lea edi,[eax+eax*4] .. shl edi,4   ; slot * 0xB0
            #     037e39d8  add edi, 0x38740d8                 ; table base
            #     037e3a2f  add ebp, 0xa8                      ; next record (+168)
            # (bit 4 of record[+0x00] picks the alternate table 0x386fc18 -- an
            # online/offline grouping, not the collapse. Left at 0 for now.)
            r[0x08] = int(index) & 0x3F
            # PRESENCE, SOLVED 2026-08-13. Byte +0x09 is the online/offline enum.
            # polcore's record-copy reads it directly --
            #     037def0c  mov al, byte ptr [edi + 9]   ; edi = record base
            #     037def10  and eax, 0x1ff               ; a 9-bit status field
            # -- and SE's own captured 2:3 record proves the value: Wiccaan was
            # OFFLINE and carried +0x09 = 0x02 (the +0x08 block reads "02 02 00 00":
            # +0x08 = slot, +0x09 = 0x02). The client's presence enum is
            # 1 = Online, 2 = Offline (FriendList.dll 0x100a2c20), so 0x02 = Offline
            # matches exactly. We were serving +0x09 = 0, which is not a valid enum,
            # so EVERY friend rendered offline even when logged in (seen live: an
            # online PS2Tester showed offline). This is the field; +0x04 does NOT
            # drive presence (SE's offline and our online guess 0x06000001 differ
            # there yet both render offline), so +0x04 is left at SE's observed
            # bytes and only +0x09 carries the state.
            # *** +0x09 IS NOT PRESENCE. RETRACTED 2026-08-16. ***
            #
            # The "SOLVED 2026-08-13" reading above rests on ONE record in which
            # +0x08 and +0x09 both read 0x02, taken as "slot 2, enum 2 = Offline".
            # Decoding every 2:3 record in BOTH SE captures shows the two bytes are
            # never independent -- six records, six times equal:
            #
            #   JackAlexender +0x08=00 +0x09=00      Cyn   @136076 00 / 00
            #   Wiccaan       +0x08=02 +0x09=02      Cyn   @155268 00 / 00
            #   Gazlo         +0x08=01 +0x09=01      Yatih @155268 01 / 01
            #
            # and they are not the list index either (Wiccaan is record 1 carrying
            # 2, Gazlo record 2 carrying 1), so both bytes are one slot number that
            # SE assigns.
            #
            # WARNING: BUT MIRRORING +0x08 HERE COST A LOGIN (POL-0008, 2026-08-16), so it
            # is NOT enabled by default. Two candidate reasons, unseparated: SE's
            # capture never had more than THREE friends, so +0x09 was only ever
            # observed as 0/1/2, while this list reaches 7 -- and the consumer masks
            # it to 9 bits (`and eax, 0x1ff`) as a status field, so a large value
            # may be a state the client refuses. The retraction of "+0x09 IS the
            # presence enum" stands on its own (six records, six times equal to
            # +0x08); what to write instead does not.
            #
            # POL_FRIEND_ROWSLOT9=1 re-enables the mirror for a clean A/B.
            if os.environ.get("POL_FRIEND_ROWSLOT9", "0") == "1":
                r[0x09] = r[0x08]
            else:
                r[0x09] = 1 if online else 2
            pend = int(status or 0) == 1
            if pend:
                state = 0x5A000009                            # observed PENDING
            elif os.environ.get("POL_FRIEND_STATEPROBE", "0") == "1":
                # ONE-LOGIN SWEEP for the ONLINE value. Byte 0 of this dword is
                # the state enum in both captures (offline 04, pending 09), so
                # give record i the candidate i+1 and let the UI say which one
                # renders as Online. Seed dummy friends named S01.. to read the
                # answer straight off the screen. Beats one login per guess.
                base = int(os.environ.get("POL_FRIEND_STATEBASE", "1"), 0)
                state = 0x06000000 | ((base + index) & 0xFF)
            elif online:
                # *** +0x04 BYTE 0 IS THE STATE, AND ONLINE IS 6. ***
                #
                # This branch and the offline one below both sent 0x06000004, so
                # presence could not vary no matter what `online` said -- which is
                # the whole "everyone shows offline" report. The sweep below was
                # built to find the online value one login at a time; SE's own
                # bytes give it directly. Every 2:3 record in both captures, with
                # the state each one is independently known to be in:
                #
                #   +0x04 dword   byte 0   who              state
                #   5A000009        09     JackAlexender    pending  (documented)
                #   02000009        09     Gazlo            pending
                #   06000004        04     Wiccaan          OFFLINE  (documented)
                #   04000004        04     Cyn  @136076     offline
                #   02000004        04     Cyn  @155268     offline
                #   02000006        06     Yatih@155268     ONLINE
                #
                # 4 = offline, 6 = online, 9 = pending. Yatih is the one online
                # sample and it is a good one: the narration has the friend going
                # offline on `Cyn` and reappearing on `Yatih`, and the two rows sit
                # in the SAME reply with different byte 0 -- so it is a within-
                # capture contrast, not a comparison across sessions.
                #
                # Bytes 1..3 are left exactly as they were. They differ across
                # accounts (5A/06/04/02) with no relation to state, so they are
                # somebody else's field and this changes only the byte named above.
                # *** REFUTED 2026-08-16, AND REVERTED TO SE'S OBSERVED VALUE. ***
                #
                # The table above read 4/6/9 as offline/online/pending, with "6 =
                # online" resting on ONE record (Yatih) assumed online from the
                # narration. `Cyn` settles it against that reading: byte 0 is
                #
                #   04 at line 136076 -- Cyn ONLINE, actively chatting
                #   04 at line 155268 -- Cyn OFFLINE, having switched to Yatih
                #
                # Same friend, same value, opposite states. So this byte does not
                # carry presence; whatever distinguishes Yatih (whose whole head
                # word differs too) is some other property. Serving 6 for an online
                # friend was unfounded, and it did not move the client either way.
                #
                # 0x06000004 is what SE sends for an ordinary friend in both
                # states, so it is what we send in both states.
                state = int(os.environ.get("POL_FRIEND_ONLINE", "0x06000004"), 0)
            else:
                state = 0x06000004                            # observed OFFLINE
            struct.pack_into("<I", r, 0x04, state)
            bf = _friend_bitfield_mode()
            if bf:
                # POL_FRIEND_BITFIELD -- the whole u64, see the note there.
                hs = handle_slot if os.environ.get(
                    "POL_FRIEND_HSLOT", "1") == "1" else None
                struct.pack_into("<Q", r, 0x00, _friend_row_bitfield(
                    hid if os.environ.get("POL_FRIEND_ROW_TAG", "1") == "1"
                    else 0, hs, state, flags_low))
                if bf == "full":
                    r[0x09] = r[0x08]            # custom position = creation
        # WHERE IS THE COMMENT? The 2:3 record is 168 bytes and this builder
        # fills only +0x00 (flags), +0x10 (guid) and +0x18 (name). WARNING: Since
        # 2026-09-02 the 2:3 SERVE PATH overlays the 8x16 per-content block at
        # +0x28..0xA8 after this returns (`_IDREC_CONTENT_AT`, the TM friend
        # "View Profile" fix; POL_FRIEND_CONTENTS=0 reverts) -- so the probes
        # below now collide with it there. +0x28 was also where a ~100-byte
        # UTF-16 comment would fit, and the comment IS shown on the friend list. POL_FRIEND_PROBE=1 writes
        # a UTF-16LE offset label at the start of every 16-byte row from +0x28,
        # each NUL-terminated, so whichever offset the client reads a string
        # from, the text it draws NAMES that offset. One login identifies the
        # field instead of a sweep -- the same idea as POL_FRIEND_IDPROBE above
        # and the marker payloads used to map the account record.
        #
        # UTF-16LE because that is what 04:03 carries on the way IN, and what
        # the group name turned out to be; if nothing renders, re-run with
        # POL_FRIEND_PROBE=2 for cp932 labels instead.
        #
        # STRONG PRIOR ON THE OFFSET: the UI caps a comment at 50 characters =
        # 100 bytes UTF-16LE + a terminator = 102, which is EXACTLY _COMMENT_MAX
        # (0x8E - 0x28) in the 04:03 write path. Same width, and 0x28 is the
        # first free byte here too -- so +0x28..+0x8E is the field to beat.
        probe = os.environ.get("POL_FRIEND_PROBE", "0")
        if probe in ("1", "2") and rec_size >= 0xA8:
            for off in range(0x28, rec_size - 8, 0x10):
                label = "%04X" % off
                raw = (label.encode("utf-16-le") + b"\x00\x00" if probe == "1"
                       else label.encode("cp932") + b"\x00")
                r[off:off + len(raw)] = raw
        # WHERE IS THE FACE ICON? The friend list draws no picture while the
        # account holder's OWN badge draws one correctly -- measured 2026-08-15 --
        # so the icon pipeline works (0:9 record +0x04 -> handle_slot_record
        # +0x1C -> badge) and the friend ROW reads it from somewhere else. Serving
        # friends in the 0:9 handle table with their icons AND pointing each row's
        # +0x44 slot at them changed nothing, so it is not the handle table.
        # That leaves this record's 128 untouched bytes.
        #
        #   POL_FRIEND_ICONPROBE=all     the icon in EVERY free dword at once.
        #                                ONE login answers the only question worth
        #                                asking first: is the field in this record
        #                                AT ALL? Pictures appear -> yes, bisect.
        #                                Nothing -> no, stop looking here.
        #   POL_FRIEND_ICONPROBE=sweep   row i gets the icon at ONE offset, the
        #                                i'th free dword, and nothing elsewhere.
        #                                Whichever row draws a picture names its
        #                                own offset. POL_FRIEND_ICONBASE picks
        #                                which dword row 0 starts at, so 32
        #                                candidates take ceil(32/rows) logins.
        #
        # The value written is the friend's REAL stored icon, so a rendered
        # picture is recognisable rather than just "something appeared".
        iprobe = os.environ.get("POL_FRIEND_ICONPROBE", "0").strip().lower()
        if iprobe in ("all", "sweep") and rec_size >= 0xA8 and face_icon:
            free = list(range(0x28, rec_size - 4, 4))
            if iprobe == "all":
                for off in free:
                    struct.pack_into("<I", r, off, int(face_icon) & 0xFFFFFFFF)
            else:
                try:
                    ibase = int(os.environ.get("POL_FRIEND_ICONBASE", "0"), 0)
                except ValueError:
                    ibase = 0
                k = ibase + int(index)
                if 0 <= k < len(free):
                    struct.pack_into("<I", r, free[k],
                                     int(face_icon) & 0xFFFFFFFF)
                    log("lobby", f"  ICONPROBE row {index} ({name!r}): icon "
                                 f"{face_icon} at record +0x{free[k]:02X}")
        if os.environ.get("POL_FRIEND_IDPROBE", "0") == "1" and hid:
            for i, tag in enumerate((0xAA, 0xBB, 0xCC, 0xDD)):
                struct.pack_into("<I", r, i * 4,
                                 (tag << 24) | (int(hid) & 0xFFFFFF))
        # THE MISTAKE THIS REPLACES, made and watched inside the hour: the first
        # cut wrote a row tag into all four dwords of this block "because which
        # one the client keeps is unknown". Three of the four are known and
        # load-bearing -- +0x04 is the STATUS word and +0x08 is the DESTINATION
        # SLOT -- so every record landed in slot 0x21 and the account holder's
        # eleven friends rendered as one. That is the one-record collapse
        # documented below, caused again from the other end. Only +0x00 is ours
        # to fill, and only its bits 13..31.
        # NO hid is written into this record any more. Writing it at +0x04 was a
        # guess, and it clobbered the STATUS field above. The capture settled it:
        # the friend's handle id (0x004C0EEF for "Popoto Gaz") appears ONLY in the
        # 604-byte profile reply, in none of 0:9 / 1:3 / 2:3 / 4:6 -- so the client
        # does not learn it from this record and there is nothing here to put it in.
        # See the send-side probe for how it actually names a friend.
    return bytes(r)


def _db_friends(kinds=None):
    """[(guid, name, kind, peer_handle, status)] for the session's primary handle.

    `kinds` filters by the wire flag field. GROUPS (0x0001) are NOT friends: SE's
    capture puts them in the compact 32-byte list alongside friends, but the
    168-byte 2:3 records held only people. So the friend list asks for 0x0800 and
    the compact list takes everything.
    """
    if accounts is None:
        return []
    try:
        db = accounts.connect()
        try:
            mid = lobbysession._session_member_id()
            h = db.execute("SELECT id FROM handle WHERE member_id = %s"
                           " ORDER BY is_primary DESC, id ASC LIMIT 1",
                           (mid,)).fetchone() if mid else None
            if h is None:
                return []
            # status=None so PENDING entries are listed too. SE's own capture had
            # a pending friend (JackAlexender) sitting in the 2:3 list with the
            # 0900005A status word -- "awaiting approval" is a row in the list,
            # not an absence from it. Filtering to active hid every friend request
            # the moment it was made, which reads as "adding a friend did nothing".
            #
            # **OUTGOING ONLY, THOUGH.** The list holds friends you have and
            # people YOU asked; it does not hold people who asked YOU. There is
            # only one pending word on the wire (0900005A) and it renders as
            # "Awaiting confirmation", so serving an INCOMING request here tells
            # the recipient they sent a request they never sent -- reported live
            # 2026-08-16, and with no way to accept or delete it from that screen.
            # An incoming request reaches its recipient as a MESSAGE instead;
            # that is what SE's capture shows and it is why its 2:3 never carried
            # one. POL_FRIEND_LIST_INVITED=1 puts them back.
            # CLOSE ANY REQUEST THEY HAVE ALREADY ACCEPTED, before reading the
            # list. The accept is two steps and the asker's half waits on a
            # notification that -- measured 2026-08-16 -- is not always sent by
            # anyone, leaving the row on "Awaiting confirmation" for good. See
            # `accounts.reconcile_pending`, which no-ops unless something is
            # actually pending and steps aside when the acceptance mail is on.
            # Hold off on anyone whose acceptance is still sitting UNREAD in
            # this mailbox -- their client is about to close its own row, and
            # promoting it first is what makes the client offer to RESEND.
            healed = accounts.reconcile_pending(
                db, int(h["id"]),
                skip=lobbymail._unread_notice_senders(lobbysession._session_member_id(),
                                            lobbymail.MAIL_KIND_FRIEND_ACCEPTED))
            if healed:
                log("lobby", f"  2:3 friends: {len(healed)} request(s) they had "
                             f"already accepted, closed now [{', '.join(healed)}]"
                             " -- nothing had told this client about it")
            rows = accounts.list_friends(db, int(h["id"]), status=None)
            if os.environ.get("POL_FRIEND_LIST_INVITED", "0") != "1":
                hidden = [r["peer_name"] for r in rows
                          if r["status"] == accounts.STATUS_INVITED]
                rows = [r for r in rows
                        if r["status"] != accounts.STATUS_INVITED]
                if hidden:
                    log("lobby", f"  2:3 friends: {len(hidden)} incoming request(s) "
                                 f"not listed [{', '.join(hidden)}] -- an incoming "
                                 "request is a MESSAGE, not a friend-list row")
            out = []
            for r in rows:
                if kinds is not None and int(r["kind"]) not in kinds:
                    continue
                # THE GUID IS DERIVED LIVE for a local friend, exactly as
                # `live_name` overrides the stored name. The client echoes
                # whatever guid we put here back as `z_hid` when it opens that
                # friend's profile, so a STALE stored value resolves to nobody
                # and `_profile_record` falls back to the session handle --
                # serving YOUR profile under THEIR name. Seen live 2026-08-12:
                # rows seeded before the guid format changed still held
                # 0x5400000000000006 while handle_guid(6) is 0x80000000006, and
                # every profile opened from the friend list was the viewer's own.
                # Deriving it means the stored column can never drift again.
                guid = int(r["peer_guid"] or 0)
                if r["peer_handle"]:
                    guid = accounts.handle_guid(int(r["peer_handle"]))
                    # IDENTITY ALIGNMENT (2026-08-21, tm-member-sidebar-identity;
                    # PROVEN and defaulted ON 2026-08-22). The TM room member
                    # menu's friend gate TRANSFORMS the room POL-ID before
                    # searching TM's friend cache (TM 0x402B7 -> 0x19FD50 ->
                    # polcore cft 313/352-355): POLID ^ CONST(0xAB12CDD0E4BCBB90,
                    # baked in TM.dll at [0x52AF828]) ^ K, base36 round-trip,
                    # repack, ^ K -- the K's cancel and the net compare is
                    #     served_2:3_guid == member's CLIENT guid.
                    # Verified against all 12 SE dev-table pairs (TM 0x51B3958)
                    # and both live accounts (Fox 0xE13883D826, LaptopTest2
                    # 0x860FB3E2A2); the transform of Fox's POL-ID reproduces
                    # polcore's measured own_id exactly. Serving handle_guid
                    # (0x80000000d-space) can never match, which is the whole
                    # "Ask to become friends offered for an existing friend" bug.
                    # The 2026-08-21 "disproven live" run of this flag is
                    # retracted: the knob was in neither compose's environment
                    # block, so the container never saw it (the .env-substitution
                    # trap), and the test also predated
                    # the ~22 s TM cache race.
                    # Flows into the 0:9 handle table (built from this list), is
                    # echoed as the profile z_hid (_profile_record resolves by
                    # client guid) and in every 2:6 record (friend_row_by_guid
                    # matches client_guid too). POL_FRIEND_GUID_CLIENT=0
                    # restores handle_guid. NOT YET CONFIRMED ON SCREEN: needs
                    # a clean-order live test (fresh login, no cached list).
                    if os.environ.get("POL_FRIEND_GUID_CLIENT", "1") == "1":
                        cg = db.execute(
                            "SELECT client_guid FROM handle WHERE id = %s",
                            (int(r["peer_handle"]),)).fetchone()
                        if cg and cg["client_guid"]:
                            guid = int(cg["client_guid"])
                # THE LABEL IS A SEVENTH FIELD, NOT A SUBSTITUTED SECOND ONE.
                # It has to reach the 2:3 record (a rename that vanished at the
                # next login would not be a rename), but element 1 is the row's
                # real name and several things downstream key on it -- the
                # served slot map, `_FRIEND_PUT_ASSIGNED`, the guid resolution
                # in `_friend_put_reply`. Substituting the caption there would
                # make every one of them look up a person who does not exist.
                out.append((guid, r["live_name"] or r["peer_name"],
                            int(r["kind"]), int(r["peer_handle"] or 0),
                            r["status"], int(r["id"]),
                            (r["label"] if "label" in r.keys() else None)))
            return out
        finally:
            db.close()
    except Exception as exc:
        log("lobby", f"friend list: DB unavailable ({exc})")
        return []


# --------------------------------------------------------------------------- #
# THE SERVED SLOT MAP -- what a 2:6 delete's +0x04 actually indexes (2026-08-16)
#
# **THE CLIENT NEVER RENUMBERS.** It parses a 2:3 reply by copying each record to
# `table_base + record[+0x08] * 0xB0` (see friend-list-one-record-bug) and a
# delete just names that same slot back. Nothing re-reads the list: a delete is
# answered with the 184-byte echo, the row is hidden LOCALLY, and the slots of
# everyone below it DO NOT MOVE for the rest of the session.
#
# So the slot is an index into the list AS SERVED, not into the list as it now
# stands, and resolving it against a fresh `_db_friends` was off by one more row
# after every delete. Measured live (logs/lobby.log 2026-08-16T07:14): four
# deletes, no 2:3 between them, records byte-identical but for +0x04 = 2, 4, 3, 5.
#
#     served (7): [ ., ., S03, ., S05, PS2Tester, . ]
#     +0x04=2 -> re-query 7 rows -> S03        correct, it was the first delete
#     +0x04=4 -> re-query 6 rows -> PS2Tester  WRONG, slot 4 was S05
#     +0x04=3 -> re-query 5 rows -> S05        WRONG, slot 3 was another row
#     +0x04=5 -> re-query 4 rows -> refused    "slot 5, but we served only 4"
#
# which is exactly the reported symptom: the client hides the row you clicked, so
# it looks right until you log out, and the relog shows whoever the shifted index
# actually named. The out-of-range refusal at the end is the same bug run out of
# list -- a renumbering client could never send a slot past its own count.
#
# The fix is to remember the ordering we SERVED and resolve against that. Keyed
# by the owning handle rather than the session id, because that is the scope the
# list itself has (`_db_friends` reads the session member's primary handle) and
# it survives a sid this thread never bound.
_FRIEND_SLOTS_LOCK = threading.Lock()
#: handle_id -> {"at": ts, "slots": {slot: (name, friend-row id)}}
_FRIEND_SLOTS = {}
_FRIEND_SLOTS_TTL = 24 * 3600


def _friend_list_handle_id():
    """The handle whose friend list 2:3 serves -- the session's PRIMARY handle.

    Deliberately the same query `_db_friends` and `_capture_friend_put` use, so a
    slot map cannot be filed under a different owner than the list it describes.
    """
    if accounts is None:
        return None
    try:
        db = accounts.connect()
        try:
            mid = lobbysession._session_member_id()
            if not mid:
                return None
            h = db.execute("SELECT id FROM handle WHERE member_id = %s"
                           " ORDER BY is_primary DESC, id ASC LIMIT 1",
                           (mid,)).fetchone()
            return int(h["id"]) if h else None
        finally:
            db.close()
    except Exception:
        return None


def _friend_slots_publish(served):
    """Record the slot -> row mapping a 2:3 reply just handed the client.

    `served` is [(slot, name, row id)] for the records ACTUALLY emitted -- not
    the rows we meant to send, since a short payload can stop the record loop
    early and the client only ever sees what arrived.

    A fresh 2:3 REPLACES the map: that reply renumbers the client's table, so
    every slot it did not mention is gone from the client's view too.
    """
    hid = _friend_list_handle_id()
    if hid is None:
        return
    now = time.time()
    with _FRIEND_SLOTS_LOCK:
        for k in [k for k, v in _FRIEND_SLOTS.items()
                  if now - v["at"] > _FRIEND_SLOTS_TTL]:
            del _FRIEND_SLOTS[k]
        _FRIEND_SLOTS[hid] = {
            "at": now,
            "slots": {int(s): (nm, int(rid)) for s, nm, rid in served},
        }


def _friend_slots_map(handle_id):
    """The slot map we served this handle, or None if we have not served one.

    None is not the same as an empty map: it means this process never answered a
    2:3 for the handle (a restart mid-session, or the split-service layout), and
    the caller falls back to the live list -- which is right for the FIRST delete
    of a session and wrong only for the ones this map exists to fix.
    """
    with _FRIEND_SLOTS_LOCK:
        ent = _FRIEND_SLOTS.get(int(handle_id))
        return dict(ent["slots"]) if ent else None


#: [(name, slot)] the last 02:06 write was given, for `_friend_put_reply` to hand
#: back. Cleared and refilled per write, like `_LAST_FRIEND_PUT` beside it.
_FRIEND_PUT_ASSIGNED = []

#: {name: guid} for the same write -- the REAL guid of each named peer, which the
#: client's own 2:6 request leaves 0 for a search-based
#: add. `_friend_put_reply` writes it into the
#: echoed record's +0x10 so the Put-response table rebuild keys the match entry
#: by a real guid instead of 0, which is what SE's reply carries. Cleared and
#: refilled per write beside `_FRIEND_PUT_ASSIGNED`.
_FRIEND_PUT_GUIDS = {}


def _friend_slots_assign(handle_id, wanted, row):
    """Give `row` = (name, db row id) a slot, honouring `wanted` where it can.

    THE CLIENT PICKS ITS OWN SLOT FOR AN ADD and gets it wrong the moment it makes
    two adds without a 2:3 in between. It fills the lowest hole the SERVED list
    left and does not count its own previous add, so the second add lands on top
    of the first and the first vanishes from its table. Measured 2026-08-16: two
    requests a minute apart, both records `+0x04 = 01`, and only the second friend
    rendered. The store had both -- a relog brought them back -- so this is about
    the client's table, not the data.

    SE's capture cannot contradict this: its two adds have a 2:3 between them
    (lines 73345, 136076, 141215), so its client had already been renumbered and
    every slot it sent was ALREADY the one this function would hand back. All four
    of its writes reproduce byte for byte either way.

    Returns the slot assigned. Bounded by the 2:3 record cap -- a slot the list
    reply could never serve is no use to the client's table.
    """
    cap = handlelists._LOBBY_LIST[(0x02, 0x03)][2]
    with _FRIEND_SLOTS_LOCK:
        ent = _FRIEND_SLOTS.get(int(handle_id))
        if ent is None:
            return int(wanted)          # nothing served, nothing to reconcile
        slots = ent["slots"]
        held = slots.get(int(wanted))
        if held is None or int(held[1]) == int(row[1]):
            slots[int(wanted)] = (row[0], int(row[1]))
            return int(wanted)
        free = next((s for s in range(cap) if s not in slots), None)
        if free is None:
            # The client's table is full as far as the list reply is concerned.
            # Honour what it asked for rather than invent a slot 2:3 cannot serve.
            slots[int(wanted)] = (row[0], int(row[1]))
            return int(wanted)
        slots[free] = (row[0], int(row[1]))
    log("lobby", f"  2:6 friend write: {row[0]!r} asked for slot {wanted}, which "
                 f"is {held[0]!r} -- assigning {free} and telling the client in "
                 "the reply (POL_FRIEND_PUT_SLOT_FIX=0 to just echo)")
    return int(free)


def _friend_slots_update(handle_id, slot, row):
    """Point one slot at `row` = (name, row id), or drop it when `row` is None.

    Dropping is what a completed delete does. The slot stays EMPTY rather than
    shifting, because the client's table does not shift either -- and a second
    delete naming the same slot must then match nothing instead of reaching the
    row that moved up into it.
    """
    with _FRIEND_SLOTS_LOCK:
        ent = _FRIEND_SLOTS.get(int(handle_id))
        if ent is None:
            return
        if row is None:
            ent["slots"].pop(int(slot), None)
        else:
            ent["slots"][int(slot)] = (row[0], int(row[1]))


def _friend_entry(guid, hid, flags, name):
    """One 32-byte handle/friend entry."""
    e = bytearray(profilerecord.FRIEND_ENTRY)
    struct.pack_into("<Q", e, 0, guid & 0xFFFFFFFFFFFFFFFF)
    struct.pack_into("<I", e, 8, hid & 0xFFFFFFFF)
    struct.pack_into("<I", e, 12, flags & 0xFFFFFFFF)
    raw = str(name).encode("cp932", "replace")[:15]
    e[16:16 + len(raw)] = raw
    return bytes(e)


def _friend_payload(n):
    """The 4:6 friends reply: the owning handle plus its friends, 32 bytes each.

    Order follows SE's capture -- the owner's own handle sits IN the list (kind
    0x1400) alongside friends (0x0800), with groups (0x0001) trailing.

    An all-zero reply parses as entries with a NULL guid and an EMPTY name, which
    is what a blank friend looks like on screen, so every entry served here is
    either real or not served at all.
    """
    out = bytearray(n)
    cap = max(0, n // profilerecord.FRIEND_ENTRY)
    entries, self_name = [], None
    if accounts is not None:
        try:
            db = accounts.connect()
            try:
                mid = lobbysession._session_member_id()
                h = db.execute(
                    "SELECT id, handle_name FROM handle WHERE member_id = %s"
                    " ORDER BY is_primary DESC, id ASC LIMIT 1",
                    (mid,)).fetchone() if mid else None
                if h is not None:
                    self_name = h["handle_name"]
                    entries.append(_friend_entry(
                        0x5400000000000000 | int(h["id"]), int(h["id"]),
                        profilerecord.FRIEND_SELF, self_name))
                    for r in accounts.list_friends(db, int(h["id"])):
                        # A local friend's CURRENT handle name wins, so renaming
                        # an account updates everyone's list without a rewrite.
                        name = r["live_name"] or r["peer_name"]
                        entries.append(_friend_entry(
                            int(r["peer_guid"]),
                            int(r["peer_handle"] or 0),
                            int(r["kind"]), name))
            finally:
                db.close()
        except Exception as exc:
            log("lobby", f"friends: DB unavailable ({exc})")
    for i, e in enumerate(entries[:cap]):
        out[i * profilerecord.FRIEND_ENTRY:(i + 1) * profilerecord.FRIEND_ENTRY] = e
    if len(entries) > cap:
        log("lobby", f"  friends 4:6: {len(entries)} entries but only {cap} fit "
                     f"in {n}B -- TRUNCATED (the reply size is fixed by the client)")
    log("lobby", f"  friends 4:6: {min(len(entries), cap)}/{len(entries)} entries "
                 f"of {profilerecord.FRIEND_ENTRY}B in {n}B (self={self_name})")
    return bytes(out)
