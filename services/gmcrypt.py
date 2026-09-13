"""The GM Call cipher, in Python -- so the GM band no longer needs a Windows host.

*** THE ALGORITHM IS CAST-128 (RFC 2144). *** For a long time the working notes
said "the algorithm is NOT identified, and does not need to be", and the honest
route was to call SE's own code out of polcore because the block functions are
raw `LAB_` targets Ghidra never lifted. Both were true and both are now
superseded: the cipher identifies itself the moment you look at the right things.

    * `LAB_037cfaa0` is a 16-entry jump table selecting among exactly THREE round
      functions, cycling F1, F2, F3 -- CAST's three round types.
    * The round count is 12 or 16, chosen by `cmp 0x50, keybits` -- CAST's own
      "16 rounds only for keys longer than 80 bits".
    * The rotate is `[ctx+0x40] & 0x1f` -- CAST's 5-bit rotation key Kr.
    * All EIGHT S-boxes sit contiguously at polcore `0x0383194c`, 0x400 apart, and
      every one matches RFC 2144 byte for byte. S1-S4 feed the round function,
      S5-S8 the key schedule, exactly as the RFC has it.

`cast_sboxes.bin` next to this file is those 8 KB, lifted from
`work/pc/polcore.dll.unpacked` and asserted against the RFC's own first words on
load. They are published standard constants, not SE material.

WHAT IS SE-SPECIFIC is only the wrapper, and it is small:

  * **Key derivation** (`FUN_037ce080`, read from asm at `0x037ce080`). The 16-byte
    key blob is folded into the 128-bit CAST key by a shift chain -- `0x3813940`
    is `__aullshr`, a 64-bit unsigned right shift, which is why Ghidra printed a
    bare `__aullshr()` with no arguments:

        w0 = (k3:k2) >> 14                       (64-bit)
        out0 = lo32(w0)
        out1 = (k0 << 18) | hi32(w0)
        w1 = (out1:out0) >> 14                   (64-bit)
        out2 = lo32(w1) ^ out0
        out3 = ((k2 << 18) | hi32(w1)) ^ out1

  * **CFB-64 with a fixed IV** `bbbe274a 457a8927` (`FUN_037ce160` / `0x037ce210`).
    Encrypt feeds back the OUTPUT, decrypt feeds back the INPUT -- i.e. both feed
    back the ciphertext, which is ordinary CFB. Both run over the PADDED wire
    length, not the logical one.

*** THE CIPHER IS NOT STATEFUL. EVERY DATAGRAM STARTS FROM THE IV. *** This file,
`gmd.py` and `gmserver.cpp` all used to say the opposite -- that the
feedback block carried from one datagram to the next, so a trial decrypt had to
run on a clone and commit only on `MAG?`. That was never measured, only assumed,
and on 2026-08-17 it was measured and is FALSE. Two independent proofs:

  * The client's five retransmissions of one `0x101` (logs/captures/, 21:03:53
    through 21:04:24 on 2026-08-15) are BYTE-IDENTICAL. A carrying send context
    could not produce the same ciphertext twice.
  * `gmkeys.py` read `msgbuf` (`0x038639bc`) out of a live Viewer while it was
    refusing our stage-2 reply, and only the FIRST 8 BYTES were wrong -- the
    length, the checksum and the session dwords at body +0x10/+0x14 were all
    perfect. That is the exact signature of a right key with a wrong initial
    feedback block: CFB resynchronises after one block. `gmd` had carried the
    feedback from the stage-1 reply; the client had not. Reproduced numerically,
    to the byte, by `tools/gmcrypt_test.py`.

So the feedback is re-seeded at the start of every `encrypt`/`decrypt`, which is
also why a retransmitted datagram simply decrypts again with no bookkeeping.

Verified three ways by `tools/gmcrypt_test.py`: the RFC 2144 128-bit test vector,
the S-box constants, and -- the one that actually matters -- decrypting the real
captured `0x101` datagram to a `MAG?` message whose stored checksum recomputes.
"""
import os
import struct

_HERE = os.path.dirname(os.path.abspath(__file__))
_SBOX_FILE = os.path.join(_HERE, "cast_sboxes.bin")

#: RFC 2144's own first word per box -- the load-time assertion that we lifted the
#: right 8 KB out of polcore and did not land one table off.
_RFC_FIRST = (0x30FB40D4, 0x1F201094, 0x8DEFC240, 0x9DB30420,
              0x7EC90C04, 0xF6FA8F9D, 0x85E04019, 0xE216300D)

M32 = 0xFFFFFFFF


def _load_sboxes(path=_SBOX_FILE):
    with open(path, "rb") as f:
        blob = f.read()
    if len(blob) != 8 * 256 * 4:
        raise ValueError(f"{path}: expected 8192 bytes, got {len(blob)}")
    boxes = [list(struct.unpack_from("<256I", blob, i * 1024)) for i in range(8)]
    for i, want in enumerate(_RFC_FIRST):
        if boxes[i][0] != want:
            raise ValueError(f"{path}: S{i + 1}[0] is {boxes[i][0]:#010x}, "
                             f"RFC 2144 says {want:#010x}")
    return boxes


S = _load_sboxes()
S1, S2, S3, S4, S5, S6, S7, S8 = S


try:
    from Crypto.Cipher import CAST            # pycryptodome
except ImportError as _e:                     # pragma: no cover
    raise ImportError(
        "gmcrypt needs pycryptodome for CAST-128 (pip install pycryptodome). "
        "A hand-written RFC 2144 key schedule was tried first and was WRONG on "
        "the first attempt -- see the module docstring. Shipping a cipher whose "
        "schedule is transcribed from memory is not worth the 8 KB it saves."
    ) from _e


#: *** THE BYTE-ORDER CONVENTION. *** polcore works in little-endian words and
#: RFC 2144 is defined big-endian, so every 8-byte block and the derived key are
#: REVERSED across the boundary. This is not cosmetic: without it the capture
#: decrypts to noise, which is exactly what the first attempt produced.
def _block(cipher, blk):
    return cipher.encrypt(blk[::-1])[::-1]


def derive_key(blob):
    """`FUN_037ce080`: fold a 16-byte key blob into the 128-bit CAST key."""
    b = (blob[:16] + b"\0" * 16)[:16]
    k = struct.unpack("<4I", b)

    def sh64(lo, hi, n):
        v = ((hi << 32) | lo) >> n
        return v & M32, (v >> 32) & M32

    o0, hi = sh64(k[2], k[3], 14)
    o1 = ((k[0] << 18) & M32) | hi
    lo1, hi1 = sh64(o0, o1, 14)
    o2 = lo1 ^ o0
    o3 = (((k[2] << 18) & M32) | hi1) ^ o1
    return struct.pack("<4I", o0, o1, o2, o3)


#: `FUN_037ce160` / `0x037ce210` locals -- the CFB feedback seed.
IV = struct.pack("<2I", 0xBBBE274A, 0x457A8927)


class GmContext:
    """One GM cipher context: a CAST key, and a CFB block re-seeded per datagram.

    A context is therefore just a key. Decrypting a datagram that belongs to a
    different context is free of consequence -- nothing is disturbed, so trials
    need neither a clone nor a commit -- and a retransmission decrypts to the
    same plaintext every time. See the module docstring for the measurement that
    replaced the carrying model.
    """

    def __init__(self, key_blob):
        self.key = derive_key(key_blob)[::-1]      # see _block: LE <-> BE
        self.cast = CAST.new(self.key, CAST.MODE_ECB)
        self.fb = bytearray(IV)

    def clone(self):
        c = GmContext.__new__(GmContext)
        c.key = self.key
        c.cast = CAST.new(self.key, CAST.MODE_ECB)
        c.fb = bytearray(self.fb)
        return c

    def _step(self, data, feedback_is_input):
        self.fb = bytearray(IV)        # every datagram starts here -- see above
        out = bytearray(data)
        for off in range(0, len(out), 8):
            ks = _block(self.cast, bytes(self.fb))
            chunk = bytes(out[off:off + 8])
            for i in range(min(8, len(out) - off)):
                out[off + i] ^= ks[i]
            self.fb = bytearray(chunk if feedback_is_input
                                else bytes(out[off:off + 8]))
        return bytes(out)

    def decrypt(self, data):
        """Receive side (`0x037ce210`): feedback is the CIPHERTEXT that came in."""
        return self._step(data, True)

    def encrypt(self, data):
        """Send side (`FUN_037ce160`): feedback is the CIPHERTEXT we produced."""
        return self._step(data, False)
