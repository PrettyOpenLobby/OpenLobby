#!/usr/bin/env python3
"""The nick crib list must come from the ACCOUNT DB, not a hardcoded tuple.

Guards the 2026-09-07 finding: `sessioncrypt.KNOWN_NICKS` was three hardcoded
strings, none of which was a real account, so `recover_iv`'s fast path never hit
once and EVERY login fell into the 64^3 brute force over nick[0:3] -- ~0.6 s on
the success path, ~2.0 s per candidate key on the failure path (41 s wall-clock
before the login was dropped, which the user sees as POL-0008 / POL-2059).

These checks fail if the registry is bypassed, if the seed is mistaken for the
whole list, or if `brute=False` stops rejecting a wrong key cheaply.
"""
import hashlib
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "services"))
import sessioncrypt as sc                                    # noqa: E402

FAILED = []


def check(ok, what, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {what}" + (f"  --  {detail}" if detail else ""))
    if not ok:
        FAILED.append(what)


def nick_line(nick, blob=b"TTTTTAISTTTTTTTTTTTTTIIa67V18sie"):
    """The exact shape prod logs show: NICK <nick>:<32 hex>:<21+11><4 chk>."""
    return (b"NICK " + nick + b":" + hashlib.md5(nick).hexdigest().encode()
            + b":" + blob + b"ab~Q")


def main():
    print("the registry ->")
    sc.set_known_nicks([b"UAAAAAAAA", b"UBBBBBBBB"])
    check(b"UAAAAAAAA" in sc.known_nicks(), "set_known_nicks installs account nicks")
    check(all(n in sc.known_nicks() for n in sc._SEED_NICKS),
          "the static seed survives as a tail, so a DB-less server is unchanged")
    sc.remember_nick(b"UBBBBBBBB")
    check(sc.known_nicks()[0] == b"UBBBBBBBB",
          "remember_nick promotes the account that just logged in to the front")
    sc.remember_nick(b"UBBBBBBBB")
    check(sc.known_nicks().count(b"UBBBBBBBB") == 1, "...and does not duplicate it")
    sc.remember_nick(b"UNEWACCT1")
    check(sc.known_nicks()[0] == b"UNEWACCT1",
          "a nick absent from login_alias is learned from a successful recovery")

    print("\nthe crib actually reads a real NICK line ->")
    nick = b"U3KFHB3K4"                       # the only nick in the 09-07 prod logs
    P0, S0 = sc.bf_setkey(b"\x00" * 8)        # K=0: the 94% path
    ct = sc.ofb_apply(P0, S0, os.urandom(8), nick_line(nick))

    sc.set_known_nicks([nick])
    t = time.time()
    iv, pt = sc.recover_iv(P0, S0, ct, brute=False)
    crib_s = time.time() - t
    check(iv is not None and pt == nick_line(nick),
          "a known nick recovers WITHOUT brute force, byte-for-byte")
    check(crib_s < 0.05, "...and does it in ~one Blowfish block", f"{crib_s*1000:.1f} ms")

    print("\nthe hardcoded tuple could never have worked ->")
    check(nick not in sc.KNOWN_NICKS,
          "the shipped seed does not contain the account that actually logs in")
    iv2, _ = sc.recover_iv(P0, S0, ct, known_nicks=sc.KNOWN_NICKS, brute=False)
    check(iv2 is None,
          "with the old list and no brute force, a real login is UNREADABLE "
          "-- this is why every login paid 64^3")

    print("\nbrute=False must still reject a wrong key ->")
    Pw, Sw = sc.bf_setkey(bytes.fromhex("43e69e6a00000000"))
    t = time.time()
    iv3, _ = sc.recover_iv(Pw, Sw, ct, brute=False)
    rej_s = time.time() - t
    check(iv3 is None, "a wrong candidate key is rejected")
    check(rej_s < 0.05, "...cheaply, which is what makes a full sweep affordable",
          f"{rej_s*1000:.1f} ms")

    print("\nbrute=True is still the fallback for an UNKNOWN nick ->")
    sc.set_known_nicks([b"USOMEONELSE"])
    iv4, pt4 = sc.recover_iv(P0, S0, ct)
    check(iv4 is not None and pt4.startswith(b"NICK " + nick),
          "an account we have never met is still found by the brute force")

    print("\nthe schedule cache must survive a full history sweep ->")
    sc._SETKEY_CACHE.clear()
    for i in range(sc._SETKEY_MAX + 20):
        sc.bf_setkey(i.to_bytes(8, "little"))
    check(len(sc._SETKEY_CACHE) <= sc._SETKEY_MAX, "the LRU stays bounded",
          f"{len(sc._SETKEY_CACHE)} <= {sc._SETKEY_MAX}")
    check(sc._SETKEY_MAX >= 64,
          "...and is big enough for a per-IP stamp history (34 seen live) plus lobby keys",
          str(sc._SETKEY_MAX))

    print()
    if FAILED:
        print(f"FAILED: {len(FAILED)} check(s): " + "; ".join(FAILED))
        return 1
    print("all nick-crib checks passed")
    return 0



if __name__ == "__main__":
    sys.exit(main())
