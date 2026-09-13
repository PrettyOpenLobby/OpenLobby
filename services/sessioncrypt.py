#!/usr/bin/env python3
"""PlayOnline PC login session cipher, reimplemented from decompiled polcore.dll.

Chain (KDF, FUN_037d5e80, numeric-300 handler):
  base = reverse(base64decode(server_token)[0:32])
  out  = base ^ e mod n         (FUN_037ebde0 -> FUN_037ebfb0; master = ctx+0x39c8)
  K    = reverse(out[0:8])       (8-byte Blowfish key, stored ctx+0x2b8)
  IV   = n_ptr[0:8]              (first 8 bytes of the modulus; ctx cipher IV)
  polcrypt_init(cipher_ctx, sbox, K, IV)

Cipher (FUN_03824300 bf_setkey / FUN_03824220 bf_core): Blowfish whose P/S are
seeded from an MD5-expansion of K (not the pi digits), then the standard
encrypt-zero finalization; OFB stream with CR/LF pass-through.
"""
import itertools
import struct
import threading

M32 = 0xFFFFFFFF
STD_IV = (0x67452301, 0xefcdab89, 0x98badcfe, 0x10325476)
ZERO_IV = (0, 0, 0, 0)


# ------------------------------------------------------------------ MD5 (settable IV)
def _lrot(x, c):
    x &= M32
    return ((x << c) | (x >> (32 - c))) & M32

_S = [7,12,17,22]*4 + [5,9,14,20]*4 + [4,11,16,23]*4 + [6,10,15,21]*4
_K = [int(abs(__import__('math').sin(i+1)) * 2**32) & M32 for i in range(64)]


def _md5_compress(state, block):
    a, b, c, d = state
    x = list(struct.unpack("<16I", block))
    A, B, C, D = a, b, c, d
    for i in range(64):
        if i < 16:
            f = (B & C) | (~B & D); g = i
        elif i < 32:
            f = (D & B) | (~D & C); g = (5*i + 1) % 16
        elif i < 48:
            f = B ^ C ^ D; g = (3*i + 5) % 16
        else:
            f = C ^ (B | (~D & M32)); g = (7*i) % 16
        f = (f + A + _K[i] + x[g]) & M32
        A, D, C = D, C, B
        B = (B + _lrot(f, _S[i])) & M32
    return ((a+A) & M32, (b+B) & M32, (c+C) & M32, (d+D) & M32)


def md5_hash(data, iv):
    """MD5 with a custom initial state (iv). Returns 16-byte digest."""
    state = iv
    ml = len(data) * 8
    msg = bytearray(data)
    msg.append(0x80)
    while len(msg) % 64 != 56:
        msg.append(0)
    msg += struct.pack("<Q", ml & 0xFFFFFFFFFFFFFFFF)
    for i in range(0, len(msg), 64):
        state = _md5_compress(state, bytes(msg[i:i+64]))
    return struct.pack("<4I", *state)


# ------------------------------------------------------------------ key schedule
def bf_expand(key):
    """Grow the 8-byte key to >=4096 bytes: buf[8:]=md5(buf[0:len]); first digest
    uses the standard MD5 IV, subsequent ones the zeroed IV (md5_final zeros ctx)."""
    buf = bytearray(key)
    n = len(key)
    first = True
    while n < 0x1000:
        digest = md5_hash(bytes(buf[:n]), STD_IV if first else ZERO_IV)
        first = False
        chunk = min(0x1000 - n, 16)
        buf += digest[:chunk]
        n += chunk
    return bytes(buf)


def _be32(b, i):
    return (b[i] << 24) | (b[i+1] << 16) | (b[i+2] << 8) | b[i+3]


def bf_core(P, S, L, R):
    for i in range(16):
        t = (L ^ P[i]) & M32
        f = (((S[0][t >> 24] + S[1][(t >> 16) & 0xff]) & M32)
             ^ S[2][(t >> 8) & 0xff]) & M32
        f = (f + S[3][t & 0xff]) & M32
        L = (f ^ R) & M32
        R = t
    return (P[16] ^ L) & M32, (P[17] ^ R) & M32   # (hi, lo)


#: (key, chain) -> (P, S). See `bf_setkey`.
_SETKEY_CACHE = {}
#: ~30 KB per schedule, so 256 is ~8 MB -- enough to hold a whole per-IP
#: stamp sweep plus the lobby's keys without evicting K=0.
_SETKEY_MAX = 256


def bf_setkey(key, chain=False):
    """Blowfish key schedule -- MEMOISED, because it is the server's hot spot.

    MEASURED in the live container 2026-08-16: **406 ms per call**. The lobby's
    `_lobby_crypt` rebuilds the schedule on every call and `_lobby_bind` calls it
    once per session candidate, so identifying a connection cost 11 x 406 ms =
    **4.5 s** -- and every one of those 11 schedules is BYTE-IDENTICAL, because
    the candidates vary the IV, not the key.

    That was the whole of Janhourou's save-data bug: the PS2 gives up on a lobby
    fetch ~4.1 s after its hello, and the bind alone was eating 4.0 s of it, so
    the reply landed at 7.5 s and the console had already stopped listening.
    It is also the "35 candidates took 2m13s" the
    auth path already had a cap for (responders.py, POL_STAMP_TRY) -- that cap
    treated the symptom.

    The cost is genuine work, not waste: 521 `bf_core` calls, each 16 Feistel
    rounds in pure Python. Caching is the fix because the INPUTS repeat, not
    because the computation is avoidable.

    WARNING: the returned P and S are SHARED. Treat them as read-only. Nothing
    outside this function mutates them today (only `_bf_setkey_uncached` writes
    P[k]/S[b][j], while `bf_core` / `ofb_keystream` / `ofb_apply` only read), and
    any future caller that wants to mutate must copy first.
    """
    ck = (bytes(key), bool(chain))
    hit = _SETKEY_CACHE.get(ck)
    if hit is not None:
        # LRU: keep the entry alive. dicts preserve insertion order, so
        # re-inserting moves it to the young end and `_evict` never picks it.
        try:
            del _SETKEY_CACHE[ck]
            _SETKEY_CACHE[ck] = hit
        except KeyError:                       # another thread evicted it; fine
            pass
        return hit
    out = _bf_setkey_uncached(key, chain)
    # WARNING: THIS USED TO BE A FULL `.clear()` AT 64 ENTRIES, with the note "keys are
    # few and long-lived". They are not: the auth failure path alone walks the
    # whole per-IP stamp history (33 keys for one address on 2026-09-07, 71 across
    # the file), and the lobby adds its own. Every overflow threw away all 64
    # schedules, so a client that failed twice paid the full 406 ms x N again --
    # the cache went cold exactly when it was needed most. An LRU keeps the hot
    # keys (K=0, this client's token) resident through a full-history sweep.
    while len(_SETKEY_CACHE) >= _SETKEY_MAX:
        try:
            del _SETKEY_CACHE[next(iter(_SETKEY_CACHE))]
        except (StopIteration, KeyError, RuntimeError):
            break
    _SETKEY_CACHE[ck] = out
    return out


def _bf_setkey_uncached(key, chain=False):
    buf = bf_expand(key)
    P = [_be32(buf, i) for i in range(18)]               # overlapping stride-1
    S = [[_be32(buf, (b*256 + j) * 4) for j in range(256)] for b in range(4)]
    for k in range(18):
        P[k] ^= _be32(buf, 4*k)
        P[k] &= M32
    # standard encrypt-zero finalization; result stored little-endian => (lo, hi)
    L = R = 0
    for i in range(9):
        hi, lo = bf_core(P, S, L, R)
        P[2*i] = lo; P[2*i+1] = hi
        if chain:
            L, R = lo, hi
    for b in range(4):
        for j in range(0, 256, 2):
            hi, lo = bf_core(P, S, L, R)
            S[b][j] = lo; S[b][j+1] = hi
            if chain:
                L, R = lo, hi
    return P, S


# ------------------------------------------------------------------ OFB keystream
def ofb_keystream(P, S, iv8, length):
    """OFB keystream. block0 = bf_core(L=IV_lo, R=IV_hi) -> (hi,lo); keystream bytes
    = LE(lo)++LE(hi); feedback L,R = lo,hi. (from FUN_03823ef0)."""
    L = int.from_bytes(iv8[0:4], "little")
    R = int.from_bytes(iv8[4:8], "little")
    out = bytearray()
    while len(out) < length:
        hi, lo = bf_core(P, S, L, R)
        out += struct.pack("<II", lo, hi)
        L, R = lo, hi
    return bytes(out[:length])


def bf_decrypt(P, S, hi, lo):
    """Inverse of bf_core: given output (hi,lo) recover input (L,R)."""
    L = hi ^ P[16]; R = lo ^ P[17]
    for i in range(15, -1, -1):
        t = R
        f = (((S[0][t >> 24] + S[1][(t >> 16) & 0xff]) & M32)
             ^ S[2][(t >> 8) & 0xff]) & M32
        f = (f + S[3][t & 0xff]) & M32
        R = (L ^ f) & M32
        L = (t ^ P[i]) & M32
    return L, R


_IVCS = (b"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghijklmnopqrstuvwxyz_@")


def _iv_from_block0(P, S, ks8):
    """Given block0's 8 keystream bytes, invert to the per-connection IV."""
    lo = int.from_bytes(ks8[0:4], "little")
    hi = int.from_bytes(ks8[4:8], "little")
    ivL, ivR = bf_decrypt(P, S, hi, lo)
    return struct.pack("<II", ivL, ivR)


def _ks_from_block0(P, S, ks8, length):
    """Extend the OFB keystream from a known block0 (ks8) to `length` bytes without
    needing the IV -- the OFB feedback is block0's own output, so the stream is
    fully determined by ks8. (Mirrors ofb_keystream's feedback: L,R = lo,hi.)"""
    out = bytearray(ks8)
    lo = int.from_bytes(ks8[0:4], "little")
    hi = int.from_bytes(ks8[4:8], "little")
    while len(out) < length:
        h2, l2 = bf_core(P, S, lo, hi)
        out += struct.pack("<II", l2, h2)
        lo, hi = l2, h2
    return bytes(out[:length])


# Nicks are constant per account (see pol-login-protocol): try known ones first so
# the common case needs NO brute force at all.
#   UBJ8OPU7G is the standalone Friend List (PolFL.exe). A client derives its own
#   nick, so the SAME account presents a different one per client -- see the
#   login_alias table in accounts.py. The brute-force fallback below finds these
#   anyway; listing them just takes the fast path.
#
# WARNING: THESE THREE WERE THE ENTIRE CRIB LIST UNTIL 2026-09-07, AND NOT ONE OF THEM
# IS A REAL ACCOUNT. Measured on prod that day: `login_alias` held all 12 live
# nicks -- including `U3KFHB3K4`, the nick on every dial in the logs -- and the
# crib list held three strings that appear in no account row at all. So the fast
# path could never hit, and EVERY login fell into the 64^3 brute force below:
# ~0.7-1.0 s on the success path (the loop exits early when it reaches the right
# nick[0:3]) and a full ~2.0 s per candidate key on the failure path. That is
# what made `POL_STAMP_TRY` necessary, and the cap is what made the search
# incomplete. The nicks were knowable the whole time -- `accounts.nick_for_polid`
# derives one from every POL ID we issue.
#
# The list is now a registry the auth path fills (`set_known_nicks` from the
# account DB at startup, `remember_nick` on every successful recovery, which also
# covers auto-provisioned accounts whose nick is not derivable). Keep the three
# static entries as a seed so a server with no account DB behaves as before.
_SEED_NICKS = (b"UBKTSUMOU", b"UH5GRSV86", b"UBJ8OPU7G")
#: Bound: the crib is tried in order before the brute force, so a runaway list
#: would just be a slower prefix. 512 covers every account we will ever have.
_NICKS_MAX = 512
_NICKS_LOCK = threading.Lock()
_KNOWN_NICKS = list(_SEED_NICKS)

#: Back-compat alias. Read `known_nicks()` instead -- this is only the seed.
KNOWN_NICKS = _SEED_NICKS


def _current_nicks():
    """The crib list, most-recently-useful first."""
    with _NICKS_LOCK:
        return tuple(_KNOWN_NICKS)


#: Public name for `_current_nicks`. `recover_iv` uses the private one because its
#: own parameter is called `known_nicks` and would shadow it.
known_nicks = _current_nicks


def set_known_nicks(nicks):
    """Replace the registry (keeping the static seed at the tail)."""
    clean = [bytes(n) for n in nicks if n]
    with _NICKS_LOCK:
        _KNOWN_NICKS[:] = list(dict.fromkeys(clean + list(_SEED_NICKS)))[:_NICKS_MAX]
    return len(clean)


def remember_nick(nick):
    """Promote `nick` to the front of the crib list. Idempotent.

    Called on every successful recovery, so the account actually playing is the
    first crib tried on its next dial -- one Blowfish block instead of up to
    262,144 of them.
    """
    if not nick:
        return
    nick = bytes(nick)
    with _NICKS_LOCK:
        if _KNOWN_NICKS[:1] == [nick]:
            return
        try:
            _KNOWN_NICKS.remove(nick)
        except ValueError:
            pass
        _KNOWN_NICKS.insert(0, nick)
        del _KNOWN_NICKS[_NICKS_MAX:]


_NICK_RE = None


def _nick_re():
    global _NICK_RE
    if _NICK_RE is None:
        import re
        # 'NICK <nick>:<32 lowercase-hex md5>:<...>' -- distinctive enough to reject
        # coincidental-printable false positives a loose check would accept.
        _NICK_RE = re.compile(rb"^NICK [A-Za-z0-9]{2,16}:[0-9a-f]{32}:")
    return _NICK_RE


def _ks_options(pt_byte, ct_byte):
    """Keystream byte(s) consistent with one known plaintext byte. See `_crib_block0`.

    Forced when the cipher actually encrypted the byte. Ambiguous when it did
    not: `ofb_apply` emits the plaintext byte unchanged whenever pt ^ ks would be
    LF or CR, so ct == pt says either "ks was zero" or "a passthrough fired".
    """
    if ct_byte != pt_byte:
        return (ct_byte ^ pt_byte,)
    # dict.fromkeys keeps order and de-duplicates (pt ^ 0x0a can itself be 0).
    return tuple(dict.fromkeys((0x00, pt_byte ^ 0x0a, pt_byte ^ 0x0d)))


def _crib_block0(crib8, ct8, cap=64):
    """Every block0 keystream consistent with a known 8-byte plaintext prefix.

    WARNING: THIS IS THE POL-0008 BUG, AND IT IS NOT A KEY PROBLEM AT ALL.
    `ofb_apply` does not encrypt every byte: when `pt ^ ks` would be 
 or 
    (or `pt` already is), the byte goes out **IN THE CLEAR**, so the wire keeps
    its 
 framing readable without the key. At such a position ct[i] == pt[i],
    and the obvious derivation

        ks8 = bytes(crib[i] ^ ct[i] for i in range(8))

    yields 0x00 instead of the true keystream byte. One wrong byte in block0
    corrupts the entire OFB stream, `_validate` sees garbage, every candidate key
    is rejected, and the client is dropped with POL-0008 / POL-2059 -- while
    holding exactly the key we thought it should.

    PROVED 2026-09-08 against a live capture (127.0.0.1:37709). The shim caught
    the client calling bf_setkey with K=0 on the same dial the server declared
    "K=0 and 40 issued session key(s) all failed". Recovering the stream from a
    second crib in the line's fixed tail gave
    `NICK UF8TOQDTX:483b19ac...:TTTTTAISTTTTTTTTTTTTTAbCdEfGhIjKwnPb` and IV
    af411a17d7bf0aa4, and re-encrypting that plaintext reproduces the captured
    ciphertext byte for byte. At offset 4 the SPACE after "NICK" hit
    0x20 ^ 0x2a == 0x0a and was sent as a bare 0x20.

    RATE: 2/256 per byte over an 8-byte crib = 1 - (254/256)**8 = **6.1%**, which
    is the ~4-6% of logins that have been failing since the beginning.

    So: where ct[i] != pt[i] the keystream byte is forced. Where ct[i] == pt[i] it
    is one of {0x00, pt[i]^0x0a, pt[i]^0x0d} -- a passthrough, or a genuine zero
    keystream byte. Enumerate; in practice that is ONE combination, occasionally
    three. `cap` bounds a pathological line rather than trusting it to be small.
    """
    opts = [_ks_options(crib8[i], ct8[i]) for i in range(8)]
    total = 1
    for o in opts:
        total *= len(o)
        if total > cap:
            return
    for combo in itertools.product(*opts):
        yield bytes(combo)


def _validate(P, S, nick_ct, ks8):
    """If ks8 is the right block0, return (iv, plaintext); else (None, None)."""
    ks = _ks_from_block0(P, S, ks8, len(nick_ct))
    pt = bytearray(len(nick_ct))
    for i, c in enumerate(nick_ct):
        x = c ^ ks[i]
        pt[i] = c if c in (0x0a, 0x0d) or x in (0x0a, 0x0d) else x
    if _nick_re().match(bytes(pt)):
        return _iv_from_block0(P, S, ks8), bytes(pt)
    return None, None


def recover_iv(P, S, nick_ct, known_nicks=None, brute=True):
    """Recover the per-connection OFB IV from a K=0 NICK line.

    Plaintext is 'NICK <nick>:...'. Fast path: a known account nick gives eight
    bytes of known plaintext, which is block0. Fallback: brute nick[0:3], with a
    printable prefilter over one extra OFB block before the full decrypt.

    Both paths go through `_crib_block0` rather than XORing the crib against the
    ciphertext, because a byte the cipher passed through IN THE CLEAR (pt ^ ks
    would have been LF or CR) breaks a plain XOR. That was POL-0008; read
    `_crib_block0` before touching either path.

    Returns (IV, plaintext) or (None, None).
    """
    if len(nick_ct) < 16:
        return None, None
    cribs = known_nicks if known_nicks is not None else _current_nicks()
    for nick in cribs or ():
        crib = b"NICK " + nick + b":"
        if len(crib) >= 8 and len(nick_ct) >= 8:
            for ks8 in _crib_block0(crib[:8], nick_ct[:8]):
                iv, pt = _validate(P, S, nick_ct, ks8)
                if iv is not None:
                    return iv, pt
    # Fallback: brute nick[0:3] with an early-exit printable prefilter.
    #
    # `brute=False` EXISTS BECAUSE THE CRIB CANNOT MAKE A *FAILURE* FAST. A wrong
    # candidate key misses every crib and falls straight through to here, so a
    # sweep of N candidate keys cost N x ~2.0 s no matter how good the crib list
    # was -- measured 2026-09-07: 33 candidates, 66.9 s before the crib fix and
    # 59.2 s after it. The brute force only earns its cost for a nick we do NOT
    # know. Callers sweeping a key history pass brute=False for the sweep and pay
    # for one brute pass on the keys that matter. Do not make False the default:
    # an account absent from `login_alias` -- auto-provisioned, or a client that
    # derives its own nick like PolFL.exe -- is found by nothing BUT this loop.
    if not brute:
        return None, None
    n = min(16, len(nick_ct))
    # Per-position keystream options, precomputed so the hot loops stay tight.
    # Every list is length 1 unless that position was passed through.
    pre = [_ks_options(b"NICK "[i], nick_ct[i]) for i in range(5)]
    pre_list = [bytes(t) for t in itertools.product(*pre)]
    o5 = {ch: _ks_options(ch, nick_ct[5]) for ch in _IVCS}
    o6 = {ch: _ks_options(ch, nick_ct[6]) for ch in _IVCS}
    o7 = {ch: _ks_options(ch, nick_ct[7]) for ch in _IVCS}
    for ks5 in pre_list:
        for a in _IVCS:
            for k5 in o5[a]:
                for b in _IVCS:
                    for k6 in o6[b]:
                        for c in _IVCS:
                            for k7 in o7[c]:
                                ks8 = ks5 + bytes((k5, k6, k7))
                                ks = _ks_from_block0(P, S, ks8, n)
                                ok = True
                                for i in range(8, n):
                                    x = nick_ct[i] ^ ks[i]
                                    if nick_ct[i] in (0x0a, 0x0d) or x in (0x0a, 0x0d):
                                        continue
                                    if not (0x20 <= x <= 0x7e):
                                        ok = False
                                        break
                                if not ok:
                                    continue
                                iv, pt = _validate(P, S, nick_ct, ks8)
                                if iv is not None and pt[5] == a:
                                    return iv, pt
    return None, None


def _cksum32(content):
    """FUN_03823df0: 32-bit little-endian word sum + trailing-byte fold."""
    s = 0
    n = len(content)
    i = 0
    while i <= n - 4:
        s = (s + int.from_bytes(content[i:i+4], "little")) & 0xFFFFFFFF
        i += 4
    u = 0
    while i < n:
        u = ((u >> 8) | (content[i] << 24)) & 0xFFFFFFFF
        i += 1
    return (s + u) & 0xFFFFFFFF


def frame_line(realtext, pad=b" "):
    """Append the client's line checksum so FUN_037d5e80 accepts an ENCRYPTED line.

    Encrypted lines carry a 6-bit-packed checksum in the last 4 bytes
    (FUN_037d3730 encode / FUN_037d5e80 verify). The checksum covers everything
    before it, and the reader strips FOUR bytes -- ours does, at
    responders.py's `pt[:-4]`.

    WARNING: THERE IS NO PAD BYTE ON THE CLIENT'S WIRE. This docstring said until
    2026-08-17 that the client checksums <realtext>+<1 pad byte> and "drops the
    pad on decode". A known-plaintext test killed that: a chat line typed live
    showed SE's client putting the checksum immediately after the last typed
    character, with nothing between. The claim outlived the test and propagated --
    an earlier vector generator inherited it and split a real captured NICK five
    bytes from the end, which left the credential blob at 31 symbols when POL
    base64 only comes in four-symbol groups.

    So `pad` is OUR generation convention, not the client's, and it is harmless
    because it simply becomes part of the checksummed text. Pass b"" to send
    exactly the shape the client sends. Do NOT let it back into any description
    of what the client does.

    Validated: reproduces the client NICK's trailing 'K|GP' exactly.
    """
    content = realtext + pad
    c = _cksum32(content)
    chk = bytes([((c >> 26) & 0x3f) + 0x3f, ((c >> 20) & 0x3f) + 0x3f,
                 ((c >> 14) & 0x3f) + 0x3f, ((c >> 8) & 0x3f) + 0x3f])
    return content + chk


def ofb_apply(P, S, iv8, data):
    """Encrypt/decrypt with CR/LF pass-through (from the OFB loop): for each byte,
    k = keystream byte; if input==\\n/\\r OR (input^k)==\\n/\\r keep input, else ^k."""
    ks = ofb_keystream(P, S, iv8, len(data))
    out = bytearray(len(data))
    for i, c in enumerate(data):
        x = c ^ ks[i]
        out[i] = c if c in (0x0a, 0x0d) or x in (0x0a, 0x0d) else x
    return bytes(out)


# ------------------------------------------------------------------ KDF (RSA)
B64 = "TSG8IncW3HFKokOg79qzeCmZs2yBYEQVAUxR5rbwi4P@jMDLtpvad0f_J1hlN6uX"
_B64INV = {c: i for i, c in enumerate(B64)}


def b64decode(token):
    out = bytearray()
    for i in range(0, len(token) - 3, 4):
        v = (_B64INV[token[i]] << 18) | (_B64INV[token[i+1]] << 12) | \
            (_B64INV[token[i+2]] << 6) | _B64INV[token[i+3]]
        out += bytes([(v >> 16) & 0xff, (v >> 8) & 0xff, v & 0xff])
    return bytes(out)


def derive_key(token, e, n, base_order="reverse"):
    """K = reverse(low8( base ^ e mod n )); base = reverse(decoded_token[0:32])."""
    dec = b64decode(token)[:32]
    base = int.from_bytes(dec[::-1] if base_order == "reverse" else dec, "big")
    out = pow(base, e, n)
    ob = out.to_bytes(128, "big")            # try both slicings when validating
    return ob
