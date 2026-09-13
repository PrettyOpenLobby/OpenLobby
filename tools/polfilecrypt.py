"""PlayOnline data-file cipher (env.dat family) -- decrypt and encrypt.

Reverse-engineered from the PS2 build `SLPS_202.00`; the PC build uses the same
construction and the same embedded key, verified against
`viewer/data/utf/env.dat` from the Win32 tree.

Provenance (VAs in SLPS_202.00):

    0x00184bb8  env.dat load state machine; rejects (filesize & 7) != 0
    0x00194bb0  decrypt_file(key8, dst, src, len)
    0x001948f8 -> 0x00194528   ctx_init(ctx, key8)
    0x00194ad0  driver: body blocks, then final block, then trailer check
    0x00194a08  per-block loop; advances pos by 8, accumulates the checksum
    0x00194970  byte stage
    0x00194900  64-bit stage
    0x001e1458  the 8-byte key (below)
    0x001e3a48  the "S-box" -- actually just S[i] = (i + 0x78) & 0xFF

Construction, per 8-byte block at byte offset `pos`:

    row   = (pos >> 3) & 31
    T     = ctx[row]                       # a 64-bit word, see ctx_init
    delta = (8*0x78 - sum(bytes of T)) & 0xFF

    byte stage  (CBC-style de-chain, then a single add per byte):
        prev = ((pos >> 3) ^ 0x45) & 0xFF
        for j in 0..7:  b[j] = ((prev ^ c[j]) + delta) & 0xFF ;  prev = c[j]

    64-bit stage:
        P = pos | (pos<<10) | (pos<<20) | (pos<<30)          # 64-bit
        X = u64le(b)
        Y = rot32( ((X - T) ^ (P + 0xa1652347)) ^ T )        # rot32 = swap halves

File layout:

    [ plaintext, zero-padded to a multiple of 8 ][ u32 payload_len, u32 checksum ]

    checksum = sum over every block except the trailer of (plain[0] + plain[4]),
    truncated to 32 bits. The loader returns -1 if it does not match.

The key schedule is only 64 bits wide: the whole 256-byte table is
T[0] = munge(key) and T[n] = T[n-1] * 5, so `key8` is the entire secret.
"""
import struct

M32 = 0xFFFFFFFF
M64 = (1 << 64) - 1
MAGIC = 0xA1652347

# VA 0x001e1458 in SLPS_202.00; identical key in the Win32 build.
KEY_ENV = bytes.fromhex("fd31425364758697")
# VA 0x001d1058, used by the 48-byte record decryptor at 0x0015b9b0.
KEY_REC48 = bytes.fromhex("cb8324b7ba3e9e0a")


def key_int(n):
    """The key a caller builds as `u64 key[1] = { n }` -- i.e. n little-endian."""
    return struct.pack("<Q", n)


# Every key reached by a call site of 0x00194bb0, and the files each opens.
# The small-integer keys are not guesses: they are what the callers store into
# their one-element u64 key array (see tools/keysites2.py), and each was then
# confirmed by a valid trailer checksum on real files.  Both builds -- PS2 and
# Win32 -- use the same set.
KEYS = {
    "int0":   (key_int(0),  "entrynw.dic, polerr.bin, sqpolcts.bin, option.bin"),
    "int1":   (key_int(1),  "entry.dic, entry_b.dic, entry_f.dic, vulgar2.dic"),
    "int9":   (key_int(9),  "PS2 _/_/PRB0nn.EPX"),
    "int16":  (key_int(16), "POLKEY.DAT (8-byte header + 65536 bytes of DNAS material)"),
    "env":    (KEY_ENV,     "data/utf/env.dat (both builds)"),
    "rec48":  (KEY_REC48,   "48-byte in-memory records at 0x0015b9b0"),
}


def find_key(data, extra=()):
    """Return (name, key) of the first known key whose trailer checksum holds."""
    for name, (key, _) in list(KEYS.items()) + list(extra):
        try:
            _, ok = decrypt(data, key)
        except ValueError:
            return None
        if ok:
            return name, key
    return None


def _rotl32(v, n):
    v &= M32
    return ((v << n) | (v >> (32 - n))) & M32


def key_schedule(key8):
    """0x00194528. -> (list of 32 u64 words, list of 32 delta bytes)."""
    K = int.from_bytes(key8, "little")
    hi, lo = (K >> 32) & M32, K & M32
    seed = ((_rotl32(lo, 8) << 32) | _rotl32(hi, 16)) & M64

    t = bytearray(seed.to_bytes(8, "little"))
    t[0] = (t[0] + 69) & 0xFF
    for i in range(1, 8):
        b = t[i - 1]
        x = (b + t[i] + 212) & M32
        x ^= (b << 2) & M32
        t[i] = (x ^ 0x45) & 0xFF

    words, deltas = [], []
    v = int.from_bytes(t, "little")
    for _ in range(32):
        words.append(v)
        deltas.append((8 * 0x78 - sum(v.to_bytes(8, "little"))) & 0xFF)
        v = (v * 5) & M64
    return words, deltas


def _mix64(X, T, pos):
    """0x00194900, forward (decrypt) direction."""
    p = pos & M32
    P = (p | (p << 10) | (p << 20) | (p << 30)) & M64
    Y = ((X - T) & M64) ^ ((P + MAGIC) & M64) ^ T
    return ((Y << 32) | (Y >> 32)) & M64


def _unmix64(Y, T, pos):
    p = pos & M32
    P = (p | (p << 10) | (p << 20) | (p << 30)) & M64
    R = ((Y << 32) | (Y >> 32)) & M64
    return (((R ^ T) ^ ((P + MAGIC) & M64)) + T) & M64


def decrypt(data, key8=KEY_ENV):
    """-> (payload_bytes, checksum_ok). Raises on a bad length."""
    if len(data) & 7 or len(data) < 16:
        raise ValueError(f"length {len(data)} is not a usable multiple of 8")
    words, deltas = key_schedule(key8)
    buf = bytearray(data)
    cksum = 0
    nblk = len(buf) // 8
    for n in range(nblk):
        pos, row = n * 8, n & 31
        d = deltas[row]
        prev = (n ^ 0x45) & 0xFF          # seeded from the block index, not the row
        blk = bytearray(8)
        for j in range(8):
            c = buf[pos + j]
            blk[j] = ((prev ^ c) + d) & 0xFF
            prev = c
        out = _mix64(int.from_bytes(blk, "little"), words[row], pos)
        buf[pos:pos + 8] = out.to_bytes(8, "little")
        if n < nblk - 1:
            cksum = (cksum + buf[pos] + buf[pos + 4]) & M32
    n_, want = struct.unpack_from("<II", buf, len(buf) - 8)
    return bytes(buf[:n_]), cksum == want


def encrypt(payload, key8=KEY_ENV):
    """Inverse of decrypt(): pads, appends the trailer, and enciphers."""
    words, deltas = key_schedule(key8)
    body = bytearray(payload)
    body += b"\0" * (-len(body) % 8)
    cksum = 0
    for n in range(len(body) // 8):
        cksum = (cksum + body[n * 8] + body[n * 8 + 4]) & M32
    buf = body + struct.pack("<II", len(payload), cksum)

    for n in range(len(buf) // 8):
        pos, row = n * 8, n & 31
        X = _unmix64(int.from_bytes(buf[pos:pos + 8], "little"), words[row], pos)
        blk = X.to_bytes(8, "little")
        d = deltas[row]
        prev = (n ^ 0x45) & 0xFF
        for j in range(8):
            c = (prev ^ ((blk[j] - d) & 0xFF)) & 0xFF
            buf[pos + j] = c
            prev = c
    return bytes(buf)


if __name__ == "__main__":
    import sys

    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    show = "-t" in sys.argv or "--text" in sys.argv
    for path in args:
        raw = open(path, "rb").read()
        hit = find_key(raw)
        if hit is None:
            print(f"{path}\n  {len(raw)} bytes -- no known key validates")
            continue
        name, key = hit
        pt, ok = decrypt(raw, key)
        rt = encrypt(pt, key)
        print(f"{path}\n  {len(raw)} bytes -> payload {len(pt)}  key={name} "
              f"({key.hex()})  checksum_ok={ok}  round_trip_exact={rt == raw}")
        if show:
            sys.stdout.write(pt.decode("shift_jis", "replace"))
