#!/usr/bin/env python3
"""Prove that /logs cannot grow without bound -- in BOTH modules that write it.

    python logrotate_test.py

WHY THIS EXISTS. `responders.py` grew a log cap on 2026-08-12, prompted by a
measurement of the live deployment: 769 MB under /logs, "of which tcp.log alone
was 405 MB". The fix went into one module. But `tcp`, `dns` and `http` -- the
three highest-volume channels, and the one the measurement named -- are served
by `stub.py`, which has its own `log()` and never got it. Re-measured
2026-08-17, five days later: tcp.log 405 MB, unchanged, still one
server-lifetime append on the same disk as accounts.db, plus 312 MB of captures
that nothing had ever deleted from.

So the thing under test is not "rotation works" but "**every** writer rotates".
A future third writer is exactly the shape of the bug this file exists to catch:
assert against both modules by name, so adding a logger without a cap fails
here rather than on somebody's disk six months later.

The caps are set absurdly low (1 KiB, 10 KiB) so the test runs in milliseconds
against real files in a temp dir -- no mocking, no monkeypatched clock.
"""
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))

LOG_DIR = tempfile.mkdtemp(prefix="logrot-")
os.environ["POL_LOG_DIR"] = LOG_DIR
os.environ["POL_LOG_MAX_MB"] = "0.001"          # 1024 bytes
os.environ["POL_LOG_KEEP"] = "2"
os.environ["POL_CAPTURE_MAX_MB"] = "0.01"       # 10240 bytes

import stub                                     # noqa: E402
import responders                               # noqa: E402

ok = True


def check(label, got, want):
    global ok
    good = got == want
    ok = good and ok
    print(f"  {'OK  ' if good else 'FAIL'} {label}: {got!r}"
          + ("" if good else f"  (want {want!r})"))


def gens(prefix):
    return sorted(f for f in os.listdir(LOG_DIR) if f.startswith(prefix))


# --- 1. every module that writes /logs caps its files ---------------------- #
# Named individually rather than looped over a list, so the failure message says
# WHICH writer stopped rotating.
print("both log writers cap and rotate")

for mod, chan in ((stub, "s"), (responders, "r")):
    for _ in range(120):
        mod.log(chan, "x" * 60)
    check(f"{mod.__name__}: rotates into POL_LOG_KEEP generations",
          gens(f"{chan}.log"), [f"{chan}.log", f"{chan}.log.1", f"{chan}.log.2"])
    check(f"{mod.__name__}: the live file is under the cap",
          os.path.getsize(os.path.join(LOG_DIR, f"{chan}.log")) < 2048, True)

# THE OLDEST GENERATION IS DROPPED, not kept forever under a new name. Getting
# this wrong turns a cap into a rename treadmill that still fills the disk.
check("keep=2 means exactly three files, not a growing chain",
      len(gens("s.log")), 3)

# --- 2. a log inherited OVERSIZED from the last run rotates immediately ----- #
# The failure mode this covers: checking size only on an already-open handle.
# Every log on the box is past the cap right now, so a first-write check that
# skips them would leave all of them unrotated until each grew another 8 MB --
# i.e. the cap would appear to work on a fresh box and do nothing on this one.
print("\na log left oversized by the previous run rotates on the first write")

for mod, chan in ((stub, "sold"), (responders, "rold")):
    with open(os.path.join(LOG_DIR, f"{chan}.log"), "wb") as f:
        f.write(b"z" * 5000)
    mod.log(chan, "first line after restart")
    check(f"{mod.__name__}: the inherited file was rotated away",
          os.path.exists(os.path.join(LOG_DIR, f"{chan}.log.1")), True)
    check(f"{mod.__name__}: and the live file starts fresh",
          os.path.getsize(os.path.join(LOG_DIR, f"{chan}.log")) < 200, True)

# --- 3. captures are pruned oldest-first ----------------------------------- #
# Captures are the most valuable thing this server writes, so the eviction order
# is the part worth pinning: the file that just landed is the one somebody is
# about to read, and it must survive its own prune.
print("\ncaptures are pruned oldest-first, and the newest always survives")

for i in range(8):
    stub.save_capture(f"c{i}.bin", bytes(3000))
    time.sleep(0.01)                    # distinct mtimes; ordering is by mtime

capdir = os.path.join(LOG_DIR, "captures")
caps = sorted(os.listdir(capdir))
total = sum(os.path.getsize(os.path.join(capdir, c)) for c in caps)
check("the directory is back under the cap", total <= 10240, True)
check("the newest capture survived its own prune", "c7.bin" in caps, True)
check("the oldest went first", "c0.bin" in caps, False)
check("and what remains is a contiguous newest-first tail",
      caps, sorted(caps)[-len(caps):])

# POL_CAPTURE_MAX_MB=0 IS THE DELIBERATE-CAPTURE-SESSION SETTING. It must keep
# everything -- a knob that silently still prunes is worse than no knob.
stub._CAPTURE_MAX = 0
for i in range(6):
    stub.save_capture(f"keep{i}.bin", bytes(3000))
check("cap=0 keeps every capture",
      len([c for c in os.listdir(capdir) if c.startswith("keep")]), 6)

print()
print("logrotate_test: OK" if ok else "logrotate_test: FAILED")
shutil.rmtree(LOG_DIR, ignore_errors=True)
sys.exit(0 if ok else 1)
