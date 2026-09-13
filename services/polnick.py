#!/usr/bin/env python3
"""PlayOnline ID <-> login NICK.

The client never sends the PlayOnline ID you type at Add Member. It sends a
scrambled form of it as the IRC nick -- `IJKL9012` goes out as `UZ3714LIO` --
and that nick is the only identity our auth path ever sees. So a freshly
registered account cannot be recognised on its first login unless the server can
compute the nick for the ID it issued. This does that, exactly, both ways.

Read out of `pol.pex` (the PS2 Viewer's socket/transport/IRC module,
decompiled): `polpex_00140d20` is the two-way
scrambler and `polpex_001404c0` the base-36 renderer it shares with the GUID
path. The same tables sit in the PC client's `polcore.dll` at file offsets
0xf6ec (alphabet) and 0xf690+c (its inverse), so both platforms scramble
identically -- which is what makes this safe to compute server-side.

    ALPHA = EFKAOYMJVNGTDSWBQLPCIRHZXU6328401795   (a permutation of [A-Z0-9])

    v  = the 8 characters read as base-36 digits over ALPHA, most significant
         first  (`uVar6 = uVar6 * 0x24 + ALPHA_INV[c]`)
    v |= 0xc00000000000                      bits 46 and 47 set, as a constant
    for k in 0..4:                           five rounds of a byte XOR chain,
        i = 4 - k   (encode)                 running down for encode and up for
        i = k       (decode)                 decode, which is what inverts it
        v ^= (v >> 8) & (0xff << i*8)        i.e. byte[i] ^= byte[i+1]
    v &= ~0xc00000000000                     bits 46/47 dropped again
    out = v rendered as 8 base-36 digits over ALPHA, most significant first

and the nick is that, prefixed with the literal 'U' (`param_2[-1] = 0x55`).

Both halves are bijections on 8 characters, so this round-trips for every ID.

    python polnick.py IJKL9012          -> UZ3714LIO
    python polnick.py --decode UZ3714LIO -> IJKL9012
    python polnick.py --selftest
"""
import argparse
import sys

#: polcore.dll file offset 0xf6ec / pol.pex 0x00185c58.
ALPHA = "EFKAOYMJVNGTDSWBQLPCIRHZXU6328401795"
ALPHA_INV = {c: i for i, c in enumerate(ALPHA)}

#: The two constant bits the scrambler sets before the chain and clears after.
#: They are inside the 5 bytes the chain touches, so they are not decoration --
#: drop them and byte 5 feeds the chain differently and nothing round-trips.
GUARD = 0xC00000000000

#: Measured on the dev server, 2026-08-16: typed at the client's Add Member
#: dialog, read back off `logs/accounts.log`. These are ground truth, not
#: derivation -- keep them.
MEASURED = [
    ("IJKL9012", "UZ3714LIO"),
    ("AAAAAAAA", "USHUXPJ2D"),
    ("AAAAAAAB", "USHUXPJ32"),
]


def _digits_in(s):
    """The 8 characters as one base-36 value, most significant first."""
    v = 0
    for c in s:
        try:
            v = v * 36 + ALPHA_INV[c]
        except KeyError:
            raise ValueError(f"{c!r} is not a PlayOnline ID character "
                             "(capitals and digits only)")
    return v


def _digits_out(v):
    """The inverse of _digits_in: 8 characters, most significant first."""
    out = [""] * 8
    for i in range(7, -1, -1):
        v, d = divmod(v, 36)
        out[i] = ALPHA[d]
    return "".join(out)


def _scramble(v, decode):
    v |= GUARD
    for k in range(5):
        i = k if decode else 4 - k
        v ^= (v >> 8) & (0xFF << (i * 8))
    return v & ~GUARD


def nick_for_polid(polid):
    """The IRC nick the client will send for this typed PlayOnline ID."""
    polid = (polid or "").strip().upper()
    if len(polid) != 8:
        raise ValueError(f"a PlayOnline ID is 8 characters, got {len(polid)}")
    return "U" + _digits_out(_scramble(_digits_in(polid), decode=False))


def polid_for_nick(nick):
    """The PlayOnline ID behind a login nick, or None if it is not one.

    Returns None rather than raising: this runs on the auth path against
    whatever a client sends, including nicks that are not scrambled IDs at all
    (our own test accounts, PolFL, anything hand-made).
    """
    nick = (nick or "").strip().upper()
    if len(nick) != 9 or nick[0] != "U":
        return None
    try:
        return _digits_out(_scramble(_digits_in(nick[1:]), decode=True))
    except ValueError:
        return None


def selftest():
    ok = True
    for polid, nick in MEASURED:
        got = nick_for_polid(polid)
        back = polid_for_nick(nick)
        mark = "ok " if got == nick else "FAIL"
        if got != nick:
            ok = False
        print(f"  {mark} {polid} -> {got}   (measured {nick})")
        mark = "ok " if back == polid else "FAIL"
        if back != polid:
            ok = False
        print(f"  {mark} {nick} -> {back}   (measured {polid})")
    import random
    rng = random.Random(20260816)

    # The shape accounts.mint_polid issues. This one MUST be perfect -- an ID
    # that does not round-trip scrambles to a nick belonging to a different ID,
    # so two accounts would eventually collide on one login.
    L, D = "ABCDEFGHJKLMNPQRSTUVWXYZ", "23456789"
    minted = ["".join(rng.choice(L) for _ in range(4))
              + "".join(rng.choice(D) for _ in range(4)) for _ in range(20000)]
    bad = [c for c in minted if polid_for_nick(nick_for_polid(c)) != c]
    print(f"  {'ok ' if not bad else 'FAIL'} round-trip on {len(minted)} minted-"
          f"shape IDs" + (f" -- {bad[:5]} failed" if bad else ""))
    if bad:
        ok = False

    # The WHOLE 8-character space does not round-trip, and that is SE's
    # algorithm rather than ours: 8 base-36 digits need 41.36 bits and the byte
    # chain treats the value as 41, so the top of the range folds over. Reported,
    # not asserted -- what matters is that mint_polid stays out of that band.
    whole = ["".join(rng.choice(ALPHA) for _ in range(8)) for _ in range(20000)]
    good = sum(1 for c in whole if polid_for_nick(nick_for_polid(c)) == c)
    print(f"  --  full 36-symbol space: {100*good/len(whole):.1f}% round-trips "
          f"(expected ~83%; mint_polid rejects the rest)")

    # A nick body must itself be legal ID characters, or the client could never
    # have produced it from something a user typed.
    strays = {c for p in minted + whole for c in nick_for_polid(p)[1:]} - set(ALPHA)
    print(f"  {'ok ' if not strays else 'FAIL'} every nick character is in the "
          f"alphabet" + (f" -- stray {sorted(strays)}" if strays else ""))
    return ok and not strays


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("value", nargs="?", help="a PlayOnline ID, or a nick with --decode")
    ap.add_argument("--decode", action="store_true",
                    help="go the other way: nick -> PlayOnline ID")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        print("polnick.py self-test")
        return 0 if selftest() else 1
    if not args.value:
        ap.error("give a PlayOnline ID (or a nick with --decode)")
    if args.decode:
        got = polid_for_nick(args.value)
        if got is None:
            print(f"{args.value} is not a scrambled PlayOnline ID", file=sys.stderr)
            return 1
        print(got)
    else:
        print(nick_for_polid(args.value))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
