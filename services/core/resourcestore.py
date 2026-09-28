"""The resource store: blobs the client fetches and writes under /data/resources."""
import os
import re
import struct
import titles                   # the title-plugin seam (services/titles.py)  # noqa: E402
from srvcore import log
from .deps import accounts
from . import fetchpath, friendgroups, lobbymail, lobbyreply, lobbysearch, lobbysession, pacing, paylen, pfc


RESOURCE_DIR = os.environ.get("POL_RESOURCE_DIR", "/data/resources")


def _resource_file(path, subject=0):
    """The filesystem name a POL resource is STORED under.

    The path is client-supplied, so it is flattened rather than joined -- a
    resource called `../../etc/whatever` must not escape the directory.

    THREE SCOPES, and which one a path gets is a decision, not a default:

      * a message gets one canonical name for everybody (`_mail_name`);
      * a LOBBY LIST is keyed by the client's SUBJECT (`_subject_keyed`), which
        is how SE addressed it -- the client names the object it wants and every
        member in one lobby therefore reads ONE file. Before this they got one
        copy each: 17 accounts held 17 copies of `b/g/ZL` in four divergent
        versions, and a `b/g/PTL` fix authored for member 1 never reached the
        player sitting in that room as member 16;
      * everything else stays scoped to the session's own member, which is right
        for save data and is a SECURITY boundary -- see `_SUBJECT_KEYED_PATHS`.

    WARNING: A subject-keyed path with subject 0 falls back to the MEMBER, never to a
    shared file: a client that sends no subject must not be handed, or allowed to
    clobber, the object every other client is reading.
    """
    name = lobbymail._mail_name(path)
    if name:
        return os.path.join(RESOURCE_DIR, name)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", path)
    if subject and fetchpath._subject_keyed(path):
        return os.path.join(RESOURCE_DIR, "s%x.%s.bin" % (subject, safe))
    member = lobbysession._session_get("member_id") or "shared"
    return os.path.join(RESOURCE_DIR, "%s.%s.bin" % (member, safe))

#: (sender handle id, recipient handle id, kind) -> when we minted it.
#:
#: WHY THIS EXISTS. Some clients post their OWN copy of a friend notification a
#: few seconds after the write that caused it, and some do not. Measured
#: 2026-08-16, both in one log: `Lex` accepting on the PC Viewer produced a
#: client 3:1 six seconds later (decoded: same sender, same recipient, same kind
#: 0x8480 as ours -- a true duplicate), while `DeckTester` accepting on the Steam
#: Deck produced nothing at all.
#:
#: So neither "always mint" nor "never mint" is right: one duplicates for the
#: Viewer, the other is silent for the Deck. Mint always, and drop the client's
#: copy when it arrives -- which is what this records. Keyed on HANDLE IDS
#: because the two sides use different id vocabularies on the wire (see
#: `_mail_normalise`) and only the resolved handle is common to both.
_MAIL_MINTED = {}
_MAIL_MINTED_TTL = 600


def _resource_read_file(path, subject=0):
    """The file a READ should serve, adopting an older name if that is where the
    content actually is.

    Migrating on the way past (rename, not copy) is deliberate: it happens once
    per message, it is atomic, and it leaves one file per message afterwards --
    so `_mailbox` cannot list the same message twice under two names.
    """
    want = _resource_file(path, subject)
    if os.path.exists(want) or not lobbymail._mail_name(path):
        return want
    for name in lobbymail._mail_legacy_names(path):
        old = os.path.join(RESOURCE_DIR, name)
        try:
            if os.path.getsize(old) <= 0:
                continue
            os.replace(old, want)
            log("lobby", f"  mail: adopted {name!r} as {os.path.basename(want)!r} "
                         "(message filed by an older build)")
            return want
        except OSError as e:
            log("lobby", f"  mail: cannot adopt {name!r} ({e})")
    return want


#: A FRESH resource is not necessarily all zeros -- some carry a magic the client
#: validates before it will use the blob at all. Each title declares its own
#: (`titles.Title.resource_init`, merged in below); the core has none.
RESOURCE_INIT = {}


def _resource_stored(path, subject=0):
    """True if the client has ever written this resource -- a stored blob must be
    served, never answered with "no data"."""
    try:
        return os.path.getsize(_resource_read_file(path, subject)) > 0
    except OSError:
        return False


def _resource_blob(path, n, subject=0):
    """`n` bytes for this resource: stored content if we have any, else the
    shipped template for paths that have one, else a fresh one (the measured
    header, zero-padded)."""
    try:
        with open(_resource_read_file(path, subject), "rb") as f:
            data = f.read()
        # LIVE VALUES PATCHED INTO A STORED BLOB: each title's patchers (room
        # roster, lobby counts, zone host, auction counts, per-build layouts,
        # the live record overlaid on a save). Every patcher is path-gated, so
        # the order between titles does not matter; the order WITHIN a title
        # does.
        data = titles.resource_patch(path, data, subject)
        return data[:n].ljust(n, b"\x00")
    except OSError:
        pass
    # THE PUBLISHED RANKING TALLY OUTRANKS THE SHIPPED FIXTURE, and it is looked
    # up through the SAME function that answered `<LN>` on the other band --
    # `_rank_list_blob`. For every other path this is the title's template.
    tmpl = pfc._rank_list_blob(path)
    if tmpl is not None:
        # WARNING: THE SAME PATCHES AS THE STORED PATH, IN THE SAME ORDER. The
        # template branch used to skip the live roster, which would have
        # served the shipped tables with a permanently EMPTY member list -- the
        # bug the roster exists to fix, reintroduced by the fallback.
        data = titles.resource_patch(path, tmpl, subject)
        log("lobby", f"  3:0 {path!r}: no stored copy for this subject -- "
                     f"serving the shipped template ({len(tmpl)}B)")
        return data[:n].ljust(n, b"\x00")
    init = RESOURCE_INIT.get(path, b"")
    env = os.environ.get("POL_RESOURCE_INIT_" + re.sub(r"\W", "_", path).upper())
    if env is not None:
        try:
            init = bytes.fromhex(env)
        except ValueError:
            pass
    blob = bytearray(init[:n].ljust(n, b"\x00"))

    # A member with NO stored save is the case that matters most here: this is a
    # brand new player, and their record -- however short -- is the only thing
    # that distinguishes them from the factory blob, so the same live patchers
    # run on the fresh blob. Applied BEFORE the two experiments below so those
    # debug levers still win when they are switched on; they deliberately
    # overwrite content.
    blob = bytearray(titles.resource_patch(path, bytes(blob), subject))
    # ...and each title's (Tetra Master's fresh save must carry the FACTORY
    # option header rather than the zeros that read as "every volume Off").
    blob = bytearray(titles.store_patch(path, bytes(blob)))

    # --- THE ZERO-SUM EXPERIMENT: ANSWERED, AND NOT BY THIS CODE ----------
    # It asked "which is wrong, our CHECKSUM or the content?" on the premise that
    # every non-zero 03:00 payload we had ever sent was rejected. That premise was
    # already false in our own log: `u/account` is served at 664 B with a NON-zero
    # trailer (79858978 / 7684aba3 / 858398e3) dozens of times a day and the
    # client closes satisfied every time. Our checksum, the seed=0 assumption and
    # non-zero content are therefore all confirmed correct ON THIS OPCODE, and the
    # one historical POL-5135 (the 664-byte marker probe) predates the signer.
    #
    # Kept as a knob because it costs nothing, but it is OFF: it writes the
    # magic's two's complement at +0x04, which is real garbage inside a blob the
    # client will eventually parse.
    if os.environ.get("POL_RESOURCE_ZEROSUM") == "1" and len(blob) >= 8:
        head = struct.unpack_from("<I", blob, 0)[0]
        struct.pack_into("<I", blob, 4, (-head) & 0xFFFFFFFF)
        log("lobby", f"  ZEROSUM experiment: {path!r} +0x00={head:#010x}, "
                     f"+0x04={((-head) & 0xFFFFFFFF):#010x} -> word-sum 0, "
                     f"content non-zero")

    # --- THE MARKER PROBE, which is what the measurement actually calls for --
    # The whole 03:00 transport is now read off EE RAM and every stage of it
    # matches what we send (the 12-state fetch machine): the reply is
    # header(24) + payload(declared-4) + trailer(4), the payload is streamed
    # straight into the CALLER's buffer, and only then summed. So the remaining
    # unknown is not the envelope, it is the 976 bytes of CONTENT -- of which we
    # have measured exactly one field, the 0x02030100 magic at +0x00.
    #
    # Filling the rest with NUL-terminated offset markers turns one launch into a
    # map of which offsets the game reads, the same way `markerz` mapped the
    # u/account record. The magic is restored afterwards so the blob still passes
    # the game's header check and it gets far enough to read anything.
    #
    #   POL_RESOURCE_MARKER="U/g/<a save path>"   (comma-separated; "*" = all)
    marks = [s.strip() for s in
             os.environ.get("POL_RESOURCE_MARKER", "").split(",") if s.strip()]
    if marks and (("*" in marks) or (path in marks)):
        blob = bytearray(lobbyreply._marker_payload_z(n))
        blob[:len(init[:n])] = init[:n]          # the magic outranks the marker
        log("lobby", f"  MARKER probe: {path!r} {n}B of NUL-terminated offset "
                     f"markers, {len(init[:n])}B of measured header kept")
    return bytes(blob)


def _resource_store(path, data):
    """Keep what the client wrote, so the next launch sees it.

    A MESSAGE THE READER WRITES BACK IS NEVER ALLOWED OVER THE AUTHOR'S COPY.
    Measured 2026-08-16: served a zero body, Lex's client answered each of the
    four 3:0 reads with a 3:2 write of its own -- 291 bytes it cannot have got
    from anywhere but the empty read. Under one canonical name per message that
    write lands on the sender's real 304 bytes and the message is gone for good.
    What that echo MEANS (a read flag? a local cache flush?) is unmeasured, so it
    is kept beside the message as `.readback` rather than dropped: harmless if it
    turns out to be nothing, and there to read if it turns out to matter.
    """
    # WARNING: A SUBJECT-KEYED PATH IS SERVER-AUTHORED AND IS NEVER WRITABLE BY A
    # CLIENT. Unreachable today (`_RESOURCE_WRITE_PATHS` is `O/m/`), and here so
    # it STAYS unreachable: the file behind `b/g/PTL` is shared by everyone in
    # the room, so one client writing it would rewrite the lobby for all of them
    # -- and the key naming that file is eight bytes the same client chose. This
    # is the same reasoning that keeps `U/g/*` off `_SUBJECT_KEYED_PATHS`, one
    # direction over.
    if fetchpath._subject_keyed(path):
        log("lobby", f"  3:x write to {path[:48]!r} REFUSED -- a subject-keyed "
                     f"lobby list is server-authored; a client cannot store one")
        return
    dest = _resource_read_file(path)
    if lobbymail._mail_name(path) and os.path.exists(dest) and os.path.getsize(dest) > 0:
        owner = lobbymail._mail_owner(path)
        me = lobbysession._session_get("member_id")
        if owner is not None and me is not None and int(owner) == int(me):
            dest += ".readback"
            log("lobby", f"  mail: member {me} is this message's RECIPIENT, not its "
                         f"author -- {len(data)}B kept as {os.path.basename(dest)!r}, "
                         "the stored message is untouched")
    try:
        os.makedirs(RESOURCE_DIR, exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)
        log("lobby", f"  stored {len(data)}B for resource {path!r}")
        return True
    except OSError as e:
        log("lobby", f"  could NOT store resource {path!r}: {e}")
        return False


#: THE OBJECT BLOCK, AND IT IS AT A FIXED OFFSET - measured from SE's OWN live
#: service, 2026-08-16 (`polshim-se.429364.log` decoded with `lobbydec.py`;
#: the 3:1 at line 77469 and the 3:0 at 74739 are the two halves of it):
#:
#:     +0x0000  02 03 0N 00  <u32 body len>       header
#:     +0x0038  "O/m/<99 chars>\0"                the path, in a FIXED 0x184 field
#:     +0x01BC  u32 OBJECT LENGTH
#:     +0x01C0  the object -- `<subject>\x07<body>\x00`, and nothing else
#:     +....    u32 checksum, the last four bytes of the frame
#:
#: so `0x1BC + 8 + objlen == len(frame)`, which is what makes the block
#: self-verifying. SE's read answers with **exactly that object plus a 4-byte
#: trailer**: a 30-byte "Friend registration accepted" message came back as a
#: 34-byte payload, not padded to anything.
#:
#: THE TWO BUGS THIS KILLS, both of which made the inbox look empty:
#:   * we stored everything after the path -- 287 bytes of the client's
#:     UNINITIALISED buffer, then the length field, then the object, then the
#:     request's own checksum -- instead of the object;
#:   * and we served it back at 664 bytes, the `u/account` default, because
#:     `O/m/` has no entry in `_FETCH_PATHLEN`. SE never pads. A reader that
#:     finds its object 350 bytes from where the length says it ends has no
#:     message to draw, and draws none.
#:
#: 03:02 frames are SHORTER than 0x1C4 and carry no object block at all (SE's
#: are pure uninitialised heap in that region). The client sends one after
#: reading each message; whatever it means, it is not a store.
_MAIL_OBJ_OFF = 0x1BC


#: Which resource paths a 03:02 write is allowed to PERSIST, as prefixes.
#:
#: Deliberately narrow. The note at the capture site warned that a wrong guess
#: here "means storing garbage and serving it back, which breaks the game more
#: thoroughly than storing nothing" -- and that is right, so this stores only
#: what the evidence actually covers. `O/m/` is the MESSAGE object store, which
#: is where the 2026-08-15 write/read pair was measured. Every game save is
#: untouched until someone
#: measures one the same way.
_RESOURCE_WRITE_PATHS = tuple(
    p.strip() for p in os.environ.get("POL_RESOURCE_WRITE_PATHS", "O/m/").split(",")
    if p.strip())

#: Exact object length required of a write, for paths whose reader asks for a
#: fixed size (a title declares these in `Title.resource_write_len`; the paths
#: are then writable too). A different length is a different build or a
#: different file, and is not stored.
_RESOURCE_WRITE_LEN = {}


def _capture_resource_write(pt, op=None):
    """Persist a 03:02 object write, so a later 03:00 can read it back.

    THE BUG THIS FIXES. `_resource_store` had existed for a long time with ZERO
    callers: the client wrote an object, we acknowledged it and dropped it on the
    floor, and the matching read was answered with `serving 664B (all zero = 'no
    data stored yet')`. On the message store that renders as a Message with no
    subject and an empty body, which is exactly what two accounts saw on
    2026-08-15.

    Measured that day, from a real write (448B, decrypted):

        +0x00  02 03 02 00  <u32 len 0x198>      header
        +0x28  body
        +0x38  "O/m/<99 chars>\\0"                the path -- same offset 03:00 uses
        +0x9C  ~292 bytes                        the content

    THE CONTENT IS NOT PARSED, and that is the point: those bytes are
    high-entropy (the client encodes them itself), so we neither can nor need to
    understand them. Store the bytes, hand the same bytes back. That is what an
    object store is.
    """
    path = fetchpath._fetch_path(pt)
    if not path or "/" not in path.strip("/"):
        log("lobby", f"  {op or '3:x'} write: no resource path in the request; nothing stored")
        return
    if not path.startswith(_RESOURCE_WRITE_PATHS):
        log("lobby", f"  {op or '3:x'} write: {path[:48]!r} is not a storable prefix "
                     f"({'|'.join(_RESOURCE_WRITE_PATHS)}) -- captured, NOT stored")
        return
    end = pt.find(b"\x00", paylen._FETCH_PATH_OFF)
    if end < 0:
        log("lobby", f"  {op or '3:x'} write: path is not NUL-terminated; nothing stored")
        return
    # THE OBJECT IS THE `[u32 len][bytes]` BLOCK AT THE TAIL, not everything
    # after the path -- see `_MAIL_OBJ_OFF`. Storing the rest meant storing 287
    # bytes of the client's uninitialised buffer in front of the message and the
    # request's checksum behind it, and then serving that back as the message.
    data = lobbymail._mail_object(pt)
    if data is None:
        # A 03:02 lands here: it is shorter than one object block, and SE's own
        # 03:02 frames hold nothing but heap junk in that region. It is not a
        # write at all -- it is the client saying IT HAS TAKEN THIS MESSAGE, and
        # SE retires it from the mailbox on the spot.
        #
        # HOW THAT IS KNOWN, 2026-08-16: in SE's own capture the client reads a
        # message (3:0) and answers with a 3:2 for the same path, four times over
        # -- and the 3:3 that comes later lists ONE message, and it is not any of
        # the four. A mailbox that keeps them is a mailbox that re-delivers every
        # message on every login, which is exactly the "already read, unread
        # again" the account holder reported.
        #
        # The bytes are kept (`.read`), never deleted: if it turns out the client
        # has no local copy, POL_MAIL_RETIRE=0 puts them all straight back.
        if lobbymail._mail_name(path) and os.environ.get("POL_MAIL_RETIRE", "1") == "1":
            lobbymail._mail_retire(path)
            return
        log("lobby", f"  {op or '3:x'} write: {path[:48]!r} carries no object block "
                     f"(frame {len(pt)}B; an object needs {_MAIL_OBJ_OFF + 8}B + its "
                     "own length) -- nothing stored, the stored message stands")
        return
    if not data:
        log("lobby", f"  {op or '3:x'} write: {path[:48]!r} carried an EMPTY object; "
                     "nothing stored")
        return
    _want = _RESOURCE_WRITE_LEN.get(path)
    if _want is not None and len(data) != _want:
        log("lobby", f"  {op or '3:x'} write: {path[:48]!r} is {len(data)}B, its "
                     f"reader wants {_want}B -- NOT stored")
        return
    if lobbymail._mail_name(path):
        try:
            db = accounts.connect(os.environ.get("POL_ACCOUNTS_DB",
                                                 accounts.DEFAULT_DB))
            try:
                path = lobbymail._mail_normalise(path, lobbysession._session_handle_id(db))
                # THE CLIENT'S OWN COPY OF A NOTIFICATION WE ALREADY POSTED.
                # The PC Viewer re-sends the friend request/acceptance itself a
                # few seconds after the 2:6 that caused it; the Steam Deck does
                # not. We mint for both (see the note by `want_acc`), so this is
                # where the Viewer's second copy is dropped -- otherwise the
                # recipient's mailbox holds two identical messages and the push
                # fires twice.
                if lobbymail._mail_is_own_echo(db, path):
                    return
                # A friend REQUEST to someone already an active friend is the TM
                # room-sidebar re-friend; drop it rather than spam the peer.
                if lobbymail._mail_drop_redundant_request(db, path):
                    return
            finally:
                db.close()
        except Exception as exc:
            log("lobby", f"  mail: cannot normalise the sender field ({exc})")
    _resource_store(path, data)
    lobbymail._mail_announce(path)                    # live delivery -- see _push_deliver_mail
    friendgroups._capture_group_invite(path, data)       # groups have no PUT of their own
    text = data.split(b"\x07")
    log("lobby", f"  {op or '3:x'} write: {len(data)}B object stored for {path[:48]!r}"
                 f" -- subject {text[0][:40].decode('cp932', 'replace')!r}"
                 + (f", body {text[1].rstrip(chr(0).encode())[:40].decode('cp932', 'replace')!r}"
                    if len(text) > 1 else ""))


#: The group name inside an invitation body, e.g.
#: `Would you like to join the group "LexGroup"?` -- SE's own wording, captured
#: verbatim at 3:1 line 31107 of polshim-se.428024.log. The quoted form is what
#: makes this parseable at all; the sibling line ("Would you like to join a
#: friend group?") names no group and is deliberately NOT matched.
_GROUP_INVITE_RE = re.compile(r'group\s+"([^"]{1,64})"')


# --------------------------------------------------------------------------- #
# The lobby opcode table's entries for resources (see lobbyops.py)
# --------------------------------------------------------------------------- #
def payload_fetch(n, req_pt):
    """3:0 KGetDetailData. ONE OPCODE, MANY RESOURCES: 03:00 is keyed by the
    path string. A `u/s/select` path is the member search's result, not a
    stored resource. Any other path is served from the store, or built live
    by a title. The account record (`u/account`, and an empty path) is left
    to the generic path, so the POL_LOBBY_TAIL="3:0=acct" override cannot
    reach a non-account path -- it would hand a game the account record
    truncated to 200 bytes, which is the same path-mixing that broke
    `u/s/select`."""
    if req_pt is None:
        return None
    path = fetchpath._fetch_path(req_pt)
    if path.startswith(paylen._SELECT_PATH):
        return lobbysearch._search_result_payload(n, req_pt)
    pacing._lobby_delay(path)
    if not path or path == "u/account":
        return None
    # A TITLE MAY BUILD THIS FETCH LIVE (its lobby lists from the room
    # registry, for a member it knows is in that title) and bypass the
    # stored copy altogether.
    live = titles.resource_live(path, n, req_pt)
    if live is not None:
        return live
    blob = _resource_blob(path, n, fetchpath._fetch_subject(req_pt))
    log("lobby", f"  3:0 {path!r}: serving {len(blob)}B"
                 + ("" if blob.strip(b"\x00") else " (all zero = 'no data "
                    "stored yet'. WARNING: That is not automatically SAFE -- a "
                    "manifest read as count=0 is a fatal error on some "
                    "titles; see RESOURCE_INIT and the title's own)"))
    return blob


def capture_write(pt):
    """3:1 and 3:2, BOTH object writes. 03:02 is an UPDATE of an existing
    object; 03:01 is the MESSAGE SEND -- measured 2026-08-15 as a 461-byte
    request carrying `O/m/<path>` + 363 bytes at the same +0x38 the read uses.
    They have the same shape, so they store the same way.

    The earlier note here was right that 03:01 is the send and wrong to
    conclude it therefore is not a write: sending a message IS creating the
    object the recipient later reads. Storing only 03:02 meant a real send
    stored nothing at all, which is why the first fix changed nothing.
    POL_RESOURCE_WRITE=0 stores nothing and lets the generic resource scan
    name the path instead."""
    if os.environ.get("POL_RESOURCE_WRITE", "1") != "1":
        return False
    _capture_resource_write(pt, f"{pt[1]:02x}:{pt[2]:02x}")
    return True
