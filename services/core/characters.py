"""Character records and the character write (1:A)."""
import os
import struct
import titles                   # the title-plugin seam (services/titles.py)  # noqa: E402
from srvcore import log, save_capture
from .deps import accounts
from . import ffxifields, friendgroups, handlelists, lobbybind



def _character_names():
    """`{(content_id, content_code): character name}`, or {} -- never raises.

    A name we cannot look up must degrade to "no name", not to a failed char
    list: this feeds the record that unlocks every game's Run button, and an
    exception here would take the whole list down over a cosmetic field.
    """
    if accounts is None:
        return {}
    try:
        db = accounts.connect(os.environ.get("POL_ACCOUNTS_DB", accounts.DEFAULT_DB))
        try:
            return accounts.character_names(db)
        finally:
            db.close()
    except Exception as exc:
        log("authserv", f"  char list: character names unavailable ({exc!r}) "
                        f"-- falling back per game")
        return {}


def _char_display_name(content_code, content_id, handle_name="", names=None):
    """The 15-byte string at char-record +0x18: **the CHARACTER's name.**

    CONFIRMED BY THE ACCOUNT HOLDER 2026-08-23 against retail: that box is the
    character name, and the Viewer's per-Content-ID sections under a handle read
    it out of this list. Two pieces of evidence already pointed the same way and
    neither was enough on its own:

      * `polcontent.py` on the live SE-connected Viewer read slot 0 (FFXI,
        present, real Content ID) with this string **EMPTY** -- a Content ID
        registered with no character created yet. SE does not put digits here.
      * Tetra Master's chain is measured end to end (the title module's POOL_CSID_IS_CID
        banner): +0x18 -> the pool struct -> the `<CR>` cname -> `@Init=/CN=`.

    We were serving the Content ID DIGITS for every game but Tetra Master, and
    TM was only right because a separate bug forced it: `@ComGameInit` carries no
    name field, so a VS. COM board labels the human seat from this string, and
    digits there produced the "name banner shows 1000001602" bug.

    Order, and why:
      1. `content_character` -- the game's real character name, per Content ID.
         For FFXI that is the bridge's `lsb/ffxi_idmap.json` imported by
         `tools/ffxi_names.py`, whose docstring named this UI as its consumer.
      2. Tetra Master's observed pool `cname` -- what the client itself echoed
         back on `<CR>`, which beats anything derived.
      3. TM ONLY: the bound handle's name, then the digits. **TM must never be
         empty** -- that string is READ, not merely displayed, and an empty seat
         banner would be a fresh bug in the scene the digits bug was found in.
      4. Everything else: **empty**, which is what SE served for a character that
         does not exist. Safe because the games read the NUMERIC fields --
         FFXI matches on +0x04/+0x08 and the PS2 title logs `Chara No` / `ContentsID`
         out of +0x04/+0x08; neither reads +0x18.

    `POL_CHAR_NAME=0` restores the digits everywhere (the pre-2026-08-23
    behaviour); `POL_CHAR_NAME_HANDLE=0` drops TM's handle-name fallback.
    """
    if os.environ.get("POL_CHAR_NAME", "1") != "1":
        return content_id
    cid = accounts.content_id_int(content_id) if accounts else None
    if names and cid is not None:
        got = names.get((cid, int(content_code)))
        if got:
            return got
    if content_code == 2:
        pooled = titles.character_name(cid)
        if pooled:
            return pooled
        if handle_name and os.environ.get("POL_CHAR_NAME_HANDLE", "1") == "1":
            return handle_name
        return content_id              # never empty -- the VS. COM seat banner
    # WARNING: EMPTY IS NOT A SAFE DEFAULT HERE, and shipping it cost the FFXI slot.
    #
    # This branch used to `return ""` for every non-TM content, reasoning that
    # SE serves an empty +0x18 and so should we. SE does -- but ONLY for a
    # Content ID with no character on it. Where a character exists SE sends its
    # NAME (`Lexffxi`, in SE's own retail bytes at +0x18). We have no character
    # names for most contents, so "be like SE" emptied EVERY slot, and the
    # client reads a nameless entry as "no character": FMO's own gate is
    # literally *a character exists iff some slot carries a non-empty name*
    # (`fmo.has_named_character`). Live testing reported the FFXI slot missing
    # within hours.
    #
    # So: a real character name if we know one, else the handle name, else the
    # digits -- anything rather than nothing. This is strictly better than the
    # pre-2026-08-23 behaviour (digits everywhere) and still shows a real name
    # wherever one exists. POL_CHAR_NAME_EMPTY=1 restores the SE-shaped empty
    # for anyone testing the "does a blank hide the slot?" question directly.
    if os.environ.get("POL_CHAR_NAME_EMPTY", "0") == "1":
        return ""
    if handle_name and os.environ.get("POL_CHAR_NAME_HANDLE", "1") == "1":
        return handle_name
    return content_id


def _char_record(rec, index, slot, pos, content_code, content_id="",
                 handle_name="", names=None, bind=True):
    """One 1:3 (`KGetChrList`) record: a CHARACTER, i.e. one Content ID linked to
    one handle. **This is the record that unlocks a game's Run button.**

    Layout read straight off polcore's consumer loop (0x37e247e..0x37e26d0,
    stride 0x68). Through the body `edi` points at record+0x0C, so the `[edi-N]`
    operands below are record offsets, not negative ones:

        +0x00  u8   character index, MUST be < 0x40 -- the slot in the 64-entry
                    table at 0x3bc3080, and the value the handle binding stores
        +0x01  u8   -> packed field bits 0..7
        +0x04  u8   BIND FLAG: non-zero binds this character to a handle, and is
                    also packed at bit 15
        +0x05  u8   HANDLE SLOT (must be < 0x40) -- packed at bits 16..21 and,
                    masked to 3 bits, again at bits 22..24
        +0x06  u8   POSITION 0..7 within that handle's 8-byte binding array
        +0x08  u16  **CONTENT CODE** -> table[+0x02]
        +0x0A  u8   packed at bit 26, but ONLY when the content code is >= 4
        +0x0B  u8   packed at bits 34..41
        +0x0C  u32  -> table[+0x04]
        +0x10  u32  -> table[+0x08]
        +0x14  u32  -> table[+0x0C]
        +0x18  15B  string -> table[+0x18]

    and for each record the loop sets `table[+0x00] |= 1` (the PRESENT bit), then
    when +0x04 is non-zero writes the binding

        handle_slot[+0x20 + rec[0x06]] = ((rec[0x00] & 0x3F) << 1) | (old & 0x81)

    into the SAME 0x3bc5800 handle table that 0:9 fills (stride 40), which is why
    `slot` here must be the handle's 0:9 slot.

    Why only these fields are set: the launch gate is `app.dll+0x199093`, which
    walks all 64 table entries via polcore's getter (function-table slot +0x2C4,
    polcore+0x227D0) and returns true iff some entry has the present bit AND a
    u16 at +0x02 equal to the content id it was asked about. Its two callers are
    the `al == 0` refusals at app.dll+0x1DEDBD / +0x1DEE21 that build string
    26073, "You have no Content ID for %s" -- so an empty table is exactly the
    refusal the user sees on every Run button. Everything the gate needs is
    +0x00, +0x04, +0x05, +0x06 and +0x08; the rest stays zero until something is
    observed reading it.

    The 15-byte string at +0x18 is the CHARACTER'S NAME -- see
    `_char_display_name`, which chooses it. It is not read by the launch gate.
    (It held the Content ID DIGITS until 2026-08-23, on the reasoning that they
    were "the natural fit for the Content ID list UI". They are not: that list is
    what the Viewer's per-Content-ID sections under a handle render, and the
    account holder confirmed against retail that the box is the character name.)

    WARNING: Tetra Master is the case that PROVED it, and the only one where the
    string is READ rather than displayed. The chain is measured, not inferred (the title module's
    POOL_CSID_IS_CID banner): record+0x18 -> the pool struct 0x5245298+0x18 ->
    the `<CR>` cname the client echoes back verbatim, and `@Init=/CN=` is the
    hex-ASCII of the same bytes. `@ComGameInit` carries no name field, so a
    VS. COM board labels the human seat from this string -- serving the Content
    ID digits here is exactly the "name banner shows 1000001602" bug. PvP never
    showed it because `@VsGameInit=/CN=` is server-authored per seat. So for
    content code 2 the bound handle's name goes in the string instead
    (`handle_name`, from the caller's `_db_handles()`); the NUMERIC identity
    below stays the Content ID either way, which is what the ranking row match
    (+0x0C/+0x10/+0x14) compares. Scoped to TM only because what SE served at
    +0x18 for other titles is unverified, and FFXI and the PS2 title demonstrably read
    only the numeric fields. `POL_CHAR_NAME_HANDLE=0` restores the digits.
    """
    out = bytearray(rec)
    out[0x00] = index & 0x3F
    # +0x01 -- the ORDER byte, and the reason the PS2 Viewer sent lobby 1:10 on
    # every list load. Read off the console's own core 2026-09-11 (pol.pex
    # `polpex_0014e6d0`): after a 1:3 the console DEDUPES this byte across the
    # present records and, if it had to reassign one, marks the list dirty and
    # writes it back as 1:10 (`sqprofdb: Update chlist`). We served 0 in every
    # record, so any account with two characters tripped it every time. The
    # console itself assigns unique values 0..63 here, so the slot index is
    # exactly the value it would have chosen. POL_CHAR_ORDER=0 restores the old
    # zero (the PC twin packs this at table bits 0..7; nothing has been seen
    # reading it there). Memory: ps2-lobby-1-10-is-sqprofdb-update-chlist.
    if os.environ.get("POL_CHAR_ORDER", "index") != "0":
        out[0x01] = index & 0x3F
    # +0x04 IS THE BINDING, AND IT IS OPTIONAL. The consumer loop sets the
    # table's present bit for every record and writes the handle binding only
    # `when +0x04 is non-zero`, so a record with 0 here is still in the 64-slot
    # table -- still passes the launch gate (app.dll+0x199093 wants the present
    # bit and the content code, nothing else) and still satisfies FFXI's world
    # lookup. That is exactly what a handle's ninth-and-later Content ID needs:
    # the binding array is eight bytes and `pos` is three bits, so a ninth bound
    # record would not get a slot of its own, it would OVERWRITE position 0 and
    # take the first character out of the handle's Content ID list to show
    # itself. Unbound costs the profile-view entry and nothing else.
    # See `_db_chars`, which decides which records overflow.
    out[0x04] = 1 if bind else 0
    out[0x05] = slot & 0x3F
    out[0x06] = (pos & 0x07) if bind else 0
    struct.pack_into("<H", out, 0x08, content_code & 0xFFFF)
    # +0x02 -- UNDOCUMENTED IN OUR OWN LAYOUT, AND SE FILLS IT. Her retail FFXI
    # record has `7F 00` here; we send zeros. Seven bits set in a field nobody
    # has characterised is a candidate for the thing that is still missing: the
    # handle profile's per-content SECTIONS. Restoring the character name at
    # +0x18 brought the Content ID LIST back (verified live) and left the
    # sections empty, so whatever gates them is elsewhere in this record, and
    # this is the only field we know SE sets and we do not.
    #
    # WARNING: PURE GUESS AS TO MEANING -- off by default, exactly like the z_pnum
    # experiment, which earned its keep by failing cleanly. POL_CHAR_F02=127
    # sends SE's value; any integer is accepted so the bits can be bisected if
    # 127 does something and we want to know which one did it.
    f02 = int(os.environ.get("POL_CHAR_F02", "0") or 0)
    if f02:
        struct.pack_into("<H", out, 0x02, f02 & 0xFFFF)
    # +0x0A -- the other field SE fills and we do not. Her FFXI record has
    # `30 30`, which is ASCII "00", not a number: two digits where our layout
    # note claims a packed bit that only applies to content code >= 4 (FFXI is
    # 1, so that note cannot be the whole story). Same treatment as +0x02:
    # POL_CHAR_F0A="00" sends SE's two bytes, off by default, and it takes a
    # STRING because SE's value is one.
    f0a = os.environ.get("POL_CHAR_F0A", "")
    if f0a:
        raw = f0a.encode("latin1", "replace")[:2]
        out[0x0A:0x0A + len(raw)] = raw
    disp = _char_display_name(content_code, content_id, handle_name, names)
    raw = str(disp).encode("cp932", "replace")
    out[0x18:0x18 + 15] = raw[:15].ljust(15, b"\x00")

    # "the rest stays zero until something is observed reading it" -- SOMETHING
    # NOW IS. Measured 2026-08-12 from a PCSX2 savestate taken while the PS2
    # title sat on its black screen. The game copies a table entry
    # into its own struct at 0x00445800 and then reads exactly two fields out of
    # it, logging them as "Chara No = %d" and "ContentsID = %016lx":
    #
    #     table[+0x04]  u32  <- our rec[+0x0C]   Chara No     ... was 0
    #     table[+0x08]  u64  <- our rec[+0x10] | rec[+0x14]<<32
    #                          ContentsID                     ... was 0
    #
    # Everything else in that struct arrived correctly -- the present bit, content
    # code 3, and our 10-digit `1000000003` at table[+0x18] -- so this record was
    # reaching the game intact and only these two fields were blank. They feed the
    # session setup at 0x002fa4e0 and are the natural cause of a game that
    # completes its whole protocol and still has nothing to run.
    #
    # The value is not invented: the numeric form of the SAME 10-digit Content ID
    # already in the string field, which is what accounts mints. `0` for either is
    # what we were sending before, so POL_CHAR_NUMS=0 restores the old behaviour
    # exactly if this turns out to be wrong.
    if os.environ.get("POL_CHAR_NUMS", "1") == "1":
        try:
            cid_num = int(str(content_id).strip() or 0)
        except ValueError:
            cid_num = 0
        struct.pack_into("<I", out, 0x0C, cid_num & 0xFFFFFFFF)          # Chara No
        struct.pack_into("<I", out, 0x10, cid_num & 0xFFFFFFFF)          # ContentsID lo
        struct.pack_into("<I", out, 0x14, (cid_num >> 32) & 0xFFFFFFFF)  # ... hi

    # ...and for FFXI, +0x0C is NOT the Content ID -- it is the WORLD IDENTITY,
    # which is the second half of POL-0001 (2026-08-14). FFXiMain's char-select
    # sub-state 14 looks the picked character up in THIS table before it will
    # open the world socket:
    #
    #     FUN_100FE32F -> FUN_100FFDB0(idx) -> FUN_100FFE00(ffxi_id, worldid, id24)
    #     match iff  table[+0x00]&1  and  table[+0x02]==1
    #                and table[+0x08]==ffxi_id  and  table[+0x0C]==0
    #                and table[+0x04]==charIdMain | worldid<<16 | charIdExtra<<24
    #
    # and on no match writes -1 to the world context and aborts (state 3 -> 203),
    # which is the POL-0001 the user sees. The first three we already satisfy --
    # the bridge rewrites the lobby's `ffxi_id` to the Content ID, so table[+0x08]
    # agrees by construction. `+0x04` we were filling with the Content ID, which
    # can never match, so the search always ran off the end of all 64 slots.
    #
    # The value comes from the BRIDGE, which is the only party that sees the
    # `0x20` record the client will compare against; see `note_world_field`
    # there. No map entry (no LSB overlay, or a character POL has never seen a
    # char list for) leaves the PS2 title's behaviour untouched.
    #
    # **Per-title, deliberately.** The PS2 measurement read these same
    # table slots as "Chara No" / "ContentsID" -- and for content code 3 that may
    # well be right. Two titles, two meanings, one struct: do not unify them
    # without a second measurement.
    if content_code == handlelists._FFXI_CONTENT_CODE:
        try:
            cid_num = int(str(content_id).strip() or 0)
        except ValueError:
            cid_num = 0
        world_field = ffxifields._ffxi_world_fields().get(cid_num)
        if world_field is not None:
            struct.pack_into("<I", out, 0x0C, world_field)
    return bytes(out)


def _chr_put(pt):
    """Apply a 01:0A -- the PS2 Viewer's character-list write-back. Returns
    b"": the reply is header-only, the side effect is the whole answer.

    WHAT THE BODY IS (read off the console's builder, state 3 of
    `polpex_0014e8e0`, one pass over the 64-slot character table at 0x00187c10):

        +0x000  64 x u8   per slot: the ORDER byte (low byte of table+0x10, i.e.
                          our 1:3 record's +0x01) -- always written
        +0x040  64 x 8 B  per slot, ONLY when the console's modified-slot bitmap
                          (0x00189610, set by the UI's link/unlink) has the bit AND
                          the slot is present:
                            [0] 1 = bound to a handle, 2 = unbound  (table bit 15)
                            [1] handle slot 0..63                   (bits 16..21)
                            [2] position 0..7 in that handle's list (bits 22..24)
                            [3..7] zero
                          otherwise [0] = [3] = 0
        +0x240  4 B       untouched by the builder
        +0x244  4 B       checksum over the first 0x244 bytes

    WHAT IS APPLIED. A block of kind 1 whose handle slot differs from the one we
    served the character on is a MOVE, and it goes through
    `accounts.link_content_to_handle`, the same call the sign-up flow uses -- the
    title's rows change handle and keep their Content IDs. `content_id` is passed
    as None on purpose: the function's own ON CONFLICT arm would otherwise
    overwrite the destination's slot-0 id with this character's, which for a
    multi-slot title (FFXI) is a different id and trips the UNIQUE index.

    WHAT IS REFUSED, AND WHY. Kind 2 (unbind) is logged and NOT applied: the only
    unbind we have, `unlink_content_from_handle`, DELETES the row, and that
    function's own neighbour warns a delete destroys the Content ID. Until a
    non-destructive unbind exists, losing a link is worse than ignoring one.
    The ORDER bytes are logged, not stored: once `_char_record` serves +0x01 as
    the slot index they can only ever echo what we sent.

    WHAT IS A GUARD. The trailer must match `_lobby_cksum` over the first 0x244
    bytes; a mismatch means the layout read is wrong somewhere, and the right
    response to that is to read the saved decrypted frame, not to apply a body
    we cannot vouch for. Slots we never served, and handle slots we never served,
    are skipped by name.

    WARNING: Static, 2026-09-11. No 1:10 frame has ever been READ -- both prod sightings
    (2026-09-04, member 10) rotated out before anyone looked. The +0x01 -> table
    packing is taken from the PC twin's layout (`_char_record`'s docstring); the
    console's own 1:3 consumer is truncated in the decompile. The next frame is
    saved as `lobby-unknown-1-10-*.bin`... no longer -- once this arm is tabled
    the generic capture still keeps it under `lobby-51220-req-*`, so read the log
    line this function writes and, if anything looks off, raise POL_CAPTURE_KEEP.
    """
    if pt is None:
        return b""
    body = bytes(pt[friendgroups._CHR_PUT_PAYLOAD_OFF:])
    # KEEP THE BYTES. No 1:10 body has ever been read by a person; the two prod
    # sightings rotated out of the generic capture. Budgeted like every other
    # capture (srvcore.save_capture), under its own name so it survives a busy
    # channel. Compare a real one against the layout above before trusting it.
    cap = save_capture("lobby-chr-put", body)
    if cap:
        log("lobby", f"  1:10 chlist: {len(body)}B decrypted body saved {cap}")
    if len(body) < friendgroups._CHR_PUT_BODY:
        log("lobby", f"  1:10 chlist: body is {len(body)}B, the console sends "
                     f"{friendgroups._CHR_PUT_BODY}; nothing applied")
        return b""
    want = int.from_bytes(body[friendgroups._CHR_PUT_CKSUM_OFF:friendgroups._CHR_PUT_CKSUM_OFF + 4], "little")
    got = lobbybind._lobby_cksum(body[:friendgroups._CHR_PUT_CKSUM_OFF])
    if want != got:
        log("lobby", f"  1:10 chlist: trailer {want:08x} != checksum {got:08x} over "
                     f"the first {friendgroups._CHR_PUT_CKSUM_OFF:#x} bytes -- the layout read is "
                     f"wrong somewhere; NOTHING applied. Read the saved frame.")
        return b""
    chars = handlelists._db_chars()
    handles = handlelists._db_handles()
    order = body[:friendgroups._CHR_PUT_SLOTS]
    reorder = sum(1 for i in range(min(len(chars), friendgroups._CHR_PUT_SLOTS))
                  if order[i] != (i & 0x3F))
    blocks = []
    for i in range(friendgroups._CHR_PUT_SLOTS):
        blk = body[friendgroups._CHR_PUT_BLOCKS_OFF + i * 8:friendgroups._CHR_PUT_BLOCKS_OFF + i * 8 + 8]
        if blk[0]:
            blocks.append((i, blk[0], blk[1], blk[2]))
    log("lobby", f"  1:10 chlist: {len(chars)} character(s) served, "
                 f"{len(blocks)} binding block(s), {reorder} order byte(s) differ "
                 f"from the served index"
                 + (f" (order[:8]={order[:8].hex(' ')})" if reorder else ""))
    if not blocks or accounts is None:
        return b""
    moved = 0
    try:
        db = accounts.connect(os.environ.get("POL_ACCOUNTS_DB", accounts.DEFAULT_DB))
        try:
            for i, kind, hslot, hpos in blocks:
                if i >= len(chars):
                    log("lobby", f"  1:10 chlist: block for slot {i} but only "
                                 f"{len(chars)} served; skipped")
                    continue
                slot, pos, code, cid, bind = chars[i]
                if kind == friendgroups._CHR_PUT_UNBOUND:
                    log("lobby", f"  1:10 chlist: slot {i} content {code} ({cid}) "
                                 f"UNBIND from handle slot {slot} requested -- NOT "
                                 f"applied: the only unbind deletes the row and a "
                                 f"delete destroys the Content ID; logged only")
                    continue
                if kind != friendgroups._CHR_PUT_BOUND:
                    log("lobby", f"  1:10 chlist: slot {i} kind {kind} is neither "
                                 f"bound(1) nor unbound(2); skipped")
                    continue
                if hslot >= len(handles):
                    log("lobby", f"  1:10 chlist: slot {i} names handle slot {hslot} "
                                 f"but {len(handles)} handle(s) were served; skipped")
                    continue
                if hslot == slot:
                    continue
                hid, hname = handles[hslot][0], handles[hslot][1]
                accounts.link_content_to_handle(db, hid, code, None)
                moved += 1
                log("lobby", f"  1:10 chlist: slot {i} content {code} ({cid}) moved "
                             f"handle slot {slot} -> {hslot} ({hname!r}), pos {hpos}")
        finally:
            db.close()
    except Exception as exc:
        log("lobby", f"  1:10 chlist: failed ({exc!r}); {moved} move(s) applied "
                     f"before the failure")
        return b""
    log("lobby", f"  1:10 chlist: applied {moved} move(s); the next 1:3 serves them")
    return b""
