"""Which member a lobby connection belongs to: IV recovery, binding, arbitration, checksums."""
import os
import struct
import time
import threading
from srvcore import log
import sessioncrypt
from . import lobbyreply, lobbysession



def _lobby_iv():
    env = os.environ.get("POL_LOBBY_IV")
    if env:
        return bytes.fromhex(env)[:8].ljust(8, b"\x00")
    return lobbysession._session_get("iv")


def _lobby_iv_candidates(peer_ip=None):
    """Every IV that could belong to this lobby connection, best guess first.

    THIS IS THE JOIN. A lobby connection carries no account, no nick and (under
    the bridge) no distinguishing address, but its first frame decrypts to a
    self-validating header under exactly one session's IV -- so the candidate
    that validates names the session, and `_lobby_bind` binds the thread to it.
    That is why this returns (sid, iv) pairs rather than bare IVs.

    Order is a cost optimisation only, never a tiebreak: a session already bound
    to this thread first, then sessions dialling from the same address, then the
    rest newest-first. Every candidate is tried before the connection is called
    unidentified, so ordering can only make the common case cheaper.
    """
    env = os.environ.get("POL_LOBBY_IV")
    if env:
        return [(None, bytes.fromhex(env)[:8].ljust(8, b"\x00"))]
    lobbysession._sessions_refresh()
    bound = lobbysession._session_sid()
    out, seen = [], set()

    def _add(sid, slot):
        for iv in list(slot.get("ivs") or []) + [slot.get("iv")]:
            if iv and (sid, iv) not in seen:
                seen.add((sid, iv))
                out.append((sid, iv))

    with lobbysession._SESSIONS_LOCK:
        items = sorted(lobbysession._SESSIONS.items(), key=lambda kv: -float(kv[1].get("at") or 0))
        for sid, slot in items:
            if sid == bound:
                _add(sid, slot)
        for sid, slot in items:
            if sid != bound and peer_ip and slot.get("peer_ip") == peer_ip:
                _add(sid, slot)
        for sid, slot in items:
            _add(sid, slot)
    return out


# --------------------------------------------------------------------------- #
# WHO DOES A LOBBY CONNECTION BELONG TO -- the evidence, and the warnings.
#
# The bug (lobby session binding was per-IP; found live
# 2026-08-23): a lobby frame carries no account id, so the bind is "whichever
# session's IV validates the header" -- and two accounts on one machine end up
# HOLDING THE SAME IV, because the client caches its session cipher and replays
# it across account switches, and the per-IP stamp pool replays the same tokens
# to every account on the address. A validating IV therefore names a CIPHER,
# not an account; candidate order then picked the member, and it flapped live
# (one client, minutes apart: 10x member 15 / 12x member 3 -- so char lists,
# profiles and friend lists were served from the wrong account, silently).
#
# What repairs it -- evidence, never deletion (the stamp/IV replay is
# load-bearing; see the banner in `key_candidates`):
#   * every `_session_put(iv=...)` stamps a PER-IV CLAIM TIME on its slot, so
#     "which session authenticated with this cipher most recently" is a stored
#     fact that survives the cross-container merge;
#   * `_lobby_arbitrate` (POL_LOBBY_BIND_ARBITRATE=1, default on): when the
#     validating IV is held by sessions of MORE THAN ONE member, bind by best
#     evidence -- a 4:7-proven owner, then the newest claim on this IV, then
#     "signed in right now" (viewer_open), then recency -- instead of by
#     candidate order;
#   * `_bind_corroborate_mismatch` (POL_LOBBY_BIND_CORROBORATE=1, default on):
#     4:7 is the one lobby request that names its own subject (the handle the
#     client is logged in AS), so a bound member who does not own it is a
#     PROVEN mis-bind -- warn, rebind the thread to the owner's session, and
#     teach that session this connection's cipher so later binds land right.
# And the free part, ALWAYS on: a warning whenever the bound member differs
# from the previous bind on the same address or socket. The bug was invisible
# for months precisely because a wrong bind logs exactly like a right one.
_BIND_NOTE_LOCK = threading.Lock()
_BIND_LAST = {}      # peer ip -> (member_id, sid, at) of the last member-bind
#: A flip between the SAME two members on one address is one fact, not a new
#: one every time it recurs. Prod logged 104 MEMBER FLIP warnings on 2026-09-04
#: alone, nearly all one box running a PC Viewer and PCSX2 side by
#: side (members 3 and 15, 0-1 s apart) -- which made the warning unreadable
#: exactly where it was meant to be read. The first flip per (address, pair)
#: keeps the full warning; repeats are counted and re-surfaced every
#: POL_LOBBY_FLIP_REPEAT-th time. Addresses listed in
#: POL_LOBBY_MULTI_CLIENT_HOSTS (comma-separated) are KNOWN to host more than
#: one client and log a plain line instead of the warning.
_BIND_FLIPS = {}     # (peer ip, lo member, hi member) -> count
_MULTI_CLIENT_HOSTS = {h.strip() for h in
                       os.environ.get("POL_LOBBY_MULTI_CLIENT_HOSTS", "").split(",")
                       if h.strip()}
_FLIP_REPEAT_EVERY = max(1, int(os.environ.get("POL_LOBBY_FLIP_REPEAT", "25") or 25))
_BIND_PROVEN = {}    # (peer ip, iv hex) -> (member_id, at), proven by a 4:7
_BIND_PROVEN_KEEP = 512


def _note_bind(peer_ip, peer, sid, member_id, prev_sid, prev_member):
    """The free half of the session-binding fix: make a member flip VISIBLE.

    Warns when this bind's member differs from (a) the session this same socket
    was bound to a moment ago, or (b) the last member any socket from this
    address bound to. On a one-account machine neither fires (same member every
    time, whatever the sid), so a warning here is always worth reading: either
    two accounts really are sharing the address, or a bind just went to the
    wrong one.
    """
    now = time.time()
    with _BIND_NOTE_LOCK:
        last = _BIND_LAST.get(peer_ip)
        if member_id:
            _BIND_LAST[peer_ip] = (member_id, sid, now)
    if prev_member and member_id and prev_member != member_id:
        log("lobby", f"WARNING: {peer} SAME-SOCKET MEMBER FLIP: this connection was "
                     f"bound to member {prev_member} ({prev_sid}) and is now "
                     f"member {member_id} ({sid}). One socket is one client, "
                     f"so one of these binds is WRONG "
                     f"(lobby-session-binding-is-per-IP)")
    elif member_id and last and last[0] != member_id:
        pair = (peer_ip, min(member_id, last[0]), max(member_id, last[0]))
        with _BIND_NOTE_LOCK:
            n = _BIND_FLIPS[pair] = _BIND_FLIPS.get(pair, 0) + 1
        if peer_ip in _MULTI_CLIENT_HOSTS:
            if n == 1 or n % _FLIP_REPEAT_EVERY == 0:
                log("lobby", f"{peer} member flip on {peer_ip} (member {member_id} "
                             f"after member {last[0]}, {int(now - last[2])}s "
                             f"apart; #{n} for this pair) -- a listed "
                             f"multi-client host (POL_LOBBY_MULTI_CLIENT_HOSTS), "
                             f"so expected")
            return
        if n > 1 and n % _FLIP_REPEAT_EVERY != 0:
            return
        log("lobby", f"WARNING: {peer} MEMBER FLIP on {peer_ip}: bound to member "
                     f"{member_id} ({sid}); the previous bind from this "
                     f"address {int(now - last[2])}s ago was member {last[0]} "
                     f"({last[1]})."
                     + (f" Flip #{n} for this pair." if n > 1 else "")
                     + " If this machine hosts ONE account, a bind was wrong "
                     f"(lobby-session-binding-is-per-IP); if it deliberately "
                     f"runs two clients, list it in POL_LOBBY_MULTI_CLIENT_HOSTS")


def _lobby_arbitrate(sid, iv, peer_ip, peer):
    """When the validating IV is held by more than one member's session, choose
    by EVIDENCE instead of candidate order. Returns the sid to bind (`sid`
    unchanged whenever there is no ambiguity to arbitrate).

    Zero extra crypto: if IV X validated this frame, every other session
    holding X "validates" identically (same lobby key, same IV), so the
    contenders are found by a dict scan, never another bf_setkey.
    """
    if os.environ.get("POL_LOBBY_BIND_ARBITRATE", "1") == "0":
        return sid
    ivhex = iv.hex()
    with lobbysession._SESSIONS_LOCK:
        holders = []
        for s, slot in lobbysession._SESSIONS.items():
            if slot.get("iv") == iv or iv in (slot.get("ivs") or []):
                holders.append((s, slot.get("member_id"),
                                float((slot.get("iv_claims") or {})
                                      .get(ivhex) or 0),
                                bool(slot.get("viewer_open")),
                                float(slot.get("at") or 0),
                                bool(slot.get("auth_refused"))))
    eligible = [h for h in holders if h[1] and not h[5]]
    members = {h[1] for h in eligible}
    if len(members) <= 1:
        return sid
    with _BIND_NOTE_LOCK:
        rec = _BIND_PROVEN.get((peer_ip, ivhex))
    if rec and rec[0] in members:
        eligible = [h for h in eligible if h[1] == rec[0]]
        why = f"a 4:7 from this address proved member {rec[0]} owns this cipher"
    else:
        why = ("it authenticated with this cipher most recently "
               "(newest per-IV claim)")
    pick = max(eligible, key=lambda h: (h[2], h[3], h[4]))
    if pick[0] != sid:
        log("lobby", f"WARNING: {peer} AMBIGUOUS BIND: IV {ivhex} is held by "
                     f"{len(holders)} session(s) spanning members "
                     f"{sorted(members)}; candidate order said {sid}, binding "
                     f"{pick[0]} (member={pick[1]}) because {why}. "
                     f"POL_LOBBY_BIND_ARBITRATE=0 reverts to candidate order")
    return pick[0]


def _lobby_bind(frame, peer_ip, peer, exact=True):
    """Identify which session a lobby frame belongs to and bind this thread to it.

    Returns (iv, sid) -- (None, None) when nothing validates, in which case the
    caller keeps whatever it had. Binding is sticky: once a connection's first
    frame has named a session, later frames on it start from that session's IVs.

    A validating IV names a CIPHER, not an account (see the banner above), so
    when sessions of more than one member hold it the choice goes through
    `_lobby_arbitrate`, and every member change is reported by `_note_bind`.
    """
    cands = _lobby_iv_candidates(peer_ip)
    for sid, iv in cands:
        if _lobby_validates(frame[:8], iv, len(frame), exact):
            # The cipher this connection speaks -- `_bind_corroborate_mismatch`
            # needs it to teach the proven owner's session this IV.
            lobbysession._session_current.iv = iv
            if sid and sid != lobbysession._session_sid():
                prev_sid = lobbysession._session_sid()
                with lobbysession._SESSIONS_LOCK:
                    prev_member = (lobbysession._SESSIONS.get(prev_sid) or {}).get("member_id") \
                        if prev_sid else None
                sid = _lobby_arbitrate(sid, iv, peer_ip, peer)
                lobbysession.session_bind(sid)
                with lobbysession._SESSIONS_LOCK:
                    slot = lobbysession._SESSIONS.get(sid) or {}
                    who = slot.get("member_id")
                log("lobby", f"{peer} bound to session {sid} (member={who}) -- "
                             f"its IV {iv.hex()} validates this frame's header; "
                             f"{len(cands)} candidate(s) considered")
                _note_bind(peer_ip, peer, sid, who, prev_sid, prev_member)
            return iv, sid
    return None, None


def _lobby_header_ok(pt, framelen, exact=True):
    """The request header validates itself: [4:8] is a LE u32 payload length and
    40 + it is the frame size. That makes IV selection a MEASUREMENT, not a
    guess -- and `type` is 0x02 on every real request.

    `exact=False` for a read that may hold several messages, where the first
    header must fit *within* the buffer rather than equal it.
    """
    if len(pt) < 8 or pt[0] != 0x02:
        return False
    plen = struct.unpack_from("<I", pt, 4)[0]
    if plen < 0 or plen > 0x100000:                 # a sane payload, not garbage
        return False
    return 40 + plen == framelen if exact else 40 + plen <= framelen


def _lobby_pick_iv(frame, candidates, exact=True):
    """The IV under which this frame's header validates, or (None, None).

    Tries best-guess first, so the ordinary single-session case costs exactly one
    decrypt and behaves as before. `candidates` may be bare IVs or the
    (sid, iv) pairs `_lobby_iv_candidates` returns -- this only picks the IV;
    binding the session to the thread is `_lobby_bind`'s job.
    """
    for cand in candidates:
        iv = cand[1] if isinstance(cand, tuple) else cand
        if not iv:
            continue
        if _lobby_validates(frame, iv, len(frame), exact):
            return iv, _lobby_crypt(frame, iv)
    return None, None


#: iv hex -> the key that validated this connection's frame under it. Per
#: THREAD (one lobby connection per thread), so every later `_lobby_crypt` on
#: the connection uses the key its first frame proved.
_PIN = threading.local()


def _lobby_validates(frame, iv, framelen, exact):
    """True when the frame's header validates under `iv` with ANY key this IV
    has carried; the winning key is pinned for this connection.

    2026-10-03: the Viewer and Tetra Master share one launch IV but each hop
    negotiates its own RSA key. TM's own hop recorded its key over the
    Viewer's, while TM's lobby frames are encrypted with the VIEWER's key, so
    the lobby tried one wrong key, answered garbage, and TM sat on "Updating
    online status..." until it timed out."""
    h = bytes(iv).hex()
    for key in _lobby_keys_for_iv(iv):
        P, S = sessioncrypt.bf_setkey(key)
        if _lobby_header_ok(sessioncrypt.ofb_apply(P, S, iv, frame), framelen, exact):
            pins = getattr(_PIN, "keys", None)
            if pins is None:
                pins = _PIN.keys = {}
            pins[h] = key
            return True
    return False


def _lobby_crypt(data, iv):
    """OFB is symmetric; the keystream restarts from the IV for each message."""
    P, S = sessioncrypt.bf_setkey(_lobby_key_for_iv(iv))
    return sessioncrypt.ofb_apply(P, S, iv, data)


def _remember_iv_key(sid, iv, key):
    """Record that `iv` belongs to a session keyed with `key` (POL_AUTH_RSA).

    The lobby identifies a connection by trying every session's IV against
    its first frame BEFORE it knows the session -- so before it can ask the
    session for its key. Under K=0 that never mattered: every session had the
    same key. An RSA-negotiated key is per connection, so each IV has to carry
    its own. Hex strings, so the map survives the JSON hop to the lobby's
    container unchanged.
    """
    if not iv or not key:
        return
    with lobbysession._SESSIONS_LOCK:
        slot = lobbysession._SESSIONS.get(sid) or {}
        ivk = dict(slot.get("iv_keys") or {})
    h, k = bytes(iv).hex(), bytes(key).hex()
    # A LIST per IV, newest first, not one key: hops of one launch share the
    # IV with different keys, and the lobby cannot tell beforehand which hop a
    # frame comes from (see _lobby_validates).
    keys = [k] + [x for x in str(ivk.pop(h, "")).split(",") if x and x != k]
    ivk[h] = ",".join(keys[:4])
    while len(ivk) > 8:                      # the slot keeps 8 IVs; match it
        del ivk[next(iter(ivk))]
    lobbysession._session_put(sid, iv_keys=ivk)


def _lobby_keys_for_iv(iv):
    """Every key a lobby frame under `iv` may be encrypted with, best first:
    the one pinned for this connection, the bound session's (newest first),
    other sessions', then `_lobby_key()` -- so with POL_AUTH_RSA off nothing
    here changes. POL_LOBBY_KEY alone when set."""
    if os.environ.get("POL_LOBBY_KEY"):
        return [lobbyreply._lobby_key()]
    out = []
    h = bytes(iv).hex() if isinstance(iv, (bytes, bytearray)) else None
    if h:
        pin = (getattr(_PIN, "keys", None) or {}).get(h)
        if pin:
            out.append(pin)
        with lobbysession._SESSIONS_LOCK:
            bound = lobbysession._SESSIONS.get(lobbysession._session_sid()) or {}
            slots = [bound] + [s for s in lobbysession._SESSIONS.values() if s is not bound]
            for slot in slots:
                for x in str((slot.get("iv_keys") or {}).get(h) or "").split(","):
                    try:
                        kb = bytes.fromhex(x) if x else None
                    except ValueError:
                        kb = None
                    if kb and kb not in out:
                        out.append(kb)
    dflt = lobbyreply._lobby_key()
    if dflt not in out:
        out.append(dflt)
    return out


def _lobby_key_for_iv(iv):
    """The key a lobby frame under `iv` is encrypted with: the first of
    `_lobby_keys_for_iv` (the pinned one once a frame has validated)."""
    return _lobby_keys_for_iv(iv)[0]


def _lobby_cksum(buf, seed=0):
    """polcore sub_03823df0 -- the payload checksum, and the reason POL-5135 fires.

    A plain 32-bit sum of the buffer read as little-endian dwords. Any trailing
    1..3 bytes are packed into the HIGH end of one more dword (the loop at
    0x3823e66 shifts the accumulator right a byte per tail byte and ORs each one
    in at bit 24), so a 6-byte buffer contributes `LE32(b0..b3) + b4<<16 +
    b5<<24`. Both the aligned (dword) and unaligned (byte-assembling) arms of the
    original compute the same value; only the dword arm is worth reproducing.

    A zero-length buffer returns the seed unchanged -- which is what makes the
    mail list's lone 4-byte trailer block work.
    """
    total = seed & 0xFFFFFFFF
    full = (len(buf) // 4) * 4
    for i in range(0, full, 4):
        total = (total + int.from_bytes(buf[i:i + 4], "little")) & 0xFFFFFFFF
    rem = len(buf) - full
    if rem:
        tail = int.from_bytes(buf[full:], "little") << (8 * (4 - rem))
        total = (total + tail) & 0xFFFFFFFF
    return total
