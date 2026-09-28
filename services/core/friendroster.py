"""Sending a member's friend roster and slot layout on the auth band."""
import os
import struct
import threading
from srvcore import log
from .deps import accounts
from . import friendlist, handlelists, logingate, presence, pushrecord



def _friend_load_payload(served_guid, slot, hslot, name):
    """The substituted-base64 payload for a friend ROSTER-LOAD gate.

    This is the message that POPULATES polcore's recognition table 0x38740d8 --
    the thing the TM room sidebar's friend-check reads, which is empty because we
    never send this (see tm-member-sidebar-identity). Same 72-byte struct and
    codec as the presence push (`_presence_xxl_payload`), through the same handler
    0x037db6f0, but it takes the LOAD arm instead of the update arm:

        the dispatcher gates the whole class on [+0x3e]&0xf80==0xf80, then splits
        at polcore+0x1b761 `test [esi+0x1b]; je`: **+0x1b != 0 -> the LOAD path**
        (+0x1b824 -> store_friend@+0x1b854 inserts the friend at slot +0x1c into
        the table); +0x1b == 0 -> the single-slot UPDATE path the presence push
        uses, which cannot create a slot. The push sets +0x1b=0; we set +0x1b=1.

    Fields, read off the LOAD arm:
        +0x00/+0x04 u64  friend guid, XOR the same const the presence guid uses
                         (the runtime key cancels vs the 2:3 store -- see
                         pol-friend-presence). Also mirrored at +0x08 and +0x30.
        +0x18 u8   the friend's HANDLE-table slot (checked vs (slot+0x9c>>13)&0x3f).
        +0x19 |=1, +0x1a =0                     the same run-up gates as the push.
        +0x1b u8   1 -- NON-zero: THE byte that selects the LOAD path.
        +0x1c u8   the friend's 2:3 SLOT index (0..0x3f; != 0xff, or it hits the
                   login-completion arm instead).
        +0x24/+0x28/+0x2c  LEFT ZERO -- FUN_037e34f0 writes these into the lobby
                   handoff globals 0x3bc3040 only when non-zero; zero = untouched.
        +0x3e u16  0x0f80 (gate kind).   +0x42 u8  bit0 set.
    A 2nd chunk carries the display NAME (bit 0x10) so the row reads right.
    """
    lo = (int(served_guid) ^ 0x67891133) & 0xFFFFFFFF
    hi = ((int(served_guid) >> 32) ^ 0x1c273e45) & 0xFFFFFFFF
    p = bytearray(72)
    struct.pack_into("<II", p, 0x00, lo, hi)           # guid1
    struct.pack_into("<II", p, 0x08, lo, hi)           # guid2 = guid1
    p[0x18] = int(hslot if hslot is not None else slot) & 0x3f
    p[0x19] |= 1
    p[0x1a] = 0
    p[0x1b] = 1                                         # LOAD path (not the push's 0)
    p[0x1c] = int(slot) & 0x3f
    struct.pack_into("<II", p, 0x30, lo, hi)           # guid again (LOAD arm reads +0x30/34)
    struct.pack_into("<H", p, 0x3e, 0x0f80)            # gate kind: &0xf80==0xf80
    p[0x42] = 1
    # 2nd chunk: flags byte + the display name (bit 0x10 = 15 chars + NUL at ext+0x08).
    ext = bytearray(presence._PRESENCE_EXT_LEN)
    ext[0x00] = 0x10
    nm = (name.encode("cp932", "replace") if isinstance(name, str) else bytes(name))
    ext[0x08:0x08 + 15] = nm[:15].ljust(15, b"\x00")
    chunk = logingate._b64encode(bytes(ext))
    declared = len(chunk) if presence._presence_cfg(
        "len38", "POL_PRESENCE_LEN38", "encoded") == "encoded" else len(ext)
    struct.pack_into("<I", p, 0x38, min(declared, 0x157))
    main = logingate._b64encode(bytes(p).ljust(0x48, b"\x00"))
    return main + chunk


def _send_friend_roster(chat_sess, member_id):
    """Schedule the roster LOAD, DELAYED past the client's 2:3 fetch.

    WARNING: THE TABLE IS ONLY WRITABLE AFTER 2:3. polcore sets the friend-table READY
    flag `0x387ca80` when it processes the 2:3 friend-list reply; before that, a
    load takes the NOT-ready path (0x1b8c5) which writes the guid to slot +0x10
    instead of the ready-path store_friend (0x1b854) writing +0x08 -- and the
    recognition compare reads +0x08. Measured live 2026-08-21: an immediate load
    at the welcome hop landed at +0x10 (Kestra's guid appeared there) and the TM
    sidebar still did not recognise the friend. The row-push already delays for
    this exact reason. So fire the load a few seconds LATER, once 2:3 has set the
    flag. POL_FRIEND_LOAD_DELAY (default 4s); the session channel persists.
    """
    mode = presence._presence_cfg("load", "POL_FRIEND_LOAD", "0")
    if accounts is None or chat_sess is None or mode not in ("1", "log"):
        return 0
    try:
        delay = float(presence._presence_cfg("loaddelay", "POL_FRIEND_LOAD_DELAY", "4"))
    except ValueError:
        delay = 4.0
    if delay > 0:
        t = threading.Timer(delay, _do_send_friend_roster, (chat_sess, member_id))
        t.daemon = True
        t.start()
        return 0
    return _do_send_friend_roster(chat_sess, member_id)


def _do_send_friend_roster(chat_sess, member_id):
    """The actual roster LOAD send (see `_send_friend_roster` for the delay)."""
    mode = presence._presence_cfg("load", "POL_FRIEND_LOAD", "0")
    if accounts is None or chat_sess is None or not getattr(chat_sess, "alive", True) \
            or mode not in ("1", "log"):
        return 0
    dry = mode == "log"
    try:
        db = accounts.connect()
    except Exception as exc:
        log("authserv", f"friend-load: db open failed ({exc!r})")
        return 0
    try:
        h = db.execute("SELECT id FROM handle WHERE member_id = %s"
                       " ORDER BY is_primary DESC, id ASC LIMIT 1",
                       (int(member_id),)).fetchone()
        if h is None:
            return 0
        hid = int(h["id"])
        # The SLOT NUMBERING the client actually holds (what 2:3 served); +0x1c
        # must match it or the load lands on the wrong row. Fall back to the live
        # list order for a handle we have not served this session.
        served = friendlist._friend_slots_map(hid)
        hslots = friendlist._friend_handle_slots()
        rows = accounts.list_friends(db, hid, status="active")
        # slot -> (name, peer_handle, guid). Prefer the served slot map.
        slot_of = {}
        if served:
            byrow = {int(rid): slot for slot, (_nm, rid) in served.items()}
            for r in rows:
                if int(r["id"]) in byrow:
                    slot_of[byrow[int(r["id"])]] = r
        if not slot_of:                              # fallback: list order
            for i, r in enumerate(rows):
                slot_of[i] = r
        sent = 0
        # WHICH GUID the load carries. The recognition matches a friend against
        # ROOM MEMBERS, whom the client identifies by their CLIENT guid (the id
        # their own client holds -- measured: LaptopTest2 = 0x860fb3e2a2 in the
        # 2:6 it sent, the room roster, and every mail). So default to the
        # friend's client_guid when we know it; fall back to our handle_guid.
        # POL_FRIEND_LOAD_GUID=handle forces the old id for an A/B.
        want_client = presence._presence_cfg(
            "loadguid", "POL_FRIEND_LOAD_GUID", "client") != "handle"
        for slot, r in sorted(slot_of.items()):
            if not _friend_push_slot_ok(slot) or not r["peer_handle"]:
                continue                 # past the PC table (and < 0x40)
            hg = accounts.handle_guid(int(r["peer_handle"]))
            cg = 0
            if want_client:
                crow = db.execute("SELECT client_guid FROM handle WHERE id = %s",
                                  (int(r["peer_handle"]),)).fetchone()
                cg = int(crow["client_guid"] or 0) if crow else 0
            guid = cg or hg
            hslot = hslots.get(int(hg))
            name = r["live_name"] if "live_name" in r.keys() and r["live_name"] \
                else r["peer_name"]
            payload = _friend_load_payload(guid, slot, hslot, name).encode()
            rec_main = payload[:0x60].decode("ascii", "replace")
            # Same carrier as the presence push: a per-record pseudo-nick NOTICE
            # with an empty host, sent as a plain line (pad byte, like push_lines).
            line = (b":" + pushrecord._push_nick(rec_main).encode() + b"!~x@ NOTICE " +
                    chat_sess.nick + b" :" + payload)
            if dry:
                log("authserv", f"friend-load[dry]: {name!r} slot={slot} "
                                f"hslot={hslot} guid={guid:#x} -> {line[:80]!r}...")
            else:
                try:
                    chat_sess.send([line])
                except Exception as exc:
                    log("authserv", f"friend-load: send failed for {name!r} ({exc!r})")
                    continue
            sent += 1
        if sent or dry:
            log("authserv", f"friend-load: {'would send' if dry else 'sent'} "
                            f"{sent} roster-load gate(s) for member {member_id} "
                            f"(POL_FRIEND_LOAD={mode}); populates polcore 0x38740d8 "
                            "so in-game friend recognition works")
        return 0 if dry else sent
    except Exception as exc:
        log("authserv", f"friend-load: {exc!r}")
        return 0
    finally:
        db.close()


def _friend_push_slot_ok(slot):
    """May a friend-row / presence push address `slot`? Only below the list cap.

    A push files a friend row at `table + slot*0xB0`; the table holds 200 rows
    (the blacklist table sits exactly 100*0xB0 below it at 0x386fc18), so any
    slot the 2:3 list can serve (`_LOBBY_LIST[(0x02, 0x03)][2]`, 0x40) is in
    bounds. The 2026-08 note here calling slot 12+ "an OUT-OF-BOUNDS WRITE"
    rested on the 12-row misread corrected in `_LOBBY_LIST`. Every friend push
    path -- the 2:3 presence burst, the row/icon spool, login/logout/status
    presence, the accept pushes, the roster load -- skips a slot this says no
    to, and the deliverers check it again so a spool line from anywhere cannot
    get past; the push records' own slot byte (+0x1c) is documented `< 0x40`.
    """
    try:
        return 0 <= int(slot) < handlelists._LOBBY_LIST[(0x02, 0x03)][2]
    except (TypeError, ValueError):
        return False


def _friend_slot(db, watcher_handle_id, subject_handle_id):
    """The slot index the watcher's client assigned to `subject` in its 2:3 list,
    or None -- including for a slot at or past the PC cap, which no push may
    address (`_friend_push_slot_ok`).
    """
    slot = _friend_slot_raw(db, watcher_handle_id, subject_handle_id)
    if slot is not None and not _friend_push_slot_ok(slot):
        return None
    return slot


def _friend_slot_raw(db, watcher_handle_id, subject_handle_id):
    """The slot index the watcher's client assigned to `subject` in its 2:3 list.

    ASKS THE SLOT MAP FIRST -- the numbering `_list_payload` actually served (see
    `_friend_slots_publish`). Re-deriving it here cannot be trusted for the same
    reason a 2:6 delete could not: the client keeps the slots it was GIVEN, and a
    deletion this session leaves a hole rather than closing the list up.

    The re-derivation below is the fallback for a watcher we have not served, and
    it is a known-imperfect one: it counts `list_friends` rows without the
    incoming-request filter `_db_friends` applies, so it is skewed by however many
    invitations the watcher holds. Returns None when the subject is not in the
    watcher's list (nothing to update).
    """
    if accounts is None:
        return None
    try:
        row = db.execute("SELECT id FROM friend WHERE handle_id = %s"
                         " AND peer_handle = %s",
                         (int(watcher_handle_id), int(subject_handle_id))).fetchone()
    except Exception:
        row = None
    if row is not None:
        served = friendlist._friend_slots_map(int(watcher_handle_id))
        if served is not None:
            for slot, (_nm, rid) in served.items():
                if int(rid) == int(row["id"]):
                    return int(slot)
            return None            # served, and this friend was not in that reply
    try:
        rows = _friend_row_order(db, int(watcher_handle_id))
    except Exception:
        return None
    for idx, r in enumerate(rows):
        if r["peer_handle"] is not None and int(r["peer_handle"]) == int(subject_handle_id):
            return idx
    return None


def _friend_row_order(db, handle_id):
    """The friend rows, in the ORDER and the SET a 2:3 reply serves them.

    A SLOT IS AN INDEX INTO THIS LIST, so every re-derivation of a slot has to
    reproduce this filter exactly -- and two of them did not. `_db_friends`
    drops STATUS_INVITED, because an incoming request is a Message and not a
    friend-list row (see the long note there); both re-derivations enumerated
    `accounts.list_friends(..., status=None)` raw, so each was wrong by however
    many INCOMING requests the watcher happened to be holding.

    WARNING: MEASURED LIVE 2026-09-06 and it is not a corner case. The account holder
    had NINE incoming requests pending and nine friends served. A friend
    changed their picture; the repaint resolved their slot by raw enumeration,
    got a number that was not the one the client held, and the client -- which
    validates the slot before applying anything -- dropped the record without a
    sound. The server logged `row repainted on 1` and the row never changed.
    The same skew silently mis-aimed presence, which is the other half of "they
    show offline while they are online".

    Kept next to `_friend_slot` on purpose: the map is the authority and this is
    the fallback, and they must not drift apart again.
    """
    rows = accounts.list_friends(db, int(handle_id), status=None)
    if os.environ.get("POL_FRIEND_LIST_INVITED", "0") != "1":
        rows = [r for r in rows if r["status"] != accounts.STATUS_INVITED]
    return [r for r in rows if int(r["kind"]) == accounts.KIND_FRIEND]
