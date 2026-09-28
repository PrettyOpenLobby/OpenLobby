#!/usr/bin/env python3
"""The minigame client-id key K is a function of the game code.

    python tests/test_mgkey.py

The formula came from Project Crystal Server's Mg.cs; the Tetra Master room
server pins the full value against the one it measured. This pins the parts
that do not depend on that measurement: the game-code integer, the collapsed
multiplier, and that the two forms of a code agree while neighbouring codes
differ.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))

import mgkey  # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


print("game code -> int")
chk("TM0", hex(mgkey.game_code_int("TM0")), "0x304d54")
chk("MJS", hex(mgkey.game_code_int("MJS")), "0x534a4d")
chk("an int passes through", mgkey.game_code_int(0x304D54), 0x304D54)

print("the multiplier is Crystal's shift/add chain, folded")
sq = 0x12345678
chain = ((((sq * 513) & 0xFFFFFFFF) * 20 + sq) & 0xFFFFFFFF)
chain = ((chain * 15) & 0xFFFFFFFF)
chain = ((chain * 128) + sq) & 0xFFFFFFFF
chk("chain == sq * 19701121 (mod 2**32)", chain, (sq * 19701121) & 0xFFFFFFFF)

print("K")
k = mgkey.minigame_client_key("TM0")
chk("64 bits", k >> 64, 0)
chk("the str and int forms agree", mgkey.minigame_client_key(0x304D54), k)
chk("TM1 differs", mgkey.minigame_client_key("TM1") != k, True)
chk("MJS differs", mgkey.JAN_CLIENT_KEY != k, True)
chk("TM_CLIENT_KEY is K(TM0)", mgkey.TM_CLIENT_KEY, k)
chk("game_id is its own inverse",
    mgkey.game_id(mgkey.game_id(0x00E13883D826, "TM0"), "TM0"), 0x00E13883D826)
chk("game_id moves the id by K", mgkey.game_id(0, "MJS"), mgkey.JAN_CLIENT_KEY)

print("%s" % ("ALL OK" if not bad else "%d FAILED" % bad))
sys.exit(1 if bad else 0)
