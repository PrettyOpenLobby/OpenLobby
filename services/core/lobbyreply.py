"""Building a lobby reply: the opcode dispatch, framing, encryption, probe payloads."""
import datetime
import os
import socket
import struct
import titles                   # the title-plugin seam (services/titles.py)  # noqa: E402
from srvcore import hexdump, log
import sessioncrypt
from .deps import accounts, contentauth, contentlist
from . import lobbyops, authcap, authnode, characters, contentprofiles, fetchpath, framing, friendgroups, friendlist, friendput, handlelists, lobbybind, lobbymail, lobbysearch, lobbysession, memberstatus, pacing, paylen, profilerecord, resourcestore, titlezone



def _build_lobby_reply_pt(req_pt, world_ip):
    """Compose the reply PLAINTEXT for a decrypted request:
        [0]=0x83 reply marker, [4:8]=u32 LE payload length,
        [8:12]=world IP little-endian (the world handoff),
        [24:24+n]=payload, whose LAST 4 BYTES are a checksum over the rest.
    [1] is the TYPE and MUST be 0 -- the reader turns anything else into
    POL-(5200+type).

    THE PAYLOAD TRAILER (found 2026-08-10, disassembly of polcore sub_037df800).
    The generic payload reader is a 4-state machine (recv / recv-more / decrypt);
    once the state passes 3 it runs the branch at 0x37df8c7, and with its `verify`
    argument set -- which is what EVERY lobby call site passes, e.g. 0x37e0cee for
    3:0 pushes `1` -- it does:

        if bytes_read < 4:                       return -5135
        sum = cksum(buf, len-4, seed=running)    # 0x3823df0
        if sum != LE32(buf[len-4:len]):          return -5135

    and -5135 is exactly POL-5135 "POLPRO protocol error". The header (0x18
    bytes, read by sub_037df690) is NOT part of the sum and does not seed it, so
    the seed is 0 for a single-block payload -- confirmed by the fact that our
    all-zero payloads have always passed (sum of zeros == the zero trailer) while
    the first NON-zero payload we ever sent, the 664-byte marker probe, drew
    POL-5135 and was dropped before app.dll read a single byte of it.

    So any payload content we invent must be signed here or the client discards
    the whole message. Set POL_LOBBY_CKSUM=0 to send it unsigned (A/B only).
    """
    n = paylen._lobby_paylen(req_pt[1], req_pt[2], req_pt)
    r = bytearray(24 + n)
    r[0] = 0x83
    r[1] = 0x00                                  # TYPE 0 = success
    # "NO DATA" IS A LEGITIMATE ANSWER, and it has a wire form. The reader turns a
    # non-zero type byte into POL-(5200 + type), so type 0x72 is exactly the
    # **-5314 "no data"** that (4,5) and (4,6) both special-case. For a resource
    # nothing has ever written, that is arguably the *correct* reply rather than a
    # zero-filled blob -- and it is what makes a client run its own "initialise"
    # path, which is precisely how the LOCAL save file behaves when absent
    # (the PS2 title's module, 0x0030acd0: read fails -> "be missing.
    # Initialize Save Data", and that branch is NOT fatal).
    #
    # Armed per path, because it is a behaviour change on a channel that works:
    #   POL_RESOURCE_NODATA="U/g/<a save path>"    (comma-separated; "*" = all)
    # WHICH no-data code, and why it matters (measured 2026-08-14, live EE debugger).
    # That module's save-load loop (0x002bc244) branches on the poll's
    # return with its OWN trace strings, and they say what each value means:
    #   s2 == -650 -> "File Not Found\n"     -> NORMAL exit (new player, not fatal)
    #   s2  <  0   -> "File Read Error\n"    -> INFINITE LOOP, hangs forever
    #   s2  >  0   -> "File Read Complite\n" -> success
    #   s2 == 0    -> keep polling until a timeout fires -> the on-screen
    #                 "communication error / failed to read user save data"
    # So **-650 is a SUCCESS path**, and it is reachable from the wire: the reader
    # turns a non-zero type byte into -(5200 + type), and sqMgReadFileCheck maps
    # -5326 -> -650. 5326 - 5200 = 126 = 0x7E.
    #   0x72 = -5314 "no data"        (the original, what (4,5)/(4,6) special-case)
    #   0x7E = -5326 -> -650          "File Not Found" -- the new-player exit
    # Anything else negative drops it into the 0x002bc2a4 infinite loop, so do NOT
    # pick a type at random.
    if (req_pt[1], req_pt[2]) == (0x03, 0x00):
        want = [s.strip() for s in
                os.environ.get("POL_RESOURCE_NODATA", "").split(",") if s.strip()]
        path = fetchpath._fetch_path(req_pt)
        nodata_type = int(os.environ.get("POL_RESOURCE_NODATA_TYPE", "0x72"), 0) & 0xFF
        # *** THE CHECK OUT EMPTY SIGNAL IS "NO FILE", NOT AN EMPTY STREAM. ***
        # The scene's loader (TM.dll 0x1331E0) treats -650 as advance-with-the-
        # bit-clear, and the inner scene -- the whole four-section stream -- is
        # only BUILT when [scene+0x188] has a bit, i.e. when at least one of
        # the four settlement saves EXISTED. Serving all-zero success for all
        # four is what forces every empty visit through the stream and into
        # SE's "The server is busy." all-empty outcome (dialog 0x149).
        # Hypothesis under test: -650 x4 skips the stream and draws the real
        # empty screen (Aucti 332, "You have no bids or payments to make").
        # With anything pending the files serve zeros exactly as before, so
        # the measured settlement flow is untouched.
        # The title that owns the path decides (`titles.Title.resource_nodata`).
        if titles.resource_nodata(path, fetchpath._fetch_subject(req_pt)):
            r[1] = 0x7E
            struct.pack_into("<I", r, 4, 0)
            return bytes(r[:24])
        if want and (("*" in want) or (path in want)) \
                and not resourcestore._resource_stored(path, fetchpath._fetch_subject(req_pt)):
            log("lobby", f"  3:0 {path!r}: replying NO DATA "
                         f"(type 0x{nodata_type:02x} = -{5200 + nodata_type}"
                         f"{' -> -650 File Not Found' if nodata_type == 0x7E else ''})"
                         f" -- nothing stored, so let the client initialise")
            r[1] = nodata_type
            struct.pack_into("<I", r, 4, 0)
            return bytes(r[:24])
    struct.pack_into("<I", r, 4, n)
    r[8:12] = socket.inet_aton(world_ip)[::-1]
    pay = _lobby_payload(req_pt[1], req_pt[2], n, req_pt)
    if pay:
        r[24:24 + len(pay)] = pay[:n]
    # The four LIST opcodes read EVERY block with verify=0 -- the helper is
    # 0x37df800(ctx, len, verify, buf) and both their call sites pass 0 for arg 3
    # -- so nothing they receive is ever compared against a trailer. Signing them
    # would overwrite the last 4 bytes of the final record with a checksum the
    # client reads as record data. Today that is invisible (an empty list signs
    # zeros over zeros) but it would corrupt the first non-empty list we serve,
    # and for 7:0c it could push the byte at payload+4 above the 0x40 that opcode
    # validates, turning a good reply into -5133.
    if n >= 4 and (req_pt[1], req_pt[2]) not in handlelists._LOBBY_LIST \
            and os.environ.get("POL_LOBBY_CKSUM", "1") != "0":
        sig = lobbybind._lobby_cksum(r[24:24 + n - 4])
        struct.pack_into("<I", r, 24 + n - 4, sig)
        if pay:
            # Only when a probe is armed -- an all-zero payload signs to zero and
            # would just be noise. This line is the cheap proof that the running
            # image actually has the signer: the reply on the wire is encrypted,
            # so an unsigned build is otherwise invisible until you decrypt it.
            log("lobby", f"  signed {req_pt[1]:x}:{req_pt[2]:x} payload "
                         f"n={n} trailer={sig:08x}")
    # IDENTITY CAPTURE (2026-08-21, tm-member-sidebar). The TM member-menu id
    # space is polcore's 64-slot character table (0x3bc3080), filled from replies
    # on THIS band -- so when the sidebar carries a wrong identity, the evidence
    # is the reply PLAINTEXT, and this is the only place it is in hand.
    # POL_LOBBY_DUMP_REPLY=N hex-dumps the first N payload bytes of every
    # composed reply; leave it 0 outside a capture window -- 3:0 resource
    # payloads alone would swamp the log.
    dump = _lobby_dump_reply_n()
    if dump > 0:
        log("lobby", f"  reply {req_pt[1]:x}:{req_pt[2]:x} n={n} payload:\n"
                     + hexdump(bytes(r[24:24 + min(n, dump)])))
    return bytes(r)


#: Live-armable without a container recreate (prod's pol-git-sync ignores
#: compose files, so an env knob needs a hand --force-recreate): a one-line
#: file `<POL_LOG_DIR>/lobbydump.ctl` containing `n=192` arms the reply dump;
#: delete the file (or set n=0) to disarm. The env is the fallback default.
_LOBBY_DUMP_CTL = os.path.join(
    os.environ.get("POL_LOG_DIR", "/logs"), "lobbydump.ctl")
_LOBBY_DUMP_CTL_CACHE = {"mtime": None, "n": None}


def _lobby_dump_reply_n():
    try:
        st = os.stat(_LOBBY_DUMP_CTL)
        if st.st_mtime != _LOBBY_DUMP_CTL_CACHE["mtime"]:
            n = 0
            with open(_LOBBY_DUMP_CTL, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("n="):
                        n = int(line[2:], 0)
            _LOBBY_DUMP_CTL_CACHE["mtime"] = st.st_mtime
            _LOBBY_DUMP_CTL_CACHE["n"] = n
            log("lobby", f"lobbydump.ctl: reply dump {'ARMED at ' + str(n) + 'B' if n else 'off'}")
        if _LOBBY_DUMP_CTL_CACHE["n"] is not None:
            return _LOBBY_DUMP_CTL_CACHE["n"]
    except OSError:
        _LOBBY_DUMP_CTL_CACHE["mtime"] = None
        _LOBBY_DUMP_CTL_CACHE["n"] = None
    except ValueError:
        pass
    return int(os.environ.get("POL_LOBBY_DUMP_REPLY", "0"), 0)


def _marker_payload_z(n):
    """`marker`, but every 16-byte row is NUL-TERMINATED.

    The plain marker is wrong for string fields. The record holds NUL-terminated
    Shift-JIS: polcore sub_0380d3a0 (the SJIS->UTF-16 converter, loop head at
    +0x4D3B0) is called on record+8, and against an unterminated marker it ran
    653 bytes straight off the field and into the checksum trailer -- which is
    why the first marker run rendered nothing sensible even where it was read.

    With a NUL in the last byte of each row, every 16-byte boundary starts a
    valid 15-char string that still names its own offset, so a converter entered
    at any row yields a readable answer instead of the whole record.
    """
    out = bytearray(_marker_payload(n))
    for row in range(15, n, 16):
        out[row] = 0
    return bytes(out)


def _marker_payload(n):
    """A payload whose every byte announces its own offset.

    Each 16-byte row is filled with the ASCII of that row's hex offset, e.g.
    `0000000000000000`, `1010101010101010`, ... `A0A0A0...`. Whatever the client
    renders (the handle field, a mail subject) then names the offset it came from
    directly, which is the whole point of using the UI as an oracle: one login
    maps a field instead of one login testing a guess.

    The last four bytes are overwritten by the payload checksum (see
    `_build_lobby_reply_pt`), so the final row does not carry its own offset.
    """
    out = bytearray(n)
    for row in range(0, n, 16):
        tag = f"{row:02X}".encode("ascii")[-2:]
        for i in range(row, min(row + 16, n)):
            out[i] = tag[(i - row) % 2]
    return bytes(out)


#: The two header string fields of the u/account record, located 2026-08-10 by
#: the `markerz` run. polcore's SJIS->UTF-16 converter (sub_0380d3a0) was entered
#: at exactly six offsets: 0x008 and 0x018, then 0x098/0x118/0x198/0x218 -- four
#: more on a 0x80 stride, i.e. an array of 4 entries whose own string sits at
#: entry+0x08 (entries at 0x090/0x110/0x190/0x210; 0x090 + 4*0x80 = 0x290, which
#: leaves the checksum trailer at 0x294 outside the array -- the reason to prefer
#: that base over 0x098). Each was read twice: once to measure, once to convert.
#:
#: The record's string fields are the MAIL ADDRESS LIST -- the mail account
#: dialog's Sender dropdown is built from them, and 0x008 renders as
#: "<polid>@pol.com". 0x018 used to get the HANDLE, from back when these two were
#: thought to be a polid/handle header pair; that predates 0:9 turning out to
#: carry handles (see _handle_record). The result was the
#: user's handle being advertised as an e-mail address: "Lex" sat in the Sender
#: list next to real addresses. The dialog has a separate `Name` field for the
#: display name, so nothing here should carry it.
#:
#: Only 0x008 is written now. 0x098/0x118/0x198/0x218 are the four alternate
#: slots (SLOT0-098 is the STARRED "Standard PlayOnline Mail address", confirmed
#: live), and 0x098 gets the member's real local part below.
#: Write the value plus ONE NUL and leave the remaining bytes as offset markers,
#: so a `marker`-based run still maps them.
_ACCT_STR_OFFSETS = (0x008,)
_ACCT_STR_MAX = 0x018 - 0x008 - 1      # room to the next known string, less NUL

#: Entry 0 of the 4 x 0x80 array. Best guess at where the MAIL ADDRESS lives:
#: the array is exactly 4 slots, `sub_037debd0` rejects any slot index outside
#: 0..3, and the Viewer's own UI strings talk about having more than one address
#: ("You have only one mail address.", "Select mail addresses to add to group.").
#: Until that is confirmed, the cost of being wrong is one marker row.
#:
#: Why it matters: with no address the client renders the placeholder text
#: "No Mail address" AND SUBMITS IT as the value on every profile write, which is
#: what makes the mail field permanently invalid, and it refuses PlayOnline-wide
#: visibility for an "incomplete" profile.
_ACCT_MAIL_OFF = 0x098
_ACCT_MAIL_MAX = 0x118 - 0x098 - 1


def _acct_payload(n, base=None):
    """`markerz`, with the member's real POL ID and handle in the two header
    string fields. Everything else stays an offset marker, so one run tests the
    strings AND keeps mapping the rest of the record.

    `base=b"\\x00"*n` gives the same strings on a ZERO record instead. That is the
    bisect for any number the UI invents out of marker bytes -- the standing
    example being the mail badge, which claims 20 messages no matter what the mail
    list itself replies. If a count goes to 0 on a zero record, it is being read
    from THIS record and can be hunted down by putting the markers back a region
    at a time."""
    out = bytearray(base if base is not None else _marker_payload_z(n))
    if accounts is None:
        return bytes(out)
    try:
        db = accounts.connect(os.environ.get("POL_ACCOUNTS_DB", accounts.DEFAULT_DB))
        try:
            # THE SESSION'S member, not the lowest id. This builder was missed by
            # the sweep that moved every other record builder onto
            # _session_member_id() (see _friend_payload / _session_record /
            # _profile_record), so 3:0 -- the record that tells the client its POL
            # ID and mail address -- served MEMBER 1 to everyone. Live effect:
            # a PS2 authenticating as UELRIS73E was told it was EFGH5678, the PC's
            # account, on every single login.
            # _session_get, NOT _session_member_id: the latter silently falls back
            # to the lowest member id, which would reintroduce exactly this bug by
            # another route and log nothing. A fallback here is a correctness
            # problem worth seeing, so it is reported.
            bound = lobbysession._session_get("member_id")
            mid = bound or lobbysession._session_member_id()
            row = db.execute("SELECT id, polid, mail_address FROM member "
                             "WHERE id = ?", (mid,)).fetchone() if mid else None
            if not bound:
                log("lobby", f"acct payload: NO BOUND SESSION for this peer; "
                             f"falling back to member {mid} "
                             f"({row['polid'] if row else '?'}) -- if two clients "
                             "share a peer address (Docker's gateway does this) "
                             "the client is being told it is the wrong account")
            if row is None:
                log("lobby", "acct payload: no member row; sending markerz")
                return bytes(out)
            # The handle deliberately does NOT go into this record any more --
            # 0:9 carries handles, and these fields are mail addresses.
            vals = (str(row["polid"] or ""),)
            mail = str(row["mail_address"] or "")
        finally:
            db.close()
    except Exception as exc:
        log("lobby", f"acct payload failed ({exc!r}); sending markerz")
        return bytes(out)
    for off, val in zip(_ACCT_STR_OFFSETS, vals):
        if off + _ACCT_STR_MAX > n:
            continue
        s = val.encode("ascii", "replace")[:_ACCT_STR_MAX] + b"\x00"
        out[off:off + len(s)] = s
    # LOCAL PART ONLY. The client appends "@pol.com" itself -- proven by the mail
    # account dialog, which lists 0x008's "UDXS6FWXX" as "UDXS6FWXX@pol.com" and
    # shows all six of the record's strings as selectable senders. Writing a full
    # address here put an '@' inside the local part, which is what rendered as two
    # garbage symbols; "SLOT0-098" displayed perfectly. (My earlier UTF-16 theory
    # for that garbling was wrong -- the encoding was never the problem.)
    # SLOT0 (0x098) IS NO LONGER WRITTEN. It is the STARRED "Standard PlayOnline
    # Mail address", so whatever sits here wins the Sender selection -- and the
    # client kept snapping the account back to it. That made the bare local part
    # ("lex") the active sender, which is not a deliverable address, so mail could
    # not even be attempted. 0x008 already renders correctly as
    # "<polid>@pol.com" and is the address we actually serve, so leaving SLOT0
    # EMPTY lets that one be selected instead. Set POL_ACCT_MAIL_SLOT0=1 to
    # restore the old behaviour for A/B.
    local = mail.split("@", 1)[0]
    if local and os.environ.get("POL_ACCT_MAIL_SLOT0") == "1" \
            and _ACCT_MAIL_OFF + _ACCT_MAIL_MAX <= n:
        s = local.encode("ascii", "replace")[:_ACCT_MAIL_MAX] + b"\x00"
        out[_ACCT_MAIL_OFF:_ACCT_MAIL_OFF + len(s)] = s
        log("lobby", f"acct payload: 0x{_ACCT_MAIL_OFF:03X}={local!r} (slot0 armed)")
    log("lobby", f"acct payload: 0x{_ACCT_STR_OFFSETS[0]:03X}={vals[0]!r} "
                 f"(slot0 0x{_ACCT_MAIL_OFF:03X} left EMPTY; mail={mail!r})")
    return bytes(out)


#: The four array entries' string fields (entry+0x08 for entries on a 0x80 stride
#: from 0x090). What the array actually IS remains open -- mail addresses were the
#: first guess, but the screen that renders it is titled "Handle Profile", and POL
#: allowed several handles per account, so handle slots are at least as likely.
_ACCT_SLOT_OFFSETS = (0x098, 0x118, 0x198, 0x218)


def _slots_payload(n):
    """`acct`, plus a self-identifying string in EVERY one of the four array
    slots, so a single relaunch says which slot drives which UI field.

    Writing one slot at a time cannot distinguish "this is the wrong slot" from
    "this is the wrong field" -- naming all four does it in one login.
    """
    out = bytearray(_acct_payload(n))
    for i, off in enumerate(_ACCT_SLOT_OFFSETS):
        if off + 12 > n:
            continue
        out[off:off + 10] = f"SLOT{i}-{off:03X}".encode("ascii")[:9] + b"\x00"
    # _acct_payload has already logged its own line, including a mail address
    # that this function then overwrites -- say so, or that line reads as a lie.
    log("lobby", "slots payload: array slots overwrite the acct strings above -> "
                 + ", ".join(f"0x{o:03X}=SLOT{i}" for i, o in
                             enumerate(_ACCT_SLOT_OFFSETS)))
    return bytes(out)


def _datescan_payload(n):
    """Probe: locate the record's DATE field(s) in ONE login.

    The date the lobby renders as 12/31/1969 is a numeric time_t read from a zero
    record (epoch 0 -> 1969 west of GMT), so the string-marker oracle cannot map
    it. This probe fills every 4-byte LE slot with a time_t one DAY apart, so the
    rendered CALENDAR DAY names the slot: slot k (offset base+4k) gets
    1985-01-01 + k days at 12:00 UTC. The lobby shows day resolution (you saw
    "12/31/1969"), so a single login maps the WHOLE record -- 166 slots, dates
    1985-01-01 .. ~1985-06-15, each a distinct day. Decode a rendered date D as

        offset = base + 4 * (days between 1985-01-01 and D)

    If the date stays 12/31/1969 even now, the field is not a 4-byte LE time_t in
    this record at all (try big-endian, a 64-bit FILETIME, or a different opcode).
    Strings in the record are clobbered -- this is a probe, not a normal reply.
    The last 4 bytes are left for the checksum trailer (_build_lobby_reply_pt)."""
    base = int(os.environ.get("POL_DATESCAN_BASE", "0"), 0)
    out = bytearray(n)
    usable = n - 4
    epoch0 = int(datetime.datetime(1985, 1, 1, 12, 0, 0,
                                   tzinfo=datetime.timezone.utc).timestamp())
    off = base
    k = 0
    while off + 4 <= usable:
        struct.pack_into("<I", out, off, epoch0 + k * 86400)
        k += 1
        off += 4
    if k:
        d0 = datetime.datetime.utcfromtimestamp(epoch0).date()
        dN = datetime.datetime.utcfromtimestamp(epoch0 + (k - 1) * 86400).date()
        log("lobby", "datescan(day): base=0x%03X slots=%d (%s at 0x%03X .. %s "
                     "at 0x%03X); rendered date D => offset 0x%03X + 4*(D - %s)"
                     % (base, k, d0, base, dN, base + 4 * (k - 1), base, d0))
    return bytes(out)


#: Mail list (3:3) reply geometry, from the 11-state machine at 0x37e7b34:
#:
#:   state 6   reads an 8-byte COUNT block (call site 0x37e789c pushes 8) into
#:             [esi+0x3c], then takes the **u32 at its offset 0** as the record
#:             count, capped by [esi+0xc4], and zeroes the index [esi+0x1c].
#:   state 7   reads records in BATCHES: remaining*264 bytes, capped at 0x738
#:             (= 7 records). The *264 is visible as shl 5 / add / shl 3, and the
#:             batch is divided back by 264 via the 0x3E0F83E1 reciprocal.
#:             These batch reads pass verify=0 -- they accumulate the checksum
#:             without comparing, which is what the flag=0 arm of sub_037df800
#:             exists for.
#:   state 9   reads the final 4 bytes with verify=1 -- the checksum trailer,
#:             covering the count block and every record.
#:
#: So the payload is 8 + count*264 + 4, and our signer already produces the
#: trailer that state 9 checks.
MAIL_RECORD = 264
MAIL_COUNT_BLOCK = 8


def _lobby_payload(op1, op2, n, req_pt=None):
    """Payload bytes for one opcode's reply.

    POL_LOBBY_TAIL used to be a single global blob, which meant probing the
    u/account record corrupted every other opcode in the same session. It is now
    per-opcode, in the same shape as POL_LOBBY_PAYLEN:

        POL_LOBBY_TAIL="3:0=marker"            offset-marker probe (see above)
        POL_LOBBY_TAIL="3:0=markerz"           same, NUL-terminated per 16B row
        POL_LOBBY_TAIL="3:0=acct"              markerz + the real POL ID/handle
        POL_LOBBY_TAIL="3:0=slots"             acct + all 4 array slots named
        POL_LOBBY_TAIL="3:0=acct0"             acct strings on a ZERO record
        POL_LOBBY_TAIL="3:0=aabbcc,4:6=00ff"   literal hex per opcode
        POL_LOBBY_TAIL="aabbcc"                legacy: applies to every opcode

    Returns b"" for "no override", i.e. the historical all-zero payload.
    """
    op = lobbyops.lookup(op1, op2)
    if op is not None and op.payload is not None:
        pay = op.payload(n, req_pt)
        if pay is not None:
            return pay
    lcount = handlelists._list_count(op1, op2, req_pt)
    if (op1, op2) == (0x02, 0x03) and not lcount \
            and handlelists._list_mode(op1, op2) == "friends":
        # AN EMPTY FRIEND LIST IS STILL AN ANSWER -- it renumbers the client's
        # table to nothing. `_list_payload` never runs for a zero count, so the
        # slot map has to be cleared here or one from an earlier session outlives
        # the list it described, and a stray delete indexes db row ids that SQLite
        # may since have handed to somebody else.
        friendlist._friend_slots_publish([])
    if lcount:
        return handlelists._list_payload(op1, op2, n, req_pt)
    if (op1, op2) == (0x00, 0x08):
        pay = handlelists._handlereg_payload(n)
        if pay:
            return pay
    spec = os.environ.get("POL_LOBBY_TAIL", "").strip()
    if not spec or spec.lower() in ("none", "echo"):
        return b""
    if "=" not in spec:                              # legacy global form
        return bytes.fromhex(spec)
    for item in spec.split(","):
        key, _, val = item.partition("=")
        a, _, b = key.strip().partition(":")
        try:
            if (int(a, 0), int(b, 0)) != (op1, op2):
                continue
        except ValueError:
            continue
        val = val.strip()
        if val.lower() == "marker":
            return _marker_payload(n)
        if val.lower() == "markerz":
            return _marker_payload_z(n)
        if val.lower() == "acct":
            return _acct_payload(n)
        if val.lower() == "acct0":
            return _acct_payload(n, base=bytes(n))
        if val.lower() == "slots":
            return _slots_payload(n)
        if val.lower() == "datescan":
            return _datescan_payload(n)
        try:
            return bytes.fromhex(val)
        except ValueError:
            log("lobby", f"bad POL_LOBBY_TAIL payload for {a}:{b}: {val!r}")
            return b""
    return b""


#: Offset of the handle string inside a decrypted 0:8 (handle registration)
#: message, measured from a live capture:
#:
#:   0x28  00 01 02 03 ... 3d 3e 3f   a 64-byte 0x00..0x3F run
#:   0x68  01 00 01 00 00 00 00 00
#:   0x70  4c 65 78 00 00 ...         "Lex", NUL-padded
#:
#: The client sends the handle it wants; we previously answered with 520 zeros,
#: so it never learned the registration took and re-prompted on every login.
_HANDLE_REG_OFF = 0x70
_HANDLE_REG_MAX = 16


#: Where the TLV items start in a decrypted 05:01: the 40-byte request header
#: plus a 16-byte preamble carrying `e8 03 <visibility> <item count>`.
_TLV_START = 0x38
_TLV_TAG = 0x03E8


def _lobby_shape(req_len):
    """(hdr byte1, hdr byte2, reply total length) for a request of this size."""
    return framing._LOBBY_SHAPES.get(req_len, framing._LOBBY_SHAPE_DEFAULT)


def _lobby_mask(req_len=104):
    """The 12-byte XOR difference between the client's request header and our
    reply header, for a request of `req_len` bytes:
        [0]     0x81      request/reply marker
        [1][2]  message type (per-shape, from _LOBBY_SHAPES)
        [3]     0         shared
        [4:6]   req_len ^ reply_len   (the masked length field -- see above)
        [6][7]  0         shared
        [8:12]  the world IP, little-endian -- the world handoff
    POL_LOBBY_MASK pins the whole thing for A/B tests."""
    env = os.environ.get("POL_LOBBY_MASK")
    if env:
        return bytes.fromhex(env)[:12].ljust(12, b"\x00")
    b1, b2, reply_len = _lobby_shape(req_len)
    return (bytes([0x81, b1, b2, 0x00])
            + struct.pack("<H", (req_len ^ reply_len) & 0xFFFF)
            + b"\x00\x00"
            + socket.inet_aton(framing._lobby_world_ip())[::-1])


def _split_lobby_messages(buf, iv):
    """Split a read into messages using the DECRYPTED header length: a request is
    a 40-byte header whose [4:8] is the u32 payload length, so the message is
    40+len bytes. OFB restarts per message, so each candidate decrypts from its
    own offset.

    The previous version split on occurrences of the hello's 12-byte "session
    handle" -- which silently corrupted every burst on a connection whose hello
    carried a non-zero handle. A message's ciphertext at [12:24] is pure keystream
    (the plaintext there is zeros) and matched that handle, so the splitter found a
    false boundary and handed the parser a message with its 40-byte header sliced
    off (seen live as `+456B = 1 message(s) [416]` decoding to type=0x53)."""
    if not iv:
        return [buf]
    out, off = [], 0
    while off + 40 <= len(buf):
        hdr = lobbybind._lobby_crypt(buf[off:off + 8], iv)
        if hdr[0] != 0x02:
            break
        total = 40 + struct.unpack_from("<I", hdr, 4)[0]
        if total < 40 or off + total > len(buf):
            break
        out.append(buf[off:off + total])
        off += total
    if off < len(buf):                      # trailing bytes we could not frame
        out.append(buf[off:])
    return out or [buf]


def _derive_lobby_reply(req, iv=None):
    """Build the whole 32-byte reply MESSAGE from the client's own request.

    `iv` is THE IV THIS MESSAGE'S HEADER VALIDATED UNDER, and passing it is not
    optional bookkeeping. This used to call `_lobby_iv()` itself, which returns
    the NEWEST session IV for the peer address -- while the caller had already
    identified the right one with `_lobby_pick_iv`. With a game launched there
    are two auth sessions on one address (`_SESSIONS` is keyed by peer IP), so
    the two disagreed: the request was logged correctly and the REPLY was built
    by re-decrypting the same bytes under the wrong key. The opcode then read as
    garbage, every handler declined it, and the client got a generic 8-byte body
    encrypted under an IV it does not hold -- surfacing as POL-5273, and as a
    member search that silently stopped finding anybody. Falls back to
    `_lobby_iv()` only when the caller has nothing better.

    Framing (proven live 2026-08-10 by the shim's wire capture): every message on
    this channel is a **24-byte header followed by a body whose length the header
    encodes**. The client `recv`s exactly 24 bytes, then reads the body. Its own
    request is written the same way: send(40) = [24B header][16B aux], send(64) =
    [64B signature] -> a 24-byte header + an 80-byte body, 104 total.

        [0:12]  reply token = req token XOR (8 const bytes + world IP LE), so
                token[8:12] carries the WORLD ADDRESS the client dials next
        [12:24] the client's session handle, echoed verbatim  (21/21 SE pairs)
        [24:32] the body -- 8 bytes, server-originated

    The `81 00` frame is NOT a wrapper around this; it is a SEPARATE, body-less
    24-byte header (the accept). Sending 81 00 + this record made the client read
    the accept header, take its zero body length, and never read the record at
    all -> POL-5368. So this goes on the wire RAW.

    The body length is not optional: our header is derived from SE headers whose
    reply carried 8 body bytes, so whatever length field it encodes says 8, and
    the client will read 8 more bytes. Content is server-chosen and not derivable
    from any client bytes (searched), so POL_LOBBY_TAIL picks it:
      <hex>  literal bytes;  echo = copy the client's req[24:32] (same message
      offset -> stays consistent if this channel turns out to be masked);
      none = send the header only (will desync if the header does say 8)."""
    if len(req) < 24:
        return None
    # Preferred path: we hold the session IV, so decrypt the request, compose the
    # reply in plaintext, and encrypt it. No XOR-differencing, no shape table.
    iv = iv or lobbybind._lobby_iv()
    if iv:
        pt = lobbybind._lobby_crypt(req, iv)
        rpt = _build_lobby_reply_pt(pt, framing._lobby_world_ip())
        return lobbybind._lobby_crypt(rpt, iv)
    mask = _lobby_mask(len(req))
    tok = bytes(a ^ b for a, b in zip(req[0:12], mask))
    body_len = max(0, _lobby_shape(len(req))[2] - 24)
    spec = os.environ.get("POL_LOBBY_TAIL", "").strip().lower()
    if spec == "none":
        body = b""
    elif spec == "echo":
        body = (req[24:24 + body_len]).ljust(body_len, b"\x00")
    elif "=" in spec:
        # Per-opcode form (e.g. "3:0=marker"). This is the NO-IV path: we could
        # not decrypt, so we do not know which opcode this is and cannot pick a
        # payload for it. Fall back to zeros rather than feeding the whole spec
        # string to fromhex -- which is exactly what threw here and dropped the
        # connection, surfacing on the client as POL-0008 (network unreachable).
        body = b"\x00" * body_len
    elif spec:
        body = bytes.fromhex(spec)[:body_len].ljust(body_len, b"\x00")
    else:
        body = b"\x00" * body_len
    return tok + req[12:24] + body


def _lobby_emit_iv(handle):
    """The OFB IV for our emitted s2c body. Client+server share one IV per
    connection (proven by the c2s/s2c keystream match). Order of preference:
    explicit POL_LOBBY_IV; else the session handle's first 8 bytes (non-zero
    sessions); else K0_IV (our K=0 zero-handle flow reuses the auth-channel IV).
    All three are candidates until a live accepted body confirms which the client
    expects -- emit is gated, so a wrong guess only costs one observed frame."""
    env = os.environ.get("POL_LOBBY_IV")
    if env:
        return bytes.fromhex(env)[:8].ljust(8, b"\x00")
    if handle and any(handle):
        return handle[0:8]
    return authnode.K0_IV


def _lobby_key():
    """The lobby rides the auth connection's session key.

    That is K=0 in practice (TOKEN0's state-8 arm keys the client to zero), and
    an explicit POL_LOBBY_KEY still wins. The session lookup only matters if the
    auth handler found the client keyed to something else -- see the SESSION
    TOKEN note in handle_authserv -- in which case the lobby must follow it or
    every frame decrypts to noise.
    """
    env = os.environ.get("POL_LOBBY_KEY")
    if env:
        return bytes.fromhex(env)[:8].ljust(8, b"\x00")
    return lobbysession._session_get("key") or b"\x00" * 8


def _find_session_const(frame):
    """Index of the session const, matching it as `b1 ?? 37 6c`.

    BYTE +1 VARIES BY CLIENT -- 0xfa on the PC Viewer, 0x00 on the PS2 (measured
    2026-08-12 from a real PS2 hello). An exact 4-byte `find` therefore never
    matched a PS2 frame, `_lobby_handle` returned None, and the IV sweep below
    lost its two best candidates (handle[0:8] and handle[4:12]) -- which is why
    every PS2 lobby message decrypted to garbage with an absurd payload length
    while the PC's decoded cleanly. This was NOT only a logging problem.
    """
    c = framing._SESSION_CONST_LE
    for i in range(len(frame) - 3):
        if frame[i] == c[0] and frame[i + 2] == c[2] and frame[i + 3] == c[3]:
            return i
    return -1


def _lobby_handle(frame):
    """The 12-byte session handle sits right after the session const in c2s."""
    i = _find_session_const(frame)
    if 0 <= i and i + 4 + 12 <= len(frame):
        return frame[i + 4:i + 4 + 12]
    return None


def _lobby_try_decrypt(frame, P, S):
    """Sweep candidate IVs x body offsets, decrypt, and score each attempt by
    content-list hits then printability. Returns the best (iv, off, pt, hits) or
    (None, None, None, []). Honest best-effort until the format is confirmed."""
    handle = _lobby_handle(frame)
    iv_cands = []
    if handle:
        iv_cands.append(("handle[0:8]", handle[0:8]))
        iv_cands.append(("handle[4:12]", handle[4:12]))
    # cleartext header fields that vary per connection are IV candidates
    for o in (0x14, 0x0c, 0x1c):
        if o + 8 <= len(frame):
            iv_cands.append((f"frame[{hex(o)}]", frame[o:o + 8]))
    body_offs = (0x14, 0x18, 0x1c, 0x20, 0x28)
    best = (None, None, None, [])
    best_score = -1
    for iv_name, iv in iv_cands:
        for off in body_offs:
            if off >= len(frame):
                continue
            pt = sessioncrypt.ofb_apply(P, S, iv, frame[off:])
            hits = contentlist.scan(pt) if contentlist is not None else []
            printable = sum(1 for c in pt if 32 <= c < 127)
            score = len(hits) * 1000 + printable
            if score > best_score:
                best_score = score
                best = (f"{iv_name}={iv.hex()}", off, pt, hits)
    return best


def _build_lobby_reply(stub_ip):
    """Assemble a SPECULATIVE lobby reply: the free-contents block (FFXI + Tetra
    Master) plus the world handoff. The exact on-wire framing of the lobby reply
    is not yet confirmed (needs a decoded SE capture), so this is gated behind
    POL_LOBBY_EMIT and logged, not trusted. It exists so the emit path is ready
    the instant lobbydec.py --key ... reveals the real frame layout."""
    ids = contentprofiles.lobby_content_ids()
    block = contentlist.build_block(ids)                 # 192-byte content list
    world_ip = os.environ.get("POL_WORLD_IP") or authcap._self_ip()
    world_port = int(os.environ.get("POL_WORLD_PORT", "51330"))
    # Placeholder world-handoff record: IPv4 + big-endian port. Real layout TBD.
    handoff = socket.inet_aton(world_ip) + struct.pack(">H", world_port)
    return block, handoff, (world_ip, world_port), ids


def _build_world_reply_body(world_ip, world_port):
    """SPECULATIVE candidate reply body (route B). PLAINTEXT -- the lobby channel
    is not encrypted (the request is proven-plaintext tokens from the dump).

    Per RE of the client's reply decoder (polcore.dll 0x037df0e8, a state machine):
    the world IP is read as the dword at message offset **+0x14**, gated by the
    **tag byte at +1 == 0**; the message must be >= 0x18 bytes. We place the IP at
    +0x14 (network order) and a candidate port at +0x18. The message opcode/byte0
    and the exact message framing are still unknown, so this is a first shot to
    iterate on live -- override the WHOLE body with POL_LOBBY_REPLY=<hex>."""
    ov = os.environ.get("POL_LOBBY_REPLY")
    if ov:
        return bytes.fromhex(ov)
    body = bytearray(0x20)
    body[1] = 0x00                                    # tag == 0 -> "read raw IP" branch
    body[0x14:0x18] = socket.inet_aton(world_ip)      # world IP @+0x14 (network order)
    struct.pack_into(">H", body, 0x18, world_port)    # world port @+0x18 (guess)
    return bytes(body)


def _parse_lobby_hello(buf):
    """Annotate the 40-byte plaintext lobby hello (PROVEN layout):
        0x00 u32 record-type (zero)   0x04 u16 opcode (0x0001)
        0x06 u16 session-token hi     0x08 magic 6c37fab1 (LE)  0x0c token body
    Returns a dict of fields; tolerant of short/oversized frames."""
    d = {"len": len(buf)}
    if len(buf) >= 8:
        d["record_type"] = struct.unpack_from("<I", buf, 0)[0]
        d["opcode"] = struct.unpack_from("<H", buf, 4)[0]
        d["token_hi"] = struct.unpack_from("<H", buf, 6)[0]
    # BYTE +0x09 IS NOT PART OF THE MAGIC. Measured 2026-08-12 from the PS2
    # client: it sends `b1 00 37 6c` where the PC sends `b1 fa 37 6c` -- three of
    # four bytes, differing only here. Requiring the whole dword made every PS2
    # lobby hello log as "probably the WORLD opener", which is a real hello
    # misfiled as world traffic and is exactly the kind of misdirection that
    # costs someone a day. It never changed behaviour (both paths "proceed
    # anyway"), only the diagnosis.
    #
    # This also fits what polcore does: it never COMPARES this dword against
    # anything (see the note above _SESSION_CONST_LE -- the only occurrences in
    # the image are the client's own outgoing template), so treating it as a
    # four-byte constant was our invention, not the protocol's.
    #
    # And the "PS2 sends 00 where the PC sends fa" reading is not a build
    # difference either (2026-09-27): these four bytes echo the [4:8] "client
    # IP" field of the auth record WE sent, and +0x09 is its byte [6], our
    # account-status code -- 0xfa back when we shipped 0xfa, 0x00 since. It
    # tells you which server version greeted the client, not which client.
    d["magic_ok"] = (buf[8:9] == framing._SESSION_CONST_LE[0:1]
                     and buf[10:12] == framing._SESSION_CONST_LE[2:4])
    d["magic_variant"] = buf[9] if len(buf) > 9 else None
    d["magic_exact"] = buf[8:12] == framing._SESSION_CONST_LE
    d["handle"] = _lobby_handle(buf)
    return d


_HTTP_METHODS = (b"GET ", b"POST ", b"HEAD ", b"PUT ", b"OPTIONS ", b"CONNECT ")
