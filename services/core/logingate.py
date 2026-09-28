"""Login refusal and login-completion gate records."""
import datetime
import os
import socket
import struct
import time
from srvcore import log
from authtoken import token_encode
from .deps import accounts
from . import authnode, lobbymail



# --------------------------------------------------------------------------- #
# REFUSING a login: the same record, with a status byte instead of an address
# --------------------------------------------------------------------------- #
# CAPTURED LIVE FROM SE, 2026-08-16. Two deliberate failures against the real
# service through the SE-facing install (bogus PlayOnline ID, then a real ID with
# a wrong password), auth band decrypted out of the shim log:
#
#   C->S  NICK ULHHYSPE3:<32-hex session digest>:<36-char blob>
#   S->C  ERROR :Closing Link: [unknown@<client public ip>] (POL <40 base-32>)
#
# A refusal is NOT a distinct message type: it is the SAME 25-byte record as a
# redirect, and the two captures differ in EXACTLY ONE BYTE -- offset 6, the
# status byte pol_error_token() documents. pol_error_token's own reading of the
# client (polcore FUN_037d5820) says status 0 or >= 0xdc means "store this and
# reconnect"; these two sit below that, so the client treats the record as
# terminal and renders a message instead of dialling on. The address field is
# left zero, which is consistent -- there is nowhere to go.
REJECT_UNKNOWN_ID = 0xC9        # SE's byte for a PlayOnline ID that does not exist
REJECT_BAD_PASSWORD = 0xCA      # SE's byte for a real ID with the wrong password
# The rest of the family, named per Project Crystal Server's AdminData notes
# (its reading of the client; only C9/CA have been captured from SE by us):
REJECT_LOCKED = 0xCB            # too many failed logins (the lockout below)
REJECT_VERSION = 0xCC           # "not working due to a version discrepancy"
REJECT_BUSY = 0xCD              # "network is busy" -- the generic one
REJECT_UNKNOWN_ERROR = 0xCE     # unknown error
# 0xDC..0xFC and 0xFF are ADMIN MESSAGES (the LM-xx dialogs, code - 220), which
# Crystal uses for "you are banned"-style logouts. Note pol_error_token's
# reading that the client treats 0 and >= 0xDC as "store and reconnect".
_REJECT_WHY = {REJECT_UNKNOWN_ID: "unknown PlayOnline ID",
               REJECT_BAD_PASSWORD: "wrong password",
               REJECT_LOCKED: "locked out after failed logins",
               REJECT_VERSION: "version mismatch",
               REJECT_BUSY: "busy",
               REJECT_UNKNOWN_ERROR: "unknown error"}

# SE's own 25 bytes, with the status byte zeroed for filling in.
#
# THE TAIL WAS SOMEBODY'S ACCOUNT. Project Crystal Server's AdminData lays this
# record out as [0:2] volume, [2] domain, [4:6] status, [6] message id (our
# status byte), [7] account number, [8:12] date, [16:24] MasterPolId
# (big-endian). In SE's captured refusal those bytes decoded to a real SE
# account's master id (the account behind the capture), and we were sending
# it to every player we refused. It is ZEROED, which is also what Crystal
# sends in its error records; nothing reads it on a refusal as far as anyone
# knows. The captured value is deliberately NOT kept anywhere in this tree.
_SE_REJECT_RECORD = bytes.fromhex(
    "0000000000000000"          # [0:8]  STATUS at [6]
    "0000000002000000"          # [8:16] address field ZERO -- nowhere to go
    "000000000000000000"        # [16:24] MasterPolId (zeroed), [24] pad
)


def pol_reject_token(status):
    """The base-32 record that REFUSES a login, for `ERROR :... (POL <token>)`."""
    raw = bytearray(_SE_REJECT_RECORD)
    raw[6] = status
    return token_encode(bytes(raw))


def reject_line(peer_ip, status):
    """SE's refusal line, byte-for-byte in shape.

    NoPad is not a guess: our frame_line() reproduces SE's OWN trailing checksums
    (`lwVN` and `lwz@`) from both captured refusals exactly, and only with pad=b""
    -- with the pad byte both come out one character wrong.
    """
    tok = pol_reject_token(status)
    return authnode.NoPad(f"ERROR :Closing Link: [unknown@{peer_ip}] (POL {tok})".encode())


# --------------------------------------------------------------------------- #
# Login-completion GATE record (rides in the 001 line's payload)
# --------------------------------------------------------------------------- #
# The client's login message handler is FUN_037db6f0 (installed at ctx+0x344 by the
# login init FUN_03804390 -> FUN_038074e0). It parses the message payload via
# FUN_037db640: base64-decode 0x60 (96) chars -> a 72-byte struct, then XOR-unmask
# [0:8] (+[8:16]); everything at offset >=0x10 is plaintext. When the struct's fields
# are [0x10]==1, [0x18]==0xff, [0x1c]==0xff (with [0x1b]!=0, [0x3e]&0xf80==0xf80,
# [0x42]&1) it calls FUN_037d9ba0 -> sets the completion gate DAT_0386a8c8, which lets
# the auth connection COMPLETE (driver FUN_037d40e0 case 0xc) and dial the lobby.
# Our old 001 line carried a tiny ":Welcome" payload (<96 chars) so the parser bailed
# and the gate never set -> POL-0008. This builds the real 96-char record (padded to
# 192 like SE's blob). Address candidates go in [0x14]/[0x24]/[0x28] (empirical).
_B64 = "TSG8IncW3HFKokOg79qzeCmZs2yBYEQVAUxR5rbwi4P@jMDLtpvad0f_J1hlN6uX"


def _b64encode(data):
    out = []
    for i in range(0, len(data), 3):
        c = data[i:i + 3] + bytes(3 - len(data[i:i + 3]))
        v = (c[0] << 16) | (c[1] << 8) | c[2]
        out += [_B64[(v >> 18) & 63], _B64[(v >> 12) & 63],
                _B64[(v >> 6) & 63], _B64[v & 63]]
    return "".join(out)


def gate_list_stamps(db, member):
    """{0x14: mail, 0x24: friends, 0x28: handles} "last changed" times for the
    login record, or None when POL_GATE_LIST_STAMPS is off or anything fails.

    WHY (2026-09-27, player report): read messages and accepted friend
    requests came back as NEW after every relog, while the server's mailbox for
    that member was EMPTY (3:3 answered 0/0 at 16:45:50) -- so the client was
    re-flagging its OWN cached copies. We wrote `now` into 0x14/0x24 and
    `now - 200 days` into 0x28 on every login. Project Crystal Server names
    these fields LastEmailUpdate / LastFriendListUpdate / LastHandleListUpdate
    (then fills all of them with `now` too), and SE's one captured login fits
    the names (0x28 = 2019, the account's creation). If they are cache stamps,
    a fresh value every login tells the client every list changed.

    With the knob: each is the last time that list really changed for this
    member (accounts.list_stamp), so an unchanged list serves the same value
    login after login. Mail grows only when a NEWER message arrives. 0x2C stays
    `now` -- it is what the client shows as the login date. Not yet checked
    against a client: A/B = read a message, accept a friend request, relog, and see whether they
    stay read; then have someone send a message and check it still shows up.
    """
    if os.environ.get("POL_GATE_LIST_STAMPS", "0") != "1" or member is None:
        return None
    try:
        mid = int(member["id"])
        newest = max((when for when, _p, _m in lobbymail._mailbox(mid)), default=0)
        return {
            0x14: accounts.list_stamp(db, mid, "mail", newest, grow=True),
            0x24: accounts.list_stamp(db, mid, "friends",
                                      accounts.friend_list_fingerprint(db, mid)),
            0x28: accounts.list_stamp(db, mid, "handles",
                                      accounts.handle_list_fingerprint(db, mid)),
        }
    except Exception as exc:
        log("authserv", f"gate list stamps unavailable ({exc!r}); serving the "
                        "old per-login values")
        return None


def build_gate_record(lobby_ip, lobby_port, pad_to=192, unread=0, stamps=None):
    """72-byte gate/redirect record -> base64 (>=96 chars) for the 001 payload.

    `stamps` ({offset: unix time}, from gate_list_stamps) replaces the per-login
    values at 0x14/0x24/0x28 when given.

    `unread` is byte +0x11: the UNREAD-MAIL COUNT the client shows on its badge
    before the mail app exists. See the note at the write below -- this used to
    be `lobby_port & 0xff`, which is why the badge read 20 (51220 & 0xff).
    """
    r = bytearray(72)
    r[0x10] = 1                                   # gate field 1
    r[0x18] = 0xff                                # gate field 2
    r[0x1c] = 0xff                                # gate field 3
    r[0x1b] = 1                                   # enter redirect block
    struct.pack_into("<H", r, 0x3e, 0x0f80)       # flag: &0xf80 == 0xf80
    struct.pack_into("<H", r, 0x42, 0x0001)       # flag: &1 != 0
    ipn = int.from_bytes(socket.inet_aton(lobby_ip), "big")
    # POL_GATE_DATES=1 (default) writes UNIX TIMESTAMPS into 0x14/0x24/0x28 instead
    # of the lobby address/port. Justification: a licence blob captured from a REAL
    # SE session on 2026-08-11 has 0x14 = the login instant (matched to the second),
    # 0x24 = an earlier login, 0x28 = 2019 (account creation) and 0x2c = the prior
    # login. Writing an IP here is why the UI renders "2072" and epoch dates.
    # RISK, stated plainly: the original code aimed these at DAT_0386a978 and
    # DAT_03bc3040/44, which look like an address+port pair, so this MAY break the
    # lobby handoff. If the client stops reaching the lobby, set POL_GATE_DATES=0.
    now = int(time.time()) & 0xFFFFFFFF
    # DATE PROBE (2026-08-12): write four DISTINCT, memorable dates into the four
    # timestamp slots SE populates (0x14/0x24/0x28/0x2C), so one login says which
    # slot drives which on-screen date -- or, if the UI still shows 12/31/1969,
    # that the date is NOT in this gate blob at all. Required gate fields are left
    # intact, so the completion gate still sets. POL_GATE_DATEPROBE=1 to arm.
    if os.environ.get("POL_GATE_DATEPROBE") == "1":
        def _ts(y, m, d):
            return int(datetime.datetime(y, m, d, 12, 0, 0,
                       tzinfo=datetime.timezone.utc).timestamp()) & 0xFFFFFFFF
        struct.pack_into("<I", r, 0x14, _ts(2001, 1, 1))
        struct.pack_into("<I", r, 0x24, _ts(2002, 2, 2))
        struct.pack_into("<I", r, 0x28, _ts(2003, 3, 3))
        struct.pack_into("<I", r, 0x2C, _ts(2004, 4, 4))
        r[0x11] = max(0, min(int(unread), 0xFE))   # unread mail; see below
        log("authserv", "gate DATEPROBE: 0x14=2001-01-01 0x24=2002-02-02 "
                        "0x28=2003-03-03 0x2C=2004-04-04")
        enc = _b64encode(bytes(r))
        if pad_to and len(enc) < pad_to:
            enc += _B64[0] * (pad_to - len(enc))
        return enc
    if os.environ.get("POL_GATE_DATES", "1") == "1":
        struct.pack_into("<I", r, 0x14, now)              # this login
        struct.pack_into("<I", r, 0x24, now - 86400)      # an earlier login
        struct.pack_into("<I", r, 0x28, now - 200 * 86400)  # account creation
        for off, val in (stamps or {}).items():
            struct.pack_into("<I", r, off, int(val) & 0xFFFFFFFF)
    else:
        struct.pack_into("<I", r, 0x14, ipn)          # -> DAT_0386a978
        struct.pack_into("<I", r, 0x24, ipn)          # -> DAT_03bc3040
        struct.pack_into("<I", r, 0x28, lobby_port)   # -> DAT_03bc3044
    # +0x11 is the UNREAD-MAIL COUNT, not part of the address/port pair.
    #
    # MEASURED 2026-08-12, end to end. A hardware watchpoint on polcore's
    # g_newMailCount (0x0386A82C) caught the write at polcore+0x184AE; its setter
    # (RVA 0x184A0) is in no dispatch slot -- which is why a static scan once
    # concluded the global "can never be written" -- and has exactly one caller,
    # polcore+0x1B817, which does:
    #
    #     cmp byte [esi+0x18], 0xFF ; cmp byte [esi+0x1C], 0xFF   <- our gate fields
    #     cmp byte [esi+0x10], 1                                  <- our gate field
    #     mov dl, byte [esi+0x11]   ; call set(g_mailCount)
    #                                 call set(g_newMailCount)
    #
    # app.dll then seeds the mail app's total from it (FUN_049763C0) and the badge
    # renders that until the user opens Mail, at which point it recounts from the
    # local store and "corrects itself". So the client was not counting anything:
    # `lobby_port & 0xff` = 51220 & 0xff = 20 was the whole of the phantom
    # "20 unread messages".
    #
    # Serving the real mailbox depth instead makes the badge right at boot.
    r[0x11] = max(0, min(int(unread), 0xFE))   # 0xFF is rejected by the client
    # +0x2C is the PREVIOUS-LOGIN timestamp. Read off a real SE blob captured at
    # the COM boundary on 2026-08-11, where it held 2026-08-10 06:37 -- the user's
    # actual prior login -- alongside 0x14 = this login and 0x28 = account
    # creation (2019). We leave 0x2C at 0, and the UI renders that as epoch, which
    # is the "12/31/1969" clock and the blank/absurd "Last login" seen all session.
    #
    # NOTE the unresolved conflict: SE uses 0x14/0x24/0x28 as TIMESTAMPS while we
    # use them for the lobby IP and port (an earlier empirical guess -- hence
    # 0x14 rendering as "2072"). Those three are NOT changed here because the
    # lobby handoff currently works and may depend on them; 0x2C is untouched by
    # us, so it is the one field that can be corrected with no risk.
    struct.pack_into("<I", r, 0x2C, int(time.time()) & 0xFFFFFFFF)
    enc = _b64encode(bytes(r))                    # 96 chars
    if pad_to and len(enc) < pad_to:
        enc += _B64[0] * (pad_to - len(enc))      # 'T' pad (decodes to 0)
    return enc
