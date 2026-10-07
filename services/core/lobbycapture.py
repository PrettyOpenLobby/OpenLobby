"""Capturing lobby writes: binding corroboration, active handle, the write dispatch."""
import os
import re
import time
from srvcore import hexdump, log
from .deps import accounts
from . import lobbyops, fetchpath, friendput, lobbybind, lobbysession, profilerecord, pushspool, resourcestore



def _bind_corroborate_mismatch(name, mid, db):
    """A 4:7 named a handle the BOUND member does not own -- is the bind wrong?

    4:7 is the one lobby request that names its own subject: the handle the
    client is logged in AS (a client cannot announce somebody else's). So when
    exactly ONE member owns a handle of that name and it is not the bound
    member, the bind is PROVEN wrong -- the per-IP key pool handed this
    connection to the wrong account (`lobby-session-binding-is-per-IP`). This
    is deliberately NOT applied to 05:04's z_hid, which legitimately names
    other members' handles (view a friend's profile), nor to 3:0 paths, which
    name resources, not members.

    Response, gated by POL_LOBBY_BIND_CORROBORATE (default on):
      * warn (the always-on half: even with everything else off, the proof is
        logged instead of being thrown away as "not one of ours");
      * record the proof against this connection's cipher, so
        `_lobby_arbitrate` picks the owner for later frames from this address;
      * rebind THIS thread to the owner's newest session and teach it this
        connection's IV, so the very next `_lobby_bind` on this socket finds
        the right session first.

    Returns True when the mismatch was recognised and handled (the caller's
    "not one of ours" log then does not apply).
    """
    if mid is None or os.environ.get("POL_LOBBY_BIND_CORROBORATE", "1") == "0":
        return False
    try:
        rows = db.execute("SELECT id, member_id FROM handle "
                          "WHERE handle_name = %s", (name,)).fetchall()
    except Exception:
        return False
    if len(rows) != 1:
        return False        # absent, or two members share the name: no proof
    owner = int(rows[0]["member_id"] or 0)
    hid = int(rows[0]["id"])
    if not owner or owner == int(mid):
        return False
    sid = lobbysession._session_sid()
    cur_iv = getattr(lobbysession._session_current, "iv", None)
    log("lobby", f"WARNING: MIS-BOUND SESSION PROVEN by 4:7: the client says it is "
                 f"logged in as {name!r} (member {owner}) but this thread is "
                 f"bound to member {mid} ({sid}) -- the per-IP key pool bound "
                 f"this connection to the wrong account "
                 f"(lobby-session-binding-is-per-IP; "
                 f"POL_LOBBY_BIND_CORROBORATE=0 disables the rebind)")
    with lobbysession._SESSIONS_LOCK:
        peer_ip = (lobbysession._SESSIONS.get(sid) or {}).get("peer_ip") if sid else None
        cands = [(s, float(sl.get("at") or 0)) for s, sl in lobbysession._SESSIONS.items()
                 if sl.get("member_id") == owner and not sl.get("auth_refused")]
    if cur_iv:
        with lobbybind._BIND_NOTE_LOCK:
            lobbybind._BIND_PROVEN[(peer_ip, cur_iv.hex())] = (owner, time.time())
            while len(lobbybind._BIND_PROVEN) > lobbybind._BIND_PROVEN_KEEP:
                del lobbybind._BIND_PROVEN[min(lobbybind._BIND_PROVEN,
                                     key=lambda k: lobbybind._BIND_PROVEN[k][1])]
    if not cands:
        log("lobby", f"  no session of member {owner} exists to rebind to; "
                     f"bind left as it is (the warning above stands)")
        return True
    best = max(cands, key=lambda kv: kv[1])[0]
    lobbysession.session_bind(best)
    fields = {"handle_id": hid}
    if cur_iv:
        # The owner's session now claims this connection's cipher (newest
        # per-IV claim), so later binds -- this socket or the next one from
        # this machine -- resolve to the right member without another 4:7.
        fields["iv"] = cur_iv
    lobbysession._session_put(best, **fields)
    log("lobby", f"  REBOUND this thread to session {best} (member={owner})"
                 + (f"; {best} now claims IV {cur_iv.hex()}" if cur_iv else ""))
    return True


def _capture_active_handle(pt):
    """Note which handle the client is logged in as, from a 4:7 request.

    Measured off a live request (2026-08-12, a 104-byte 4:7):

        0020  fc 2a 46 d0 c4 7f 11 b3 01 4c 65 78 62 6f 72 72
                                      ^^ flag, then "Lexborr...
        0030  6f 77 64 61 6c 65 00 00 00 00 00 00 00 00 00 00
              ...owdale", NUL-padded

    This is the ONLY place the client volunteers it. 05:01 (profile write) does
    not name a handle, and 05:04 sends z_hid = 0 for "mine", so without this the
    server has to guess -- and the guess it used to make was "member 1", which is
    why a second handle could never own anything of its own.

    Read-only with respect to the protocol: 4:7's reply is unchanged.
    """
    if len(pt) <= profilerecord._ACTIVE_HANDLE_OFF + 1:
        return
    raw = bytes(pt[profilerecord._ACTIVE_HANDLE_OFF + 1:
                   profilerecord._ACTIVE_HANDLE_OFF + 1 + accounts.HANDLE_MAX])
    name = raw.split(b"\x00", 1)[0].decode("ascii", "ignore").strip()
    # The same validity rule the 0:8 capture uses. A misread offset would produce
    # junk here, and junk that reached `set_handle` once already turned into a
    # gibberish handle on screen -- so anything that is not a legal POL handle is
    # dropped, and our own offset markers never count as one.
    if not name or _MARKER_RE.match(name) or accounts.check_handle_policy(name):
        return
    try:
        db = accounts.connect()
        try:
            mid = lobbysession._session_member_id()
            row = db.execute(
                "SELECT id FROM handle WHERE handle_name = %s"
                + (" AND member_id = %s" if mid else ""),
                (name, mid) if mid else (name,)).fetchone()
            if row is None:
                # Before shrugging this off as junk: a real handle owned by a
                # DIFFERENT member is the one request that can PROVE the bind
                # wrong -- this used to be exactly where that proof was thrown
                # away (lobby-session-binding-is-per-IP).
                if _bind_corroborate_mismatch(name, mid, db):
                    return
                log("lobby", f"4:7 active handle {name!r} is not one of ours; "
                             f"leaving the session handle unchanged")
                return
            sid = lobbysession._session_sid()
            if not sid:
                # Nothing to attach it to; the primary-handle fallback stands.
                log("lobby", f"4:7 active handle {name!r} seen on an unbound "
                             f"thread; not recorded")
                return
            prev = lobbysession._session_get("handle_id")
            lobbysession._session_put(sid, handle_id=int(row["id"]))
            log("lobby", f"4:7 active handle = {name!r} (handle id {row['id']}, "
                         f"guid {accounts.handle_guid(row['id']):#x})")
            # A SWITCH, not the first 4:7 of the session: tell the old handle's
            # watchers it left and the new one's that it arrived (see
            # `pushspool._push_deliver_handleswitch`). POL_HANDLE_SWITCH_PRESENCE=0
            # turns it off.
            if prev and mid and int(prev) != int(row["id"]) and \
                    os.environ.get("POL_HANDLE_SWITCH_PRESENCE", "1") == "1":
                pushspool._push_emit({"kind": "handleswitch", "member": int(mid),
                                      "old": int(prev), "new": int(row["id"])})
        finally:
            db.close()
    except Exception as exc:
        log("lobby", f"4:7 active-handle capture failed: {exc!r}")


#: Our own offset-marker strings, e.g. 'R50#0' -- never a real handle.
_MARKER_RE = re.compile(r'^R[0-9A-F]{2}#\d+$')

#: Shortest run the 0:8 scanner will accept AS A HANDLE. Not a policy rule --
#: SE's own minimum is 1 character and `accounts.check_handle_policy` keeps it --
#: this is a scavenging floor. A 1-2 character printable run inside a 1648-byte
#: binary record is overwhelmingly likely to be record data, and one such run
#: (`Y`, from offset 0x468) became a live account's handle on 2026-08-12.
#: POL_MIN_CAPTURED_HANDLE overrides it.
_MIN_CAPTURED_HANDLE = int(os.environ.get("POL_MIN_CAPTURED_HANDLE", "3"))


def _scavenged_junk_signature(txt):
    """A non-handle SIGNATURE, or None. Applied ONLY to entries SCAVENGED from
    the 0:8 table past the primary -- never to the name the user typed at 0x70.

    Every phantom the 0:8 walk has ever minted carries one of these, and no real
    handle does (a person picks a NAME): a single repeated character
    (`TTTTTTTT`/`NNNNNNNN` = A64 zero-padding), a strict A-B-C-D ascending run
    (`HIJKLMNO` = a probe/offset marker the client cached), or an all-digit run
    (`000004`/`000005` = a content-id fragment).
    Thresholds are set so a plausible
    short real handle (`007`, `abc`) is never caught.
    """
    if len(txt) >= 4 and len(set(txt)) == 1:
        return "a single repeated character"
    if len(txt) >= 4 and txt.isdigit():
        return "all digits (content-id fragment)"
    if len(txt) >= 5 and txt.isalpha() and all(
            ord(txt[i + 1]) - ord(txt[i]) == 1 for i in range(len(txt) - 1)):
        return "a sequential alphabet run (probe marker)"
    return None


def _lobby_capture(pt):
    """Harvest what the client TELLS us out of a decrypted request.

    Read-only with respect to the protocol -- it does not change any reply. It
    exists because the client volunteers real account data on the way in
    (the handle on 0:8, and again on the 4:7 probe), which is worth persisting
    even while the reply formats are still unknown.
    """
    if len(pt) < 3:
        return
    # BEFORE the `accounts is None` guard on purpose: this is a wire measurement,
    # not an account harvest, and it must still fire on a build with no db.
    if (pt[1], pt[2]) in ((0x03, 0x00), (0x03, 0x01), (0x03, 0x02)):
        fetchpath._capture_fetch_subject(pt)
    if accounts is None:
        return
    op = lobbyops.lookup(pt[1], pt[2])
    if op is not None and op.capture is not None and op.capture(pt):
        return
    # WHICH OPCODE WRITES A RESOURCE? ANSWERED 2026-08-15: **BOTH 03:01 and
    # 03:02**, same shape, both now stored (see _capture_resource_write).
    #
    #   03:01  461 B  the MESSAGE SEND -- `O/m/<path>` + 363 B
    #   03:02  448 B  an UPDATE of an existing object -- `O/m/<path>` + ~292 B
    #
    # The first fix here stored only 03:02, on the old reasoning that 03:01 "is
    # the send, therefore not the write pair". That was wrong: sending a message
    # IS creating the object the recipient later reads, so a real send stored
    # nothing and the fix changed nothing. Measured, not reasoned: a live send
    # produced a 03:01 and no 03:02 at all.
    #
    # The scan below stays: it is how the NEXT unknown write opcode (a game save,
    # say) will name itself, and storing is still restricted to the prefixes in
    # _RESOURCE_WRITE_PATHS so a game save cannot be corrupted by this.
    if os.environ.get("POL_RESOURCE_SCAN", "1") == "1" \
            and (pt[1], pt[2]) != (0x03, 0x00):
        for m in re.finditer(rb"[A-Za-z]/[A-Za-z0-9/_%]{3,40}", pt[0x28:]):
            s = m.group().decode("latin1")
            if "/" not in s.strip("/"):
                continue
            log("lobby", f"  *** RESOURCE PATH {s!r} in a {pt[1]:02x}:{pt[2]:02x} "
                         f"request ({len(pt)}B) -- this is a candidate WRITE "
                         f"opcode. Path at payload +0x{m.start():x}; "
                         f"{len(pt) - 0x28 - m.end()}B follow it.\n"
                         + hexdump(pt, 512))
            break


def capture_handle_registration(pt):
    """0:8 handle registration: the handle the client will play as, read
    out of the request it registers it with."""
    # SHOW EVERY PRINTABLE RUN in the whole request, not just the 0x80-byte window
    # the generic hexdump prints. The 0:8 payload is 1608 bytes and we have been
    # reading two fixed offsets out of it (0x68 as a flavour, 0x70 as the name)
    # inherited from ONE capture. Registrations kept arriving with neither field
    # where expected, which is exactly what looking through too small a window
    # produces. This says where the strings actually are.
    if os.environ.get("POL_HANDLE_SCAN", "1") == "1":
        runs = []
        cur, start = b"", 0
        for i, b in enumerate(pt):
            if 0x20 <= b < 0x7F:
                if not cur:
                    start = i
                cur += bytes([b])
            else:
                if len(cur) >= 3:
                    runs.append((start, cur))
                cur = b""
        if len(cur) >= 3:
            runs.append((start, cur))
        if runs:
            log("lobby", "0:8 printable runs: " + ", ".join(
                f"0x{o:03X}={r.decode('ascii', 'replace')!r}" for o, r in runs[:12]))
    # 0:8 IS A FAMILY, NOT ONE MESSAGE. The dword at 0x68 is the discriminator, and
    # only one flavour puts a handle name at 0x70:
    #
    #   01 00 01 00   handle REGISTRATION  -> 0x70 = the name, plain, NUL-padded
    #   00 00 03 00   mail ADDRESS         -> 0x70 = length byte + the address
    #
    # Captured live 2026-08-11: every recent registration was REJECTED because the
    # mail-address flavour was being read as a handle ("user@example."), and
    # an earlier one stored raw ciphertext as a handle name. So the validator was
    # doing real work, but it was covering for reading the wrong message entirely.
    # 0:8 IS THE CLIENT'S WHOLE HANDLE TABLE, not a single name. Established by
    # scanning a real registration (2026-08-11): entries run at a 0x10 stride from
    # 0x78, and a newly created handle appears in one of them --
    #
    #     0x078 'R50#0'   0x088 'Abe'   0x0A8 'R80#0'   0x0B8 'R90#0' ...
    #
    # so reading ONE fixed offset (0x70, inherited from a single old capture) was
    # never going to work: it is not where the name is, and WHICH slot a new handle
    # lands in varies. The `R..#N` entries are OUR offset-marker strings from an
    # earlier probe run, which the client stored as real handle names and now
    # reports back -- they are filtered out rather than persisted.
    # TWO CLIENT SHAPES, and reading only one of them cost a real handle.
    #
    # The PC Viewer sends its WHOLE handle table -- entries on a 0x10 stride from
    # 0x78 -- and that discovery replaced an older reading of "the handle is the
    # single name at 0x70". But the PS2 Viewer still uses the older shape. Live
    # 2026-08-12: the console registered `Test`, which arrived at 0x070 exactly as
    # the old note said, and the table walk stepped straight past it, ran a
    # kilobyte into unrelated record data and captured a stray `Y` at 0x468 --
    # which then became the account's handle. So both shapes are real client
    # behaviour and both must be read.
    #
    # `_MIN_CAPTURED_HANDLE` is the other half of that failure: a one-character
    # run out of the middle of a binary record should never be promotable to a
    # handle. accounts.check_handle_policy allows 1 character (SE's own minimum),
    # which is right for a name a user TYPED and wrong for one we SCAVENGED, so
    # the floor is applied here, at the scavenging site, rather than in the policy.
    def _run_at(off, width=16):
        raw = bytes(pt[off:off + width]).split(bytes([0]))[0]
        if not raw or not all(0x20 <= b < 0x7F for b in raw):
            return None
        txt = raw.decode("ascii")
        return None if _MARKER_RE.match(txt) else txt

    names = []
    # 0x070 FIRST, and unconditionally: it holds the handle on BOTH clients (it is
    # where the original 'Lex' capture came from on the PC, and where the PS2 puts
    # `Test`). Only the 0x78 TABLE is PC-specific.
    primary = _run_at(0x70)
    if primary is not None and len(primary) >= _MIN_CAPTURED_HANDLE:
        names.append((0x70, primary))
    # *** GATE THE TABLE WALK ON A REAL REGISTRATION. *** 0:8 is a family (the
    # dword at 0x68 discriminates it -- see above), and only the registration
    # flavour carries a handle TABLE at 0x78. Live 2026-08-19 the walk ran on
    # EVERY 0:8 and minted junk two ways:
    #   * a 0:8 whose 0x70 held NO valid handle (a non-registration flavour whose
    #     buffer tail was full of room-browser leftovers) gave member 3 the junk
    #     handles 'quare'(Town_Square), 'con'(RedBeacon), '000004', 'TTTTTTTT'...
    #   * a real registration for 'AmicableElm' let the walk read 0x078 -- which
    #     falls INSIDE the 11-char name at 0x70 -- and captured the substring
    #     'Elm' as a phantom handle.
    # So: only scavenge the table when 0x70 holds a valid handle (a real
    # registration always does; the junk flavour did not), and start the walk
    # PAST the primary's own field so an overlong primary cannot alias itself.
    # The 0x68 dword is logged for the day we gate on it precisely.
    disc = bytes(pt[0x68:0x6C]).hex() if len(pt) >= 0x6C else "?"
    if primary is None or len(primary) < _MIN_CAPTURED_HANDLE:
        log("lobby", f"0:8 (disc={disc}) has no valid handle at 0x70 -- not a "
                     "registration flavour; NOT scavenging the table (this is "
                     "what minted junk handles from room-browser buffer tail)")
    else:
        prim_end = 0x70 + len(primary) + 1        # name + its NUL
        # ...the PC's full table. These entries are SCAVENGED rather than located,
        # so they carry a stricter test: alphanumeric only. Every real handle we
        # have seen is alnum (Lex, Abe, Test, Tester2, DeckTester, RenderTest),
        # while the false positives are punctuation soup -- `r!V` at 0x088 sits on
        # this very stride and would otherwise pass the length floor.
        for off in range(0x78, min(len(pt) - 4, 0x78 + 0x40 * 0x10), 0x10):
            if off < prim_end:
                # inside the primary name field -- a substring of it (this is how
                # 'Elm' fell out of 'AmicableElm'), not a real slot
                continue
            txt = _run_at(off)
            if txt is None or txt == primary:
                continue
            if len(txt) < _MIN_CAPTURED_HANDLE or not txt.isalnum():
                log("lobby", f"0:8 ignoring {txt!r} at 0x{off:03X}: too short or "
                             "not alphanumeric -- record data, not a handle")
                continue
            junk = _scavenged_junk_signature(txt)
            if junk:
                log("lobby", f"0:8 ignoring {txt!r} at 0x{off:03X}: {junk} -- a "
                             "scavenged phantom, not a handle a user chose")
                continue
            names.append((off, txt))
    if _handle_store_layout() == "crystal":
        # POL_HANDLE_STORE_LAYOUT=crystal -- read the 64 records instead of
        # scavenging; the scavenger's names above are only logged beside them.
        _handle_store_capture(pt, names)
        return
    if not names:
        log("lobby", "0:8 carried no handle-shaped names; nothing captured")
        return
    log("lobby", f"0:8 handle table (disc={disc}): "
                 + ", ".join(f"0x{o:03X}={n!r}" for o, n in names))
    # STORE EVERY handle in the table, not just the first. The client sends its
    # WHOLE list, so the new one is not necessarily first -- taking names[0] would
    # have re-stored the handle we already knew and silently dropped the new one,
    # which looks exactly like "registration does not work".
    for _off, _name in names:
        _capture_one_handle(_name.encode("ascii"))
    return True


#: 🔬 **POL_HANDLE_STORE_LAYOUT=crystal -- 0:8 IS 64 FIXED RECORDS, NOT A TABLE
#: TO SCAVENGE. DEFAULT OFF, NOT YET CHECKED AGAINST A CLIENT.**
#:
#: Project Crystal Server reads the 0x648-byte KPutHandleList object as 0x40
#: order bytes, then 64 records of 0x18:
#:
#:     +0x00 mode    0 = unchanged (skip), 1 = store, 2 = delete
#:     +0x01 creation position     +0x02 open level     +0x08 name, 16 bytes
#:
#: With our 0x28-byte frame header that is order at 0x28..0x67 and record i at
#: 0x68 + i*0x18, name at 0x70 + i*0x18. Our own logged 0:8s fit it:
#:   * the order bytes are the 0x00..0x3F ramp at 0x28 (the printable run
#:     " !\"#...?" at 0x48 in every dump), reordered `01 00 02 ..` after a
#:     handle swap (lobby.log.2, 2026-08-16);
#:   * Lex's registration: 0x68 = `01 00 02 00` = mode 1, position 0, open
#:     level 2, "Lex" at 0x70 -- the "disc=01000100" registration flavour is
#:     record 0's mode/position/level, and the "00 00 03 00" mail flavour is a
#:     SKIPPED record 0;
#:   * every phantom the scavenger ever minted sits in a record's name field
#:     (TUTTTTTT at 0x88 = record 1, DeckTest at 0x88 = record 1, LexGoons and
#:     HIJKLMNO at record 7/8 name +8), i.e. stale buffer in records we cannot
#:     show were mode 1 -- only the first 0x80 bytes of a 0:8 were ever
#:     dumped, so records 1..63's mode bytes are UNSEEN. The 0x10 stride from
#:     0x78 lands on every other record's name and on the middle of the rest.
#:
#: With the knob: mode-1 records are captured (same validation and tombstones
#: as before), mode-2 records are logged with the handle they name and deleted
#: only if POL_HANDLE_STORE_DELETE=1 as well, and the scavenger's result is
#: logged beside the parse for every 0:8. The reply is unchanged (Crystal
#: answers 64 u64 of 0x100000000000 | handle id; ours is left alone).
#: A/B: create, reorder and delete a handle with the knob on; the log's
#: `0:8 records` line should name exactly the handle touched, with the
#: scavenger's `would have taken` list for comparison.
_HANDLE_STORE_REC_AT = 0x68
_HANDLE_STORE_REC = 0x18
_HANDLE_STORE_N = 64
_HANDLE_STORE_NAME = 0x08


def _handle_store_layout():
    """"crystal" when POL_HANDLE_STORE_LAYOUT asks for it, else None."""
    v = os.environ.get("POL_HANDLE_STORE_LAYOUT", "").strip().lower()
    return "crystal" if v == "crystal" else None


def _handle_store_records(pt):
    """[(index, mode, position, open level, 16-byte name field)] with mode != 0."""
    out = []
    for i in range(_HANDLE_STORE_N):
        at = _HANDLE_STORE_REC_AT + i * _HANDLE_STORE_REC
        if at + _HANDLE_STORE_REC > len(pt):
            break
        mode = pt[at]
        if mode == 0:
            continue
        name = bytes(pt[at + _HANDLE_STORE_NAME:at + _HANDLE_STORE_NAME + 0x10])
        out.append((i, mode, pt[at + 1], pt[at + 2], name))
    return out


def _handle_store_text(field):
    """The name in a record's field, for logs and delete lookups ('' if none)."""
    raw = field.split(b"\x00", 1)[0]
    try:
        txt = raw.decode("ascii")
    except UnicodeDecodeError:
        return ""
    return txt if txt.isprintable() else ""


def _handle_store_capture(pt, scavenged):
    """Act on the 0:8 per record -- see `_HANDLE_STORE_REC_AT`."""
    recs = _handle_store_records(pt)
    log("lobby", "0:8 records (crystal layout): " + (", ".join(
        f"#{i} mode={mode} pos={pos} open={lvl} name="
        f"{_handle_store_text(nm)!r}" for i, mode, pos, lvl, nm in recs)
        or "none changed"))
    stored = sorted(_handle_store_text(nm) for _i, mode, _p, _l, nm in recs
                    if mode == 1)
    scav = sorted(n for _o, n in scavenged)
    log("lobby", f"0:8 scavenger would have taken {scav}; the records store "
                 f"{stored}" + ("" if scav == stored else " -- THEY DISAGREE"))
    for i, mode, pos, _lvl, nm in recs:
        if mode == 1:
            _capture_one_handle(nm)
        elif mode == 2:
            _handle_store_delete(i, pos, nm)
        else:
            log("lobby", f"0:8 record #{i}: unknown mode {mode}; ignored")


def _handle_store_delete(index, pos, field):
    """A mode-2 record. Logged; acted on only with POL_HANDLE_STORE_DELETE=1."""
    name = _handle_store_text(field)
    act = os.environ.get("POL_HANDLE_STORE_DELETE", "0") == "1"
    try:
        db = accounts.connect()
        try:
            mid = lobbysession._session_member_id()
            row = db.execute("SELECT id FROM handle WHERE member_id = %s AND"
                             " handle_name = %s", (mid, name)).fetchone() \
                if mid and name else None
            if row is None:
                log("lobby", f"0:8 record #{index}: DELETE position {pos} "
                             f"name {name!r} -- names no handle of member {mid}; "
                             "nothing done")
            elif not act:
                log("lobby", f"0:8 record #{index}: DELETE {name!r} (handle "
                             f"{row['id']}, position {pos}) -- logged only, "
                             "POL_HANDLE_STORE_DELETE=1 acts on it")
            else:
                gone = accounts.delete_handle(db, mid, name)
                log("lobby", f"0:8 record #{index}: DELETE {name!r} (handle "
                             f"{row['id']}, position {pos}) -> "
                             f"{'deleted + tombstoned' if gone else 'not found'}")
        finally:
            db.close()
    except Exception as exc:
        log("lobby", f"0:8 record #{index}: delete failed ({exc!r})")


def _capture_one_handle(raw):
    """Validate and persist ONE handle name from a 0:8 table entry."""
    # VALIDATE before storing. 0x70 holds a handle name only on an actual
    # registration; other 0:8 flavours (a handle DELETE was the one that caught
    # us) carry something else there, and a blind `decode("ascii", "replace")`
    # turned that into a U+FFFD soup which `set_handle` then PROMOTED TO PRIMARY.
    # 0:9 serves the primary handle back to the client, so one bad capture became
    # a gibberish handle on screen. Anything that is not a legal POL handle is
    # dropped and logged instead.
    # WIDTH FIRST. The PC client writes an 8-bit C string here; the PS2 Viewer
    # appears to write 16-bit characters, so splitting on the first NUL kept one
    # letter -- a handle typed on the console stored as 'Y'. The length check
    # below then passed (1 == 1) and the truncation looked like a real name.
    # Detect the wide form by its signature (odd bytes NUL) rather than by
    # client, so neither has to be identified.
    field = raw.split(b"\x00\x00", 1)[0]
    if len(field) > 1 and field[1::2].strip(b"\x00") == b"" and field[0::2].strip(b"\x00") != b"":
        try:
            handle = field.decode("utf-16-le", "ignore").split("\x00", 1)[0].strip()
        except Exception:
            handle = ""
        raw = field[0::2] + b"\x00"          # narrow it for the checks below
    else:
        handle = raw.split(b"\x00", 1)[0].decode("ascii", "ignore").strip()
    if not handle:
        return
    why = accounts.check_handle_policy(handle)
    if why or any(ord(c) < 0x20 or ord(c) > 0x7E for c in handle) \
            or len(handle) != len(raw.split(b"\x00", 1)[0]):
        log("lobby", f"handle registration REJECTED {handle!r} "
                     f"(raw={raw.split(bytes(1), 1)[0].hex()}): "
                     f"{why or 'non-printable or non-ASCII bytes'}")
        return
    try:
        db = accounts.connect()
        try:
            # THE MEMBER WHO IS LOGGED IN, not "the first one in the table". The
            # old query gave every account's newly registered handle to member 1,
            # so a second account's handle was created under the first account and
            # then never appeared in its own 0:9 list.
            mid = lobbysession._session_member_id()
            row = db.execute("SELECT id FROM member WHERE id = %s",
                             (mid,)).fetchone() if mid else None
            if row is None:
                log("lobby", f"handle registration {handle!r} but no member row")
                return
            if accounts.is_handle_deleted(db, row["id"], handle):
                log("lobby", f"handle {handle!r} is TOMBSTONED (deleted); "
                             "not re-capturing the client's cached copy")
            elif accounts.member_by_handle(db, handle) is None:
                accounts.set_handle(db, row["id"], handle)
                log("lobby", f"handle registration CAPTURED: {handle!r} -> "
                             f"member {row['id']}")
            else:
                log("lobby", f"handle registration {handle!r} already known")
        finally:
            db.close()
    except Exception as exc:
        log("lobby", f"handle capture failed: {exc!r}")


# --------------------------------------------------------------------------- #
# The lobby opcode table's capture entries for handles (see lobbyops.py)
# --------------------------------------------------------------------------- #
def capture_active_handle(pt):
    """4:7: the client names the handle it is logged in as."""
    _capture_active_handle(pt)
    return True
