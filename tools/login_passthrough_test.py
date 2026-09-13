#!/usr/bin/env python3
"""POL-0008: a cipher-passthrough byte must not lose the login.

`ofb_apply` sends a byte IN THE CLEAR whenever `pt ^ ks` would be LF or CR, so
the wire keeps its framing readable without the key. Deriving block0 by XORing a
crib against the ciphertext therefore puts 0x00 where the real keystream byte
belongs, corrupts the whole OFB stream, and the login is dropped as
"could not recover IV" -- POL-0008 / POL-2059 -- while the client holds exactly
the key we expect.

Proved live 2026-09-08 (127.0.0.1:37709): the shim caught the client using K=0
on the very dial the server rejected, and the SPACE after "NICK" had hit
0x20 ^ 0x2a == 0x0a. Rate: 2/256 per byte over an 8-byte crib = 6.1%.

These checks build that collision deterministically instead of waiting for luck.
"""
import hashlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "services"))
import sessioncrypt as sc                                    # noqa: E402

FAILED = []
NICK = b"UF8TOQDTX"


def check(ok, what, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {what}" + (f"  --  {detail}" if detail else ""))
    if not ok:
        FAILED.append(what)


def nick_line(nick=NICK, salt=b"x"):
    return (b"NICK " + nick + b":" + hashlib.md5(salt).hexdigest().encode()
            + b":" + b"TTTTTAISTTTTTTTTTTTTT" + b"AbCdEfGhIjK" + b"ab~Q")


def iv_forcing(P, S, pt, positions, ctrl=0x0a):
    """An IV whose block0 makes `pt` pass through in the clear at `positions`.

    Any 8 bytes are a valid block0 (Blowfish is a bijection), so pick the
    keystream we want and invert it to the IV it must have come from.
    """
    ks8 = bytearray(os.urandom(8))
    for i in positions:
        ks8[i] = pt[i] ^ ctrl
    return sc._iv_from_block0(P, S, bytes(ks8)), bytes(ks8)


def main():
    P, S = sc.bf_setkey(b"\x00" * 8)          # K=0: the real login path
    sc.set_known_nicks([NICK])
    pt = nick_line()

    print("the collision itself ->")
    iv, ks8 = iv_forcing(P, S, pt, [4])
    ct = sc.ofb_apply(P, S, iv, pt)
    check(ct[4] == pt[4], "a byte whose pt^ks is LF goes out IN THE CLEAR",
          f"ct[4]={ct[4]:#04x} == pt[4]={pt[4]:#04x}, true ks[4]={ks8[4]:#04x}")
    check((pt[4] ^ ks8[4]) == 0x0a, "...which is exactly the LF case")
    naive = bytes(a ^ b for a, b in zip(b"NICK " + NICK + b":", ct))[:8]
    check(naive[4] == 0x00 and naive[4] != ks8[4],
          "THE BUG: a plain crib XOR yields 0x00 for that byte, not the keystream")

    print("\nrecovery must survive it ->")
    got_iv, got_pt = sc.recover_iv(P, S, ct)
    check(got_pt == pt, "crib path recovers the line byte-for-byte")
    check(got_iv == iv, "...and the IV", got_iv.hex() if got_iv else "none")

    print("\n...at every position of block0, and for CR as well as LF ->")
    for ctrl, label in ((0x0a, "LF"), (0x0d, "CR")):
        bad = []
        for i in range(8):
            iv2, _ = iv_forcing(P, S, pt, [i], ctrl)
            ct2 = sc.ofb_apply(P, S, iv2, pt)
            if sc.recover_iv(P, S, ct2)[1] != pt:
                bad.append(i)
        check(not bad, f"a {label} collision at any of block0's 8 bytes is recovered",
              f"failed at {bad}" if bad else "0..7 all recovered")

    print("\n...and more than one at once ->")
    iv3, _ = iv_forcing(P, S, pt, [1, 4, 6])
    ct3 = sc.ofb_apply(P, S, iv3, pt)
    check(sc.recover_iv(P, S, ct3)[1] == pt, "three simultaneous collisions")

    print("\nthe brute-force path (unknown nick) must survive it too ->")
    sc.set_known_nicks([b"USOMEONELSE"])
    iv4, _ = iv_forcing(P, S, pt, [4])
    ct4 = sc.ofb_apply(P, S, iv4, pt)
    check(sc.recover_iv(P, S, ct4)[1] == pt,
          "an account absent from login_alias still recovers")
    iv5, _ = iv_forcing(P, S, pt, [6])          # inside the brute-forced nick[0:3]
    ct5 = sc.ofb_apply(P, S, iv5, pt)
    check(sc.recover_iv(P, S, ct5)[1] == pt,
          "...including a collision on a brute-forced byte")
    sc.set_known_nicks([NICK])

    print("\nno regression on the ordinary case ->")
    clean = 0
    for k in range(40):
        ivc = os.urandom(8)
        ptc = nick_line(salt=bytes([k]))
        if sc.recover_iv(P, S, sc.ofb_apply(P, S, ivc, ptc))[1] == ptc:
            clean += 1
    check(clean == 40, "40 random logins all recover", f"{clean}/40")

    print()
    if FAILED:
        print(f"FAILED: {len(FAILED)} check(s): " + "; ".join(FAILED))
        return 1
    print("all passthrough checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
