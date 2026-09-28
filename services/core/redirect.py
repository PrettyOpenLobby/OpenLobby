"""IRC redirect tokens and login nicks: what the directory hands a client to reach the auth node."""
import os
import socket
import struct
import time
from srvcore import log
from authtoken import _CONST_48, _CONST_END, _STAMPS, _STAMPS_LOCK, _stamps_refresh, session_token_key, token_encode
import sessioncrypt
from .deps import accounts
from . import lobbysession



# --------------------------------------------------------------------------- #
# IRC redirect token (base-32) -- shared with stub.py token_encode
# --------------------------------------------------------------------------- #


def key_candidates(peer_ip, stamp):
    """Keys to try after K=0, best first: [(why, key), ...].

    Newest first, because a client that re-dialled twice is far likelier to be
    holding the token from a moment ago than one from the start of the launch.
    """
    out, seen = [], set()

    def add(why, key):
        if key not in seen:
            seen.add(key)
            out.append((why, key))

    if stamp is not None:
        add("this connection's session token", session_token_key(stamp))
    # WARNING: THE LAST-AUTHENTICATED KEY GOES SECOND, NOT LAST, AND THAT IS THE WHOLE
    # POINT OF THIS LINE'S POSITION. It used to be appended AFTER the stamp
    # history, which put it at index ~N in the list -- and `POL_STAMP_TRY` then
    # deleted everything past 12. So on any client with more than ~11 stamps the
    # single best candidate after this connection's own token was built, ranked
    # last, and thrown away without ever being tried.
    #
    # Measured 2026-08-19: `127.0.0.1` accumulated **27 stamps** over 12 hours
    # and every login failed with "K=0 and 12 issued session key(s) all failed
    # the criteria" while the key it had actually authenticated with sat at
    # index 28. It is also self-worsening -- each failed retry issues another
    # token, pushing the good key further down -- which is why it presents as a
    # client that suddenly cannot log in at all rather than an intermittent one.
    #
    # It costs nothing: it is ONE key, it is the likeliest to work, and putting
    # it second preserves the fast-fail property the cap exists for.
    cached = lobbysession._session_get_for(peer_ip, "key")
    if cached:
        add("the key this client last authenticated with", cached)
    # Tokens may have been issued by the authserv container, not this one.
    _stamps_refresh()
    with _STAMPS_LOCK:
        history = list(_STAMPS.get(peer_ip, []))
    for old, at in reversed(history):
        add("a session token issued %ds ago" % int(time.time() - at),
            session_token_key(old))
    # A CAP, BECAUSE EACH CANDIDATE IS EXPENSIVE. bf_setkey is a pure-Python key
    # expansion; measured live 2026-08-16, 35 candidates took **2m13s** before
    # the login gave up, and the client sat on "Connecting to PlayOnline" for
    # every second of it and then retried into the same wall. The list is newest
    # first and a client re-keys from a token seconds old, so the tail is nearly
    # worthless -- and a slow failure is worse than a fast one, because the fast
    # one lets the client retry into a fresh token that WILL work.
    # 12 -> 40 on 2026-09-07. The cap is a WALL-CLOCK proxy, and the clock it was
    # proxying for changed: with `recover_iv(brute=False)` doing the sweep, a
    # candidate costs one key schedule (~0.4 s cold, free once cached) instead of
    # a ~2.0 s brute force, so the whole 33-stamp history of the capture that
    # prompted this now sweeps in 11.0 s cold / 3.7 s warm -- against 41 s for
    # twelve of them before. 40 covers every history observed (max 34) with
    # headroom; `_STAMP_KEEP` (512) is still the outer bound, and
    # POL_IV_BUDGET_S caps the wall clock directly, which is what actually
    # matters to the client.
    cap = _stamp_try()
    if cap > 0 and len(out) > cap:
        log("authserv", f"{peer_ip}: {len(out)} key candidates, trying the "
                        f"newest {cap} (POL_STAMP_TRY)")
        del out[cap:]
    return out


#: ONE definition, read by both `key_candidates` (which applies the cap) and the
#: failure log (which reports whether it truncated). These were two separate
#: `os.environ.get("POL_STAMP_TRY", ...)` calls with DIFFERENT defaults for about
#: five hours on 2026-09-07, and the live log immediately said the thing that
#: cannot be true: "34 candidate(s) tried ... TRUNCATED at POL_STAMP_TRY=12 of 33
#: known stamps". A cap that reports itself wrongly is worse than no cap: the
#: whole point of that sentence is to tell an operator whether the search was
#: exhaustive.
_STAMP_TRY_DEFAULT = "40"


def _stamp_try():
    try:
        return int(os.environ.get("POL_STAMP_TRY", _STAMP_TRY_DEFAULT))
    except ValueError:
        return int(_STAMP_TRY_DEFAULT)


def load_login_nicks():
    """Seed `sessioncrypt`'s crib list from the account DB.

    THE FIX FOR THE COST THAT MADE `POL_STAMP_TRY` NECESSARY. `recover_iv` reads
    a NICK line for free when it already knows the nick ("NICK <nick>:" is 8+
    bytes of known plaintext = block0 outright); when it does not, it brute-forces
    nick[0:3] over a 64-character alphabet -- 262,144 trial keystreams PER
    CANDIDATE KEY.

    Until 2026-09-07 the crib list was three hardcoded strings, none of which was
    a real account, so the fast path never hit once. Measured on prod that day:
    every successful login spent ~0.7-1.0 s brute-forcing a nick sitting in
    `login_alias` (all 12 of them, including the only nick in the logs), and every
    failed login spent ~2.0 s per candidate -- 41 s wall-clock before it gave up,
    with the client showing "Connecting to PlayOnline" for all of it.

    Best-effort and never fatal: no DB, no table, or no rows just leaves the
    static seed in place and the brute force still finds the nick. `remember_nick`
    on each successful recovery keeps the list in MRU order and covers
    auto-provisioned accounts, whose nick is not derivable from a POL ID.
    """
    if accounts is None:
        return 0
    try:
        db = accounts.connect()
        try:
            rows = db.execute("SELECT nick FROM login_alias").fetchall()
        finally:
            db.close()
        nicks = [str(r[0]).encode() for r in rows if r and r[0]]
    except Exception as exc:                      # missing DB/table -- keep the seed
        log("authserv", f"login nick crib: could not read login_alias ({exc}); "
                        "recover_iv will brute-force nick[0:3] as before")
        return 0
    n = sessioncrypt.set_known_nicks(nicks)
    log("authserv", f"login nick crib: {n} account nick(s) loaded -- recover_iv "
                    "takes the crib fast path instead of a 64^3 brute force")
    return n


def pol_error_token(node_ip, node_port):
    """The base-32 record for an ERROR redirect: ``ERROR :... (POL <token>)``.

    The client's ERROR handler (polcore FUN_037d5820) scans to ``(POL ``,
    base-32-decodes everything up to ``)`` (FUN_037c7210), and -- when the record's
    status byte is 0 (or >= 0xdc) -- stores the decoded 25-byte redirect via
    FUN_037d5a00 -> DAT_03868258 and reconnects to it. Same 25-byte layout as the
    300 token.
    40 base-32 symbols = 25 bytes exactly, no trailing checksum (decode stops at ')').

    CORRECTION (2026-08-12): an earlier version of this docstring put that status
    byte at offset 0x11, in the [14:20] zero run, and concluded status = 0. It is
    at offset **6**, inside what this module called the ``[4:8]`` constant, and we
    were shipping 0xFA there on every login -- see the _CONST_48 comment. The 0x11
    reading came from mis-anchoring ``mov al,[esp+0x22]`` at 0x37d5871: the decode
    buffer is ``lea eax,[esp+0x14]`` taken one push EARLIER, so the read is
    buffer+6, not buffer+0x12.
    """
    raw = bytearray(25)
    raw[0:4] = b"\x00\x00\x00\x00"
    raw[4:8] = _CONST_48
    raw[8:12] = socket.inet_aton(node_ip)
    raw[12:14] = struct.pack(">H", node_port)
    raw[22:25] = _CONST_END
    return token_encode(bytes(raw))
