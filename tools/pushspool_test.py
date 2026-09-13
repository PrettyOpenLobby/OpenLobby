#!/usr/bin/env python3
"""Prove the push spool is drained ONCE, and cannot grow forever.

    python pushspool_test.py

THE BUG THIS PINS. `_push_spool_watcher` kept its byte offset in a local, so it
started every process at 0 and re-delivered the entire spool on each restart.
Found 2026-08-17 by restarting authsess and watching 315 records replay out of a
353-record file that nothing had ever trimmed.

It presented as harmless -- every replayed record was dropped, because no session
had registered yet -- and that is the trap. It inverts the point of `authrelay`,
which exists precisely so a client KEEPS its socket across an authsess restart.
In the case the relay is built for, re-registration races the drain, and a client
that wins the race is handed the whole accumulated push history at once: stale
friend rows, stale events, replayed as though they had just happened. The 315
drops were luck, not design.

So the property under test is "a record is delivered exactly once, across a
restart" -- and the restart is simulated by running the drain loop's logic twice
over the same files, which is what a new process does.
"""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))

LOG_DIR = tempfile.mkdtemp(prefix="spool-")
os.environ["POL_LOG_DIR"] = LOG_DIR
os.environ["POL_PUSH_SPOOL_MAX_MB"] = "0.001"        # 1024 bytes

import responders as R                                # noqa: E402

ok = True
delivered = []


def check(label, got, want):
    global ok
    good = got == want
    ok = good and ok
    print(f"  {'OK  ' if good else 'FAIL'} {label}: {got!r}"
          + ("" if good else f"  (want {want!r})"))


R._push_deliver = lambda rec: delivered.append(rec)   # capture, do not send


def spool(n):
    with open(R._PUSH_SPOOL, "a", encoding="utf-8") as f:
        for i in range(n):
            f.write(json.dumps({"kind": "rows", "seq": i}) + "\n")


def drain_once():
    """One pass of the watcher's body -- i.e. what a fresh process does."""
    off = R._push_offset_load()
    try:
        if off > os.path.getsize(R._PUSH_SPOOL):
            off = 0
    except OSError:
        off = 0
    size = os.path.getsize(R._PUSH_SPOOL)
    if size < off:
        off = 0
    if size > off:
        # BINARY, mirroring the watcher exactly -- a text-mode read translates
        # newlines and the byte offset then drifts one byte per line. That is not
        # hypothetical: it is what this test hit on Windows before the watcher
        # was changed to read bytes, and it is the reason the mirror is here
        # rather than the test calling the loop directly.
        with open(R._PUSH_SPOOL, "rb") as f:
            f.seek(off)
            chunk = f.read()
        cut = chunk.rfind(b"\n") + 1
        if cut:
            off += cut
            for raw in chunk[:cut].split(b"\n"):
                if raw.strip():
                    R._push_deliver(json.loads(raw.decode("utf-8")))
            R._push_offset_save(off)
    return R._push_spool_rotate(off)


print("a record is delivered once, and a restart does not replay it")

spool(5)
drain_once()
check("the first drain delivers everything", len(delivered), 5)

drain_once()
check("a second pass in the SAME process delivers nothing new", len(delivered), 5)

# THE REGRESSION ITSELF: a fresh process must resume, not restart. Before the
# fix this returned 10 -- the whole spool, a second time.
delivered.clear()
drain_once()
check("a RESTART replays nothing", len(delivered), 0)

spool(3)
drain_once()
check("...but new records after the restart still arrive", len(delivered), 3)

print("\nthe offset survives nonsense without going deaf")

# An offset past the end means the spool was replaced while we were down. The
# reader must start over, not seek past EOF and silently stop delivering.
R._push_offset_save(10 ** 9)
delivered.clear()
drain_once()
check("an offset past the end restarts from 0 rather than going deaf",
      len(delivered) > 0, True)

# A shrinking file is a rotation someone else did.
with open(R._PUSH_SPOOL, "w", encoding="utf-8") as f:
    f.write(json.dumps({"kind": "rows", "seq": 99}) + "\n")
delivered.clear()
drain_once()
check("a shrunken spool is re-read from the start", len(delivered), 1)

print("\nthe spool cannot grow forever")

# Rotation is deliberately conservative: fully drained AND over the cap AND
# idle. The idle test is what makes the rename safe against the OTHER
# container's appender, so assert it holds the rotation back.
spool(60)                                              # well past 1024 bytes
drain_once()
check("a big, freshly-written spool is NOT rotated (still warm)",
      os.path.exists(R._PUSH_SPOOL + ".1"), False)

old = time.time() - (R._PUSH_SPOOL_IDLE + 5)
os.utime(R._PUSH_SPOOL, (old, old))
drain_once()
check("once drained, oversized and idle, it rotates",
      os.path.exists(R._PUSH_SPOOL + ".1"), True)
check("and the offset resets with it", R._push_offset_load(), 0)

# The generation kept must be the real content -- a spool is the only record of
# what was pushed and when.
kept = sum(1 for _ in open(R._PUSH_SPOOL + ".1", encoding="utf-8"))
check("the retired generation still holds its records", kept > 0, True)

delivered.clear()
spool(2)
drain_once()
check("draining continues into the fresh spool", len(delivered), 2)

print("\na row push whose watcher has not registered yet is HELD, not dropped")

# THE RACE THIS COVERS. The row push is issued as the 2:3 friend list is
# composed, with only a 1.5s delay, and the watcher's session registers on its
# own schedule. Lose that race and the friend keeps their old face picture --
# the icon does NOT travel in the 2:3 record, so this push is its only carrier.
import responders as _R

sent = []


class _FakeSession:
    """Just enough session for the row push: it addresses lines to `nick` and
    hands them to `send`."""
    alive = True
    nick = b"UTESTNICK"

    def send(self, lines):
        sent.extend(lines)
        return True


_R._PUSH_DEFER[:] = []
_R._PUSH_DEFER_WINDOW = 5.0
rec = {"kind": "rows", "member": 4242, "rows": [[0, 8796093022210, 7, None]],
       "after": 0}

_R._push_deliver_rows(dict(rec))
check("with no session, the record is HELD rather than dropped",
      len(_R._PUSH_DEFER), 1)
check("and nothing was sent", len(sent), 0)

# A retry while the session is STILL absent must keep holding, not give up.
_R._push_defer_retry()
check("a retry with still no session keeps holding", len(_R._PUSH_DEFER), 1)

# ...and once the session registers, the very next tick delivers it.
_R.PRESENCE._by_member.setdefault(4242, []).append(_FakeSession())
_R._push_defer_retry()
check("once the session registers, the held rows go out", len(sent) > 0, True)
check("and the queue is empty again", len(_R._PUSH_DEFER), 0)

# EXPIRY. A member who really is gone must not be held forever.
_R.PRESENCE._by_member.pop(4242, None)
expired = dict(rec)
expired["defer_until"] = time.time() - 1
_R._push_deliver_rows(expired)
check("a record past its window is dropped, not re-held",
      len(_R._PUSH_DEFER), 0)

# The cap is the backstop against a flood for members who are really gone.
_R._PUSH_DEFER_WINDOW = 60.0
for i in range(_R._PUSH_DEFER_MAX + 10):
    _R._push_deliver_rows({"kind": "rows", "member": 5000 + i,
                           "rows": [[0, 1, 1, None]], "after": 0})
check("the defer queue is capped", len(_R._PUSH_DEFER), _R._PUSH_DEFER_MAX)
_R._PUSH_DEFER[:] = []

print()
print("pushspool_test: OK" if ok else "pushspool_test: FAILED")
sys.exit(0 if ok else 1)
