#!/usr/bin/env python3
"""The minigame client-id key K, computed from the game code.

Tetra Master and Janhourou both move 64-bit ids in two spaces -- the id the
title works with, and that id XORed with a 64-bit key `K` (see the Tetra Master
room server's `CLIENT_KEY_DEFAULT` banner for what that does to a table id). We measured
TM's K off live traffic on 2026-08-22. Project Crystal Server (a collaborator's
C# POL server, `POLCommon/MiniGame/Mg.cs`) shows where the number comes from:
it is a pure function of the title's 3-letter game code, so it is the same for
every install and every session of one title, and different per title.

    code = the game code's three ASCII bytes read little-endian
           ("TM0" -> 0x304D54, "MJS" -> 0x534A4D)
    sq   = code * code                      (mod 2**32)
    lo   = sq * 19701121                    (mod 2**32)
    hi   = sq * 0x012CC4F5                  (mod 2**32)
    K    = hi << 32 | lo

19701121 is Crystal's shift/add chain folded into one multiplier:
((sq*513)*20 + sq) * 15 * 128 + sq. Every step wraps at 32 bits there, and
multiplication mod 2**32 distributes, so the product is the same number.

"TM0" gives, bit for bit, the value the Tetra Master room server measured off
live traffic, which is the check that this is the formula the client uses and
not a lookalike. "MJS" gives a value whose top 24 bits are the prefix every
captured Janhourou `<PD>` PolID carries, which fits K's role there, but no
Janhourou code path uses the full value yet.

    python mgkey.py            -> TM0 and MJS
    python mgkey.py XYZ        -> any code
"""
import sys

MASK32 = 0xFFFFFFFF
MASK64 = 0xFFFFFFFFFFFFFFFF

#: The low half's multiplier -- Crystal's chain, collapsed (see the docstring).
_LO_MUL = ((513 * 20 + 1) * 15 * 128) + 1
#: The high half's multiplier, as Crystal has it.
_HI_MUL = 0x012CC4F5

assert _LO_MUL == 19701121


def game_code_int(code):
    """"TM0" -> 0x304D54: the code's ASCII bytes, first letter lowest."""
    if isinstance(code, int):
        return code & MASK32
    raw = code.encode("ascii") if isinstance(code, str) else bytes(code)
    return int.from_bytes(raw[:4], "little")


def minigame_client_key(code):
    """The 64-bit id key for game code `code` ("TM0", "MJS", or the int form)."""
    sq = (game_code_int(code) ** 2) & MASK32
    lo = (sq * _LO_MUL) & MASK32
    hi = (sq * _HI_MUL) & MASK32
    return (hi << 32) | lo


def game_id(polid, code):
    """A POL-ID-space 64-bit value moved into game `code`'s id space (and back:
    XOR is its own inverse). Crystal's `MjKey`/`TmKey`."""
    return (int(polid) ^ minigame_client_key(code)) & MASK64


TM_CLIENT_KEY = minigame_client_key("TM0")
JAN_CLIENT_KEY = minigame_client_key("MJS")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    for code in (argv or ["TM0", "MJS"]):
        print("%-4s 0x%06X  K = 0x%016X"
              % (code, game_code_int(code), minigame_client_key(code)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
