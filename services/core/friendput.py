"""The friend list write (lobby 2:6): parsing, applying, replying."""
import hashlib
import os
import struct
import time
from srvcore import log
from .deps import accounts
from . import friendlist, friendroster, handlelists, lobbymail, lobbysearch, lobbysession, paylen, pushchannel, pushrecord, pushspool



def _friend_put_reply(n, req_pt=None):
    """The 02:06 reply body. Shapes, because the right one is NOT known.

    THE PROBLEM THIS EXISTS FOR: after a friend write the client sits on
    "Updating friend list" and re-sends the SAME 2:6 every ~35 s, never
    re-fetching 2:3 in between -- so it is waiting on this reply, not on the
    list. We answer 168 zero bytes plus a checksum.

    THE PRECEDENT, from this same protocol family: `0:8 KPutHandleList` behaved
    identically. An all-zero reply meant "nothing applied", the client never
    committed the registration, and it took a status word at +0x00 plus a
    timestamp at +0x14 (whose bits 12-13 must read 01) to make it stick -- see
    `_handle_reg_payload`. 2:6 is the sibling PUT and is getting exactly the
    all-zero answer that did not work for its sibling.

    That is an analogy, not a measurement, so every shape here is opt-in and
    `zero` stays the default:

        zero   168 zero bytes (today's behaviour)
        ack    0:8's shape: status word at +0x00, applicable stamp at +0x14
        stamp  the timestamp only, no status word
        echo   the record the client just wrote, returned verbatim

    Pick with `friend_reply=` in the calibration control file, which is re-read
    per request -- so the shapes can be tried without a rebuild, and a rebuild is
    what drops the session being tested.
    """
    mode = (lobbysearch._search_calib()["friend_reply"]
            or os.environ.get("POL_FRIEND_PUT_REPLY", "se")).strip().lower()
    out = bytearray(n)
    if mode == "se":
        # SE'S ANSWER IS AN ECHO, and that is the finding -- not a record we get
        # to compose. All four 2:6 replies in polshim-se.429364.log are the
        # REQUEST's own bytes from 0x158 onward, copied to reply +0x08, with a
        # count at +0x00 and exactly two bytes cleared. Verified against every
        # sample, including the deletion whose name field is stale heap:
        #
        #     reply +0x00  u32 1                       entries applied
        #     reply +0x08  <- request 0x158 .. 0x187   48 bytes, verbatim
        #     reply +0x12  0                           the `0a` byte, cleared
        #     reply +0x2F  0                           last byte of the name
        #                                              field, cleared
        #
        # The three bytes SE additionally FILLS on a first add (a row id at
        # +0x09..+0x0B and a status at +0x0C) are left as the client sent them:
        # on every later write the client echoes back whatever came out of here,
        # so consistency is what matters and inventing an id is not needed to get
        # one. Echoing is also what makes a DELETE answerable at all -- there is
        # no name in that record to build a reply around.
        if req_pt is not None and len(req_pt) >= 0x208 and n >= 0xB8:
            # *** ONE UNIT PER RECORD, NOT ONE UNIT FULL STOP. ***
            #
            # This used to hardcode a single record: count 1, one 176-byte copy,
            # and every normalisation offset written as an absolute number. SE's
            # own capture has a TWO-record write (the ignore-add at capture line
            # 259509) answered with a 352-byte, two-unit reply -- so a client
            # that sends N>1 was told `count=1` and given one record back, which
            # is the walk-off-the-end failure one opcode over.
            #
            # The grid is the same 168 the REQUEST uses, because the copy is 1:1
            # from request 0x158 (`reply[+0x08 + k] == request[0x158 + k]`), and
            # every offset below is now stated as its delta from the unit base.
            # At N=1 that is byte-for-byte what this produced before -- base is
            # 0x08, so 0x08+0x09 is the same 0x11 it always was.
            _s, _recs = _friend_put_records(req_pt)
            count = len(_recs) or 1
            if os.environ.get("POL_FRIEND_PUT_MULTI", "1") != "1":
                count = 1
            # The reply cannot carry more than the length we were given room for.
            fits = max(1, (n - paylen._FRIEND_PUT_REPLY_FIXED) // _FRIEND_PUT_REC)
            if count > fits:
                log("lobby", f"  2:6 reply: {count} record(s) written but only "
                             f"{fits} fit in {n}B -- echoing {fits}. The length "
                             "and the count must agree or the client reads past "
                             "the last record.")
                count = fits
            struct.pack_into("<I", out, 0x00, count)
            span = count * _FRIEND_PUT_REC + 4      # + the trailing state dword
            src = _FRIEND_PUT_STARTS[0] - 4         # 0x158, the first state dword
            out[paylen._FRIEND_PUT_REPLY_BASE:paylen._FRIEND_PUT_REPLY_BASE + span] = \
                req_pt[src:src + span]
            for _i in range(count):
                base = paylen._FRIEND_PUT_REPLY_BASE + _i * _FRIEND_PUT_REC
                # The bytes SE NORMALISES. Checked byte-for-byte against all four
                # of its single-record replies AND against both units of its
                # two-record one: with these cleared, they reproduce exactly bar
                # the row id below.
                out[base + 0x09] = out[base + 0x0A] = 0   # record +0x05/+0x06
                out[base + 0x27] = 0                 # last byte of the name field
                for at in range(base + 0x39, base + 0xA8, 0x10):
                    out[at] = 0                      # empty slots' index markers
                named = bytes(out[base + 0x18:base + 0x28]).split(b"\x00")[0]
                # THE ROW ID IS THE SERVER'S TO ASSIGN, and the client echoes it back
                # on every later write -- sample 1 sent zeros and got `a0 13 ea`,
                # sample 4 sent that same id straight back. So it only has to be
                # stable -- but NOT arbitrary: the client DERIVES identities from
                # it. The record's head LE32 is `rid << 8 | state`, and
                #   * a profile click on the fresh row sends
                #     `z_hid = 0x200000 | (head >> 13)` (see `_FRIEND_HID_TAG`),
                #     which `_profile_record` resolves as a HANDLE ID;
                #   * a later DELETE hands the head back, and
                #     `_friend_row_handles` reads `word >> 13` as the handle.
                # So `rid = handle_id << 5` makes both true by construction
                # ((hid<<5)<<8 >> 13 == hid, and the low 13 bits stay 0x021,
                # SE's own pattern). The old md5 rid was garbage in BOTH
                # consumers: live 2026-08-22, Fox viewing a friend added THIS
                # SESSION resolved "handle 426116 -- WHICH WE DO NOT HAVE" and
                # fell through to Fox's OWN profile (the view-profile-shows-me
                # report). Rows already echoed with an md5 id heal at relog,
                # when 2:3 re-serves the correct head word.
                if out[base + 0x01:base + 0x05] == b"\x00\x00\x00\x00":
                    peer_hid = 0
                    if named and accounts is not None:
                        try:
                            _db = accounts.connect(os.environ.get(
                                "POL_ACCOUNTS_DB", accounts.DEFAULT_DB))
                            try:
                                _hrow = accounts.handle_by_name(
                                    _db, named.decode("cp932", "replace"))
                                peer_hid = int(_hrow["id"]) if _hrow else 0
                            finally:
                                _db.close()
                        except Exception:
                            peer_hid = 0
                    if peer_hid:
                        rid = peer_hid << 5
                    else:
                        # A peer with no handle here (or no name at all): fall
                        # back to the old stable hash. Their profile cannot
                        # resolve anyway, so the encoding carries nothing.
                        # hashlib, not hash(): PYTHONHASHSEED randomises per
                        # process and this id has to survive a restart -- the
                        # client keeps echoing the one it was given.
                        rid = (int.from_bytes(
                            hashlib.md5(bytes(out[base + 0x18:base + 0x28])
                                        ).digest()[:3], "little") or 1)
                    out[base + 0x01:base + 0x04] = struct.pack("<I", rid)[:3]
                    out[base + 0x04] = 0x04
                    if peer_hid and friendlist._friend_bitfield_mode():
                        # POL_FRIEND_BITFIELD: the plain handle id in bits 13-50
                        # (no 04 above it), matching what 2:3 then serves.
                        word = struct.unpack_from("<Q", out, base)[0]
                        word = (word & ~(friendlist._FRIEND_HID_BITS << 13)) \
                            | ((peer_hid & friendlist._FRIEND_HID_BITS) << 13)
                        struct.pack_into("<Q", out, base, word)
                    log("lobby", f"  2:6 reply: assigned row id {rid:#08x} to "
                                 f"record {_i} ("
                                 + (f"handle {peer_hid} << 5, so profile/delete "
                                    f"derivations resolve" if peer_hid else
                                    "no local handle -- stable hash")
                                 + "; the client echoes this back)")
                # THE SLOT IS THE SERVER'S TO CONFIRM, for the same reason the row id
                # above is. Reply +0x10 is the echoed record +0x04 -- the slot -- and
                # the client's own choice collides when it adds twice without
                # re-reading the list (see `_friend_slots_assign`). Writing the slot
                # we actually filed the row under is byte-identical to an echo on
                # every SE sample, because its client was never wrong.
                if named and friendlist._FRIEND_PUT_ASSIGNED \
                        and os.environ.get("POL_FRIEND_PUT_SLOT_FIX", "1") == "1":
                    for nm, slot in friendlist._FRIEND_PUT_ASSIGNED:
                        if nm.encode("cp932", "replace") != named:
                            continue
                        if out[base + 0x08] != (slot & 0xFF):
                            log("lobby", f"  2:6 reply: correcting the slot for "
                                         f"{nm!r}, {out[base + 0x08]} -> {slot}")
                            out[base + 0x08] = slot & 0xFF
                        break
                # FILL THE PEER'S REAL GUID at the echoed record's +0x10 (reply
                # payload +0x18). The client's own 2:6 request leaves it 0 for a
                # search-based add, so the friend enters its match array anonymous and
                # every later `Friend registration accepted` misses it (string 18161,
                # stuck "Awaiting confirmation") until a relog's 2:3 supplies the guid.
                #
                # CONFIRMED against app.dll + SE's wire: polcore's 2:6-reply parser
                # (`FUN_037e4170` case 0xb) stores `cft_0355(record+0x10)` into the
                # friend table `DAT_038740d8`, and app.dll's Put-response rebuild
                # (`0x488fb56` -> `0x488f0f0`) keys the match array from it. SE's reply
                # carries the RAW served guid here -- the same bytes as its 2:3 row and
                # select tail (`91 bc 04 1e ..` for Cyn) -- and `cft_0355` applies the
                # session key K on the CLIENT side, exactly as it does for the 2:3 the
                # relog path already proves works. So we send the same raw guid we
                # serve in 2:3, and inject it (not mask it) -- the server never needs
                # K. We OVERWRITE rather than fill-if-zero, to reproduce SE's reply
                # byte-for-byte whatever the client echoed; the value is what a correct
                # 2:3 for this friend would carry, so it cannot harm an add that was
                # already right.
                # POL_FRIEND_PUT_GUID=0 restores the pure echo.
                if named and os.environ.get("POL_FRIEND_PUT_GUID", "1") == "1":
                    nm = named.decode("cp932", "replace")
                    # RESOLVE THE GUID HERE, not from a map populated elsewhere. The
                    # capture pass that filled `_FRIEND_PUT_GUIDS` runs AFTER this
                    # reply is built for the first 2:6 of an add, so the map was empty
                    # exactly when it mattered and the guid never went out (the
                    # 2026-08-19 miss: no "set real guid" line despite a clean add).
                    # A name -> handle -> handle_guid lookup here has no such ordering
                    # dependency; it is the same resolution the select record uses.
                    guid = friendlist._FRIEND_PUT_GUIDS.get(nm)
                    if not guid and accounts is not None:
                        try:
                            db = accounts.connect(os.environ.get(
                                "POL_ACCOUNTS_DB", accounts.DEFAULT_DB))
                            try:
                                hrow = accounts.handle_by_name(db, nm)
                                if hrow is not None:
                                    guid = accounts.handle_guid(int(hrow["id"]))
                            finally:
                                db.close()
                        except Exception as exc:
                            log("lobby", f"  2:6 reply: guid lookup for {nm!r} "
                                         f"failed ({exc!r}); leaving the echo")
                    was = bytes(out[base + 0x10:base + 0x18])
                    if guid:
                        struct.pack_into("<Q", out, base + 0x10,
                                         int(guid) & 0xFFFFFFFFFFFFFFFF)
                        if was != bytes(out[base + 0x10:base + 0x18]):
                            log("lobby", f"  2:6 reply: set real guid "
                                         f"{guid:#018x} for {nm!r} at "
                                         f"+{base + 0x10:#x} (echoed {was.hex()}; "
                                         "this is what closes a search-add "
                                         "mid-session; POL_FRIEND_PUT_GUID=0 "
                                         "restores the echo)")
                    # DIAGNOSTIC (2026-08-19): the injection was seen to no-op --
                    # log the whole decision and the echoed record head so we can see
                    # exactly what the client sent at each offset. POL_FRIEND_PUT_DIAG=0
                    # silences it once the acceptance-match path is understood.
                    if os.environ.get("POL_FRIEND_PUT_DIAG", "1") == "1":
                        log("lobby",
                            f"  2:6 reply DIAG {nm!r}: resolved_guid="
                            f"{(guid or 0):#018x}, echoed_guid={was.hex()}, "
                            f"rec_head={bytes(out[base:base + 0x20]).hex()}")
            log("lobby", f"  2:6 reply: SE echo shape, {n}B, {count} record(s) "
                         "(friend_reply=zero in the calib file restores all-zero)")
            return bytes(out)
        log("lobby", f"  2:6 reply: {len(req_pt or b'')}B request is too short to "
                     "echo; sending zeros")
        return b""
    if mode == "zero" or not mode:
        return b""                      # unchanged: caller's generic all-zero
    if mode == "echo" and lobbysearch._LAST_FRIEND_PUT:
        rec = lobbysearch._LAST_FRIEND_PUT[0]
        out[0:min(len(rec), n)] = rec[:n]
        log("lobby", f"  2:6 reply: echoing the written record ({len(rec)}B)")
        return bytes(out)
    if mode in ("ack", "stamp"):
        ts = int(time.time()) & 0xFFFFFFFF
        ts = (ts & ~(3 << 12)) | (1 << 12)      # the 0:8 applicability rule
        if n >= 0x18:
            if mode == "ack":
                struct.pack_into("<I", out, 0x00,
                                 int(os.environ.get("POL_FRIEND_PUT_ACK_STATUS",
                                                    "0x81"), 0))
            struct.pack_into("<I", out, 0x14, ts)
        log("lobby", f"  2:6 reply: {mode} shape, ts={ts:#x} "
                     f"((ts>>12)&3 = {(ts >> 12) & 3})")
        return bytes(out)
    log("lobby", f"  2:6 reply: unknown shape {mode!r}; sending zeros")
    return b""


# --------------------------------------------------------------------------- #
# THE FRIEND LIST WRITE -- lobby 02:06 (2026-08-12)
#
# There is no "add one friend" message in this protocol. The Viewer's own class
# names say so: app.dll carries `KGetFriendList` and `KPutFriendList` as a
# get/put PAIR, and every friend mutation -- add, accept, rename, delete -- is a
# PUT of the WHOLE LIST.
#
# How the pairing was established, so it can be re-checked:
#
#   1. polcore's request encoder is `sub_037df4d0(ctx, type, opcode, len)`. Its
#      28 call sites each push type/opcode as literals, which is the complete
#      lobby opcode map (0:8 falls out as len 0x648, matching the known 0:8).
#   2. app.dll reaches polcore through the table returned by
#      IPOLCoreCom::GetCommonFunctionTable, held at app.dll `0x4e17c84` and
#      called as `[eax+N]`. Solving for the base over the 410 distinct N values
#      seen in app.dll gives exactly ONE table that makes every offset land on a
#      function: polcore .data 0x6fbe8. So N converts to a polcore function.
#   3. Each `K*` class's vtable is reachable from its RTTI type descriptor, and
#      its methods' table offsets resolve through (1)+(2) to an opcode.
#
# That yields the whole social API, and it CORRECTS several guesses in this file:
#
#     KGetHandleList 0:9    KPutHandleList 0:8    KGetChrList/KPutChrList 1:3
#     KGetFriendList 2:3    KPutFriendList 2:6
#     KGetDetailData 3:0    KPutDetailData 3:1
#     KPutMyCommentForFriend 4:3            KGetMyStatus  4:6
#     KChangeMyStatus 4:5   KPutBusy 4:5
#     KCreateGroup 7:1      KDeleteGroup 7:2      KChgGrpMemClass 7:3
#     KGetGroupList 7:12    KChgMyGrpStatus 7:11  KChgMyAllGrpOnlineStatus 7:11
#
# so 2:6 is NOT "member search" (its old label here), 4:6 is MY STATUS rather
# than friends data, and 7:12 is the group list that was listed as unidentified.
#
# The request SIZE is computed in the builder at polcore 0x37e4217:
#
#     ecx = [ebx+0xC0]            ; N, the friend count
#     eax = ecx*8 - ecx           ; 7N
#     eax = eax + eax*2           ; 21N
#     ecx = eax*8 + 0x138         ; 168N + 312   <- pushed as `len`
#
# i.e. a 0x138 preamble followed by N records of **168 bytes** -- the same
# 168-byte record 2:3 serves back, which is exactly what a get/put pair should
# look like. Payload starts at frame 0x28 (see the 07:01 note below), so the
# records run from 0x28+0x138 = 0x160 to the 4-byte trailing checksum.
# The 0x138 above is the whole declared payload for N=0, and the 4-byte
# request CHECKSUM is the last 4 bytes OF that payload -- established by the
# 07:01 capture below, where a declared 0x20 payload is 0x1C of data at 0x28
# plus a checksum at 0x44. So the split is
#
#     0x28  preamble, 0x134 bytes
#     0x15C N x 168 records
#     tail  4-byte checksum
#
# and 0x134 + 168N + 4 == 0x138 + 168N, which is the size the builder computes.
_FRIEND_PUT_REC = 168            # one record, same shape as the 2:3 reply
#: Candidate record-grid starts, best-derived first. The preamble length is a
#: DEDUCTION from where the checksum sits, not something we have watched on the
#: wire, so the parser tries the neighbours too and logs which one fit. One live
#: write settles it; until then a wrong constant degrades to "did not parse"
#: instead of to a corrupted friend list.
_FRIEND_PUT_STARTS = (0x28 + 0x134, 0x28 + 0x138, 0x28 + 0x130)


def _friend_put_records(pt):
    """(start, [record bytes]) for the grid, else (0, []).

    THE GRID IS ARITHMETIC, NOT SOMETHING TO SNIFF FOR. polcore's builder computes
    `168N + 312`, so N follows from the frame length and nothing else -- and that
    matters because **a record's name field is not always a name**. SE's own
    capture (2026-08-16) has four 2:6 writes and the third one, the DELETION the
    session narration describes, carries stale heap where the name should be
    while keeping the friend's 12-byte id right behind it. The account holder's
    delete is byte-for-byte the same shape.

    The old code required every record to hold a parseable name before it would
    accept the grid, so a delete read as "this 520-byte frame did not parse" and
    the write was refused -- which is why deleting a friend hung the UI forever.
    """
    end = len(pt) - 4                              # the trailing checksum
    body = end - _FRIEND_PUT_STARTS[0]
    if body >= 0 and body % _FRIEND_PUT_REC == 0:
        start = _FRIEND_PUT_STARTS[0]
        return start, [bytes(pt[start + i * _FRIEND_PUT_REC:
                                start + (i + 1) * _FRIEND_PUT_REC])
                       for i in range(body // _FRIEND_PUT_REC)]
    # Only a frame whose length does NOT fit the builder's formula falls through
    # to guessing, and then a parseable name is the only evidence available.
    for start in _FRIEND_PUT_STARTS:
        body = end - start
        if body < 0 or body % _FRIEND_PUT_REC:
            continue
        recs = [bytes(pt[start + i * _FRIEND_PUT_REC:
                         start + (i + 1) * _FRIEND_PUT_REC])
                for i in range(body // _FRIEND_PUT_REC)]
        # An empty list fits every candidate, so it proves nothing about the
        # offset -- accept it at the derived one and do not guess further.
        if not recs:
            return start, []
        if all(_friend_put_name(r) is not None for r in recs):
            if start != _FRIEND_PUT_STARTS[0]:
                log("lobby", f"  2:6 grid fit at 0x{start:X}, not the derived "
                             f"0x{_FRIEND_PUT_STARTS[0]:X} -- update the constant")
            return start, recs
    return 0, []


#: Where the name sits in a 02:06 WRITE record. **0x14, MEASURED** from the first
#: populated 2:6 ever captured (2026-08-12, adding "Tester" from a search result):
#:
#:   +00  00 00 00 ce 01 01 0a 00 00 00 00 00 00 00 00 00
#:   +10  00 00 00 00 54 65 73 74 65 72 00 80 1a f8 e4 c0   ....Tester......
#:                   ^^ the name starts here, at +0x14
#:
#: **The get/put pair do NOT share a layout**, which is what this file assumed.
#: SE's 2:3 READ record (captured 2026-08-11) has a u64 guid at +0x10 and the name
#: at +0x18; the client's 2:6 WRITE puts the name four bytes earlier and has no
#: guid we can find -- Tester's wire guid (0x80000000006) appears nowhere in the
#: record. Reading +0x18 took the tail of the name, so adding "Tester" created a
#: friend called **"er"**, and the whole-list PUT then deleted everything the
#: misread had not matched. 0x18 is kept as a fallback because SE's own read
#: record uses it, and the parser logs which one fit.
_FRIEND_PUT_NAME_OFFS = (0x14, 0x18)


def _friend_put_name(rec):
    """The handle name in one 02:06 record, or None if this is not one.

    An empty slot is a legal record and reads as "".
    """
    raw = b""
    for off in _FRIEND_PUT_NAME_OFFS:
        cand = rec[off:off + 0x10].split(b"\x00")[0]
        if cand:
            raw = cand
            break
    if not raw:
        return ""
    try:
        name = raw.decode("cp932").strip()
    except UnicodeDecodeError:
        return None
    # check_handle_policy returns None when the name is ACCEPTABLE and a message
    # when it is not -- the inverse of the obvious reading.
    if accounts is not None and accounts.check_handle_policy(name) is not None:
        return None
    return name


def _parse_friend_put(pt):
    """Records out of a decrypted 02:06, or [] if it does not parse cleanly.

    Returns [{"name","guid","kind","raw"}]. Refuses to return a partial answer:
    the caller REPLACES the stored list with this, so a half-parsed request must
    look like "no data" rather than like "the user deleted everyone".
    """
    _start, recs = _friend_put_records(pt)
    out = []
    for rec in recs:
        name = _friend_put_name(rec)
        if not name:
            continue                                # empty slot, or unparseable
        out.append({
            # NO GUID IS READ FROM THE WRITE RECORD. +0x10 is the 2:3 READ
            # record's guid slot, and in a 2:6 it holds `00 00 00 00` followed by
            # the first four bytes of the name -- reading it as a u64 produced
            # 0x74736554_00000000, i.e. "Test" as a number. The peer's real wire
            # guid appears nowhere in the record we captured, so the name is the
            # only identity here; `add_friend` mints the guid from the resolved
            # local handle, which is what makes a profile lookup on that friend
            # land on THEM (see the z_hid note in accounts.add_friend).
            "name": name,
            "guid": 0,
            "kind": accounts.KIND_FRIEND if accounts is not None else 0x0800,
            # THE CLIENT'S OWN ID FOR THE ROW. Not a guid and not ours to read --
            # it is the 12 bytes a DELETE arrives with when the name field has
            # already been overwritten with heap. Proven stable across an add and
            # the delete of the same friend, in our capture and in SE's.
            "client_ref": _friend_put_ref(rec, name),
            # The second id: the 4 bytes in front of the grid. Same for every
            # record in one write, which is right -- 2:6 carries one row.
            "wire_ref": _friend_put_wire_ref(pt),
            "raw": rec,
        })
    return out


#: The name field in a 02:06 record starts at +0x14 and the client's 12-byte id
#: for the row follows the name's NUL -- so **its offset moves with the name's
#: length**, which the first cut here got wrong by nailing it to +0x18.
#:
#: The evidence, from the live store: the ref recorded for "LaptopTest2" came out
#: as `6f705465737432 00 35cd138e` -- the TAIL of the name plus four bytes of the
#: real id. "Fox" is three characters, so +0x18 was right for it and for nothing
#: else. SE's records agree: its id sits at +0x1A behind "Yatih" and +0x18 behind
#: "Cyn".
_FRIEND_PUT_NAME_AT = 0x14
_FRIEND_PUT_REF_LEN = 12


#: WHERE THE IGNORE STATE AND THE STABLE IDENTITY SIT IN A 02:06 RECORD.
#: All three offsets are MEASURED, 2026-08-19, off the retail capture
#: `grouplife.txt`. One record,
#: whole, as SE's client sent it when the account holder renamed `Cyn`:
#:
#:   0x158  21 a0 13 ea   <- the STATE DWORD, in front of the grid. Its LOW byte
#:                           carries the ignore scope (21 normal / 31 ignored /
#:                           51 PlayOnline-id scope); the other three are the row
#:                           id the server assigned.
#:   0x15C  04 00 00 00   <- record +0x00. +0x03 is the ignore ACTION flag
#:                           (00 none / 34 add / 40 delete).
#:   0x160  00 00 0a 00   <- +0x04 slot, +0x06 the 0x0A live marker
#:   0x168  91 bc 04 1e 2c 00 8c 00   <- +0x0C, the peer's u64 client guid
#:   0x170  "Cool friend :3\0"        <- +0x14, the name field holding a LABEL
#:
#: The state dword belongs to the record BEHIND it, not the one in front: in the
#: two-record write at capture line 259509 the second record's dword sits at
#: 0x200 = grid + 168 - 4, which is why `_friend_put_state_low` indexes it that
#: way rather than reading 0x158 for every record.
_FRIEND_PUT_FLAG_AT = 0x03
_FRIEND_PUT_GUID_AT = 0x0C
_FRIEND_PUT_TEXT_LEN = 0x10


def _friend_put_guid(rec):
    """The peer's u64 client guid from a 02:06 record, or 0.

    **THE ONE FIELD THAT DOES NOT MOVE.** Across an add, a rename, an ignore, a
    scope change and an un-ignore of the same friend it was `91 bc 04 1e 2c 00
    8c 00` every time, while +0x00 mutated with the ignore state and +0x14 held
    a different string on each write. It is also the field `_friend_put_reply`
    already fills on our side (reply +0x18), so the client echoes back the guid
    we served -- which is what makes it usable as a key here.
    """
    if len(rec) < _FRIEND_PUT_GUID_AT + 8:
        return 0
    return struct.unpack_from("<Q", rec, _FRIEND_PUT_GUID_AT)[0]


def _friend_put_state_low(pt, index=0, start=None):
    """The ignore-scope byte in front of record `index`, or None.

    See the offsets note above: each 168-byte record is preceded by a 4-byte
    state dword, so record 0's is at the grid start minus 4 and record 1's is at
    grid + 164.
    """
    start = _FRIEND_PUT_STARTS[0] if start is None else start
    at = start + index * _FRIEND_PUT_REC - 4
    if at < 0 or at >= len(pt):
        return None
    return pt[at]


def _friend_put_flag(rec):
    """The ignore ACTION byte at record +0x03, or None."""
    if len(rec) <= _FRIEND_PUT_FLAG_AT:
        return None
    return rec[_FRIEND_PUT_FLAG_AT]


def _friend_put_text(rec):
    """The TEXT in a record's name field -- a name, a rename LABEL, or None.

    This is deliberately NOT `_friend_put_name`: that one asks "is this an
    acceptable handle?" and answers None for anything else, which is why a
    RENAME used to be indistinguishable from a DELETE and got the friend
    deleted. "Cool friend :3" has spaces and a colon, `check_handle_policy`
    rejects it, and the record then fell into `_friend_put_deletes` -- whose
    target is the +0x04 slot, which on a rename names the very friend being
    renamed. Renaming a friend deleted them.

    What separates a label from a delete's heap is measured and simple: the
    label is NUL-TERMINATED PRINTABLE TEXT and the heap is not. SE's own delete
    records carry `66 76 47 5b f9 d5 cb 95 bb 88 0e e2 69 53 33 a3` in this
    field -- sixteen bytes with no terminator anywhere -- while every real write
    ends its string. So:

        None   the field is heap (no NUL, or unprintable) -> this is a delete
        ""     the field is empty -> an empty slot
        text   whatever the client put there, name or caption

    Judging it is the CALLER's job (see `_friend_put_kind` in the capture path):
    the same "Cyn" can be a plain re-write or a rename back from a caption, and
    only the stored row can say which.
    """
    at = _FRIEND_PUT_NAME_AT
    field = bytes(rec[at:at + _FRIEND_PUT_TEXT_LEN])
    if b"\x00" not in field:
        return None                     # heap: a real string is always terminated
    raw = field.split(b"\x00")[0]
    if not raw:
        return ""
    try:
        text = raw.decode("cp932")
    except UnicodeDecodeError:
        return None
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in text):
        return None                     # printable, or it is not text at all
    return text.strip()


def _friend_put_ref(rec, name):
    """The client's 12-byte id for a row, read from behind its name."""
    at = _FRIEND_PUT_NAME_AT + len(str(name).encode("cp932", "replace")) + 1
    return bytes(rec[at:at + _FRIEND_PUT_REF_LEN])


def _friend_put_wire_ref(pt):
    """The 4 bytes immediately in front of the record grid.

    The other id for a row, and the more useful one on a DELETE because it does
    not move: measured over seven consecutive delete attempts on 2026-08-16 it
    was `a21af5eb` for one friend and `e2b8e568` for another, identical every
    time, while the record's own name field held heap. SE fills this field in its
    2:6 REPLY and the client echoes it back on later writes, which is what makes
    it a row handle rather than a nonce.
    """
    at = _FRIEND_PUT_STARTS[0] - 4
    ref = bytes(pt[at:at + 4])
    return ref if any(ref) else b""


def _friend_put_deletes(pt):
    """The raw records in this write that name nobody -- i.e. the DELETES.

    A record with no parseable name is not an empty slot: 2:6 sends ONE record
    per change, so it is a delete. Its target is the +0x04 SLOT (below); the id
    searches are fallbacks kept only because they cost nothing.
    """
    _start, raw = _friend_put_records(pt)
    return [rec for rec in raw if not _friend_put_name(rec)]


def _friend_put_bitfield_deletes(pt, start, raw_recs, deletes, renamed=()):
    """POL_FRIEND_BITFIELD: add rows whose bit 0 (valid) is clear to `deletes`.

    Each row's bitfield starts 4 bytes before our grid record (the "state
    dword", `_friend_put_state_low`); the u16 row count sits 4 bytes before
    that. Both readings are LOGGED wherever they disagree -- the name-field
    heuristic has held since 2026-08-16 and this does not replace it.
    Returns (deletes, set of raw records added by the bit alone).
    """
    at = start - 8
    if 0 <= at and at + 2 <= len(pt):
        count = struct.unpack_from("<H", pt, at)[0]
        if count != len(raw_recs):
            log("lobby", f"  2:6 bitfield: header count {count} but the frame "
                         f"length gives {len(raw_recs)} record(s)")
    heur = {bytes(r) for r in deletes}
    out, extra = list(deletes), set()
    for i, rec in enumerate(raw_recs):
        low = _friend_put_state_low(pt, i, start)
        if low is None:
            continue
        bit_del = not (low & 1)
        h_del = bytes(rec) in heur
        if bit_del == h_del:
            continue
        log("lobby", f"  2:6 bitfield: record {i} valid bit says "
                     f"{'DELETE' if bit_del else 'keep'} (low {low:#04x}), the "
                     f"name heuristic says {'DELETE' if h_del else 'keep'} "
                     f"(text {_friend_put_text(rec)!r}, slot "
                     f"{_friend_put_slot(rec)})"
                     + (" -- deleting (POL_FRIEND_BITFIELD)" if bit_del and
                        bytes(rec) not in renamed else ""))
        if bit_del and bytes(rec) not in renamed:
            out.append(rec)
            extra.add(bytes(rec))
    return out, extra


#: WHICH FRIEND A DELETE NAMES -- the record's +0x04 byte, the friend's slot in
#: the 2:3 list WE served. Measured 2026-08-16 against SE's own capture
#: (`pol-shim/build-se/polshim-se.429364.log`, decoded with `tools/lobbydec.py`)
#: after both client-minted ids below were shown to be uninitialised buffer.
#:
#: SE's four 2:6 writes carry, at +0x04, exactly that friend's 2:3 slot:
#:
#:     line  73345  add "Cyn"       +0x04 = 00   Cyn is 2:3 slot 0
#:     line 141215  add "Yatih"     +0x04 = 01   Yatih is 2:3 slot 1
#:     line 143962  DELETE (heap)   +0x04 = 00   -> slot 0 = Cyn ...
#:     line 146089  re-add "Cyn"    +0x04 = 00   ... and the next write re-adds
#:                                               Cyn, which is exactly the
#:                                               "deletions and re-adds" the
#:                                               session narration describes.
#:
#: It is NOT an index within the write: 141215 sends +0x04 = 1 with N = 1.
#:
#: Our own client agrees. Across seven consecutive deletes (lobby.log, 2026-08-16)
#: every byte from +0x0C on was IDENTICAL stale heap and +0x04 was the ONLY byte
#: that moved -- 5, 0, 2, 5, 4, 3, 0, every one a valid slot in an 8-friend list.
#:
#: This is the only identifier a delete carries that exists for EVERY row, because
#: the SERVER mints it: `_friend_list_record` writes the same number into the 2:3
#: record's +0x08. So a row the client has only ever READ is deletable, which is
#: what the two ids below could never manage.
_FRIEND_PUT_SLOT_AT = 0x04
#: +0x06 reads 0x0A in every 2:6 record from both clients, add and delete alike,
#: while everything around it is heap on a delete. A cheap "this record is live"
#: check before acting on the slot.
_FRIEND_PUT_LIVE_AT = 0x06
_FRIEND_PUT_LIVE_VAL = 0x0A


def _friend_put_slot(rec):
    """The 2:3 slot a 02:06 record names, or None if the record is not readable."""
    if len(rec) <= _FRIEND_PUT_LIVE_AT:
        return None
    if rec[_FRIEND_PUT_LIVE_AT] != _FRIEND_PUT_LIVE_VAL:
        return None
    slot = rec[_FRIEND_PUT_SLOT_AT]
    # The client's own cap on the 2:3 record's +0x08 -- a bigger number is heap,
    # not a slot, and must not be allowed to index anything.
    return slot if slot < 0x40 else None


def _friend_slot_map_live():
    """A slot map built from the list as it stands RIGHT NOW.

    The fallback for when `_friend_slots_map` has nothing -- this process never
    served the 2:3 the client is indexing. Correct only while the served list and
    the stored list still agree, i.e. before the session's first delete, so it is
    a last resort and never a substitute for the real map.

    `_db_friends(kinds=(KIND_FRIEND,))` is the SAME call that builds the reply,
    which matters: that list is KIND_FRIEND only and drops incoming requests, so
    a plain `list_friends` index would be wrong by however many groups and
    invitations the account holds (for `Fox`, by three).
    """
    rows = friendlist._db_friends(kinds=(accounts.KIND_FRIEND,))
    return {i: (r[1], int(r[5])) for i, r in enumerate(rows)}


def _friend_slot_row(slot_map, slot):
    """The (name, db row id) our 2:3 put in `slot`, or None."""
    if slot_map is None:
        return None
    return slot_map.get(int(slot))


def _friend_put_row_for(db, handle_id, rec, slot_map=None):
    """The stored friend row a 02:06 record names, or None -- guid first.

    THE ORDER IS THE WHOLE POINT, and it is what tells a RENAME from a DELETE.

      1. **the guid at +0x0C**, matched against what we hold and against what a
         2:3 for that peer would serve. It is the field that survives every kind
         of write (see `_friend_put_guid`), and on a DELETE it is stale heap --
         so heap resolves to nobody and a delete can never be mistaken for a
         rename.
      2. **the NAME**, when the text field holds an acceptable handle name. That
         is the ordinary add/accept/re-write record and the row it names is the
         row it names -- no inference required.
      3. **the +0x04 slot**, and only for a record whose text is NOT a handle
         name, i.e. a caption. Two things fall out of that restriction, and both
         matter:

           * a DELETE cannot reach it. Its name field is stale heap, so
             `_friend_put_text` answers None and the record resolves to nobody
             by any route -- which is what keeps it a delete.
           * an ADD cannot reach it either. "Add Bob" carries a perfectly good
             handle name and lands on step 2; if it fell through to the slot it
             would resolve to whoever the client currently has THERE, and the
             caller would read that as "rename Alice to Bob". The slot arm is
             for captions alone, because a caption is the one thing that can
             never be a new friend's name.

    The remaining ambiguity is honest and unavoidable: renaming somebody to a
    string that looks like a handle ("Buddy") is indistinguishable from adding a
    friend called Buddy unless the guid resolves. With the guid it is a rename;
    without it, it reads as an add, exactly as it did before any of this.
    """
    guid = _friend_put_guid(rec)
    if guid:
        try:
            row = accounts.friend_row_by_guid(db, int(handle_id), guid)
        except Exception as exc:
            log("lobby", f"  2:6 friend write: guid lookup failed ({exc!r})")
            return None
        if row is not None:
            return row
    text = _friend_put_text(rec)
    if not text:
        return None                              # heap (a delete), or empty
    if _friend_put_name(rec) is not None:
        return db.execute("SELECT * FROM friend WHERE handle_id = ?"
                          " AND peer_name = ?",
                          (int(handle_id), text)).fetchone()
    if slot_map is None:
        return None
    slot = _friend_put_slot(rec)
    hit = _friend_slot_row(slot_map, slot) if slot is not None else None
    if hit is None:
        return None
    return db.execute("SELECT * FROM friend WHERE id = ? AND handle_id = ?",
                      (int(hit[1]), int(handle_id))).fetchone()


def _friend_put_renames(db, handle_id, pt, slot_map=None):
    """`[(row, label, rec)]` for the records in this write that are RENAMES.

    **RENAMING A FRIEND USED TO DELETE THEM.** Measured on our own code path
    against retail's bytes (2026-08-19): the client sends an ordinary 2:6 whose
    record holds the caption where the name goes, `check_handle_policy` rejects
    "Cool friend :3" for its spaces, `_friend_put_name` therefore answers None,
    `_friend_put_deletes` claims the record, and the delete's target is the
    +0x04 slot -- which on a rename is the renamed friend's own slot. So the
    write that was meant to caption them removed them instead.

    A record is a rename when all three hold:

      * it carries printable NUL-terminated TEXT (`_friend_put_text`) -- heap is
        a delete and an empty field is an empty slot;
      * it RESOLVES to a row we already hold (`_friend_put_row_for`, guid first);
      * the text is not that row's own name. Writing the handle's real name back
        is the rename being CLEARED, which is the very next thing the capture
        does -- and that is handled by the caller, which clears the label rather
        than storing a caption identical to the name.

    An add is not caught here: a name we have never seen resolves to no row.
    """
    _start, raw = _friend_put_records(pt)
    out = []
    for rec in raw:
        text = _friend_put_text(rec)
        if not text:
            continue                          # heap (a delete) or an empty slot
        row = _friend_put_row_for(db, handle_id, rec, slot_map)
        if row is None:
            continue                          # an ADD, or a record we cannot place
        if text == row["peer_name"]:
            continue                          # the name itself -- see the caller
        out.append((row, text, rec))
    return out


def _friend_put_apply_flags(db, handle_id, pt, slot_map=None):
    """Persist the ignore-state bytes every record in this write carries.

    IGNORE IS NOT AN OPCODE -- it is state inside the whole-list PUT, and the
    server's job is to round-trip it, byte for byte. The
    reply already echoes both bytes back verbatim for the write that carried
    them; this is what makes them survive the write, so the next 2:3 can serve
    an ignore that outlives the session it was set in.

    Deliberately not INTERPRETED. Four samples pin the structure and the
    transitions (see `set_friend_flags`) and not the meaning of every bit, and a
    bit we guess at is worse than a byte we hand back.

    Returns the number of rows whose stored flags moved.
    """
    start, raw = _friend_put_records(pt)
    moved = 0
    for i, rec in enumerate(raw):
        row = _friend_put_row_for(db, handle_id, rec, slot_map)
        if row is None:
            continue
        low = _friend_put_state_low(pt, i, start)
        flag = _friend_put_flag(rec)
        if low is None and flag is None:
            continue
        was = (row["ignore_low"], row["ignore_flag"])
        if was == (low, flag):
            continue
        accounts.set_friend_flags(db, int(handle_id), int(row["id"]),
                                  low=low, flag=flag)
        moved += 1
        log("lobby", f"  2:6 friend write: ignore state for "
                     f"{row['peer_name']!r} "
                     f"{was[0] if was[0] is not None else '-'}/"
                     f"{was[1] if was[1] is not None else '-'} -> "
                     f"{low:#04x}/{flag:#04x} (stored, not interpreted)")
    return moved


def _friend_addrow_push(db, mid, h, recs, before):
    """Row-push (icon + comment) for friends gained MID-SESSION, both directions.

    The face icon and the comment have no pull carrier -- they ride the row
    push (`push_friend_icons`), and the only place that fired was the 2:3
    compose at login. A friendship formed mid-session therefore rendered
    pic-less and comment-less on BOTH ends until a relog re-served the list
    (live 2026-08-22, Fox <-> DeckTestNew: the request mail, acceptance mail
    and presence pushes all landed; the rows stayed bare). This pushes the
    same (slot, guid, icon, comment) record the 2:3 compose would have:

      * to THIS session, for every named peer this write stored -- the slot
        is the one `_friend_slots_assign` just reconciled (the client files
        the row at the +0x08 it sent, corrected in the reply), the guid the
        one the reply echo carries (`_FRIEND_PUT_GUIDS` = handle_guid);
      * to the PEER, when this write ACCEPTED their request (both rows just
        went active) -- their slot for us comes from THEIR slot map (taught
        by their own add write), their guid for us is our handle_guid, the
        value their reply echo carried.

    A peer whose slot this process never learned (service restart between the
    add and the accept) is logged and skipped -- their row heals at relog,
    which is the pre-existing behaviour. Rows with neither icon nor comment
    push nothing, same as the 2:3 compose.
    """
    hid = int(h["id"])
    my_name = h["handle_name"]
    ficons = handlelists._face_icons_by_handle()
    rcomments = handlelists._comments_by_handle()
    slots = {nm: s for nm, s in friendlist._FRIEND_PUT_ASSIGNED}
    mine = []
    for r in recs:
        nm = r.get("name")
        if not nm:
            continue
        prow = accounts.handle_by_name(db, nm)
        if prow is None:
            continue                # a caption or foreign name: nothing to paint
        phid = int(prow["id"])
        icon = int(ficons.get(phid) or 0)
        cmt = rcomments.get(phid)
        if nm in slots and (icon or cmt):
            mine.append((int(slots[nm]), accounts.handle_guid(phid),
                         icon, cmt))
        # THE OTHER DIRECTION, only on the accept transition: their row for us
        # already exists (their add created it) and just went active with ours.
        row = db.execute("SELECT status FROM friend WHERE handle_id = ?"
                         " AND peer_name = ?", (hid, nm)).fetchone()
        if row is None or row["status"] != accounts.STATUS_ACTIVE \
                or before.get(nm) == accounts.STATUS_ACTIVE:
            continue
        picon = int(ficons.get(hid) or 0)
        pcmt = rcomments.get(hid)
        if not (picon or pcmt):
            continue
        pmap = friendlist._friend_slots_map(phid) or {}
        pslot = next((int(s) for s, ent in pmap.items()
                      if ent and ent[0] == my_name), None)
        if pslot is None:
            log("lobby", f"  2:6 friend write: no slot known for {my_name!r} "
                         f"on {nm!r}'s list -- their row heals at relog")
            continue
        n = pushspool.push_friend_icons(None, int(prow["member_id"]),
                              [(pslot, accounts.handle_guid(hid), picon, pcmt)])
        log("lobby", f"  2:6 friend write: ACCEPT row push -> {nm!r} "
                     f"(slot {pslot}, icon {picon}, "
                     f"comment {'yes' if pcmt else 'no'}; {n} spooled)")
    if mine:
        n = pushspool.push_friend_icons(None, int(mid), mine)
        log("lobby", f"  2:6 friend write: row push for this session -- "
                     f"slots {[s for s, _g, _i, _c in mine]} ({n} spooled)")


def _friend_addrow_presence(db, mid, h, recs, before):
    """Assert CURRENT presence on a friendship formed MID-SESSION, both directions.

    *** THE ONLINE ICON IS PAINTED ONLY BY A PUSH, AND NOTHING PUSHED. *** The
    2:3 record carries no presence the client reads (polcore 0x037deeb0 never
    writes the slot+0x08 bits 11/13..15 the renderer 0x0488efc5 tests -- see
    `push_presence_burst`), so a friend row can only ever go online when a push
    says so. Two things push: `_broadcast_presence`, which fires on a
    TRANSITION, and the initial burst, which fires on a 2:3 FETCH. A friendship
    that becomes active mid-session is neither -- so both new rows sat grey
    until the peer next changed zone or status, or until a relog re-served the
    list.

    MEASURED LIVE 2026-08-25 (prod authserv.log/lobby.log), which is what this
    fixes: `Fox` added `clem` at 02:19:52Z, `clem`'s client accepted at
    02:20:16Z (`2:6 friend write: 1 entr(y|ies) [Fox] -> +0 requested, 1
    accepted`) and BOTH rows went active. `Fox`'s last 2:3 was 02:19:21Z --
    before the row existed -- so the burst could not carry it, and the next
    line naming `clem` in the whole log is a *zone* change at 02:23:17Z
    (`presence: clem -> online ... [slots [5]]`), three minutes later and
    entirely by luck. Between the accept and that accident the account holder
    was looking at a grey row for a friend who was plainly online. Reported the
    same night: "after someone accepted a friend request, they did not update
    in my friends list as being online".

    So this is the accept-time counterpart of the fetch-time burst, and it
    reuses it exactly -- `push_presence_burst` carries identities only and
    authserv resolves each subject's live state and zone at DELIVERY time, so
    the lobby container (which holds no session registry at all) does not have
    to guess. Both directions, because both sides just gained a row that cannot
    paint itself:

      * to THIS session, for every peer this write moved to ACTIVE;
      * to the PEER, for us -- the accepter's write flips the ASKER's row too
        (`POL_FRIEND_ACCEPT_BOTH`), and the asker's client sends nothing at
        all, so their side has no other opportunity.

    The slot comes from `_friend_slot`, i.e. the numbering we actually served /
    just assigned, for the same reason the push identity comes from
    `_push_identity_guid`: a presence record whose slot or guid disagrees with
    the client's row is dropped SILENTLY, and a silent drop is indistinguishable
    from this bug. A peer whose slot this process never learned is logged and
    skipped -- their row heals at relog, which is the pre-existing behaviour.

    POL_FRIEND_ADD_PRESENCE=0 reverts. The master presence gates still apply
    underneath (`push_presence_burst` checks POL_PRESENCE_PUSH and
    POL_FRIEND_PRESENCE_BURST / presence.ctl `burst=`), so this cannot turn
    presence on for a deployment that has it off.
    """
    hid = int(h["id"])
    my_name = h["handle_name"]
    assigned = {nm: s for nm, s in friendlist._FRIEND_PUT_ASSIGNED}
    mine = []
    for r in recs:
        nm = r.get("name")
        if not nm:
            continue
        prow = accounts.handle_by_name(db, nm)
        if prow is None:
            continue                # a caption or a foreign name: nobody to assert
        phid = int(prow["id"])
        if phid == hid:
            continue                # the self-add guard's leftovers
        row = db.execute("SELECT status FROM friend WHERE handle_id = ?"
                         " AND peer_name = ?", (hid, nm)).fetchone()
        # ONLY THE TRANSITION. A write that merely re-names an existing friend
        # (a rename and a delete are both 2:6s naming the whole list) has
        # already had its presence asserted by the burst; re-asserting on every
        # list write would put a push on the wire for every row every time.
        if row is None or row["status"] != accounts.STATUS_ACTIVE                 or before.get(nm) == accounts.STATUS_ACTIVE:
            continue
        slot = assigned.get(nm)
        if slot is None:
            slot = friendroster._friend_slot(db, hid, phid)
        if slot is None:
            log("lobby", f"  2:6 friend write: no slot known for {nm!r} on our "
                         f"own list -- presence not asserted, the row heals at "
                         f"relog")
        else:
            mine.append((int(slot), pushrecord._push_identity_guid(db, phid), phid))
        pslot = friendroster._friend_slot(db, phid, hid)
        if pslot is None:
            log("lobby", f"  2:6 friend write: no slot known for {my_name!r} "
                         f"on {nm!r}'s list -- their presence row heals at relog")
            continue
        n = pushspool.push_presence_burst(None, int(prow["member_id"]),
                                [(int(pslot), pushrecord._push_identity_guid(db, hid), hid)])
        log("lobby", f"  2:6 friend write: ACCEPT presence push -> {nm!r} "
                     f"(our slot {pslot} on their list; {n} queued)")
    if mine:
        n = pushspool.push_presence_burst(None, int(mid), mine)
        log("lobby", f"  2:6 friend write: presence push for this session -- "
                     f"slots {[s for s, _g, _h in mine]} ({n} queued)")


def _capture_friend_put(pt):
    """Persist a 02:06 friend-list write against the session's primary handle."""
    if accounts is None:
        return
    start, raw_recs = _friend_put_records(pt)
    recs = _parse_friend_put(pt)
    # Keep the raw records for the `echo` reply shape (see _friend_put_reply).
    # This runs BEFORE the reply is composed, so the echo has them in time.
    del lobbysearch._LAST_FRIEND_PUT[:]
    lobbysearch._LAST_FRIEND_PUT.extend(raw_recs)
    # Same lifetime, same reason -- and cleared HERE rather than beside the
    # assignment below, so an early return cannot leave the next reply confirming
    # a slot from the write before it.
    del friendlist._FRIEND_PUT_ASSIGNED[:]
    friendlist._FRIEND_PUT_GUIDS.clear()
    if os.environ.get("POL_FRIEND_PUT_DUMP", "1") == "1":
        # The record layout past name+guid is still unread. Dumping the preamble
        # and the first records whole is what will name the rest of the fields --
        # the 0x00/0x04 status words in particular, which is how ACCEPT is going
        # to be told apart from REQUEST on the wire. On by default because the
        # very first real write is the one that answers this.
        log("lobby", f"  2:6 {len(pt)}B, grid@0x{start:X}, {len(raw_recs)} record(s)")
        log("lobby", "  2:6 preamble: " + bytes(pt[0x28:start or 0x15C]).hex())
        for i, r in enumerate(raw_recs[:6]):
            log("lobby", f"  2:6 rec[{i}]: {r.hex()}")
    if not raw_recs and len(pt) > _FRIEND_PUT_STARTS[0] + 4:
        log("lobby", f"  2:6 friend write: {len(pt)}B did not parse "
                     f"(0x15C + N*{_FRIEND_PUT_REC} + 4 expected) "
                     "-- NOT touching the stored list")
        return
    try:
        db = accounts.connect(os.environ.get("POL_ACCOUNTS_DB", accounts.DEFAULT_DB))
        try:
            mid = lobbysession._session_member_id()
            h = db.execute("SELECT id, handle_name FROM handle WHERE member_id = ?"
                           " ORDER BY is_primary DESC, id ASC LIMIT 1",
                           (mid,)).fetchone() if mid else None
            if h is None:
                log("lobby", "  2:6 friend write: no handle for this session")
                return
            # AN EMPTY PUT MEANS "I HAVE NO FRIENDS", and taken literally it wipes
            # the list -- the most destructive thing this parser can do on a
            # misread. It used to be refused outright, which was right while the
            # grid was a deduction and wrong once it stopped being one: a real
            # "delete my only friend" is an arithmetically PERFECT frame whose one
            # record has no name in it (SE's own deletion capture, and the account
            # holder's, are the same shape), so refusing it hung the UI forever.
            #
            # So the test is now "did the frame fit the builder's formula", not
            # "did anything survive parsing". A length that does not fit is still
            # refused, because that is the misread the guard was built for.
            # A NAMELESS RECORD IS A DELETE, NOT AN EMPTY LIST. 2:6 sends one
            # record per change (measured 2026-08-13: every write is N=1, even
            # from an account holding two friends), so "no names in this write"
            # cannot mean "I have no friends" -- it means the row named by the
            # id in that record is going. Taking it as a whole-list wipe was the
            # first cut here and it deleted nothing only because deletion by
            # omission is off; taken literally it would have emptied the list.
            fits = (len(pt) - 4 - _FRIEND_PUT_STARTS[0]) % _FRIEND_PUT_REC == 0
            wire = _friend_put_wire_ref(pt)
            deletes = _friend_put_deletes(pt) if fits else []
            # THE ORDERING WE SERVED, not the ordering we now hold. The client
            # indexes the 2:3 reply it was given and never renumbers, so a
            # re-query is one row short per delete already made this session --
            # see the slot-map note above `_friend_slots_publish`.
            #
            # Resolved UP FRONT now rather than only when there are deletes: the
            # rename and ignore passes below index the same served list, and a
            # rename must be recognised before the delete loop runs (it used to
            # BE the delete loop -- see `_friend_put_renames`).
            slot_map = friendlist._friend_slots_map(int(h["id"]))
            if slot_map is None:
                # We never served this handle a 2:3 (restarted mid-session).
                # The live list is the best guess available and is right up
                # to the session's first delete.
                slot_map = _friend_slot_map_live()
                if deletes:
                    log("lobby", "  2:6 friend write: no 2:3 of ours to index -- "
                                 f"resolving the slot against the live list "
                                 f"({len(slot_map)} row(s))")
            # *** RENAMES, BEFORE ANYTHING ELSE TOUCHES THESE RECORDS. ***
            # A rename record has no acceptable handle name in it, so every path
            # downstream reads it as a deletion of the slot it names -- which is
            # the renamed friend's own slot. Claiming them here is what stops a
            # caption from deleting the person it was meant to caption.
            renamed = set()
            if fits and os.environ.get("POL_FRIEND_RENAME", "1") == "1":
                for row, label, rec in _friend_put_renames(db, int(h["id"]), pt,
                                                           slot_map):
                    accounts.set_friend_label(db, int(h["id"]), int(row["id"]),
                                              label)
                    renamed.add(bytes(rec))
                    log("lobby", f"  2:6 friend write: RENAME "
                                 f"{row['peer_name']!r} -> {label!r} "
                                 "(a per-friend label, not a new friend; "
                                 "POL_FRIEND_RENAME=0 restores the old "
                                 "read-it-as-a-delete behaviour)")
            # A record we just took as a rename is not a delete and not an add.
            deletes = [r for r in deletes if bytes(r) not in renamed]
            if fits and friendlist._friend_bitfield_mode():
                deletes, extra = _friend_put_bitfield_deletes(
                    pt, start, raw_recs, deletes, renamed)
                if extra:
                    # Deleted, so it must not also be read as an add below.
                    recs = [r for r in recs if bytes(r["raw"]) not in extra]
            for rec in deletes:
                # THE SLOT, and on a live record ONLY the slot. It is the one
                # identifier here that WE minted, so it resolves a row the client
                # has only ever read; the two client-minted ids below are
                # uninitialised buffer (proven on SE's own capture -- see
                # `_friend_put_ref`), so letting them answer for a slot that
                # already failed is how a delete reaches somebody at random.
                gone = None
                slot = _friend_put_slot(rec)
                if slot is not None:
                    hit = _friend_slot_row(slot_map, slot)
                    if hit is None:
                        log("lobby", f"  2:6 friend write: delete names 2:3 slot "
                                     f"{slot}, which is not one of the "
                                     f"{len(slot_map)} row(s) we served "
                                     "-- ignoring it rather than guessing")
                        continue
                    gone = accounts.remove_friend_by_row(db, int(h["id"]), hit[1])
                    # The slot is spent either way: the row is gone, or it was
                    # already gone. Leaving it mapped would let a repeat of the
                    # same slot delete a row that has since been re-added into
                    # that db id, and the client's own table has the row hidden.
                    friendlist._friend_slots_update(int(h["id"]), slot, None)
                    if gone:
                        log("lobby", f"  2:6 friend write: DELETE {gone!r} "
                                     f"(2:3 slot {slot})")
                    else:
                        log("lobby", f"  2:6 friend write: 2:3 slot {slot} was "
                                     f"{hit[0]!r} (row {hit[1]}), which is no "
                                     "longer in the store -- nothing to delete")
                    continue
                for fid in friendlist._friend_row_handles(rec) + friendlist._friend_row_handles(wire):
                    gone = accounts.remove_friend_by_peer_handle(
                        db, int(h["id"]), fid)
                    if gone:
                        log("lobby", f"  2:6 friend write: DELETE {gone!r} "
                                     f"(our head word, handle {fid})")
                        break
                if gone:
                    continue
                gone = accounts.remove_friend_in_record(db, int(h["id"]), rec,
                                                        wire_ref=wire)
                if gone:
                    log("lobby", f"  2:6 friend write: DELETE {gone!r}")
                else:
                    log("lobby", "  2:6 friend write: a delete matched no stored "
                                 f"row -- no slot, wire ref {wire.hex() or '-'}, "
                                 f"{len(slot_map or {})} row(s) served. The SLOT "
                                 "is the identifier that should have carried this "
                                 "(record +0x04, see _FRIEND_PUT_SLOT_AT); it read "
                                 "as None, so the record failed the +0x06 == 0x0A "
                                 "live check -- dump it and re-check that constant.")
            if not recs and not fits:
                log("lobby", f"  2:6 friend write: {len(pt)}B does not fit "
                             "0x15C + N*168 + 4 and names nobody -- ignored")
                return
            before = {r["peer_name"]: r["status"] for r in
                      accounts.list_friends(db, int(h["id"]), status=None)}
            # SELF-ADD VISIBILITY (2026-08-21). The TM
            # room sidebar can offer "Ask to become friends" on the player's own
            # row; if that produces a 2:6 naming one of this member's OWN handles,
            # the self-add guard in accounts.request_friend drops it. Name the
            # records that resolve to self here so the repro is legible
            # in the log and the guard's effect is measured, not inferred.
            own_handle_names = {
                r["handle_name"] for r in db.execute(
                    "SELECT handle_name FROM handle WHERE member_id = ?", (mid,))
            } if mid else set()
            self_named = [r["name"] for r in recs
                          if r.get("name") in own_handle_names]
            if self_named:
                log("lobby", f"  2:6 friend write: SELF-ADD in this write "
                             f"[{', '.join(self_named)}] -- these name this "
                             "member's own handle(s); request_friend returns "
                             "'self' and writes no row (POL_FRIEND_NO_SELF=0 "
                             "disables the guard). This is the TM member-sidebar "
                             "self-request reaching the server.")
            add, acc, upd, rem, kept, kgrp = accounts.replace_friends(
                db, int(h["id"]), recs)
            log("lobby", f"  2:6 friend write: {len(recs)} entr(y|ies) "
                         f"[{', '.join(r['name'] for r in recs) or 'empty'}] "
                         f"-> +{add} requested, {acc} accepted, ~{upd} ~, -{rem}")
            # THE RENAME BEING TAKEN BACK. The capture's very next write after
            # "Cool friend :3" put `Cyn` -- the handle's own name -- straight
            # back into the same slot. That record IS a valid handle name, so it
            # takes the ordinary upsert path above and never reaches
            # `_friend_put_renames`; without this the caption would outlive the
            # rename that was undone, and the friend list would keep showing it.
            if os.environ.get("POL_FRIEND_RENAME", "1") == "1":
                for r in recs:
                    row = db.execute(
                        "SELECT id, label FROM friend WHERE handle_id = ?"
                        " AND peer_name = ?",
                        (int(h["id"]), r["name"])).fetchone()
                    if row is not None and row["label"]:
                        accounts.set_friend_label(db, int(h["id"]),
                                                  int(row["id"]), None)
                        log("lobby", f"  2:6 friend write: rename CLEARED for "
                                     f"{r['name']!r} (was {row['label']!r}) -- "
                                     "the client wrote the real name back")
            # THE IGNORE FLAGS, for every record that resolves to a row -- add,
            # rename and plain re-write alike. Round-trip only; see
            # `_friend_put_apply_flags`.
            if fits and os.environ.get("POL_FRIEND_IGNORE_FLAGS", "1") == "1":
                try:
                    _friend_put_apply_flags(db, int(h["id"]), pt, slot_map)
                except Exception as exc:
                    log("lobby", f"  2:6 friend write: ignore flags not stored "
                                 f"({exc!r}) -- the reply still echoes them")
            # TEACH THE SLOT MAP THE ROWS THIS WRITE NAMED. An ADD carries a slot
            # too (SE's capture: `Yatih` added at +0x04 = 1 with N = 1), and the
            # client puts the new row there in its own table without re-reading
            # the list -- so without this, deleting a friend you added in the same
            # session lands on a slot we never served and is refused.
            for r in recs:
                rslot = _friend_put_slot(r.get("raw") or b"")
                if rslot is None:
                    continue
                row = db.execute("SELECT id, peer_handle, peer_guid FROM friend"
                                 " WHERE handle_id = ? AND peer_name = ?",
                                 (int(h["id"]), r["name"])).fetchone()
                if row is None:
                    continue
                got = friendlist._friend_slots_assign(int(h["id"]), rslot,
                                           (r["name"], int(row["id"])))
                friendlist._FRIEND_PUT_ASSIGNED.append((r["name"], got))
            # THE PEER'S REAL GUID, for `_friend_put_reply` to fill into the reply
            # the client rebuilds its match array from -- correcting a search-add's
            # guid-0 local row without a relog.
            # A SEPARATE pass over every record, by NAME: the slot loop above skips
            # any record whose +0x06 live marker is absent (an ADD carries none, so
            # the guid never got populated -- the 2026-08-19 miss), and the friend
            # row may not carry a peer_handle. Resolving name -> handle -> guid is
            # the same path the select/profile record uses, and it is what proved
            # to serve CredibleAsh's real guid.
            for r in recs:
                try:
                    # Case-insensitive when unambiguous (POL_HANDLE_NOCASE):
                    # "kelpie" must reach kElpie's guid, not a hash of the name.
                    hrow = accounts.handle_by_name(db, r["name"])
                    if hrow is not None:
                        g = accounts.handle_guid(int(hrow["id"]))
                    else:
                        frow = db.execute(
                            "SELECT peer_guid FROM friend WHERE handle_id = ?"
                            " AND peer_name = ?",
                            (int(h["id"]), r["name"])).fetchone()
                        g = int(frow["peer_guid"] or 0) if frow else 0
                    if g:
                        friendlist._FRIEND_PUT_GUIDS[r["name"]] = g
                except Exception as exc:
                    log("lobby", f"  2:6 friend write: guid for {r['name']!r} "
                                 f"unavailable ({exc!r}); reply left as echoed")
            # PAINT THE ROWS THIS WRITE JUST CREATED OR ACTIVATED -- the icon
            # and comment only ever rode the login 2:3 row push, so a friend
            # gained mid-session stayed bare on both ends until a relog. See
            # _friend_addrow_push. Guarded like every push issued from a
            # handler (the POL-0008 lesson). POL_FRIEND_ADD_ROWPUSH=0 reverts.
            if os.environ.get("POL_FRIEND_ADD_ROWPUSH", "1") == "1":
                try:
                    _friend_addrow_push(db, mid, h, recs, before)
                except Exception as exc:
                    log("lobby", f"  2:6 friend write: add-row push skipped "
                                 f"({exc!r})")
            # ...AND MAKE THE NEW ROW ABLE TO GO ONLINE. The icon push above
            # paints the picture; only a PRESENCE push can light the online
            # icon, and neither of the two things that send one (a transition,
            # a 2:3 fetch) happens on an accept. See _friend_addrow_presence.
            # POL_FRIEND_ADD_PRESENCE=0 reverts.
            if os.environ.get("POL_FRIEND_ADD_PRESENCE", "1") == "1":
                try:
                    _friend_addrow_presence(db, mid, h, recs, before)
                except Exception as exc:
                    log("lobby", f"  2:6 friend write: add-row presence "
                                 f"skipped ({exc!r})")
            # TELL THE OTHER PERSON, the way SE does: as a MESSAGE. Its capture
            # carries `Let's be friend` (kind 0x8080) and `Friend registration
            # accepted` (0x8480) on the message channel, which is why its 2:3
            # never showed an incoming request -- the request IS the message.
            #
            # **OFF, BECAUSE THE CLIENT POSTS THEM ITSELF.** Watched live
            # 2026-08-16: six seconds after the accept landed, the accepting
            # client sent its own 3:1 `Friend registration accepted` for the same
            # peer, and the recipient's mailbox held two. Every 3:1 in SE's
            # capture comes from a client too, so the server minting one is
            # duplication, not delivery. Kept because it is the only way to post
            # a notification for a state the client never announced -- the
            # requests that predate this code were backfilled with it, and a
            # client that turns out not to send its own would need it.
            # *** THE SWITCH WAS TOO COARSE, AND IT COST THE REQUEST. ***
            #
            # The live observation behind turning this off was specifically about
            # the ACCEPT: the accepting client sent its own 3:1 `Friend
            # registration accepted` six seconds later and the mailbox held two.
            # One switch then disabled BOTH mints -- and nothing was ever observed
            # duplicating the REQUEST. Result: an incoming friend request produced
            # no mail, and because the mail push is what carries a live
            # notification (`push: MAIL ... 1 session(s)`, working), the recipient
            # was told nothing until they went looking. Reported 2026-08-16:
            # "friend requests don't seem to push".
            #
            # Split in two, defaulting the way the evidence points: mint the
            # REQUEST (nothing has been seen to duplicate it), stay off for the
            # ACCEPT (seen duplicating). POL_FRIEND_REQUEST_MAIL=0 restores the
            # old silence; POL_FRIEND_ACCEPT_MAIL=1 re-enables the other half if
            # a client turns out not to post its own.
            # *** BOTH OFF NOW -- RELAY-ONLY, MATCHING SE. Settled by a two-sided
            # retail capture 2026-08-19 (Examplemember <-> Fox, both machines
            # capturing). SE's server mints NOTHING: each client posts its OWN
            # notification as a 3:1 and the server just relays it.
            #   ASKER    (Examplemember): 2:6 add, then a 3:1 `Let's be friends!`.
            #   ACCEPTER (Fox): reads the request (3:0/3:2), 2:6 add, then a 3:1
            #                   `Friend registration accepted` (kind 0x8480, empty
            #                   body -- byte-identical to what we mint).
            # `_capture_resource_write` ALREADY stores a client 3:1 and pushes it
            # live (`_mail_announce`), so with minting off the client's own mail is
            # the sole, non-duplicated, instant notification -- exactly retail.
            # This RETIRES the whole mint/dedupe dance (`_MAIL_MINTED`,
            # `_mail_notice_already_there`): there is nothing to de-duplicate when
            # we never mint. The "Steam Deck posts nothing" that once justified
            # minting was a FLAWED measurement -- it is the same `pol.exe`/polcore
            # under Proton, and this capture shows the PC Viewer posts in BOTH
            # roles, so every client posts its own. POL_FRIEND_REQUEST_MAIL=1 /
            # POL_FRIEND_ACCEPT_MAIL=1 restore minting for a client that genuinely
            # turns out not to (none seen).
            want_req = os.environ.get("POL_FRIEND_REQUEST_MAIL", "0") == "1"
            want_acc = os.environ.get("POL_FRIEND_ACCEPT_MAIL", "0") == "1"
            if want_req or want_acc:
                me = h["handle_name"]
                for r in recs:
                    was = before.get(r["name"])
                    if was is not None and was != accounts.STATUS_INVITED:
                        continue                    # nothing changed for them
                    peer = accounts.handle_by_name(db, r["name"])
                    if peer is None:
                        continue                    # not one of ours to post to
                    kind = (lobbymail.MAIL_KIND_FRIEND_REQUEST if was is None
                            else lobbymail.MAIL_KIND_FRIEND_ACCEPTED)
                    if lobbymail._mail_notice_already_there(int(peer["member_id"]), me, kind):
                        # The peer's client got there first with its own 3:1 --
                        # see `_mail_notice_already_there`. Minting now is the
                        # duplicate, not the delivery.
                        log("lobby", f"  2:6 friend write: {r['name']!r} already "
                                     f"holds this notice (kind {kind:#06x}); not "
                                     "minting a second copy")
                        continue
                    if was is None:
                        if not want_req:
                            continue
                        lobbymail._mail_mint(me, accounts.handle_guid(int(h["id"])),
                                   accounts.handle_guid(int(peer["id"])),
                                   friendlist._FRIEND_REQ_SUBJECT, friendlist._FRIEND_REQ_BODY,
                                   kind=lobbymail.MAIL_KIND_FRIEND_REQUEST)
                        lobbymail._mail_note_minted(int(h["id"]), int(peer["id"]),
                                          lobbymail.MAIL_KIND_FRIEND_REQUEST)
                        log("lobby", f"  2:6 friend request: posted invite mail "
                                     f"to {r['name']!r} -- it pushes live")
                    else:
                        if not want_acc:
                            continue
                        lobbymail._mail_mint(me, accounts.handle_guid(int(h["id"])),
                                   accounts.handle_guid(int(peer["id"])),
                                   friendlist._FRIEND_ACC_SUBJECT, friendlist._FRIEND_ACC_BODY,
                                   kind=lobbymail.MAIL_KIND_FRIEND_ACCEPTED)
                        lobbymail._mail_note_minted(int(h["id"]), int(peer["id"]),
                                          lobbymail.MAIL_KIND_FRIEND_ACCEPTED)
                        log("lobby", f"  2:6 friend accept: posted acceptance "
                                     f"mail to {r['name']!r} -- it pushes live, "
                                     "and closes their 'Awaiting confirmation'")
            if kgrp:
                # Not a request and not a deletion -- a friend-list write simply
                # does not carry groups, so their absence means nothing.
                log("lobby", f"  2:6 friend write: {len(kgrp)} group(s) not in "
                             f"this list, left alone [{', '.join(kgrp)}]")
            if kept:
                # THE FIRST REAL WRITE ANSWERS THIS. If the client does echo
                # incoming requests it has not acted on, this line never appears;
                # if it appears, the omission was the client not listing them and
                # protecting them was right. Either way one capture settles it.
                log("lobby", f"  2:6 friend write: kept {len(kept)} incoming "
                             f"request(s) the write omitted [{', '.join(kept)}] "
                             "-- decline is explicit; POL_FRIEND_PUT_DECLINE=1 "
                             "makes an omission delete them")
            if add or acc:
                # Print the resulting status per name. The mirror writes a row on
                # ANOTHER account, which is the one effect here that the client
                # doing the writing can never show you, so the log is the only
                # place the two sides of a request are visible together.
                now = {r["peer_name"]: r["status"] for r in
                       accounts.list_friends(db, int(h["id"]), status=None)}
                log("lobby", "  2:6 friend write: " + ", ".join(
                    f"{r['name']}={now.get(r['name'], 'gone')}" for r in recs))
                # A request that is still 'pending' is one we just made of
                # someone else -- push event 7 so their client shows it without
                # a relog, which is what SE does (the account holder received
                # exactly this from their friend's second handle).
                #
                # *** RETIRED 2026-08-22 -- THE EVENT-7 PUSH FABRICATED A BLANK
                # MESSAGE AND CRASHED THE RECIPIENT ON ITS FIRST-EVER LIVE
                # DELIVERY. *** Measured on prod (lobby.log 19:55:08-19:55:15Z,
                # LaptopTest2 -> DeckTestNew via the TM member sidebar): the
                # client treats a delivered event-7 record as a MAIL ANNOUNCEMENT
                # -- it canonicalised the record's state byte 03->02 and 3:0'd it
                # as an `O/m/` token, exactly as it does a real mail push. No
                # such message exists (the requester's own 3:1 arrives ~2 s
                # AFTER the 2:6, so at push time there is nothing to name), the
                # store answered the miss with 12 zero bytes ('no data stored
                # yet'), the client listed it as a BLANK message next to the
                # real request, and OPENING the zero object crashed the client.
                # That was also the first time this push ever reached a live
                # session (1 `push: event 7` in all of authserv.log), so the
                # path had never once worked.
                #
                # It also cannot do its stated job: the greeting lookup below is
                # empty BY CONSTRUCTION on a fresh add, because the message that
                # carries the greeting is the 3:1 the client has not sent yet.
                # Relay-only (a deliberate design decision, see the mint note above)
                # already covers the notification: the requester's client posts
                # its own `Let's be friend` 3:1 and `_mail_announce` pushes THAT
                # message's real token, which is the second, readable message
                # the recipient saw in the same incident; `_friend_addrow_push`
                # paints the INVITED row mid-session. POL_FRIEND_REQUEST_EVENT_PUSH=1
                # re-arms this for measuring what SE's own event-7 record looks
                # like -- do not turn it on for delivery.
                #
                # OK: THE GREETING IS RECOVERED, NOT INVENTED, AND IT WAS NEVER
                # IN THE 2:6 RECORD. The note that stood here said the parser
                # "does not currently recover it" and left the field empty --
                # correct about the outcome, wrong about where to look. SE's
                # own capture settles it two lines further down this same
                # function: a friend request travels as a MESSAGE the requester's
                # client posts itself (kind 0x8080, `Let's be friend`), and the
                # words the requester typed are that message's SUBJECT. The
                # 2:6 record never carried them, so no capture of one ever could
                # have shown "where the client puts it".
                #
                # So the greeting is read back out of the peer's own mailbox --
                # `_mail_meta` already decodes the subject straight from the
                # `O/m/` path, which is why this costs one lookup and no new
                # parsing. `_PUSH_TEXT_MAX` is 15 bytes and SE's sample was
                # "o/", so a real greeting fits the field it is going into.
                #
                # WARNING: EMPTY IS STILL A CORRECT ANSWER, and it is why this stays a
                # lookup rather than a default. A client that posts no message of
                # its own has told us nothing to forward -- the Steam Deck is
                # exactly that client (see the accept-mail note above) -- and for
                # it the honest push carries no greeting at all. Our own minted
                # `_FRIEND_REQ_SUBJECT` is deliberately NOT used as a stand-in:
                # it is our wording, not the requester's, and putting it in a
                # field that means "what this person said to you" would be the
                # fabrication the old note was right to refuse.
                if os.environ.get("POL_FRIEND_REQUEST_EVENT_PUSH", "0") == "1":
                    for rec in recs:
                        if now.get(rec["name"]) != "pending":
                            continue
                        peer = db.execute(
                            "SELECT peer_handle FROM friend WHERE handle_id = ? "
                            "AND peer_name = ?",
                            (int(h["id"]), rec["name"])).fetchone()
                        if peer and peer["peer_handle"]:
                            greeting = lobbymail._friend_request_greeting(
                                db, int(peer["peer_handle"]), h["handle_name"])
                            if greeting:
                                log("lobby", f"  2:6 friend write: forwarding "
                                             f"{rec['name']!r} the greeting "
                                             f"{greeting!r} that "
                                             f"{h['handle_name']!r} actually "
                                             "typed")
                            pushspool.push_to_handle(db, int(peer["peer_handle"]),
                                           pushchannel._PUSH_EV_FRIEND_REQUEST, greeting,
                                           from_handle_id=int(h["id"]),
                                           from_name=h["handle_name"])
        finally:
            db.close()
    except Exception as exc:
        log("lobby", f"  2:6 friend write: DB unavailable ({exc!r})")


#: The 07:01 CREATE GROUP request, decrypted from a live create (2026-08-11):
#:
#:     0000  02 07 01 00 20 00 00 00  ...      header: type 2, op 7:1, paylen 0x20
#:     0018  8b5dcc9a479ab782 c1cce197e676b368  the 16-byte session token
#:     0028  46 6f 78 47 72 6f 75 70  00 00 ..  payload+0x00: "FoxGroup", NUL-padded
#:     0044  b5 d0 e8 b7                        the request's own checksum
#:
#: So the name is the whole payload bar the 4-byte trailer -- there is no id, no
#: member list and no flags field in the request. The reply stays the 16 zero
#: bytes we have always sent (the client accepts it; the create completes).
_GROUP_NAME_OFF = 0x28                  # payload +0x00
_GROUP_NAME_MAX = 0x20 - 4              # payload length less the checksum


#: 07:01's reply payload, MEASURED 2026-08-15 off SE's live create of
#: `CRAZY PEOPLE` (checksum verified):
#:
#:     6dcf0200 00000000 | b05b4044 | 1d2b4344
#:     ^ u64 the NEW GROUP ID ^ +0x08  ^ the 4-byte checksum
#:
#: We answered 16 zero bytes, i.e. group id 0 -- and `_group_record` documents
#: exactly why that is not harmless: `find @0x37e7d40` opens `or eax,esi; je ->
#: return 0`, so a zero id never matches an existing slot and 0x37e7d80
#: allocates a FRESH one, of which there are four. The id has to come back here.
_GROUP_CREATE_ID_OFF = 0x00
_GROUP_CREATE_F08_OFF = 0x08
#: SE's `+0x08` is 0x44405bb0 and we cannot read it: it is not the group id (that
#: is the u64 at +0x00), not a creation time (as a unix stamp it lands in 2006,
#: and this group was made during the capture) and not a length. Left ZERO, the
#: value every other unknown in this protocol takes, and sweepable with
#: `create_f08=` in the group control file rather than replayed blind -- SE's
#: constant may well be account- or shard-scoped, in which case replaying it is
#: worse than sending nothing.
_GROUP_CREATE_REPLY = 12                # 12 data + the signer's 4-byte trailer
