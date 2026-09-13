#!/usr/bin/env python3
"""polkey -- the client's guid-scrambling key K, offline.

Every handle/friend guid the client holds in memory is stored as `served ^ K`,
where K is a 64-bit per-session value the client derives ONCE from an 8-byte
seed. That is why a profile view's `z_hid` does not resolve server-side: it is
the scrambled value, not the guid we minted.

Everything here is transcribed from US polcore.dll (memimg base 0x037C0000);
addresses are polcore VAs. Nothing is guessed -- the three consumers are all
plain XOR, which is what makes K sufficient to undo the whole thing:

    0x37D9D40  store(lo,hi)        -> guid ^ K            (the 2:3 record copy)
    0x37DA010  getK()              -> K
    0x37DA020  ownGuid(seed)       -> xform_0x37D9C70(seed) ^ K
    0x37DA080  presenceDecrypt()   -> a ^ b ^ c, K cancels (see the memory note)

K is WRITTEN at exactly one place, 0x37D9FE9/0x37D9FEF, at the end of
`Kinit(seed*)` = 0x37D9F20, which is reproduced below as `kinit()`.

WHERE THE SEED COMES FROM (RE'd 2026-08-13, and why this tool takes it as an
argument rather than computing it): Kinit has two callers.

    0x37DEA6F  via 0x37DEA60 <- 0x37DEB6A <- 0x37DEB00 <- 0x38043DD, which
               passes a NULL seed -- 0x37DEA6C's `test esi,esi; je` skips
               Kinit outright. This path never keys anything.
    0x37DEABF  via 0x37DEAB0 <- 0x3804BBF, inside the SESSION state machine.
               The seed is a stack local built there from the 8-byte session
               global at 0x3859280 (copied out by 0x3806E90, itself a memcpy of
               8). It is client-local state, NOT a field the server sends, so
               the server cannot compute K from what it serves.

So: capture the seed once per session (see `--how`), or recover K empirically
from one known-friend profile view (`--solve`), which needs no debugger at all.
"""

import argparse
import sys

M32 = 0xFFFFFFFF
M64 = 0xFFFFFFFFFFFFFFFF


def kinit(seed):
    """polcore 0x37D9F20 -- the seed -> K hash, instruction for instruction.

    `seed` is the 8 bytes at the pointer Kinit is handed. Returns K as a 64-bit
    int (low dword = [0x386A848], high dword = [0x386A84C]).
    """
    if len(seed) < 8:
        raise ValueError("seed must be 8 bytes")
    b = bytearray(seed[:8])

    # 0x37D9F4F..0x37D9F6F -- in-place byte chain. Note it starts at i=1, so
    # b[0] is never touched and is used raw by the third loop.
    for i in range(1, 8):
        b[i] ^= b[i - 1]
        if b[i] & 2:
            b[i] |= 0x80

    # 0x37D9F77..0x37D9FA5 -- one 64-bit sum and one 64-bit product, together.
    #   edi:ebx starts at 1        (0x37D9F2C `mov edi,1` / `xor ebx,ebx`)
    #   ebp:[esp+0x1c] starts at 0x7048_860DDF79
    acc = 1
    mul = (0x7048 << 32) | 0x860DDF79
    for i in range(1, 8):
        acc = (acc + b[i]) & M64
        mul = (mul * (b[i - 1] * b[i])) & M64        # __allmul, 0x38134A0

    # 0x37D9FAA..0x37D9FD3 -- a 32-bit LCG iterated (byte - 0x30) times per
    # byte, and ONLY for bytes above 0x30. `jle` is signed but the byte is
    # zero-extended first (`xor eax,eax; mov al,...`), so unsigned is right.
    h = 0
    for i in range(0, 8):
        if b[i] > 0x30:
            for _ in range(b[i] - 0x30):
                h = (h * 0x425F0CBD + 0x7F4F) & M32

    # 0x37D9FD5..0x37D9FE5 -- K = (acc + mul), then the LOW dword only is
    # XOR'd with h (`xor ebx,eax` with eax=0 is a no-op on the high dword).
    k = (acc + mul) & M64
    return (k & ~M32) | ((k & M32) ^ h)


def unmangle(scrambled, k):
    """Undo polcore 0x37D9D40 -- what the client sends back is `guid ^ K`."""
    return (int(scrambled) ^ int(k)) & M64


def solve(served_guid, observed):
    """K from one (guid we served, value the client sent back) pair.

    The transform is a plain XOR, so a single known pair gives K outright -- no
    seed, no debugger. The catch is that the pair must be KNOWN-good: pick the
    friend yourself so `served_guid` is not in doubt, and beware that a client
    which never resolved the friend at all sends its OWN id for everyone (a
    CONSTANT z_hid across different friends means exactly that, and the pair is
    then meaningless -- check two friends give two different values first).
    """
    return (int(served_guid) ^ int(observed)) & M64


HOW = """\
Capturing the seed with one breakpoint
--------------------------------------
polcore VAs; add the module base the loader gave you (memimg base 0x037C0000).

  bp  polcore+0x19FE9      ; = 0x37D9FE9, `mov [0x386a848], edi`
  On hit:
      edi          = K low dword
      ebx          = K high dword
      [esp+0x24]   = the seed pointer (arg1; esp has 3 pushes + sub 0x14 on it)
      db <seedptr> L8      ; the 8 seed bytes

Then check the transcription:  polkey.py --seed <16 hex digits>
and compare against the edi/ebx you just read. If they disagree, this file is
wrong and the disassembly at 0x37D9F20 is the authority.

It fires ONCE per session, at session attach (0x3804BBF), so set it before you
log in -- after the lobby is up you have missed it.

Without a debugger: `--solve` a known friend's profile view instead.
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seed", help="8 seed bytes as 16 hex digits (as dumped)")
    ap.add_argument("--key", help="K directly, hex, if you already have it")
    ap.add_argument("--unmangle", nargs="*", default=[],
                    help="scrambled guid(s) to resolve, hex")
    ap.add_argument("--solve", nargs=2, metavar=("SERVED", "OBSERVED"),
                    help="recover K from one known (served, observed) pair")
    ap.add_argument("--how", action="store_true", help="the debugger recipe")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)

    if a.how:
        print(HOW)
        return 0
    if a.selftest:
        return selftest()

    k = None
    if a.solve:
        k = solve(int(a.solve[0], 16), int(a.solve[1], 16))
        print(f"K = {k:016X}   (from served {int(a.solve[0],16):X} ^ "
              f"observed {int(a.solve[1],16):X})")
    if a.key:
        k = int(a.key, 16)
    if a.seed:
        raw = bytes.fromhex(a.seed.replace(" ", ""))
        ks = kinit(raw)
        print(f"seed {raw.hex()} -> K = {ks:016X}  "
              f"(lo {ks & M32:08X} hi {ks >> 32:08X})")
        if k is not None and k != ks:
            print(f"  !! disagrees with K = {k:016X}", file=sys.stderr)
        k = ks
    if a.unmangle:
        if k is None:
            ap.error("--unmangle needs a K (--seed, --key or --solve)")
        for s in a.unmangle:
            v = int(s, 16)
            print(f"  {v:016X} -> {unmangle(v, k):016X}")
    if k is None:
        ap.print_help()
    return 0


def selftest():
    """Only what can be checked WITHOUT a live client.

    There is no captured (seed, K) pair in the repo yet, so this asserts the
    algebra and the shape -- not the constants. The one real check is the
    debugger comparison in `--how`; do not let a green selftest here stand in
    for it.
    """
    ok = 0

    # A plain XOR is an involution: unmangle(mangle(g)) == g, for any K.
    k = kinit(bytes.fromhex("0011223344556677"))
    for g in (0x8000000000A, 0x0000080000355000, 0, M64):
        assert unmangle(unmangle(g, k), k) == g
    ok += 1

    # `solve` and `unmangle` are the same XOR seen from both ends.
    served, kk = 0x8000000000A, 0x0000000000203641
    assert solve(served, unmangle(served, kk)) == kk
    ok += 1

    # kinit is deterministic and total over every 8-byte input shape we can
    # cheaply cover, including the two the byte-chain treats specially
    # (bit 1 set -> bit 7 forced) and the LCG's 0x30 threshold.
    seen = set()
    for pat in (b"\x00" * 8, b"\xff" * 8, b"\x02" * 8, b"\x30" * 8,
                b"\x31" * 8, bytes(range(8)), bytes(range(0x28, 0x30)),
                b"\x00\x02\x04\x06\x08\x0a\x0c\x0e"):
        v = kinit(pat)
        assert 0 <= v <= M64
        seen.add(v)
    assert len(seen) > 1, "kinit collapsed every input to one value"
    ok += 1

    # b[0] is NOT chained (the loop starts at i=1) but IS read by the LCG loop,
    # so changing it alone must change K. This is the transcription detail most
    # likely to be got wrong, so it gets its own check.
    assert kinit(b"\x40" + b"\x00" * 7) != kinit(b"\x00" * 8)
    ok += 1

    # The high dword is never XOR'd with the LCG (`xor ebx,eax`, eax=0), so K's
    # high half must equal the high half of (acc + mul). Recompute it the long
    # way and compare -- this is the assertion that would catch a stray mask.
    seed = bytes.fromhex("4142434445464748")
    b = bytearray(seed)
    for i in range(1, 8):
        b[i] ^= b[i - 1]
        if b[i] & 2:
            b[i] |= 0x80
    acc, mul = 1, (0x7048 << 32) | 0x860DDF79
    for i in range(1, 8):
        acc = (acc + b[i]) & M64
        mul = (mul * (b[i - 1] * b[i])) & M64
    assert kinit(seed) >> 32 == ((acc + mul) & M64) >> 32
    ok += 1

    print(f"polkey selftest: {ok}/5 OK "
          "(algebra only -- no captured (seed,K) pair exists yet)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
