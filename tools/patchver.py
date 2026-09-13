#!/usr/bin/env python3
"""PlayOnline `patch.ver` codec -- the 288-byte encrypted per-content patch state.

WHY THIS MATTERS: the Viewer's pre-login "Check Files" list enumerates contents whose
`patch.ver` is a valid 288-byte encrypted blob. A content installed without it (e.g. via
`msiexec /qb!`, which skips the InstallScript UI sequence) is invisible there no matter
what registry keys exist -- verified by making Fantasy Earth byte-identical to Front
Mission Online across InstallFolder/Interface/ContentsCLSID/ContentsIID and still not
appearing.

Reversed from `sqexca.dll` (Square Enix's MSI custom-action DLL, extracted from the
Fantasy Earth disc-2 MSI ISSetupFile table). Export `OnSetEncryptPatchVer` -> worker
`SetEncryptPatchVer` @ RVA 0x1fd0. Note sqexca's registry-path strings are +1 Caesar
obfuscated in .rdata (`TPGUXBSF\\QmbzPomjof` = `SOFTWARE\\PlayOnline`), so plain string
scans of that DLL find nothing.

FORMAT
    288 (0x120) bytes on disk. Only the first 280 (0x118) are enciphered; the trailing
    8 bytes are whatever the output buffer held. Plaintext is a zero-filled 0x120 buffer
    with an ASCII string at OFFSET 24:
        patch.ver          -> the version string, e.g. "20060419_2" (or "0000000000")
        registry Product\\<id> -> sprintf("%04d_%02d", content_id, sub) e.g. "0014_00"
    `Product\\<id>` in the registry is the same structure as the file -- it is NOT a
    licence blob, so never fabricate one blindly.

KEY
    The key is the REGISTRY STRING at `SOFTWARE\\PlayOnline[US]\\Interface\\%04d`
    (value name = content id, 4-digit decimal). That value is just
    sprintf("%08x", timeGetTime()) captured at install time -- an install nonce, not a
    version. Any value works as long as the blob is encrypted with the same one.
        key64 = little-endian u64 of acc[], where acc[i & 7] += keystring[i]
        seed64 = (int64)content_id + key64

CIPHER (all little-endian, 64-bit wraparound)
    state: 32 quadwords q[0..31] + a block counter.
      q[0] = ror32(seed_hi,16) | rol32(seed_lo,8)<<32   (as bytes: dword0, dword1)
      then a byte pass over q[0]'s 8 bytes, then q[i] = q[i-1] * 5.
    per 8-byte block i, with k = i & 31 and ctr = 8*i:
      1. swap the two dwords
      2. ^= q[k]
      3. ^= f(ctr),  f(x) = (((x<<10|x)<<10|x)<<10|x) + 0xa1652347
      4. += q[k]
      5. byte chain, d = (i ^ 0x45) & 0xff:
             for each byte b: for t in 0..7: b = SBOX[(q[k].byte[t] + b) & 0xff]
                              b ^= d ; d = b
    SBOX[i] = (i + 0x88) & 0xff -- a permutation, so trivially invertible.

VALIDATED against real installed files (decrypt -> 270/280 zero bytes + a sane string):
    FriendList  key Interface\\0014=070a00fd id 14 -> "20060419_2"
    FMO         key Interface\\0004=0701b6fa id  4 -> "0000000000"
    registry Product\\0014 (same key/id)           -> "0014_00"
"""
import argparse
import struct
import sys

M64 = (1 << 64) - 1
M32 = (1 << 32) - 1
BLOB_LEN = 0x120        # 288 bytes written to disk
CIPHER_LEN = 0x118      # 280 bytes actually enciphered
TEXT_OFF = 24           # plaintext lives here

SBOX = [(i + 0x88) & 0xff for i in range(256)]
INV_SBOX = [0] * 256
for _i, _v in enumerate(SBOX):
    INV_SBOX[_v] = _i


def _rol32(v, n):
    return ((v << n) | (v >> (32 - n))) & M32


def _ror32(v, n):
    return ((v >> n) | (v << (32 - n))) & M32


def key64(keystring):
    """8-byte accumulator over the Interface value string: acc[i & 7] += s[i]."""
    if isinstance(keystring, str):
        keystring = keystring.encode("ascii")
    acc = bytearray(8)
    for i, ch in enumerate(keystring):
        acc[i & 7] = (acc[i & 7] + ch) & 0xff
    return struct.unpack("<Q", bytes(acc))[0]


def seed64(content_id, keystring):
    return (content_id + key64(keystring)) & M64


def _state(seed):
    lo, hi = seed & M32, (seed >> 32) & M32
    s = bytearray(0x100)
    struct.pack_into("<I", s, 0, _ror32(hi, 16))
    struct.pack_into("<I", s, 4, _rol32(lo, 8))
    s[0] = (s[0] + 0x45) & 0xff
    for i in range(1, 8):
        prev = s[i - 1]
        b = (s[i] + ((prev - 0x2c) & 0xff)) & 0xff
        s[i] = (((prev << 2) & 0xff) ^ b ^ 0x45) & 0xff
    for i in range(1, 32):
        prev = struct.unpack_from("<Q", s, (i - 1) * 8)[0]
        struct.pack_into("<Q", s, i * 8, (prev * 5) & M64)
    return s


def _f(ctr):
    v = x = ctr & M64
    for _ in range(3):
        v = ((v << 10) & M64) | x
    return (v + 0xA1652347) & M64


def encrypt(buf, seed):
    s = _state(seed)
    out = bytearray(buf)
    for i in range(len(buf) // 8):
        ctr, k = i * 8, i & 0x1f
        qb = s[k * 8:k * 8 + 8]
        q = struct.unpack_from("<Q", s, k * 8)[0]
        blk = bytearray(out[i * 8:i * 8 + 8])
        lo, hi = struct.unpack_from("<II", blk, 0)
        struct.pack_into("<II", blk, 0, hi, lo)
        v = struct.unpack_from("<Q", blk, 0)[0]
        v = (((v ^ q) ^ _f(ctr)) + q) & M64
        struct.pack_into("<Q", blk, 0, v)
        d = (i ^ 0x45) & 0xff
        for j in range(8):
            b = blk[j]
            for t in range(8):
                b = SBOX[(qb[t] + b) & 0xff]
            b ^= d
            blk[j] = b
            d = b
        out[i * 8:i * 8 + 8] = blk
    return bytes(out)


def decrypt(buf, seed):
    s = _state(seed)
    out = bytearray(buf)
    for i in range(len(buf) // 8):
        ctr, k = i * 8, i & 0x1f
        qb = s[k * 8:k * 8 + 8]
        q = struct.unpack_from("<Q", s, k * 8)[0]
        blk = bytearray(out[i * 8:i * 8 + 8])
        d = (i ^ 0x45) & 0xff
        for j in range(8):
            c = blk[j]
            b = (c ^ d) & 0xff
            for t in range(7, -1, -1):
                b = (INV_SBOX[b] - qb[t]) & 0xff
            blk[j] = b
            d = c
        v = struct.unpack_from("<Q", blk, 0)[0]
        v = ((v - q) & M64) ^ _f(ctr) ^ q
        struct.pack_into("<Q", blk, 0, v)
        lo, hi = struct.unpack_from("<II", blk, 0)
        struct.pack_into("<II", blk, 0, hi, lo)
        out[i * 8:i * 8 + 8] = blk
    return bytes(out)


def make_blob(text, content_id, keystring):
    """Build a full 288-byte blob carrying `text` (max 0xFF chars, as sqexca enforces)."""
    if isinstance(text, str):
        text = text.encode("ascii")
    if len(text) > 0xFF:
        raise ValueError("plaintext longer than 0xFF bytes is rejected by sqexca")
    plain = bytearray(BLOB_LEN)
    plain[TEXT_OFF:TEXT_OFF + len(text)] = text
    enc = encrypt(bytes(plain[:CIPHER_LEN]), seed64(content_id, keystring))
    return enc + bytes(plain[CIPHER_LEN:])


def read_blob(blob, content_id, keystring):
    """Return the plaintext string carried by a 288-byte blob."""
    plain = decrypt(bytes(blob[:CIPHER_LEN]), seed64(content_id, keystring))
    tail = plain[TEXT_OFF:]
    end = tail.find(b"\x00")
    return tail[:end if end >= 0 else len(tail)].decode("ascii", "replace")


def _solve_sum_xor(v, k):
    """All x with (x ^ k) + x == v, 64-bit.

    Because x ^ (x ^ k) == k, the sum bit is v_i = k_i ^ c_i, so the CARRY chain
    is fixed by v and k alone: c_i = v_i ^ k_i.  Then maj(x_i, x_i^k_i, c_i) is
    x_i where k_i == 0 (which pins x_i) and c_i where k_i == 1 (which pins
    nothing but demands c_(i+1) == c_i).  So the solution set is a subcube whose
    free bits are exactly the set bits of k -- 14 of them for k = f(0).
    """
    carry = [((v >> i) & 1) ^ ((k >> i) & 1) for i in range(64)]
    if carry[0]:
        return []
    free, fixed = [], {}
    for i in range(64):
        nxt = carry[i + 1] if i + 1 < 64 else None      # bit 63 overflows out
        if (k >> i) & 1:
            if nxt is not None and nxt != carry[i]:
                return []
            free.append(i)
        elif nxt is None:
            free.append(i)
        else:
            fixed[i] = nxt
    base = 0
    for i, bit in fixed.items():
        base |= bit << i
    out = []
    for mask in range(1 << len(free)):
        x = base
        for n, i in enumerate(free):
            x |= ((mask >> n) & 1) << i
        if ((x ^ k) + x) & M64 == v:
            out.append(x)
    return out


def _unstate_head(q0):
    """q[0] -> the seed that produced it (inverse of _state's byte pass)."""
    s = bytearray(struct.pack("<Q", q0))
    orig = bytearray(8)
    for i in range(7, 0, -1):
        prev = s[i - 1]                       # already-updated value, as _state uses
        b = (s[i] ^ ((prev << 2) & 0xff) ^ 0x45) & 0xff
        orig[i] = (b - ((prev - 0x2c) & 0xff)) & 0xff
    orig[0] = (s[0] - 0x45) & 0xff
    d0, d1 = struct.unpack("<II", bytes(orig))
    return (_rol32(d0, 16) << 32) | _ror32(d1, 8)


def recover_key(blob, content_id):
    """Recover the seed and key from a blob alone -- no registry value needed.

    The registry keeps only the LAST install-time nonce written per hive per
    content id, so a machine with several regional installs of one title has one
    surviving key and the rest undecodable.  They are recoverable anyway:

      * the 8-round SBOX chain COLLAPSES.  SBOX[x] = x + 0x88 applied 8 times,
        adding q.byte[t] each round, is just b + (sum(q.bytes) + 0x40) mod 256 --
        one unknown byte C for the whole stage, so 256 guesses cover it.
      * plaintext block 0 is all zeros (the version string sits at offset 24), so
        that block is just chain((q ^ f(0)) + q), and `_solve_sum_xor` inverts it.
      * `_state`'s byte pass is invertible, giving the seed and hence
        key64 = seed - content_id.

    Returns [(seed, key64, key_bytes)], usually one entry.  Validated against
    the installed Viewer, whose registry key 02612f6a it reproduces exactly.
    """
    ct = blob[:8]
    out, seen = [], set()
    for c in range(256):
        d, vb = 0x45, bytearray(8)            # block 0: d starts at (0 ^ 0x45)
        for j in range(8):
            vb[j] = (((ct[j] ^ d) & 0xff) - c) & 0xff
            d = ct[j]
        v = struct.unpack("<Q", bytes(vb))[0]
        for q in _solve_sum_xor(v, _f(0)):
            if ((sum(struct.pack("<Q", q)) + 0x40) & 0xff) != c:
                continue
            seed = _unstate_head(q)
            if _state(seed)[:8] != struct.pack("<Q", q) or seed in seen:
                continue
            seen.add(seed)
            key64 = (seed - content_id) & M64
            out.append((seed, key64, struct.pack("<Q", key64)))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("decode", help="print the plaintext of a patch.ver / Product blob")
    p.add_argument("path")
    p.add_argument("content_id", type=int)
    p.add_argument("key", help="the Interface\\%%04d registry value string")
    p = sub.add_parser("recover", help="decode a blob whose registry key is lost")
    p.add_argument("path")
    p.add_argument("content_id", type=int)
    p = sub.add_parser("encode", help="write a 288-byte blob carrying TEXT")
    p.add_argument("path")
    p.add_argument("content_id", type=int)
    p.add_argument("key")
    p.add_argument("text")
    a = ap.parse_args(argv)
    if a.cmd == "decode":
        blob = open(a.path, "rb").read()
        if len(blob) < CIPHER_LEN:
            sys.exit("not a %d-byte blob (got %d) -- plaintext patch.ver?"
                     % (BLOB_LEN, len(blob)))
        print(read_blob(blob, a.content_id, a.key))
    elif a.cmd == "recover":
        blob = open(a.path, "rb").read()
        if len(blob) < CIPHER_LEN:
            sys.exit("not a %d-byte blob (got %d) -- plaintext patch.ver?"
                     % (BLOB_LEN, len(blob)))
        hits = 0
        for seed, key64, kb in recover_key(blob, a.content_id):
            plain = decrypt(bytes(blob[:CIPHER_LEN]), seed)
            text = plain[TEXT_OFF:].split(b"\x00")[0]
            # A wrong seed gives noise, so demand what a real blob looks like:
            # a nearly all-zero buffer with one printable run at offset 24.
            if plain.count(0) < 230 or not text or not all(0x20 <= b < 0x7f for b in text):
                continue
            hits += 1
            keystr = kb.decode("latin-1") if all(32 <= b < 127 for b in kb) else None
            print("%s  (key %s)" % (text.decode("ascii"),
                                    keystr or "seed %#018x" % seed))
        if not hits:
            sys.exit("no key recovered -- plaintext block 0 is not zero, so this "
                     "blob does not have the usual layout (a known case: the EU "
                     "Tetra Master install)")
    else:
        open(a.path, "wb").write(make_blob(a.text, a.content_id, a.key))
        print("wrote %s (%d bytes)" % (a.path, BLOB_LEN))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
