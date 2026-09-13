#!/usr/bin/env python3
"""Decode a captured lobby message into its TLV items.

The lobby's larger requests are not fixed structs -- they are a TLV stream, found
2026-08-10 from the first profile write we ever captured (opcode 05:01, 572B):

    [40B request header]
    [16B preamble]        one `e8 03 01 XX` marker (0x03E8 = 1000) plus 8 zero
                          bytes, in EITHER order: 05:01 puts the marker at 0x28,
                          05:04 puts it at 0x30. Items always start at 0x38.
                          XX is NOT an item count -- 05:01 carries 11 items with
                          XX=0x0B but 05:04 carries 1 item with XX=0x00, so it
                          reads more like a message subtype. Unconfirmed.
    [items]               u8 id, u8 JUNK, u16 len, u32 pad, value[len],
                          each item padded up to an 8-byte boundary
    [4B checksum trailer] see responders._lobby_cksum

The id is one BYTE. Reading it as a u16 makes the ids look random, because the
next byte is uninitialised client stack: the same mail-address field arrives as
0xB512 in one capture and 0x0012 in another -- constant low byte 0x12, garbage
high byte. The same junk shows up inside len=8 values, which are really a u32
followed by four uninitialised bytes.

Walking with this rule lands exactly on the trailer for BOTH captured messages
(05:01 at 572B and 05:04 at 76B), which is the check that the framing is right --
a wrong length or padding rule drifts off the end.

Usage:
    python lobby_tlv.py <capture.bin> [--iv HEX] [--key HEX]

With no --iv the file is treated as already-decrypted plaintext.
"""
import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services"))

SECTION_TAG = 0x03E8
REQ_HEADER = 0x28
TRAILER = 4


def decrypt(buf, iv_hex, key_hex):
    import sessioncrypt
    P, S = sessioncrypt.bf_setkey(bytes.fromhex(key_hex))
    return sessioncrypt.ofb_apply(P, S, bytes.fromhex(iv_hex), buf)


def items(pt, start=REQ_HEADER + 16, end=None):
    """Yield (offset, id, length, value). Stops at the checksum trailer."""
    end = len(pt) - TRAILER if end is None else end
    off = start
    while off + 8 <= end:
        if struct.unpack_from("<H", pt, off)[0] == SECTION_TAG:
            yield off, None, None, pt[off:off + 8]      # a section header
            off += 8
            continue
        ident = pt[off]                                 # u8 -- pt[off+1] is junk
        ln = struct.unpack_from("<H", pt, off + 2)[0]
        yield off, ident, ln, pt[off + 8:off + 8 + ln]
        off += 8 + ln + (-ln % 8)
    if off != end:
        print(f"WARNING: walk ended at 0x{off:03X}, trailer at 0x{end:03X} "
              f"(off by {end - off}) -- framing rule is wrong for this message",
              file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("capture")
    ap.add_argument("--iv", help="session IV hex; omit if the file is plaintext")
    ap.add_argument("--key", default="00" * 8)
    a = ap.parse_args()
    pt = open(a.capture, "rb").read()
    if a.iv:
        pt = decrypt(pt, a.iv, a.key)
    print(f"opcode {pt[1]:02x}:{pt[2]:02x}  payload_len={struct.unpack_from('<I', pt, 4)[0]}")
    print(f"section header @0x{REQ_HEADER:03X}: {pt[REQ_HEADER:REQ_HEADER + 16].hex(' ')}")
    for off, ident, ln, val in items(pt):
        if ident is None:
            print(f"  @0x{off:03X}  SECTION HDR {val.hex(' ')}  (count={val[3]})")
            continue
        head = val.split(b"\x00")[0]
        if ln == 8:
            # Four meaningful bytes then four uninitialised ones -- but the field's
            # real WIDTH varies: 0x03/0x13/0x1D read cleanly as u32, while
            # 0x04..0x10 are u8s whose upper bytes are junk too. Show both.
            shown = (f"u32={struct.unpack_from('<I', val)[0]:<10} "
                     f"u8={val[0]:<4} junk={val[4:].hex()}")
        elif head[:1].isalnum():
            shown = "ASCII=" + repr(head.decode("ascii", "ignore")[:48])
        else:
            shown = val[:16].hex()
        print(f"  @0x{off:03X}  id=0x{ident:02X} ({ident:3})  len={ln:4}  {shown}")


if __name__ == "__main__":
    main()
