"""Resource fetch subjects and paths."""
import os
import struct
from srvcore import log
from .deps import accounts
from . import lobbysession, paylen



def _fetch_subject(pt):
    """The u64 the client put at frame +0x30 -- the object it is asking for."""
    if not pt or len(pt) < paylen._FETCH_SUBJECT_OFF + 8:
        return 0
    return struct.unpack_from("<Q", pt, paylen._FETCH_SUBJECT_OFF)[0]


#: PATHS KEYED BY THE CLIENT'S SUBJECT INSTEAD OF THE SESSION'S MEMBER.
#:
#: WARNING: THIS LIST IS DELIBERATELY SHORT, AND THE OMISSIONS ARE A SECURITY BOUNDARY,
#: not an oversight. Settled 2026-08-18 after mining every surviving capture:
#:
#:     member 16  u/account         0x860fb3e2a2
#:     member 16  (a title's save)   0x860fb3e2a2   <- the SAME
#:     member 16  b/g/ZL            0x384ea5822c
#:     member 16  b/g/RL000         0x384ea5822c   <- the SAME as ZL
#:     member 16  b/g/PTL           0xf0e4bcbb91
#:     member  8  b/g/PTL           0x3d7c0054cc
#:     member  8  a game save       0xc8e3816e18
#:     member  1  u/account         0x162e92cdc54
#:
#: The `b/g/*` rows prove the subject is NOT the account -- one session, two
#: different values -- which is what makes keying by it worth doing at all.
#:
#: WARNING: AND THE SAME TABLE IS WHY `U/g/*`, `u/account` AND MAIL ARE REFUSED.
#: `u/account` and a title's save carry the IDENTICAL value, so the subject on
#: a user path is the client's own guid -- and it is CLIENT-ASSERTED. Keying
#: stored data by it would let any client read or overwrite another account's
#: save by sending eight different bytes, with no login involved. The
#: authenticated session member is the only defensible key for anything a user
#: owns. Do not "simplify" this by widening the list.
#:
#: It also disposes of the subject-0 hazard: the dangerous class is not keyed
#: this way at all, and where we DO key by subject a zero falls back to the
#: member id -- never to a shared file, which would be the collapse this split
#: exists to prevent.
#:
#: THE MODEL (a model, not a measurement): the subject is
#: an sqMg OBJECT NICK -- `cp__002fcae8` prints the cp context as
#: Nick / LnNick / RgmNick / TgmNick. Own nick for user paths, the LOBBY's for
#: ZL+RL (hence identical), the ROOM's for PTL, which is `cp__002fc608`'s fourth
#: argument `DAT_003f1878`, set by `sqMgCpEnterRoom(RgmNick, chan, ...)`. Two
#: testable predictions ride on it: the two games' ZL subjects should DIFFER
#: (the PS2 title dials its own game host and TM does not), and PTL's subject
#: should be per-ROOM. If the PS2 title's `b/g/ZL` comes back as 0x384ea5822c
#: the model is wrong and the tracks have a real collision -- which keying could
#: not fix anyway, since two games wanting different content at one path under
#: one key is resolvable only by per-game generation (a title's
#: `resource_live`).
_SUBJECT_KEYED_PATHS = ("b/g/ZL", "b/g/PTL")
_SUBJECT_KEYED_PREFIXES = ("b/g/RL",)


def _subject_keyed(path):
    """True if `path`'s store is keyed by the client's subject, not the member."""
    if os.environ.get("POL_RESOURCE_SUBJECT_KEY", "1") != "1":
        return False
    return path in _SUBJECT_KEYED_PATHS or path.startswith(_SUBJECT_KEYED_PREFIXES)


#: MEASUREMENT ONLY -- log the subject of EVERY 3:x, not just `u/account`.
#:
#: WHY. `_resource_file` keys the store by the SESSION'S MEMBER, which is not
#: what SE did: the subject above is the client naming the object it wants, and
#: it varies BY PATH within one session (the same 0x30 the PS2 title's lobby
#: module reads,
#: four captures 2026-08-18: member 16 sends 0x384ea5822c for `b/g/ZL` and
#: `b/g/RL000` but 0xf0e4bcbb91 for `b/g/PTL`). Keying by member instead has
#: already forked one global zone list into four divergent copies across 17
#: accounts, and it is why a `b/g/PTL` fix authored for member 1 never reached
#: the player sitting in the room as member 16.
#:
#: WHAT IS STILL UNMEASURED, and why this logs instead of just switching:
#:   * `b/g/PTL`'s 0xf0e4bcbb91 is unexplained -- it is NOT the room id the
#:     client reports in its own `<DE>` (0x2000000001), so "PTL's subject is the
#:     room" is inference, not measurement.
#:   * no save-data path (`U/g/...`) has ever had its subject looked at.
#: A path that sends 0 would collapse every account onto ONE file, and that
#: reads as a working system until two accounts are on at once. So: measure
#: first, re-key second.
#:
#: WARNING: THIS CHANGES NO REPLY. It is a log line on the request path, gated so it
#: can be silenced without a code change (POL_FETCH_SUBJECT_LOG=0).
def _capture_fetch_subject(pt):
    """Log `<op> <path> subject=<u64>` for a 3:0 / 3:1 / 3:2 request."""
    if os.environ.get("POL_FETCH_SUBJECT_LOG", "1") != "1":
        return
    if len(pt) < paylen._FETCH_SUBJECT_OFF + 8:
        return
    try:
        subject = struct.unpack_from("<Q", pt, paylen._FETCH_SUBJECT_OFF)[0]
        path = _fetch_path(pt) or "<no path>"
    except Exception:
        return
    log("lobby", f"  {pt[1]:02x}:{pt[2]:02x} {path[:48]!r} subject={subject:#x}"
                 f"{'  <-- ZERO: this path cannot be keyed by subject' if not subject else ''}")


def _capture_self_guid(pt):
    """Learn what the client on this socket calls itself, from a `u/account` fetch.

    Why it matters: a message's RECIPIENT field is the reader itself, and a
    reader that does not recognise the id there draws "To: Unknown User" (see
    `_mail_normalise`). We can only address someone by an id they know if they
    have told us one, and before this the only channel was a message they sent --
    so the very first message to a new account was guaranteed to render
    anonymously. This channel fires on every login instead.
    """
    if accounts is None or len(pt) < paylen._FETCH_SUBJECT_OFF + 8:
        return
    if _fetch_path(pt) != paylen._SELF_GUID_PATH:
        return
    value = struct.unpack_from("<Q", pt, paylen._FETCH_SUBJECT_OFF)[0]
    if not value:
        return                       # the client does not know yet; nothing to learn
    try:
        db = accounts.connect()
        try:
            # A guid of ours means the client is echoing something we served,
            # which teaches us nothing about what it calls itself.
            if accounts.handle_by_guid(db, value) is not None:
                return
            hid = lobbysession._session_handle_id(db)
            if hid and accounts.learn_client_guid(db, int(hid), value):
                log("lobby", f"  u/account: handle {hid} calls itself {value:#x} -- "
                             "recorded, so mail addressed TO them can carry an id "
                             "they recognise")
        finally:
            db.close()
    except Exception as exc:
        log("lobby", f"  u/account: cannot record the caller's own id ({exc})")


def _fetch_path(pt):
    """The 03:00 key string, e.g. 'u/account' or 'u/s/select0'."""
    if pt is None or len(pt) < paylen._FETCH_PATH_OFF + 4:
        return ""
    end = pt.find(b"\x00", paylen._FETCH_PATH_OFF)
    if end < 0:
        end = min(len(pt), paylen._FETCH_PATH_OFF + 0x17F)
    return pt[paylen._FETCH_PATH_OFF:end].decode("cp932", "replace")


def _select_window(pt):
    """(first_byte, bytes_wanted) for a u/s/select fetch, or None."""
    if len(pt) < paylen._FETCH_WINDOW_LEN + 4:
        return None
    first = struct.unpack_from("<I", pt, paylen._FETCH_WINDOW_OFF)[0]
    want = struct.unpack_from("<I", pt, paylen._FETCH_WINDOW_LEN)[0]
    return (first, want) if want else None


def capture_fetch(pt):
    """3:0 (lobby opcode table). Read-only, and deliberately BEFORE the fetch
    is answered: this is where a client volunteers the id it knows itself by
    (see `_capture_self_guid`)."""
    _capture_self_guid(pt)
    return True
