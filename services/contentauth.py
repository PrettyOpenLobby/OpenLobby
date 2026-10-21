"""The per-login CONTENT AUTH value: which POL member launched this title.

A game service that cannot tell who a connection belongs to falls back to the
peer ADDRESS, i.e. the freshest POL sign-in from that IP. Two devices behind
one home router share that address, so one player's device can be served the
other player's character, and a result earned on one can be paid onto the
other's record.

The design this follows was read from Project Crystal Server
(github.com/Project-Crystal-Server/Project-Crystal-Server, AGPL-3.0,
POLProfile/RequestHandler.cs UpdateStatus + FMOServer/Database.cs) and
reimplemented here:

  * When the client enters a title it sends the lobby status update 4:5
    (Crystal's request 0x405). The server answers with a fresh 16-byte RANDOM
    VALUE in the first 16 bytes of the reply and records it against that POL
    login.
  * polcore keeps it for the rest of the POL session and hands it to the title.
    Front Mission Online's TCP cipher key is exactly those 16 bytes + the
    4-byte tail of our own 0x0322 reply (Crystal: `polRandomValueBinary +
    "IJKL"`). With a 4:5 reply that echoes the request's zero bytes, the key
    begins with 16 zero bytes.

So: mint a real value per login (responders.py, lobby side), publish it here,
and let the title recover the member from the KEY THE CLIENT ACTUALLY USES by
trial-decrypting its first encrypted packet under each candidate. A wrong
candidate fails the client's own checksum, so the match does not depend on the
address. Relaunching a title inside one POL session sends no new 4:5, which is
why a value lives for the whole POL session.

SCOPE. Only the zones in POL_CONTENT_AUTH_ZONES (default "4" = FMO) get a
random value. Any other title keeps the exact bytes it has always had, because
it is not known whether the other titles derive a key from these 16 bytes.

The values cross containers (the lobby writes, the title's container reads),
through the live-state store (polcore.kv): one JSON list under
`contentauth:rows`, newest first, expiring TTL after the last value minted.
One writer, the lobby.
"""

import json
import os
import threading
import time

from polcore import kv

#: The live-state key the minted values are kept under.
KEY = "contentauth:rows"

#: Content ids (the 4:5 zone) that receive a minted value. Empty = feature off.
#: Left at "4" on purpose, and the mint can be gated to a few members: the blob
#: hash below needs no minted value, it works with the echoed zeros as well.
ZONES = frozenset(int(z) for z in
                  os.environ.get("POL_CONTENT_AUTH_ZONES", "4").split(",")
                  if z.strip())

#: How long a value stays a candidate. A POL session can run all evening and
#: polcore reuses the value on every relaunch inside it.
TTL = int(os.environ.get("POL_CONTENT_AUTH_TTL", str(24 * 3600)))

#: Hard cap on rows kept, newest first, so a busy night cannot grow the list
#: (and the trial list) without bound.
MAX_ROWS = int(os.environ.get("POL_CONTENT_AUTH_MAX", "256"))

LEN = 16

#: WHO GETS A MINTED VALUE -- the rollout gate, read on every 4:5 so it changes
#: with NO restart. One line: `*` = every member, or member ids `3,11`. Missing
#: or empty = nobody, i.e. the 4:5 reply is byte-for-byte what it always was.
#: Why a gate at all: only the PC client has been measured. A client whose
#: polcore builds its key from this value some other way (the PS2 is unmeasured)
#: would match no candidate and lose the title, so it is switched on per member.
#: POL_CONTENT_AUTH_MEMBERS, when set, overrides the file.
ENABLE_FILE = os.environ.get(
    "POL_CONTENT_AUTH_ENABLE_FILE",
    os.path.join(os.environ.get("POL_DATA_DIR", "/data"),
                 "content-auth-members.txt"))

_LOCK = threading.Lock()
_ENABLE_CACHE = {"mtime": None, "spec": ""}


def _parse_members(spec):
    """'*' -> '*'; '3, 11' -> {3, 11}; '' -> empty set. Junk ids are ignored."""
    spec = (spec or "").strip()
    if spec == "*":
        return "*"
    return {int(x) for x in spec.replace(" ", "").split(",") if x.isdigit()}


def enabled_for(member_id):
    """Is minting switched on for this member right now?"""
    if member_id is None:
        return False
    spec = os.environ.get("POL_CONTENT_AUTH_MEMBERS")
    if spec is None:
        try:
            mtime = os.stat(ENABLE_FILE).st_mtime
        except OSError:
            return False
        if mtime != _ENABLE_CACHE["mtime"]:
            try:
                with open(ENABLE_FILE, "r", encoding="utf-8") as f:
                    _ENABLE_CACHE["spec"] = f.read()
                _ENABLE_CACHE["mtime"] = mtime
            except OSError:
                return False
        spec = _ENABLE_CACHE["spec"]
    who = _parse_members(spec)
    return who == "*" or int(member_id) in who


def _load():
    try:
        rows = json.loads(kv.get(KEY) or "[]")
    except (ValueError, TypeError):
        return []
    return rows if isinstance(rows, list) else []


def mint():
    """A fresh value. Never all-zero: zero is the legacy key and must stay
    distinguishable from a minted one."""
    while True:
        v = os.urandom(LEN)
        if any(v):
            return v


def publish(member_id, peer_ip, zone, value, now=None):
    """Record that `value` was handed to `member_id` for title `zone`.
    Returns True when written. Lobby side only."""
    if member_id is None or len(value) != LEN:
        return False
    now = time.time() if now is None else now
    row = {"v": value.hex(), "member_id": int(member_id),
           "peer_ip": peer_ip or "", "zone": int(zone), "at": now}
    with _LOCK:
        rows = [r for r in _load()
                if now - float(r.get("at") or 0) < TTL and r.get("v") != row["v"]]
        rows.insert(0, row)
        del rows[MAX_ROWS:]
        kv.set(KEY, json.dumps(rows), ttl=TTL)
    return True


def candidates(peer_ip=None, zone=None, now=None):
    """[(value bytes, member_id, row)] to trial, best first: rows from this
    address newest first, then every other live row newest first. The address
    only ORDERS the trial -- it never decides; the key check does."""
    now = time.time() if now is None else now
    live = [r for r in _load()
            if now - float(r.get("at") or 0) < TTL
            and (zone is None or int(r.get("zone", -1)) == zone)]
    live.sort(key=lambda r: float(r.get("at") or 0), reverse=True)
    same = [r for r in live if peer_ip and r.get("peer_ip") == peer_ip]
    rest = [r for r in live if r not in same]
    out = []
    for r in same + rest:
        try:
            v = bytes.fromhex(r["v"])
        except (KeyError, ValueError):
            continue
        if len(v) == LEN:
            out.append((v, int(r["member_id"]), r))
    return out


# ---------------------------------------------------------------------------
# THE CREDENTIAL BLOB: what the client sends BACK.
#
# Every title's first packet carries a 52-byte "contents authentication
# password" built by polcore at launch: FFXI puts it in the 0x26 lobby packet
# at payload offset 24, Front Mission Online in 0x0321, Fantasy Earth in
# 0xC007 and 0x400F, Dirge of Cerberus in its world-door handshake. The layout
# is the one Project Crystal Server reads in SqCrypto.DecryptAuthPassword:
#
#   [0] = enc[14] ^ volume      [1] = [6]^[12]   [2] = [8]^[10]   [3] = [9]^[13]
#   [4..51] = 48 bytes of ciphertext, XOR-chained (b[i] ^= b[i-1] undoes it)
#
# and the 48 bytes are SE's data-file cipher (tools/polfilecrypt.py, the env.dat
# construction) under the key polfilecrypt names KEY_REC48 - the key of the
# 48-byte record decryptor in the PS2 Viewer. Crystal left the seed at 0 with a
# "find this in polcore.dll" note, which is why its port never opened a real
# blob; ours does, trailer checksum and all, on every real blob we have tried.
#
# The 40-byte plaintext is a check byte, 23 bytes that are the same for every
# launch and every client, and at +24 a 16-byte hash:
#
#   md5( the 16 bytes we answered in 4:5 || login time u32 LE || session key 8 )
#
# (Crystal: PolGenerateContentsAuthHashCode). The login time is the clock we put
# in the session token's first dword (authtoken.build_session_token), the key is
# the Blowfish key the client ended the auth handshake with (all zero under
# TOKEN0, the RSA-wrapped random otherwise). Every input is ours, so the lobby
# can compute the hash the moment it answers 4:5 and file it under the session;
# a title then decodes its blob, looks the hash up, and knows the member without
# ever looking at an address. That is how SE told two consoles behind one
# router apart, and it is what `member_for_blob` does.
#
# The MD5 order has not been seen live yet (the first PS2 launch with this code
# proves or disproves it), so `hash_candidates` also files the two byte orders
# of the key. A miss is logged by the title with the blob's hash, so a wrong
# formula shows up as "no session filed this hash", never as a wrong member.
# ---------------------------------------------------------------------------
import hashlib
import struct

BLOB_LEN = 52
HASH_OFF = 24
#: polfilecrypt.KEY_REC48, embedded so the service image does not need tools/.
_REC48_KEY = bytes.fromhex("cb8324b7ba3e9e0a")
_M32 = 0xFFFFFFFF
_M64 = (1 << 64) - 1
_MAGIC = 0xA1652347

#: Where a filed hash lives: `contentauth:h:<hex>` = JSON {sid, member_id, zone,
#: peer_ip, at}, expiring with the session.
HASH_KEY = "contentauth:h:"


def _rotl32(v, n):
    v &= _M32
    return ((v << n) | (v >> (32 - n))) & _M32


def _sq_schedule(key8):
    k = int.from_bytes(key8, "little")
    hi, lo = (k >> 32) & _M32, k & _M32
    seed = ((_rotl32(lo, 8) << 32) | _rotl32(hi, 16)) & _M64
    t = bytearray(seed.to_bytes(8, "little"))
    t[0] = (t[0] + 69) & 0xFF
    for i in range(1, 8):
        b = t[i - 1]
        x = (b + t[i] + 212) & _M32
        x ^= (b << 2) & _M32
        t[i] = (x ^ 0x45) & 0xFF
    words, deltas = [], []
    v = int.from_bytes(t, "little")
    for _ in range(32):
        words.append(v)
        deltas.append((8 * 0x78 - sum(v.to_bytes(8, "little"))) & 0xFF)
        v = (v * 5) & _M64
    return words, deltas


def _sq_decrypt(data, key8):
    """polfilecrypt.decrypt: (payload, checksum_ok)."""
    words, deltas = _sq_schedule(key8)
    buf = bytearray(data)
    cksum = 0
    nblk = len(buf) // 8
    for n in range(nblk):
        pos, row = n * 8, n & 31
        d = deltas[row]
        prev = (n ^ 0x45) & 0xFF
        blk = bytearray(8)
        for j in range(8):
            c = buf[pos + j]
            blk[j] = ((prev ^ c) + d) & 0xFF
            prev = c
        x = int.from_bytes(blk, "little")
        p = pos & _M32
        pp = (p | (p << 10) | (p << 20) | (p << 30)) & _M64
        y = ((x - words[row]) & _M64) ^ ((pp + _MAGIC) & _M64) ^ words[row]
        y = ((y << 32) | (y >> 32)) & _M64
        buf[pos:pos + 8] = y.to_bytes(8, "little")
        if n < nblk - 1:
            cksum = (cksum + buf[pos] + buf[pos + 4]) & _M32
    n_, want = struct.unpack_from("<II", buf, len(buf) - 8)
    return bytes(buf[:n_]), cksum == want


def decode_blob(blob):
    """The 16-byte hash inside a 52-byte credential blob, or None when the
    bytes are not one (the outer check bytes or the trailer checksum fail).
    Accepts anything 52 bytes or longer and reads the first 52."""
    if blob is None or len(blob) < BLOB_LEN:
        return None
    a = bytearray(blob[:BLOB_LEN])
    if not (a[1] == (a[6] ^ a[12]) and a[2] == (a[8] ^ a[10])
            and a[3] == (a[9] ^ a[13])):
        return None
    for i in range(BLOB_LEN - 1, 4, -1):
        a[i] ^= a[i - 1]
    try:
        plain, ok = _sq_decrypt(bytes(a[4:4 + 48]), _REC48_KEY)
    except (ValueError, struct.error):
        return None
    if not ok or len(plain) < HASH_OFF + 16:
        return None
    return bytes(plain[HASH_OFF:HASH_OFF + 16])


def hash_for(reply16, stamp, key8):
    """What polcore hashes from our 4:5 reply, the session clock and the key."""
    return hashlib.md5(bytes(reply16[:16]).ljust(16, b"\0")
                       + struct.pack("<I", int(stamp) & _M32)
                       + bytes(key8[:8]).ljust(8, b"\0")).digest()


def hash_candidates(reply16, stamp, key8):
    """The hashes to file for one 4:5: both byte orders of the key (Crystal
    swaps it before hashing; which order polcore uses is settled by the first
    live match). Under TOKEN0 the key is zero and the two coincide."""
    k = bytes(key8[:8]).ljust(8, b"\0")
    out = [hash_for(reply16, stamp, k)]
    if k[::-1] != k:
        out.append(hash_for(reply16, stamp, k[::-1]))
    return out


def file_hashes(hashes, member_id, sid, zone, peer_ip="", now=None):
    """Lobby side: record which session (and member) a hash belongs to.
    Returns how many were written."""
    if member_id is None or not sid:
        return 0
    now = time.time() if now is None else now
    row = json.dumps({"sid": sid, "member_id": int(member_id),
                      "zone": int(zone or 0), "peer_ip": peer_ip or "",
                      "at": now})
    n = 0
    for h in hashes:
        if h and len(h) == 16:
            kv.set(HASH_KEY + bytes(h).hex(), row, ttl=TTL)
            n += 1
    return n


def member_for_hash(h):
    """Title side: the row filed for this hash, or None."""
    if not h or len(h) != 16:
        return None
    try:
        row = json.loads(kv.get(HASH_KEY + bytes(h).hex()) or "null")
    except (ValueError, TypeError):
        return None
    return row if isinstance(row, dict) and row.get("member_id") else None


def member_for_blob(blob, log=None, tag="content auth"):
    """(member_id, row, how) for the member whose POL login produced this
    credential blob, or (None, None, why). This is the identity SE's titles
    used; nothing here reads an address. `log` is called with one line that
    says what happened, so a miss is diagnosable from the title's log."""
    h = decode_blob(blob)
    if h is None:
        why = ("no credential blob (bytes fail the check or the trailer checksum)"
               if blob is not None and len(blob) >= BLOB_LEN
               else f"no credential blob ({0 if blob is None else len(blob)}B)")
        if log:
            log(f"{tag}: {why}")
        return None, None, why
    row = member_for_hash(h)
    if row is None:
        why = (f"blob decodes (hash {h.hex()[:12]}..) but no session filed that "
               f"hash - the lobby has not seen this login's 4:5, or the hash "
               f"formula differs from this client's polcore")
        if log:
            log(f"{tag}: {why}")
        return None, None, why
    how = (f"credential blob hash {h.hex()[:12]}.. filed by session "
           f"{row.get('sid')} (member {row['member_id']}, zone {row.get('zone')})")
    if log:
        log(f"{tag}: {how}")
    return int(row["member_id"]), row, how


def blob_for_selftest(hash16):
    """A 52-byte credential blob carrying `hash16`, built the way polcore builds
    one (tools/polfilecrypt encrypts the 40-byte record; the outer XOR chain
    and check bytes follow Crystal's generator). For the offline suites of the
    titles, which need a blob that decode_blob accepts; the encryptor lives in
    tools/, so no service calls this at run time."""
    import sys as _sys
    tools = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "tools")
    if tools not in _sys.path:
        _sys.path.insert(0, tools)
    import polfilecrypt
    plain = bytearray(40)
    plain[4:8] = b"\x01\0\0\0"
    plain[HASH_OFF:HASH_OFF + 16] = bytes(hash16[:16]).ljust(16, b"\0")
    plain[0] = plain[27] ^ plain[37]
    enc = bytearray(polfilecrypt.encrypt(bytes(plain), polfilecrypt.KEY_REC48))
    assert len(enc) == 48
    blob = bytearray(52)
    blob[4:52] = enc
    for i in range(5, 52):
        blob[i] ^= blob[i - 1]
    blob[0] = blob[14] ^ 0x5A
    blob[1] = blob[6] ^ blob[12]
    blob[2] = blob[8] ^ blob[10]
    blob[3] = blob[9] ^ blob[13]
    return bytes(blob)


def selftest():
    """Round-trip through an in-memory store, with twins that must FAIL."""
    import tempfile
    ok = True
    kv.reset(kv.MemoryKV(prefix="selftest:"))
    with tempfile.TemporaryDirectory() as d:
        t0 = 1_000_000.0
        a, b, old = mint(), mint(), mint()
        publish(3, "1.2.3.4", 4, a, now=t0)
        publish(11, "1.2.3.4", 4, b, now=t0 + 5)
        publish(7, "9.9.9.9", 4, old, now=t0 - TTL - 1)
        c = candidates("1.2.3.4", zone=4, now=t0 + 10)
        got = [(m, v) for v, m, _ in c]
        want = [(11, b), (3, a)]
        print(f"  newest-first, same address, expired dropped: "
              f"{'OK' if got == want else 'FAIL ' + repr(got)}")
        ok &= got == want
        # Twin: the address orders but must not exclude.
        c2 = [m for _, m, _ in candidates("5.5.5.5", zone=4, now=t0 + 10)]
        print(f"  other address still sees every live row: "
              f"{'OK' if sorted(c2) == [3, 11] else 'FAIL ' + repr(c2)}")
        ok &= sorted(c2) == [3, 11]
        # Twin: wrong zone must find nothing.
        c3 = candidates("1.2.3.4", zone=2, now=t0 + 10)
        print(f"  another title's zone gets no candidates: "
              f"{'OK' if not c3 else 'FAIL'}")
        ok &= not c3
        # Twin: no member -> nothing written.
        wrote = publish(None, "1.2.3.4", 4, mint(), now=t0)
        print(f"  unknown member is not published: {'OK' if not wrote else 'FAIL'}")
        ok &= not wrote
        print(f"  mint is never zero: {'OK' if all(any(mint()) for _ in range(64)) else 'FAIL'}")
        # The credential blob, built the way polcore builds it (tools/polfilecrypt
        # encrypts; the outer XOR chain and check bytes follow Crystal's
        # generator) and read back by decode_blob. Twins: a flipped byte, a
        # short buffer, and a hash nobody filed.
        want = hash_for(b"\0" * 16, 0x68E6A5B8, b"\0" * 8)
        blob = bytearray(blob_for_selftest(want))
        got = decode_blob(bytes(blob) + b"\xAA" * 28)      # FMO's PC 0x0321 is 80B
        print(f"  blob decodes to the filed hash: {'OK' if got == want else 'FAIL'}")
        ok &= got == want
        bad = bytearray(blob)
        bad[30] ^= 0x01
        twins = (decode_blob(bytes(bad)) is None and decode_blob(bytes(blob[:51])) is None
                 and decode_blob(None) is None)
        print(f"  flipped byte / short / missing blob are refused: "
              f"{'OK' if twins else 'FAIL'}")
        ok &= twins
        m0 = member_for_blob(bytes(blob))[0]
        n = file_hashes(hash_candidates(b"\0" * 16, 0x68E6A5B8, b"\0" * 8), 7, "uABC", 1,
                        "192.0.2.9", now=t0)
        m1, row, how = member_for_blob(bytes(blob))
        print(f"  unfiled hash names nobody, filed hash names member 7 "
              f"({n} row written): {'OK' if m0 is None and m1 == 7 and row['sid'] == 'uABC' else 'FAIL'}")
        ok &= m0 is None and m1 == 7 and n == 1
        n2 = len(hash_candidates(b"\0" * 16, 1, bytes(range(8))))
        print(f"  a non-palindromic key files both byte orders: {'OK' if n2 == 2 else 'FAIL'}")
        ok &= n2 == 2
        # The rollout gate, through the FILE (env unset), and its twins.
        global ENABLE_FILE
        saved_ef, saved_env = ENABLE_FILE, os.environ.pop("POL_CONTENT_AUTH_MEMBERS", None)
        ENABLE_FILE = os.path.join(d, "members.txt")
        _ENABLE_CACHE.update(mtime=None, spec="")
        g0 = enabled_for(3)
        with open(ENABLE_FILE, "w") as f:
            f.write("3, 11\n")
        g1 = enabled_for(3) and enabled_for(11) and not enabled_for(12)
        os.utime(ENABLE_FILE, (t0, t0 + 1))     # force a new mtime
        with open(ENABLE_FILE, "w") as f:
            f.write("*")
        os.utime(ENABLE_FILE, (t0, t0 + 2))
        g2 = enabled_for(12)
        print(f"  gate: no file = nobody: {'OK' if not g0 else 'FAIL'}; "
              f"'3, 11' = exactly those: {'OK' if g1 else 'FAIL'}; "
              f"'*' = everyone, re-read without restart: {'OK' if g2 else 'FAIL'}")
        ok &= (not g0) and g1 and g2
        ENABLE_FILE = saved_ef
        if saved_env is not None:
            os.environ["POL_CONTENT_AUTH_MEMBERS"] = saved_env
        _ENABLE_CACHE.update(mtime=None, spec="")
    kv.reset()
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if selftest() else 1)
