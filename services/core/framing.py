"""Lobby band framing: headers, frame boundaries, reading one frame."""
import os
import socket
import struct
from . import authcap, lobbybind



# --------------------------------------------------------------------------- #
# Lobby responder (pp000, port ~512xx) -- the hop AFTER auth
# --------------------------------------------------------------------------- #
# The lobby returns the character/world list and the raw world IP:port the client
# dials next (the POL-0008 boundary), and it also carries the free-contents list
# (games menu, command code 1 -> contentlist.py). It is a BINARY framed protocol
# (not IRC), so the auth crib trick does not apply: reading it needs the real
# session key + per-connection IV. See lobbydec.py for the key-free framing work.
#
# The lever that makes this testable NOW: our authserv mints token0 -> the client
# derives K=0. IF the lobby reuses that session key (unconfirmed but likely, since
# ctx+936 is the one per-session key the whole protocol module shares), then the
# lobby speaks K=0 too and this handler reads/writes it with no dump. If the lobby
# re-keys, drop the dumped key in via POL_LOBBY_KEY and the same code path works.
#
# FRAME SHAPE -- fully decoded 2026-08-10 from the 21 on-disk SE pairs
# (logs/captures/relay-51220-*). Two corrections to the older notes, both proven:
#
#  (1) The 4 bytes at s2c 0x14 are NOT "a u16 + the const 78 6a". They are a
#      little-endian u32 UNIX TIMESTAMP: all 23 captured s2c frames decode to the
#      capture file's own wall-clock second (0x6a78c88e = 2026-08-09T18:35:58,
#      ...). "78 6a"/"79 6a" were just the high bytes of time_t rolling over.
#      => our emit must put time.time() there. Feeding it an IP (the old
#      POL_LOBBY_ADDR14=1 default, from the misread that 0x14 was the world
#      address) hands the client a year-2072 clock.
#  (2) The s2c body is NOT an OFB stream. The "shared keystream" the earlier
#      analysis saw (c2s and s2c tails byte-identical) is simply an ECHO: the
#      server repeats the client's own 12-byte session handle. Verified 21/21.
#
# The exchange is two-phase; the relay capture files concatenate both phases:
#   C->S 40B hello   00000000 0100 <u16 session id> b1fa376c  + 28 zero
#   S->C 24B accept  81 00 + 18 zero + <u32 LE unix time @0x14>
#   C->S 104B request  [0:12 token][12:24 session handle][24:40 aux][40:104 sig]
#   S->C 32B reply     [0:12 token][12:24 ECHOED handle][24:32 server tail]
# and the s2c reply token is a pure function of the client's request token:
#   reply_tok = req_tok XOR 81 04 07 00 48 00 00 00 ?? 36 43 ca   (18/18 probes;
#   bytes 1/2/4/5 carry the length field, so the mask's 0x48 is specific to the
#   104B-request/32B-reply shape -- which is exactly what our client sends).
# So the whole accepted reply is CONSTRUCTIBLE FROM THE CLIENT'S OWN REQUEST --
# no session key, no dump, nothing of SE's. Only the 8-byte tail at body[24:32]
# is server-originated (not derivable from any client bytes -- searched); it is
# POL_LOBBY_TAIL, and dropping it (24B body = exactly one record) is also valid.
#
# Still true from before: status 0x00 = ACCEPT, 0xe8 = REJECT; SE answered every
# REAL probe with 81 00 (18 of 18) and only ever sent 81 e8 to our own zero-token
# hello -- i.e. the reject was SE refusing a session it never issued, NOT a normal
# step the client shrugs off. Emitting a reject ourselves is what produces POL-0512.
#
# (2026-09-27) "6c37fab1" is an IPv4 address (the client's own address in the
# SE capture these bytes were copied from), and the hello carries it because our auth
# record's [4:8] "client IP" field does (authtoken._CONST_48). Byte +0x09 is
# that record's [6], which we overwrite with the account-status code.
_SESSION_CONST_LE = bytes.fromhex("b1fa376c")     # 6c37fab1 little-endian
_LOBBY_HDR_LEN = 0x18                             # s2c body starts here

# How much of a DECRYPTED lobby request to hexdump.
#
# 128 was the historic value and it has now cost this project twice, both times
# on the same opcode. A 3:0 fetch request is 456 bytes: 40 of header, then a
# 416-byte payload whose window fields (offset/length) sit at payload +0x190 and
# +0x194 -- i.e. frame +0x1b8, a hundred bytes past where the dump stopped. Those
# fields stayed invisible for hours -- the standing
# warning about 0x80-byte dumps.
#
# It is now blocking a live question: the PS2 title's save fetch stalls 23/23 while
# Tetra Master's identical 3:0 fetch completes 18/18, and `sqMgReadFile` packs
# the caller's identity (Nick/Domain/Volume/HandleID -- named off SE's own debug
# strings) into that same unseen region. Dumping 3:0 in full
# lets the working game and the broken one be compared straight off the wire,
# with no emulator run at all.
#
# Only 3:0 is widened. Everything else keeps 128, because these dumps are most of
# the log volume and 3:0 is a few requests an hour.
_LOBBY_DUMP = int(os.environ.get("POL_LOBBY_DUMP", "128"), 0)
_LOBBY_DUMP_FETCH = int(os.environ.get("POL_LOBBY_DUMP_FETCH", "512"), 0)
# reply_tok = req_tok XOR mask, where the mask is 8 constant bytes followed by
# THE WORLD SERVER'S IP, little-endian (i.e. reversed dotted quad).
#
# That last field was the "unknown, no derivation found" part until the client
# resolved it for us: served the modal capture value `9c3643ca` it dialed
# **202.67.54.156:51220** -- a real SE world node (independently recorded
# as an observed world address, along with .160). Decoding
# all 21 captured pairs the same way yields SE's whole world pool,
# 202.67.54.148 ... .174. So this is the WORLD HANDOFF field, and it is ours to
# set: put our own world IP here and the client dials us instead.
#
# The world PORT does not come from here -- it is record1's port, programmed by
# the auth NOTICE gate record ([0x28] = POL_LOBBY_PORT) and left in place when the
# lobby reply overwrites record1's IP. So the world hop lands on the SAME port the
# lobby used (51220), and the dial arrives back at handle_lobby.
_LOBBY_MASK_HEAD = bytes.fromhex("8104070048000000")

# THE LOBBY CHANNEL IS THE K=0 SESSION CIPHER (proven 2026-08-10). The auth
# channel's recovered K=0 keystream, XORed against a lobby request header, yields
# clean structured plaintext -- so pp000 is Blowfish-OFB under the SAME key AND IV
# as the auth connection (our token0 => K=0; the IV is per Viewer launch and we
# already recover it from the NICK line). Three different messages decode to:
#     104B: 02 04 07 00 40 00 00 00 + 16 zero   (payload len 0x40 = 64)
#      64B: 02 04 06 00 18 00 00 00 + 16 zero   (payload len 24)
#     456B: 02 03 00 00 a0 01 00 00 + 16 zero   (payload len 416)
# and in every case 40 + payload_len == the message size, matching the client's own
# send pattern exactly (it writes send(40) then send(payload)):
#     REQUEST header = 40 bytes, [0]=type 0x02, [1][2]=opcode, [4:8]=u32 LE payload
#     REPLY   header = 24 bytes, [0]=type 0x83, [4:8]=u32 LE payload,
#                      [8:12]=the world IP little-endian
# This retires two earlier readings: the "12-byte session handle echoed at [12:24]"
# is plaintext ZEROS on both sides (that is why echoing it "worked"), and the
# per-shape XOR masks were just plaintext differences. With the IV we no longer
# derive replies by XOR -- we compose them.
#
# The header's u16 LE at [4:6] is the MESSAGE LENGTH xor a per-connection mask.
# Anchor: all 18 captured probe pairs show a [4:6] difference of 0x0048 = 104 ^ 32
# = request length ^ reply length, so the mask cancels and we can compute the
# field for any reply size without ever knowing it:
#     reply_field = req_field ^ req_len ^ reply_len
# Bytes [1] and [2] differ per MESSAGE TYPE and are not derivable from lengths, so
# they come from this table -- one row per request size SE was observed answering.
# request length -> (hdr byte1, hdr byte2, reply total length)
_LOBBY_SHAPES = {
    104: (0x04, 0x07, 32),    # the keepalive/probe exchange -- 18 SE samples
    456: (0x03, 0x03, 36),    # 1 SE sample
    132: (0x05, 0x03, 40),    # 1 SE sample
    76:  (0x05, 0x04, 628),   # 1 SE sample -- the big one (char/world list)
}
_LOBBY_SHAPE_DEFAULT = (0x04, 0x07, 32)


def _http_request_complete(buf):
    """True once `buf` holds a COMPLETE bodyless HTTP request.

    The portal tunnels HTTP over these same lobby band ports, and a GET/HEAD ends
    at the blank line -- there is nothing further to wait for. Without an early
    exit the generic frame reader below burns its whole `idle` window (1.0s) on
    EVERY asset, because the only way out of its loop is a recv timeout. The main
    menu pulls ~27 files one connection at a time, so that alone serialised into a
    >30s page load and let the client give up with POL-0008 partway through.

    Methods that can carry a body are deliberately NOT matched -- they fall
    through to the idle window rather than risk truncating a payload.
    """
    return buf.startswith((b"GET ", b"HEAD ")) and b"\r\n\r\n" in buf


def _lobby_frame_complete(buf, peer_ip=None):
    """True once `buf` holds exactly ONE whole lobby frame.

    *** THE SAME BUG `_http_request_complete` ABOVE WAS WRITTEN FOR, ON THE
    BINARY HALF OF THE SAME PORT, AND IT WAS NEVER GIVEN A PREDICATE. ***

    `_read_frame`'s only way out of its loop is a recv TIMEOUT, so a client that
    sends a request and then WAITS for the reply -- which is what a real Viewer
    does, and what a request/reply protocol means -- pays the full `idle` window
    on every single message before the server even starts composing an answer.
    Measured 2026-08-19 against the real reader:

        client CLOSES after sending (what our test clients do):   57 ms
        client WAITS for the reply (what the Viewer does):      1072 ms

    -- **~1.0 s of dead wait per lobby op**. Prod runs `POL_LOBBY_EMIT=derive`,
    a multi-turn conversation loop, so it is 1 s per MESSAGE, not per connection;
    and the lobby is one-op-per-connection for most things, with `4:5` firing
    twice per status toggle. That is the account holder's "our server runs so
    much slower than [SE] on almost everything", and it is three ORDERS OF
    MAGNITUDE more than everything else on the request path put together (the
    whole database cost of a group-list serve on prod's filesystem is ~0.34 ms).

    Every suite missed it because every test client sends and then shuts down its
    write side, which makes `recv` return EOF immediately. The one client that
    waits is the real one.

    **THE FRAME SAYS ITS OWN LENGTH, so this is a measurement and not a guess.**
    `_lobby_header_ok` is exactly the self-validating test `_lobby_bind` already
    trusts to pick a session: decrypt the first EIGHT bytes and check that
    `40 + <declared payload length> == <bytes held>`. Nothing new is trusted here
    -- it is the same check, one step earlier.

    Conservative by construction: if no candidate IV validates (an unknown
    session, a partial frame, HTTP, TLS, garbage) this answers False and the
    reader falls back to the idle window exactly as before. The predicate can
    only ever make the reader return SOONER, never differently.

    `exact=True` deliberately: a burst holding two messages does not match, and
    the caller's turn loop reads the second one on its next pass -- which is the
    path it already takes today.
    """
    if len(buf) < 40:
        return False                    # shorter than a header: nothing to test
    try:
        for _sid, iv in lobbybind._lobby_iv_candidates(peer_ip):
            if lobbybind._lobby_header_ok(lobbybind._lobby_crypt(buf[:8], iv), len(buf), exact=True):
                return True
    except Exception:
        # This runs inside the read loop of every lobby connection. A failure to
        # RECOGNISE a frame must degrade to the old behaviour (wait out the idle
        # window), never to a dropped connection.
        return False
    return False


def _lobby_until(peer_ip=None, http=False):
    """An `until=` predicate for a lobby read. `http` also accepts a bodyless
    HTTP request, for the first read of a connection where the portal tunnels
    over this same port -- see `_http_request_complete`."""
    def done(buf):
        if http and _http_request_complete(buf):
            return True
        return _lobby_frame_complete(buf, peer_ip)
    done.pending = lambda buf: _lobby_frame_pending(buf, peer_ip)
    return done


#: The largest request payload a split frame may declare and still be waited
#: for. Requests are small (the biggest measured is the 0:8 handle store at
#: 0x648); the cap keeps a random decrypt from holding a read open.
_LOBBY_PARTIAL_MAX = 0x20000


def _lobby_frame_pending(buf, peer_ip=None):
    """True when `buf` is the FRONT of one lobby request whose tail has not
    arrived yet -- a header that decrypts under a known session and declares
    more payload than we hold.

    WHY (2026-09-27, POL-5204 creating a handle): the 0:8 handle store is 1648
    bytes, and twice the Viewer's first 40 arrived alone, more than `idle` (1 s)
    before the rest. `_read_frame` returned the bare header, `_lobby_bind`'s
    exact-length check then matched NO session ("no session claims this
    frame"), the lobby answered blind, and the client showed POL-5204. The
    header had decrypted cleanly under the member's own IV: type 02, opcode
    00,08, payload 0x648. With this, the reader keeps waiting (up to its
    maxwait) for such a frame instead of giving up at the idle mark.
    POL_LOBBY_WAIT_PARTIAL=0 restores the old read.
    """
    if os.environ.get("POL_LOBBY_WAIT_PARTIAL", "1") == "0" or len(buf) < 40:
        return False
    try:
        for _sid, iv in lobbybind._lobby_iv_candidates(peer_ip):
            pt = lobbybind._lobby_crypt(buf[:8], iv)
            if len(pt) < 8 or pt[0] != 0x02:
                continue
            plen = struct.unpack_from("<I", pt, 4)[0]
            if 0 < plen <= _LOBBY_PARTIAL_MAX and 40 + plen > len(buf):
                return True
    except Exception:
        return False
    return False


def _read_frame(conn, idle=1.0, maxwait=8.0, minlen=1, until=None):
    """Read one binary frame: accumulate until an idle gap (no bytes for `idle`s)
    once we hold >=minlen bytes, or until EOF / maxwait. Suits request/reply
    binary protocols (the lobby/world) where the peer sends a frame then waits
    for our answer -- so we reply promptly instead of always burning a fixed
    capture window like _read_for (which only breaks on CRLF/EOF).

    `until(buf)` is an optional completeness predicate: when it returns True the
    frame is returned immediately instead of waiting out the idle gap. Binary
    lobby frames never satisfy the HTTP predicate, so this cannot affect them."""
    conn.settimeout(idle)
    buf = b""
    waited = 0.0
    pending = getattr(until, "pending", None)
    while waited < maxwait:
        try:
            chunk = conn.recv(4096)
        except socket.timeout:
            if buf and len(buf) >= minlen:
                # A frame that PROVES more is coming (see _lobby_frame_pending)
                # is not finished by an idle gap -- keep reading to maxwait.
                if pending is not None and pending(buf):
                    waited += idle
                    continue
                break
            waited += idle
            continue
        if not chunk:
            break
        buf += chunk
        if until is not None and until(buf):
            break
    return buf


def _lobby_stamp():
    """The dword at s2c 0x14: a little-endian u32 UNIX timestamp (proven 23/23
    against the capture wall-clock). POL_LOBBY_STAMP=<hex8> pins it for A/B tests."""
    env = os.environ.get("POL_LOBBY_STAMP")
    if env:
        return bytes.fromhex(env)[:4].ljust(4, b"\x00")
    import time
    return struct.pack("<I", int(time.time()) & 0xFFFFFFFF)


def _build_lobby_frame(status, body=b"", field14=None):
    """Assemble an s2c lobby frame:
        81 <status> | 00*18 | <u32 LE unix time @0x14> | <body @0x18>
    status 0x00 = ACCEPT, 0xe8 = REJECT. field14 overrides the timestamp dword
    (kept only for A/B experiments -- the old "world address at 0x14" reading was
    a misdiagnosis; that field is a clock)."""
    hdr = bytes(field14[:4]).ljust(4, b"\x00") if field14 is not None \
        else _lobby_stamp()
    return bytes([0x81, status & 0xFF]) + b"\x00" * 18 + hdr + body


def _lobby_world_ip():
    return os.environ.get("POL_WORLD_IP") or authcap._self_ip()
