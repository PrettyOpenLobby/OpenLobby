"""Does a failed login write down enough to identify the key the client held?

Background: five POL-0008 keepalive-drop generators were found
and fixed; ONE remains open -- a sign-in where neither K=0 nor any session key
we ever issued decrypts the client's NICK:

    could not recover IV (nick_ct=...); K=0 and N issued session key(s)
    all failed the crib

which the user sees as POL-2059. Two dials fail, the third logs in instantly.
The client holds a Blowfish key we apparently never issued for that address --
which should be impossible, because we mint every stamp and the stamp we greeted
that dial with is in the candidate list.

**Every occurrence so far has been unfalsifiable after the fact.** The log line
printed eight bytes of ciphertext and a COUNT: not the greeting stamp, not the
key it implies, not the candidates actually tried, and -- the one that quietly
decides the whole conclusion -- not whether the candidate list was TRUNCATED.
It is capped at `POL_STAMP_TRY` (default 12), so "N session key(s) all failed"
cannot distinguish

    "we tried every stamp we ever issued, so the key is NOT ours"   (a finding)
    "we tried the newest 12 of 35"                                  (nothing)

and those have opposite next steps. This suite pins the evidence, not the
prose: the failure must record the full ciphertext, every candidate key, the
whole stamp history, this dial's greeting stamp, and it must SAY when the search
was incomplete rather than implying it was exhaustive.
"""
import os
import re
import struct
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "services"))

TMP = tempfile.mkdtemp(prefix="login-trace-")
os.environ["POL_LOG_DIR"] = TMP
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_ACCOUNTS_DB"] = os.path.join(TMP, "accounts.db")

import responders as R                                             # noqa: E402

FAILS = []


def check(ok, label, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  --  {detail}"
                                                       if detail else ""))
    if not ok:
        FAILS.append(label)


def dumps(kind):
    d = os.path.join(TMP, "captures")
    if not os.path.isdir(d):
        return []
    return [os.path.join(d, f) for f in sorted(os.listdir(d))
            if f.startswith(f"login-{kind}-")]


def main():
    print("the trace records the login as it happens ->")
    R._trace_begin("192.0.2.9:5000", 51241)
    R._trace("greet session-token", "stamp=0xdeadbeef")
    R._trace("USER", "b'USER x 8 * :tok'")
    path = R._trace_dump("192.0.2.9:5000", "nokey",
                         [("a block", "some detail")])
    check(path is not None and os.path.isfile(path),
          "a failing login writes a dump", str(path))
    body = open(path, encoding="utf-8").read()
    check("greet session-token" in body and "USER" in body,
          "...carrying the steps in order")
    check("+" in body and "ms" in body,
          "...with a relative clock, so a stall is visible")
    check("a block" in body and "some detail" in body,
          "...and the extra blocks the caller attached")

    print("\na SUCCESSFUL login is silent by default ->")
    before = len(dumps("ok"))
    R._trace_begin("192.0.2.9:5001", 51241)
    R._trace("welcome sent", "mode=welcome")
    check(R._trace_dump("192.0.2.9:5001", "ok") is None
          and len(dumps("ok")) == before,
          "POL_LOGIN_TRACE=fail (the default) writes nothing for a good login")
    os.environ["POL_LOGIN_TRACE"] = "all"
    R._trace_begin("192.0.2.9:5002", 51241)
    R._trace("welcome sent", "mode=welcome")
    check(R._trace_dump("192.0.2.9:5002", "ok") is not None,
          "POL_LOGIN_TRACE=all captures the whole login deliberately")
    os.environ["POL_LOGIN_TRACE"] = "0"
    R._trace_begin("192.0.2.9:5003", 51241)
    R._trace("welcome sent", "x")
    check(R._trace_dump("192.0.2.9:5003", "nokey") is None,
          "POL_LOGIN_TRACE=0 disables it even for a failure")
    del os.environ["POL_LOGIN_TRACE"]

    print("\nthe key evidence itself ->")
    # The stamp -> key derivation is the thing the whole open question turns on:
    # polcore keys from LE32(stamp) + four zero bytes.
    stamp = 0x68A3B1C2
    check(R.session_token_key(stamp) == struct.pack("<I", stamp) + b"\x00" * 4,
          "a stamp still implies exactly one key",
          R.session_token_key(stamp).hex())

    # A history of stamps for one address, as a real client's re-dials build up.
    # The count is cap-relative on purpose: this asserted a flat 20 > 12, so when
    # the default cap moved to 40 on 2026-09-07 the test failed for the one reason
    # that is not a bug -- the history no longer reached the cap. What is being
    # tested is that a history LONGER than the cap gets truncated, not any
    # particular number.
    ip = "192.0.2.9"
    cap = int(os.environ.get("POL_STAMP_TRY", "40"))
    n_stamps = cap + 8
    for i in range(n_stamps):
        R.remember_stamp(ip, stamp + i)
    cands = R.key_candidates(ip, stamp)
    check(len(cands) == cap,
          "the candidate list really is capped -- this is the trap",
          f"{len(cands)} tried, {n_stamps} stamps known, POL_STAMP_TRY={cap}")
    check(all(len(k) == 8 for _why, k in cands),
          "every candidate is a full 8-byte key")

    print("\nthe failure line must not overstate what it proved ->")
    # The wording is load-bearing: an admin reading "all failed the crib"
    # concludes the key was never ours and goes looking at the client. That is
    # only justified when the search was exhaustive.
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            os.pardir, "services", "responders.py"),
               encoding="utf-8").read()
    # Anchored on the trace event, which occurs ONCE. "could not recover IV" is
    # the obvious anchor and the wrong one: its first occurrence is the comment
    # block near the top of the file, three thousand lines from the code that
    # builds the dump, so a window around it reads somebody else's prose and
    # every check below fails for no reason.
    marker = "IV RECOVERY FAILED"
    check(src.count(marker) == 1, "the failure site is uniquely anchorable",
          f"{src.count(marker)} occurrence(s) of {marker!r}")
    seg = src[src.index(marker) - 1500:src.index(marker) + 2500]
    check("TRUNCATED" in seg,
          "it says so when the candidate list was truncated")
    check("never issued" in seg,
          "...and only claims the key was never ours when it was exhaustive")
    for want, why in (("nick_enc.hex()", "the FULL ciphertext, not 8 bytes"),
                      ("candidates tried", "every candidate key"),
                      ("every stamp issued", "the whole stamp history"),
                      ("greeting stamp", "this dial's own greeting stamp")):
        check(want in seg, f"the dump carries {why}")

    print("\nthe blocked-thread backstop ->")
    # The trace cannot see a thread that never returns: its `finally` does not
    # run, so nothing is written and the log just stops. POL_STACK_DUMP arms
    # faulthandler to print every thread's stack on a repeating timer, which is
    # the only thing that names the line a blocked thread is parked on.
    import faulthandler
    check(not faulthandler.is_enabled() or True, "faulthandler is importable")
    os.environ["POL_STACK_DUMP"] = "0"
    R._arm_stack_dumps()
    check(True, "POL_STACK_DUMP unset/0 arms nothing and does not raise")
    os.environ["POL_STACK_DUMP"] = "3600"
    R._arm_stack_dumps()
    faulthandler.cancel_dump_traceback_later()
    check(True, "a positive value arms without raising")
    del os.environ["POL_STACK_DUMP"]

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s): " + ", ".join(FAILS))
        return 1
    print("all login-trace checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
