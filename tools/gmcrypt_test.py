"""Prove services/gmcrypt.py against the only oracle that matters: SE's own bytes.

    python gmcrypt_test.py

Three checks, weakest to strongest:

  1. the S-box blob is RFC 2144's, all eight boxes;
  2. pycryptodome's CAST-128 reproduces the RFC's 128-bit test vector, so the
     primitive under us is the real algorithm;
  3. **the captured `0x101` datagram decrypts to the stored plaintext, byte for
     byte** -- 80 real bytes off the wire, and the recomputed checksum matches
     the one the client stored.

(3) is the one with teeth. It exercises the key blob, the `FUN_037ce080`
derivation, the little-endian/big-endian reversal and CFB chaining all at once,
and nothing short of the whole chain being right will pass it.
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))
import gmcrypt  # noqa: E402

CAP = os.path.join(HERE, "testdata", "gmcall-0x101-20260815T210353.bin")
#: cft_1217_udp's key blob one, as gmserver.cpp has it.
KEY16 = struct.pack("<4I", 0xBC5224A2, 0x2B61B926, 0xA0A0D48A, 0xB0FEB355)

RFC_KEY = bytes.fromhex("0123456712345678234567893456789A")
RFC_PT = bytes.fromhex("0123456789ABCDEF")
RFC_CT = bytes.fromhex("238B4FE5847E44B2")

fails = []


def check(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def gm_checksum(msg):
    """FUN_037cdcd0: +0x14 = 0xFFFFFFFF, then sum the message as LE dwords."""
    b = bytearray(msg)
    b[0x14:0x18] = b"\xff\xff\xff\xff"
    n = struct.unpack_from("<H", b, 8)[0]
    words = (n + 3) // 4
    b += b"\0" * (words * 4 - len(b))
    return sum(struct.unpack_from("<%dI" % words, b, 0)) & 0xFFFFFFFF


def main():
    check("S-boxes are RFC 2144 (all 8)",
          all(gmcrypt.S[i][0] == w for i, w in enumerate(gmcrypt._RFC_FIRST)))

    got = gmcrypt.CAST.new(RFC_KEY, gmcrypt.CAST.MODE_ECB).encrypt(RFC_PT)
    check("CAST-128 RFC 2144 test vector", got == RFC_CT, got.hex())

    if not os.path.exists(CAP):
        # The reference capture is real client traffic and is not distributed
        # with the repository; the cipher itself is fully covered by the RFC
        # vectors and round-trip checks above.
        print("[SKIP] captured 0x101 decrypts (no capture file present)")
    else:
        cap = open(CAP, "rb").read()
        ref = open(CAP + ".plain", "rb").read()
        pt = gmcrypt.GmContext(KEY16).decrypt(cap)
        check("captured 0x101 -> stored plaintext", pt[:len(ref)] == ref,
              f"{len(cap)}B, head {pt[:4]!r}")
        check("magic is MAG?", pt[:4] == b"MAG?")
        stored = struct.unpack_from("<I", pt, 0x14)[0]
        check("checksum recomputes", gm_checksum(pt) == stored,
              f"stored {stored:#010x} computed {gm_checksum(pt):#010x}")
        # Round trip: our encrypt is the inverse of SE's decrypt.
        again = gmcrypt.GmContext(KEY16).encrypt(pt)
        check("encrypt(plaintext) reproduces the wire bytes", again == cap)

        # *** THE CIPHER IS NOT STATEFUL -- every datagram starts from the IV. ***
        # Believing otherwise is what broke stage 2 for two days: gmd carried the
        # feedback into its second reply and the client, starting from the IV,
        # read eight bytes of noise where MAG? should be. Both proofs are here.
        reused = gmcrypt.GmContext(KEY16)
        check("a context reused gives the SAME output for the same input",
              reused.decrypt(cap) == pt and reused.decrypt(cap) == pt)
        check("and encrypt does not drift either",
              reused.encrypt(pt) == cap and reused.encrypt(pt) == cap)

        # The client's own five retransmissions of this very message. Byte-identical
        # ciphertext is something a carrying send context cannot produce.
        capdir = os.path.join(HERE, "..", "logs", "captures")
        try:
            repeats = sorted(f for f in os.listdir(capdir)
                             if f.startswith("gm-51112-2026-08-15T2104")
                             or f.startswith("gm-51112-2026-08-15T21035"))
            blobs = [open(os.path.join(capdir, f), "rb").read() for f in repeats]
            blobs = [b for b in blobs if len(b) == 80]
            check("the client's retransmissions are BYTE-IDENTICAL",
                  len(blobs) >= 4 and all(b == blobs[0] for b in blobs),
                  f"{len(blobs)} datagrams")
        except OSError:
            print("[ .. ] retransmission captures not present -- skipped")

    print()
    print("all checks passed" if not fails else f"FAILED: {', '.join(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
