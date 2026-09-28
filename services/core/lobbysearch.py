"""Member search: calibration, result payloads, TLV parsing."""
import os
import re
import struct
from srvcore import log
from .deps import accounts
from . import lobbyreply, lobbyrooms, lobbysession, paylen, profilerecord



def _search_result_payload(n, pt):
    """The rows behind a hit count -- content still being mapped.

    ZEROS BY DEFAULT, and that default is not laziness. The record is enormous
    (60000 bytes for one row, live 2026-08-12) and column-structured -- the client
    struct builder at 0x37e03a0 walks `desc[0x1b]` columns of stride 0x18 and
    charges 16 bytes of header per column -- so a record full of marker text is
    read as columns whose length fields are ASCII garbage. That is what crashed
    the Viewer outright. All-zero columns are the blank-record case: nothing to
    walk, nothing to dereference.

    Mapping the layout therefore needs a probe that keeps the record ZERO except
    for one narrow window: POL_SEARCH_RESULT="window:0x40:16" writes markers at
    +0x40..+0x50 only, so a crash costs one 16-byte range instead of the record.
    `marker` (the whole record) is still available and still expected to crash.
    """
    # The control file wins over the env var, so the record can be mapped one
    # window at a time WITHOUT recreating the container between probes. See
    # `_search_calib`. Empty string there = "no opinion", i.e. use the env var.
    mode = (_search_calib()["result"]
            or os.environ.get("POL_SEARCH_RESULT", "profile")).strip().lower()
    if mode.startswith("window:"):
        out = bytearray(n)
        try:
            _, off, ln = mode.split(":", 2)
            off, ln = int(off, 0), int(ln, 0)
        except ValueError:
            log("lobby", f"bad POL_SEARCH_RESULT {mode!r}; serving zeros")
            return bytes(out)
        mark = lobbyreply._marker_payload(off + ln)[off:off + ln]
        out[off:off + len(mark)] = mark
        log("lobby", f"  select probe: markers at +{off:#x}..{off + ln:#x} only")
        return bytes(out)
    if mode == "profile":
        # *** SOLVED, CONFIRMED LIVE 2026-08-12: a search result row IS a profile
        # record. *** The names render in the results list. It was worth trying
        # before any marker sweep because the sizes agree exactly -- the row
        # measured 600 bytes live, and 05:04 serves a 600-byte profile record
        # (604 on the wire, the extra 4 being the checksum) -- and "a member
        # search returns member profiles" is what the feature is for.
        #
        # This is now the DEFAULT rather than a probe. `zero` (the old default)
        # renders one blank entry per hit, which is what a solved layout should
        # no longer be doing.
        #
        # One record per announced hit, each built for the handle that MATCHED.
        # The row fetch carries no z_hid, so the subject comes from the search
        # (`_LAST_SEARCH`), not from the request.
        # ROOM ROWS ARE A DIFFERENT RECORD. A member hit is a 600-byte profile;
        # a room is the 160-byte 0x03-tagged record SE sends (see the ROOM SEARCH
        # note). Same opcode, same select path -- the result SET decides, which is
        # why the kind is remembered when the 5:3 ran rather than guessed here.
        rows, kind = _search_result_get()
        if kind == "zone":
            out = bytearray(n)
            served = 0
            for i, row in enumerate(rows):
                base = i * lobbyrooms._ZONE_SUMMARY_RECORD
                if base + lobbyrooms._ZONE_SUMMARY_RECORD > n:
                    break
                out[base:base + lobbyrooms._ZONE_SUMMARY_RECORD] = lobbyrooms._zone_summary_record(row)
                served += 1
            log("lobby", f"  select rows: {served} zone summar"
                         f"{'y' if served == 1 else 'ies'} [" + "; ".join(
                             f"{r['zone']}: {r['rooms']}r/{r['users']}u"
                             for r in rows[:served]) + "]")
            return bytes(out)
        if kind == "room":
            out = bytearray(n)
            for i in range(max(1, n // lobbyrooms._ROOM_RECORD)):
                base = i * lobbyrooms._ROOM_RECORD
                if base + lobbyrooms._ROOM_RECORD > n or i >= len(rows):
                    break
                out[base:base + lobbyrooms._ROOM_RECORD] = lobbyrooms._room_record(rows[i])
            # name=occupancy, because "the count in the browser is stale" (live
            # 2026-08-19) cannot be split into served-wrong vs drawn-wrong
            # without seeing the +0x99 byte that actually went out.
            occ = ", ".join("%s=%d" % (r["handle_name"],
                                       lobbyrooms._room_members(lobbyrooms._room_chan(r)))
                            for r in rows[:4])
            log("lobby", f"  select rows: {min(len(rows), max(1, n // lobbyrooms._ROOM_RECORD))} "
                         f"ROOM record(s) of {lobbyrooms._ROOM_RECORD}B [{occ or 'none'}]")
            return bytes(out)
        rec = int(os.environ.get("POL_SEARCH_RECORD", "0"), 0) or 600
        out = bytearray(n)
        for i in range(max(1, n // rec)):
            base = i * rec
            if base + rec > n:
                break
            if i >= len(rows):
                # NO ROW FOR THIS SLOT -> leave it zero. Passing force_hid=None
                # would make _profile_record fall back to the SESSION handle, so
                # every unfilled slot would render as the searcher's own profile
                # under somebody else's name -- the same profile-bleed class of
                # bug that z_hid resolution was fixed for.
                continue
            out[base:base + rec] = profilerecord._profile_record(rec, force_hid=rows[i]["id"])
        log("lobby", f"  select rows: {min(len(rows), max(1, n // rec))} profile "
                     f"record(s) of {rec}B "
                     f"[{', '.join(str(r['handle_name']) for r in rows[:4]) or 'none'}]")
        return bytes(out)
    if mode == "marker":
        return lobbyreply._marker_payload(n)
    if mode == "markerz":
        return lobbyreply._marker_payload_z(n)
    if mode != "zero":
        try:
            return bytes.fromhex(mode)[:n].ljust(n, b"\x00")
        except ValueError:
            log("lobby", f"bad POL_SEARCH_RESULT {mode!r}; serving zeros")
    return bytes(n)


def _search_calib_path():
    """Where the live calibration byte count is read from, per fetch."""
    return os.environ.get(
        "POL_SEARCH_CALIB",
        os.path.join(os.environ.get("POL_LOG_DIR", "/logs"), "search-calib.txt"))


def _search_calib():
    """The live probe settings, as `{"bytes": int, "result": str}`.

    Two accepted forms, because the file started life holding only a number:

        600                         a bare byte count (the original form)
        bytes=600                   the same, named
        result=window:0x40:16       row CONTENT, overriding POL_SEARCH_RESULT

    The row content is here and not only in the env var for the same reason the
    byte count is: mapping the 600-byte record means stepping a marker window
    across it dozens of times, and doing that through compose would recreate the
    container -- and drop the client's session -- on every single step.

    Deliberately forgiving. A missing file, an empty one, a stray line, a bad
    number: all mean "no opinion" for that key. This runs inside a live request,
    so nothing in this file may raise.
    """
    out = {"bytes": 0, "result": "", "friend_reply": "", "friend_paylen": 0}
    try:
        with open(_search_calib_path(), "r") as fh:
            text = fh.read()
    except OSError:
        return out
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        key, sep, val = line.partition("=")
        if not sep:                             # the bare-number legacy form
            try:
                out["bytes"] = max(0, int(line, 0))
            except ValueError:
                pass
            continue
        key, val = key.strip().lower(), val.strip()
        if key == "bytes":
            try:
                out["bytes"] = max(0, int(val, 0))
            except ValueError:
                pass
        elif key == "result":
            out["result"] = val
        elif key == "friend_reply":
            out["friend_reply"] = val
        elif key == "friend_paylen":
            if val.strip().lower() == "sweep":
                out["friend_paylen"] = "sweep"
            else:
                try:
                    out["friend_paylen"] = max(0, int(val, 0))
                except ValueError:
                    pass
    return out


def _search_calib_bytes():
    """Just the byte count -- kept as its own name for the call site's sake."""
    return _search_calib()["bytes"]


#: The records of the last 02:06 write, for the `echo` reply shape below.
_LAST_FRIEND_PUT = []


def _room_search_rows(preds, zone=None):
    """Rooms in `zone` matching a z_topic predicate; all of that zone's if none.

    `zone=None` means the request carried no zone item at all, which no observed
    client does -- it is kept as "every zone" so an unparsed request degrades to
    the old behaviour rather than to an empty browser.
    """
    want = None
    for field, val, _upper in preds:
        if field.lower() == "z_topic":
            want = str(val).upper()
    rows = []
    for room in lobbyrooms._room_list():
        if zone is not None and room["zone"] != zone:
            continue
        if want and want not in room["handle_name"].upper():
            continue
        rows.append(dict(room))
    return rows


#: A fixture seats 20 (SE's +0x94) and answers `MODE` with `+l 21`, because the
#: resident bot takes a seat of its own -- which is a second thing that reading
#: explains. A created room's capacity is whatever its creator set with
#: `MODE +l N`; SE's FOXROOM was 10, in the record and on the wire alike.
_ROOM_CAPACITY = 20
_ROOM_CAPACITY_CREATED = 10


#: MEMBER SEARCH (opcode 05:03, tag 0x03E8). The client sends a QUERY PREDICATE
#: as a TLV string -- live 2026-08-12, searching for "Lex" produced
#:
#:   0x50  e8 03 01 00 00 00 00 00      tag 0x03E8, 1 item
#:   0x58  00 00 18 00 00 00 00 00      item id 0, len 0x18
#:   0x60  "z_up_name=UPPER(\x04Lex\x04)\0"
#:
#: so \x04 is the string quote. The same opcode carries tag 0x03E9 (a status
#: WRITE, items 0x0b/0x06) and 0x03EA (0 items), which must keep their all-zero
#: reply -- only 0x03E8 is a search.
#:
#: The 16-byte reply is read into the machine's buffer and unpacked at
#: polcore 0x37e122d into a 12-byte result struct:
#:
#:   [0x00] u32  result-set slot; if < 4 the struct is cached at 0x386fbe0+slot*12
#:   [0x04] u32  HIT COUNT   <- 0 here is exactly "no error, but nobody found"
#:   [0x08] u8   flag        <- non-zero makes the poll bail out with 0x7FFFFFFF
#:   [0x0c] u32  the payload checksum (written by _build_lobby_reply_pt)
#:
#: and the poll wrapper at 0x37f08c0 returns the hit count verbatim when it is
#: > 0, 0x7FFFFFFE when it is <= 0 (the -5325 "No matching results were found"
#: path) and 0x7FFFFFFF when the flag byte is set.
_SEARCH_TAG = 0x03E8
_SEARCH_TLV_START = 0x50
_SEARCH_QUOTE = 0x04

#: field name in the client's predicate -> (table alias, SQL column). z_up_name
#: is the only one seen live; the "up" is UPPER, i.e. the case-folded index.
_SEARCH_FIELDS = {
    "z_up_name": "UPPER(h.handle_name)",
    "z_name": "h.handle_name",
    "z_up_mail": "UPPER(m.mail_address)",
    "z_mail": "m.mail_address",
}

_SEARCH_PRED = re.compile(
    r"^\s*(\w+)\s*=\s*(UPPER\()?\x04(.*?)\x04\)?\s*$", re.S)

#: Last search's rows, for the follow-up row fetch. That fetch is 03:00 keyed by
#: the `u/s/select<slot>` path, NOT 02:06 -- 02:06 is KPutFriendList, which this
#: comment used to name here on a size coincidence.
#: *** THE RESULT SET IS PER SESSION, NOT PER PROCESS. *** These three globals
#: are the process-wide fallback for a thread that never bound a session; the real
#: storage is on the session, beside `search_slot`, which was already per-session
#: for exactly the same reason.
#:
#: Why it matters, and it is not theoretical: `_LAST_SEARCH_KIND` decides whether
#: the follow-up `3:0 u/s/select<slot>` is answered with 160-byte ROOM records or
#: 600-byte PROFILE records, and `_select_bytes` sizes the reply from it. Two
#: clients are the normal case while testing chat rooms -- it takes two people to
#: have a conversation -- and with a process-global kind, player two running a
#: member search between player one's room search and player one's row fetch
#: hands player one profile records in a room-sized window. The note on
#: `_select_bytes` already records what that costs: the client divides by the
#: wrong record size and answers POL-0008.
#:
#: This is the same failure as the one `_session_get` warns about ("the old code
#: took the most recent regardless, which is precisely how player two was served
#: player one's account"), in a second place.
_LAST_SEARCH = []
_LAST_SEARCH_KIND = ["member"]


def _search_result_put(rows, kind):
    """Remember a search's rows for the row fetch that follows it."""
    sid = lobbysession._session_sid()
    if sid:
        lobbysession._session_put(sid, search_rows=list(rows), search_kind=kind)
        return
    del _LAST_SEARCH[:]
    _LAST_SEARCH.extend(rows)
    _LAST_SEARCH_KIND[0] = kind
    paylen._LAST_SEARCH_COUNT[0] = len(rows)


def _search_result_get():
    """(rows, kind) for THIS session's last search."""
    rows = lobbysession._session_get("search_rows")
    kind = lobbysession._session_get("search_kind")
    if rows is None and kind is None:
        return list(_LAST_SEARCH), _LAST_SEARCH_KIND[0]
    return list(rows or []), kind or "member"


def _parse_search_tlv(pt):
    """[(field, value, upper)] from a decrypted 05:03, or [] if it is not a search.

    Same item framing as the profile TLV (u8 id, u8 junk, u16 len, u32 pad,
    value padded to 8) and the same integrity rule: the walk must land exactly
    on the 4-byte checksum trailer, otherwise nothing is returned.
    """
    preds = []
    for _id, val in _search_tlv_items(pt):
        mo = _SEARCH_PRED.match(val.split(b"\x00")[0].decode("cp932", "replace"))
        if mo:
            preds.append((mo.group(1), mo.group(3), bool(mo.group(2))))
    return preds


def _search_tlv_items(pt):
    """[(item id, raw value)] from a decrypted 05:03 body, or [] if it is not one.

    Split out of `_parse_search_tlv` because the ROOM form of this message carries
    no predicate at all -- it carries the ZONE as a plain integer item -- so the
    walk has to hand back the item ids, not only the strings it could parse.

    Same framing and same integrity rule as before: u8 id, u8 type, u16 len, u32
    pad, value padded to 8; the walk must land exactly on the 4-byte checksum
    trailer or nothing is returned.
    """
    end = len(pt) - 4
    if end <= _SEARCH_TLV_START + 8:
        return []
    if struct.unpack_from("<H", pt, _SEARCH_TLV_START)[0] not in (
            _SEARCH_TAG, lobbyrooms._SEARCH_TAG_ROOM):
        return []
    off, items = _SEARCH_TLV_START + 8, []
    while off + 8 <= end:
        ln = struct.unpack_from("<H", pt, off + 2)[0]
        if off + 8 + ln > end:
            return []
        items.append((pt[off], pt[off + 8:off + 8 + ln]))
        off += 8 + ln + (-ln % 8)
    return items if off == end else []


#: The zone rides in item 0x06, and ONLY ITS FIRST TWO BYTES ARE REAL. The item
#: declares length 8, but SE's own client leaves the upper six as stale heap: the
#: four zone items in its capture read `4c 04` + `4e 6f 76 69 63 65` ("Novice"),
#: `4f 04` + `43 41 53 52 4f 4f` ("CASROO") and `4f 04` + `6f 20 4d 61 69 6c`
#: ("o Mail") -- residue of the last strings that buffer held. Reading the item as
#: a u32 or u64 therefore produces a different "zone" on almost every request.
_SEARCH_ITEM_ZONE = 0x06


def _search_zone(pt):
    """The zone a room search is asking about, or None if it carried no zone."""
    for iid, val in _search_tlv_items(pt):
        if iid == _SEARCH_ITEM_ZONE and len(val) >= 2:
            return struct.unpack_from("<H", val, 0)[0]
    return None


def _search_rows(preds):
    """Handles matching every predicate, most recent first.

    Raises on a DB fault instead of returning []. **A LOCK IS NOT AN ABSENCE.**
    This used to swallow every exception and answer with an empty list, which the
    caller announced as 0 hits -- and 0 hits is how the client renders
    "user not found". Live 2026-08-12 a transient
    `OperationalError('database is locked')` told the user an account that
    plainly exists could not be found, which is the worst kind of wrong answer:
    confident, plausible, and indistinguishable from the truth.
    """
    if accounts is None:
        return []
    where, args = [], []
    for field, value, upper in preds:
        col = _SEARCH_FIELDS.get(field.lower())
        if col is None:
            log("lobby", f"  search: unknown field {field!r} -- no rows")
            return []
        # SE's client sends an EQUALITY predicate, so exact match is the faithful
        # answer -- "Test" does not find "Tester". POL_SEARCH_LIKE=1 relaxes it to
        # a prefix match, which is friendlier but is our invention, not SE's.
        if os.environ.get("POL_SEARCH_LIKE", "0") == "1":
            where.append(f"{col} LIKE {'UPPER(?)' if upper else '?'}")
            args.append(value + "%")
        else:
            where.append(f"{col} = {'UPPER(?)' if upper else '?'}")
            args.append(value)
    if not where:
        return []
    try:
        db = accounts.connect(os.environ.get("POL_ACCOUNTS_DB",
                                             accounts.DEFAULT_DB))
        try:
            return [dict(r) for r in db.execute(
                "SELECT h.id, h.handle_name, h.member_id, m.polid, m.mail_address"
                " FROM handle h JOIN member m ON m.id = h.member_id"
                " WHERE " + " AND ".join(where) +
                " ORDER BY h.id DESC LIMIT ?", args + [_search_cap()])]
        finally:
            db.close()
    except Exception as exc:
        # Deliberately NOT `return []` -- see the docstring. The caller turns an
        # exception into the reply's error flag, which the client shows as a real
        # failure, instead of into a confident "nobody by that name".
        log("lobby", f"  search: DB FAULT ({exc!r}) -- reporting an error, "
                     "NOT an empty result")
        raise


def _search_cap():
    """Hit cap. 12 is the client's per-read record cap for 168-byte records."""
    return max(1, int(os.environ.get("POL_SEARCH_MAX", "12"), 0))


#: SE ROTATES THE RESULT-SET SLOT 0,1,2,3,0,... ONCE PER 5:3, and we answered a
#: hardcoded 0 forever. Measured across ALL SIXTEEN 5:3 replies in SE's live
#: session (polshim-se.429364.log, 2026-08-15): the slot advances on every query
#: in issue order and wraps at 4, regardless of the section tag -- member
#: searches (0x03E8), room searches (0x03E9) and the 0x03EA form all draw from
#: the same counter.
#:
#: WHY IT MATTERS, and it is not cosmetic: polcore caches the result struct at
#: 0x386fbe0 + slot*12 whenever slot < 4, and the client then fetches the rows
#: with `3:0 u/s/select<slot>`. Answering 0 every time means every search writes
#: the same cache entry and re-reads the same path, so a second search can be
#: served the first one's cached result set. Rotating is what keeps consecutive
#: searches distinct -- which is exactly what the counter is for.
#:
#: Per SESSION, because that is the scope of the client's cache; falls back to a
#: process-global counter when the thread has no bound session.
_SEARCH_SLOTS = 4
_SEARCH_SLOT_GLOBAL = [0]


def _search_slot_next():
    """The next result-set slot for this session, cycling 0..3 as SE's does."""
    sid = lobbysession._session_sid()
    if sid is None:
        slot = _SEARCH_SLOT_GLOBAL[0] % _SEARCH_SLOTS
        _SEARCH_SLOT_GLOBAL[0] = (slot + 1) % _SEARCH_SLOTS
        return slot
    slot = int(lobbysession._session_get("search_slot") or 0) % _SEARCH_SLOTS
    lobbysession._session_put(sid, search_slot=(slot + 1) % _SEARCH_SLOTS)
    return slot


def _search_payload(n, req_pt):
    """The 05:03 search answer: slot, hit count, flag. b"" leaves it all-zero."""
    preds = _parse_search_tlv(req_pt)
    tag = struct.unpack_from("<H", req_pt, _SEARCH_TLV_START)[0]         if len(req_pt) > _SEARCH_TLV_START + 2 else 0
    # A ROOM search carries no predicate when it means "list this zone" (the
    # 52-byte form), so unlike a member search an empty predicate list is a
    # legitimate query here and must not fall through to the all-zero reply.
    if tag == lobbyrooms._SEARCH_TAG_ZONE:
        # ONE ROW PER ZONE THAT HAS ROOMS. Measured live 2026-08-18 with two
        # clients: the zone list fires a SINGLE 0x03EA when it opens (the
        # decrypted 52-byte body carries the tag, session tokens and the
        # sender's own zone -- no queried zone anywhere) and draws EVERY zone's
        # rooms/users from that one answer, matching rows to list rows by the
        # zone id at record +2. The old one-row answer for a session-inferred
        # zone left every other zone drawn as 0/0 -- a room in Hobbies (2102)
        # with somebody in it read as an empty zone. A zone with no rooms gets
        # no row and the client draws 0/0 itself, which is also why SE's
        # captured answer was count=1: only the PlayOnline Zone had rooms.
        rows = [lobbyrooms._zone_summary_row(z)
                for z in sorted({int(r["zone"]) for r in lobbyrooms._room_list()})]
        _search_result_put(rows, "zone")
        log("lobby", "  zone 5:3: " + ("; ".join(
            f"zone {r['zone']} holds {r['rooms']} room(s) and {r['users']} user(s)"
            for r in rows) or "no zone has rooms"))
        slot = _search_slot_next()
        out = bytearray(n)
        struct.pack_into("<I", out, 0x00, slot)
        struct.pack_into("<I", out, 0x04, len(rows))
        struct.pack_into("<I", out, 0x0C, (slot + 1) & 0xFFFFFFFF)
        return bytes(out)
    if not preds and tag != lobbyrooms._SEARCH_TAG_ROOM:
        return b""
    zone = _search_zone(req_pt) if tag == lobbyrooms._SEARCH_TAG_ROOM else None
    lobbyrooms._note_browse_zone(zone)
    try:
        rows = _room_search_rows(preds, zone) if tag == lobbyrooms._SEARCH_TAG_ROOM             else _search_rows(preds)
    except Exception as exc:
        # THE FLAG BYTE IS WHAT THIS IS FOR. polcore's poll wrapper (0x37f08c0)
        # returns 0x7FFFFFFF when reply[0x08] is set, which is a distinct path
        # from the 0x7FFFFFFE "no matching results" one. So a fault can be told
        # apart from an empty result on the wire, and the user gets an error
        # rather than being told the person does not exist.
        log("lobby", f"  search 5:3: FAILED ({exc!r}) -- setting the error flag")
        out = bytearray(n)
        if n >= 0x09:
            struct.pack_into("<I", out, 0x00, _search_slot_next())
            out[0x08] = 1
        return bytes(out)
    _search_result_put(rows, "room" if tag == lobbyrooms._SEARCH_TAG_ROOM else "member")
    q = ", ".join(f"{f}={'UPPER(' if u else ''}{v!r}{')' if u else ''}"
                  for f, v, u in preds)
    where = (f" in zone {zone} ({zone:#06x})" if zone is not None else
             " in EVERY zone (the request carried no zone item)"
             if tag == lobbyrooms._SEARCH_TAG_ROOM else "")
    log("lobby", f"  search 5:3: {q}{where} -> {len(rows)} hit(s): "
                 f"{[r['handle_name'] for r in rows]}")
    slot = _search_slot_next()
    out = bytearray(n)
    struct.pack_into("<I", out, 0x00, slot)          # result-set slot, ROTATING
    struct.pack_into("<I", out, 0x04, len(rows))     # hit count
    out[0x08] = 0                                    # flag: 0 = normal result
    # +0x0C is the PAYLOAD CHECKSUM, not a field of our own -- the signer in
    # _build_lobby_reply_pt overwrites it with cksum(payload[:-4]), which for this
    # 16-byte reply is exactly slot + count + flag. That is why SE's four dwords
    # always satisfy d3 == d0 + d1 + d2: it is arithmetic, not a "result window
    # end". Confirmed on all 16 of SE's 5:3 replies in polshim-se.429364.log.
    # Left written here so an unsigned run (POL_LOBBY_CKSUM=0) still matches SE.
    struct.pack_into("<I", out, 0x0C, (slot + len(rows)) & 0xFFFFFFFF)
    return bytes(out)


#: Per-resource blobs for the 03:00 fetch, stored per account under
#: POL_RESOURCE_DIR. A PS2 title's save was the first user: an all-zero
#: reply of the requested length reads as "nothing stored yet", which is the state
#: that makes the game run its own "User Save Data be missing. Initialize Save
#: Data" path rather than failing to load. Once the client WRITES its data we want
#: it back verbatim next launch, so a stored file wins over zeros.
#: Cap on the unhandled-line dump above. An unhandled line is untrusted
#: length, and this goes to a shared log.
_NOHANDLER_LOG_MAX = int(os.environ.get("POL_NOHANDLER_LOG_MAX", "400"))


#: THE `O/m/` PATH IS NOT AN OPAQUE TOKEN -- IT IS THE 72-BYTE PUSH RECORD,
#: POL-base64 encoded, and it carries the message's ADDRESSING in clear.
#: Decoded 2026-08-16 from our own stored objects, confirmed against a send whose
#: subject and body the account holder typed and told us ("subject" / "body"):
#:
#:     +0x00  u64  message/thread id (constant across a sender's messages)
#:     +0x08  u64  RECIPIENT handle guid, XOR _PUSH_GUID_MASK  <- the whole point
#:     +0x10  16B  SENDER handle name, NUL-terminated
#:     +0x20  16B  SUBJECT, NUL-terminated
#:     +0x34  u32  unix timestamp
#:     +0x40  u32  0x000203E8, the member tag
#:
#: So the recipient never needed the 363-byte body decoded: it is in the path,
#: which both sides already exchange verbatim.
_MAIL_PATH_PREFIX = "O/m/"
