"""Login token minting and auth-stamp persistence.

Split out of responders.py (2026-08-27) as pure motion -- no behaviour
change. Layers directly on srvcore; imported by responders.
"""

import json
import os
import socket
import struct
import threading
import time

from srvcore import (
    log,
)


TOKEN_ALPHABET = "N43OVHBJ1Y2C0WSXED5QFILRZMUTAPGK"

# [4:8] was recorded here as one opaque "constant" lifted from a capture. It is
# NOT one field, and byte [6] is not a constant at all -- it is the ACCOUNT
# STATUS CODE, which is why every login raised the credit-card dialog.
#
# The client's store routine (polcore sub_037d5a00, dst = DAT_03868258) copies
# the 25-byte record field by field with per-field byte order, and that split
# tells you the layout: [4:6] is a big-endian u16, [6] and [7] are separate
# single bytes. polcore never compares any of them against 0x6c37fab1 (the only
# occurrences of that dword in the image are DATA -- 0x386a8d4, the client's own
# outgoing lobby-hello magic), so [6] carries no magic weight.
#
# Downstream of the store:
#   polcore sub_03804e20  memcpy(0x38593f0, 0x3868258, 0x18)   -- login result
#   polcore sub_038060f0  mov al, [0x38593f6]                  -- struct + 6
#   polcore table slot +0xD20 -> app.dll                       -- the getter
#   app.dll  sub_049f919f  scans the message table at 0x4cf6d68 (stride 0x68)
#                          for an entry whose [0] == this byte, and renders it
#                          with "(LM-%02d)" (string 22330) where NN = code - 220.
#
# 0xFA = 250 = LM-30 = string 28754, "The credit card registered for your
# payment method will expire at the end of the month...". Codes below 0xDD raise
# no dialog at all (sub_049f919f: `cmp al,0xdd / jb`), and 0 still satisfies the
# ERROR handler's store condition (`al == 0 || al >= 0xdc`, sub_037d5820), so 0
# is both silent and accepted. Set POL_ACCT_STATUS=0xfa to get the old behaviour
# back, or to any code in 0xDD..0xFC to raise that dialog deliberately.
_ACCT_STATUS = int(os.environ.get("POL_ACCT_STATUS", "0"), 0) & 0xFF
_CONST_48 = bytes.fromhex("6c37") + bytes([_ACCT_STATUS]) + bytes.fromhex("b1")
_CONST_END = bytes.fromhex("010100")      # [22:25] constant


def token_encode(raw):
    """25-byte redirect record -> 40 base-32 symbols."""
    bits = int.from_bytes(raw, "big")
    return "".join(TOKEN_ALPHABET[(bits >> (5 * (39 - i))) & 31]
                   for i in range(40))


_nonce_counter = [0x11223344]


def _next_nonce():
    # A per-connection value in [0:4]; the real server varies it every connection.
    # We don't need randomness (the trailing checksum isn't enforced), just change.
    _nonce_counter[0] = (_nonce_counter[0] + 0x01010101) & 0xFFFFFFFF
    return struct.pack(">I", _nonce_counter[0])


def build_redirect_token(node_ip, node_port):
    """Mint a base-32 redirect record pointing the client at node_ip:node_port.

    The nonce field [0:4] is the SEED of the first-hop (state-5) session key:
    the client computes K = byteswap(base32decode(token)[0:4]) with the high 4
    bytes zero (KDF FUN_037d5e80 state-5 path). We set nonce=0 so the client
    derives K=0 -> we can read its NICK with our K=0 cipher.
    """
    raw = bytearray(25)
    raw[0:4] = b"\x00\x00\x00\x00"          # nonce=0 -> first-hop K=0
    raw[4:8] = _CONST_48
    raw[8:12] = socket.inet_aton(node_ip)
    raw[12:14] = struct.pack(">H", node_port)
    # [14:20] zero, [20:22] varies (leave zero), [22:25] constant
    raw[22:25] = _CONST_END
    return token_encode(bytes(raw)) + "NNNN"   # 4-sym trailing checksum: unenforced


def build_session_token(stamp):
    """The FINAL hop's 300 token: a SESSION token, not a redirect.

    Same 25-byte base-32 record, but the address at [8:12] is ZERO and [0:4]
    carries the server's UTC unix time. Both details are load-bearing, read off
    polcore's state-5 line handler (sub_037d5e80, the `[esi+0x208] == 5` arm):

        the token decodes (base-32, sub_037c7210) and its first 0x18 bytes are
        copied to conn+0x29c; the first dword is BYTE-SWAPPED, so [0:4] is read
        BIG-ENDIAN. Then, at 0x37d6083:

            if redirect_flag == 0 and dword[+0x08] != 0:   # an ADDRESS
                state = 3; redirect_flag = 1; return      # "go dial that node"
            else:                                          # a SESSION token
                key = {dword0, 0}; setClock(dword0); arm the 400s deadline

    So a token carrying an address is a redirect and its first dword is never
    looked at -- which is why our clock read 1970 + uptime: every hop we have
    ever sent carried an address, so the client took the redirect arm every
    time and the clock base (polcore 0x3ba4c30, added to elapsed by 0x380ccf0,
    which is what feeds PML $YEA/$MON/$HOU/...) was never set.

    The SAME dword also seeds a Blowfish key. That is harmless here because the
    NEXT thing we send is TOKEN0, whose state-8 RSA arm (0x37d61f1) overwrites
    both key dwords -- but it is why `handle_authserv` tries this key as well as
    K=0 when recovering the IV, instead of assuming which one the client kept.

    POL_AUTH_CLOCK=0 restores the plain redirect token on every hop.
    """
    raw = bytearray(25)
    struct.pack_into(">I", raw, 0, stamp & 0xFFFFFFFF)   # server clock + key seed
    raw[4:8] = _CONST_48
    # [8:12] MUST stay zero -- a non-zero address makes this a redirect.
    raw[22:25] = _CONST_END
    return token_encode(bytes(raw)) + "NNNN"


def session_token_key(stamp):
    """The 8-byte Blowfish key the client derives from a session token.

    polcore stores `bswap(LE32(token[0:4]))` at conn+0x2b8 and zero at +0x2bc,
    and hands that 8-byte window to the key schedule (0x3823eb0 -> 0x3824300),
    so the key BYTES are the little-endian image of the big-endian dword we
    wrote -- i.e. LE(stamp) followed by four zeros.
    """
    return struct.pack("<I", stamp & 0xFFFFFFFF) + b"\x00" * 4


# Every session token we have handed out, per client, so a login can be rescued
# when the client is still keyed to an EARLIER one.
#
# The failure this exists for: the client keeps the Blowfish key from a previous
# run instead of re-keying to the TOKEN0 we just sent, so neither K=0 nor the
# key from THIS connection's token decrypts its NICK, and the login is dropped
# with "could not recover IV". It was ~11% of logins, and it reads to the user
# as POL-0010, "disconnected from the server".
#
# What makes the rescue exact rather than a guess is that the key is simply
# LE32(stamp) + four zero bytes, and WE mint every stamp. A stuck client cannot
# be holding a key we never issued, so replaying the handful we did issue to
# that address covers the case exactly -- no brute-force window, no heuristic.
_STAMPS_LOCK = threading.Lock()
_STAMPS = {}                    # peer ip -> [(stamp, issued_at), ...] newest last
#: HOW LONG A RESCUE KEY IS KEPT. This was 3600 with the note "match
#: _SESSION_TTL: a launch, not forever" -- and that premise is simply wrong. A
#: Viewer stays open for an ENTIRE EVENING, not an hour, and it keeps the
#: Blowfish key it derived at launch for its whole life. So `load_stamps()` was
#: throwing away the key of every client that had been running more than an hour
#: -- which is exactly the client most likely to be up when the service is
#: restarted. Measured 2026-08-13: the file held 50 stamps spanning 4.2 hours and
#: a restart reloaded 12, then 9. The rescue whose own log line says "clients
#: running across the restart can still log in" was discarding precisely those
#: clients, and the symptom is "the session server restarted and now nothing can
#: sign in".
#:
#: 12 hours covers a play session. The cost is bounded and small: _STAMP_KEEP
#: (512) still caps the list, the file stays tiny, and the only per-login cost is
#: a few hundred Blowfish key schedules in `key_candidates()` on the failure path
#: -- microseconds, and only when the fast path has already missed.
_STAMP_TTL = 12 * 3600
#: Keep a full TTL-hour of stamps, NOT a handful. This was 8, and 8 is a trap: a
#: client that cannot recover its session key re-dials every ~45s, and every
#: greeting we answer records a NEW stamp -- so after 8 failed retries the FIFO has
#: evicted the very token the stuck client is still keyed to, and recovery becomes
#: impossible for the rest of the loop. The client then spins on POL-0008 forever.
#: Seen live 2026-08-13. A client dialing every 45s issues ~80 stamps/hour; 512
#: covers even a pathological 7s loop, the file stays ~15 KB, and _STAMP_TTL still
#: bounds it. Used at all three trim sites (load_stamps, _stamps_refresh,
#: remember_stamp).
_STAMP_KEEP = 512

#: ...and on disk, because otherwise RESTARTING THIS SERVICE STRANDS EVERY
#: CLIENT THAT IS ALREADY RUNNING.
#:
#: The failure, observed live: a Viewer keeps the Blowfish key from a session
#: token it was given earlier. If we restart, `_STAMPS` comes back empty, so
#: neither K=0 nor any remembered token decrypts that client's NICK, the login
#: is dropped, and the user sees POL-0008 ("network unreachable") on a server
#: that is up and answering. A PS2 launched after the restart logs in fine at
#: the same moment, which makes it look like a client-specific bug rather than
#: lost server state.
#:
#: The stamps are not secret in any useful sense -- they are session tokens we
#: broadcast to the client in the clear -- and they expire in an hour, so this
#: is a small file that removes a whole class of confusing breakage during
#: development, when this service is restarted constantly.
_STAMP_FILE = os.environ.get(
    "POL_STAMP_FILE", os.path.join(os.environ.get("POL_DATA_DIR", "/data"),
                                   "auth-stamps.json"))


def _stamps_save_locked():
    """Persist `_STAMPS`. Caller holds _STAMPS_LOCK."""
    try:
        os.makedirs(os.path.dirname(_STAMP_FILE), exist_ok=True)
        tmp = _STAMP_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({ip: [[int(s), float(t)] for s, t in v]
                       for ip, v in _STAMPS.items()}, f)
        os.replace(tmp, _STAMP_FILE)          # atomic: never a half-written file
    except OSError as exc:
        log("authserv", f"could not persist session stamps: {exc}")


def load_stamps():
    """Reload the stamp history at startup. Expired entries are dropped."""
    now = time.time()
    try:
        with open(_STAMP_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return 0
    kept = 0
    with _STAMPS_LOCK:
        for ip, rows in (raw or {}).items():
            fresh = [(int(s), float(t)) for s, t in rows
                     if now - float(t) <= _STAMP_TTL]
            if fresh:
                _STAMPS[ip] = fresh[-_STAMP_KEEP:]
                kept += len(_STAMPS[ip])
    if kept:
        log("authserv", f"reloaded {kept} session stamp(s) from {_STAMP_FILE} "
                        "-- clients running across the restart can still log in")
    return kept


#: Last mtime of _STAMP_FILE we folded in, so the common case (file unchanged)
#: costs one stat() and nothing else.
_STAMPS_MTIME = [0.0]


def _stamps_refresh():
    """Fold in stamps ISSUED BY ANOTHER PROCESS.

    `authserv` runs in its own container (it owns the long-lived session
    channel, so it is restarted rarely), while `lobby`/`world`/`mail` restart
    every time that code is edited. Only authserv issues session tokens, but the
    lobby needs them to recover a client's key -- and with the two split across
    processes, `_STAMPS` in the lobby's memory would never see them. The file is
    already written atomically on every issue, so re-reading it when its mtime
    moves is enough to share the history both ways.

    MERGES rather than replaces: this process's own issuances must survive, and
    the file is only ever appended to per IP, so a union by token value is
    exactly right.
    """
    try:
        mtime = os.stat(_STAMP_FILE).st_mtime
    except OSError:
        return
    if mtime <= _STAMPS_MTIME[0]:
        return
    try:
        with open(_STAMP_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return                       # a torn read is harmless; try again later
    now = time.time()
    added = 0
    with _STAMPS_LOCK:
        _STAMPS_MTIME[0] = mtime
        for ip, rows in (raw or {}).items():
            have = {s for s, _ in _STAMPS.get(ip, [])}
            fresh = [(int(s), float(t)) for s, t in rows
                     if now - float(t) <= _STAMP_TTL and int(s) not in have]
            if not fresh:
                continue
            merged = sorted(_STAMPS.get(ip, []) + fresh, key=lambda r: r[1])
            _STAMPS[ip] = merged[-_STAMP_KEEP:]
            added += len(fresh)
    if added:
        log("authserv", f"picked up {added} session stamp(s) issued by another "
                        f"process -- cross-container key recovery")


def remember_stamp(peer_ip, stamp):
    """Record a session token we issued, so key_candidates() can replay it."""
    now = time.time()
    with _STAMPS_LOCK:
        for ip in [k for k, v in _STAMPS.items() if now - v[-1][1] > _STAMP_TTL]:
            del _STAMPS[ip]
        seen = _STAMPS.setdefault(peer_ip, [])
        seen.append((stamp, now))
        del seen[:-_STAMP_KEEP]
        _stamps_save_locked()
        # Our own write is the newest state; don't re-read it back in.
        try:
            _STAMPS_MTIME[0] = os.stat(_STAMP_FILE).st_mtime
        except OSError:
            pass
