"""PlayOnline .slc / patch-blob decompressor.

The POLP patch server (tcp/54000) returns files and the patch list as ".slc"
containers. A container is [u8 method][method-specific]. Two methods exist,
both reverse-engineered 2026-08-09 and verified byte-exact against a live
capture of a full Windows (W2U/1000) update:

  method 0x03 = [0x03] + a standard zlib stream        (used for every file)
  method 0x02 = [0x02][u32 bit_count] + custom bit-LZSS (used for the patch LIST)

The bit-LZSS (method 2) reads the stream LSB-first:
    flag bit 0 -> literal: next 8 bits are a byte
    flag bit 1 -> match  : next 16 bits = offset, next 8 bits = length;
                           copy `length` bytes from `distance = offset` back.
The u32 after the method byte is the exact number of stream bits to consume.

Verified: method-3 patch.txt.slc -> patch.txt (83942 B, exact); a 15 MB/199-chunk
BGM (exact); method-2 patch list -> patch.cfg (149338 B, exact).
"""
import struct
import zlib


def _lzss_bits(stream, bit_count):
    out = bytearray()
    pos = 0
    end = bit_count
    d = stream

    def getbits(n):
        nonlocal pos
        v = 0
        for i in range(n):
            byte = pos >> 3
            v |= ((d[byte] >> (pos & 7)) & 1) << i
            pos += 1
        return v

    while pos + 9 <= end + 0 and pos < end:
        # literal or match; need at least 1 flag bit
        if pos >= end:
            break
        flag = getbits(1)
        if flag == 0:
            out.append(getbits(8))
        else:
            off = getbits(16)
            ln = getbits(8)
            if off < 1 or off > len(out):
                break
            for _ in range(ln):
                out.append(out[len(out) - off])
    return bytes(out)


def slc_decompress(blob):
    """Decompress a .slc container (bytes) -> raw file bytes."""
    method = blob[0]
    if method == 0x03:
        return zlib.decompress(blob[1:])
    if method == 0x02:
        bit_count = struct.unpack_from("<I", blob, 1)[0]
        return _lzss_bits(blob[5:], bit_count)
    if method == 0x01:
        # STORED -- the file verbatim, no compression.  Used where compressing
        # would not pay (PNG/BGW and other already-packed assets); such rows have
        # slc_size == size + 1, the +1 being this method byte.
        return blob[1:]
    raise ValueError(f"unknown SLC method 0x{method:02x}")


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        raw = slc_decompress(open(p, "rb").read())
        sys.stdout.write(f"{p}: {len(raw)} bytes\n")


def slc_compress_literal(payload):
    """`payload` -> an .slc method-2 container, all literals.

    The mirror image of slc_decompress for the one case we need to WRITE: the
    cmd-1 patch list. The decoder reads LSB-first -- flag bit 0, then 8 data
    bits per literal, for exactly `bit_count` bits. A matcher would shrink it
    ~12%, but a patch list is 200 KB at most and being byte-correct matters more.

    Ported from work/friendlist/publish.py, which proved it against SE's own
    served list.
    """
    bits = bytearray()
    acc = [0, 0]

    def put(v, n):
        for i in range(n):
            if acc[1] == 8:
                bits.append(acc[0])
                acc[0] = 0
                acc[1] = 0
            acc[0] |= ((v >> i) & 1) << acc[1]
            acc[1] += 1

    for b in payload:
        put(0, 1)
        put(b, 8)
    if acc[1]:
        bits.append(acc[0])
    return b"\x02" + struct.pack("<I", 9 * len(payload)) + bytes(bits)
