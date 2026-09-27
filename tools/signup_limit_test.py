#!/usr/bin/env python3
"""The sign-up wizard's rate limit: what stands where the registration code used to.

    python tools/signup_limit_test.py

`POL_SIGNUP_MODE=permissive` removes the only gate on a path a public server
exposes to the internet. These checks drive the
limiter directly -- they are about the RULE, not the wire (the wire is
tools/signup_mode_test.py).

The address is NOT always a person here: the plain-HTTP route arrives with the
player's own address, the TLS route arrives from the terminator on loopback
where every caller looks alike. So the per-address cap must exempt loopback and
private addresses, and a global per-day cap has to be what bounds those.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))

# small, explicit limits -- set before the module reads them
os.environ["POL_SIGNUP_PER_DAY"] = "5"
os.environ["POL_SIGNUP_PER_IP"] = "2"
os.environ["POL_ACCOUNTS_DB"] = os.path.join(HERE, "_nonexistent_for_import.db")

import ucscgi  # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


def reset():
    with ucscgi._MADE_LOCK:
        ucscgi._MADE[:] = []


# WARNING: NOT the RFC 5737 documentation ranges (203.0.113.x, 198.51.100.x, 192.0.2.x).
# Python reports every one of them as `is_private`, so using them here made the
# per-address cap look permanently disabled -- three checks failed against code
# that was in fact correct. Real, globally routable addresses only.
PUB, PUB2 = "8.8.8.8", "1.1.1.1"

print("which addresses count as a person")
chk("a public address counts", ucscgi._countable_peer(PUB), PUB)
chk("loopback does not (it is the TLS terminator)", ucscgi._countable_peer("127.0.0.1"), None)
chk("a LAN address does not", ucscgi._countable_peer("192.168.50.4"), None)  # polcheck: allow
chk("an IPv4-mapped IPv6 address is folded to its v4 form",
    ucscgi._countable_peer("::ffff:8.8.8.8"), PUB)
chk("garbage does not crash it", ucscgi._countable_peer("not-an-address"), None)
# Carrier-grade NAT (the RFC 6598 shared range) is `is_private == False` in Python, so it
# IS counted and unrelated players behind one carrier share a budget; the
# global cap is what bounds anyone this lets through.

print("\nthe per-address cap (2 here)")
reset()
chk("the first is allowed", ucscgi.signup_over_limit(PUB), None)
ucscgi.note_signup(PUB)
chk("the second is allowed", ucscgi.signup_over_limit(PUB), None)
ucscgi.note_signup(PUB)
third = ucscgi.signup_over_limit(PUB)
chk("the third is REFUSED", bool(third) and "already registered" in third, True)
chk("...and a DIFFERENT address is unaffected",
    ucscgi.signup_over_limit(PUB2), None)

print("\nloopback is exempt from the per-address cap, or the terminator locks everyone out")
reset()
for _ in range(4):
    ucscgi.note_signup("127.0.0.1")
chk("four sign-ups from loopback, a fifth still allowed",
    ucscgi.signup_over_limit("127.0.0.1"), None)

print("\n...which is why the GLOBAL cap (5 here) has to exist")
reset()
for _ in range(5):
    ucscgi.note_signup("127.0.0.1")
sixth = ucscgi.signup_over_limit("127.0.0.1")
chk("the sixth is refused by the daily cap", bool(sixth) and "busy right now" in sixth, True)
chk("...and it refuses a PUBLIC address too, not just loopback",
    bool(ucscgi.signup_over_limit(PUB)), True)

print("\nthe window really is a window")
reset()
import time  # noqa: E402
with ucscgi._MADE_LOCK:
    old = time.time() - ucscgi.SIGNUP_DAY - 60
    ucscgi._MADE[:] = [(old, PUB)] * 9        # yesterday's, all of them
chk("yesterday's sign-ups do not count against today",
    ucscgi.signup_over_limit(PUB), None)

print("\nonly SUCCESS is counted")
reset()
chk("a refusal writes nothing (so a mistyped handle is not spent)",
    (ucscgi.signup_over_limit(PUB), len(ucscgi._MADE)), (None, 0))

print("\nturning it off")
reset()
saved = ucscgi.SIGNUP_PER_DAY, ucscgi.SIGNUP_PER_IP
ucscgi.SIGNUP_PER_DAY = ucscgi.SIGNUP_PER_IP = 0
for _ in range(50):
    ucscgi.note_signup(PUB)
chk("0 means no limit", ucscgi.signup_over_limit(PUB), None)
ucscgi.SIGNUP_PER_DAY, ucscgi.SIGNUP_PER_IP = saved

print("\nFAILURES:", bad)
sys.exit(1 if bad else 0)
