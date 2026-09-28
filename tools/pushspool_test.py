#!/usr/bin/env python3
"""Prove the push queue delivers each record, in order, and never twice.

    python pushspool_test.py

THE BUG THIS PINS. The old file spool's drain kept its byte offset in a local,
so it started every process at 0 and re-delivered the entire spool on each
restart. Found 2026-08-17 by restarting authsess and watching 315 records
replay out of a 353-record file that nothing had ever trimmed.

It presented as harmless -- every replayed record was dropped, because no session
had registered yet -- and that is the trap. It inverts the point of `authrelay`,
which exists precisely so a client KEEPS its socket across an authsess restart.
In the case the relay is built for, re-registration races the drain, and a client
that wins the race is handed the whole accumulated push history at once: stale
friend rows, stale events, replayed as though they had just happened. The 315
drops were luck, not design.

The spool is now a queue in the live-state store (polcore.kv), and the
properties under test are the ones the file had to be engineered for:

  * a record is delivered once, and a restart (a fresh process running the
    watcher's start-up recovery) replays nothing already delivered;
  * a record taken by a consumer that died before delivering it IS delivered
    by the next one (at least once), and only once;
  * order is kept, a malformed record cannot wedge the queue, and nothing is
    left behind once it is drained.

They run against the in-process store and against a real Valkey, and on Valkey
the lobby half runs in ANOTHER PROCESS, which is the hop the queue exists for.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.join(HERE, "..", "services")
sys.path.insert(0, SERVICES)
sys.path.insert(0, HERE)

LOG_DIR = tempfile.mkdtemp(prefix="spool-")
os.environ["POL_LOG_DIR"] = LOG_DIR
os.environ["POL_DATA_DIR"] = LOG_DIR

import pgtest                                         # noqa: E402
import responders as R                                # noqa: E402
from polcore import kv                                # noqa: E402

ok = True
delivered = []


def check(label, got, want):
    global ok
    good = got == want
    ok = good and ok
    print(f"  {'OK  ' if good else 'FAIL'} {label}: {got!r}"
          + ("" if good else f"  (want {want!r})"))


R._push_deliver = lambda rec: delivered.append(rec)   # capture, do not send
R._PUSH_LOCAL[0] = False                              # we are the lobby


def spool(n, start=0):
    """Queue n records the way the lobby does (`_push_emit`). A group MODE
    record, because its switch is on by default."""
    for i in range(start, start + n):
        R._push_emit({"kind": "gmode", "gid": 1, "member": 1, "seq": i})


def drain():
    """What the watcher does while running: take, deliver, acknowledge."""
    while R._push_spool_drain_one() is not None:
        pass


def seqs():
    return [r.get("seq") for r in delivered]


def queue_suite():
    R._push_spool_clear()
    delivered.clear()
    print(" a record is delivered once, and a restart does not replay it")
    spool(5)
    check("the lobby's pushes are queued", kv.llen(R._PUSH_QUEUE), 5)
    drain()
    check("the first drain delivers everything, in order", seqs(), [0, 1, 2, 3, 4])
    drain()
    check("a second pass in the SAME process delivers nothing new", len(delivered), 5)

    # THE REGRESSION ITSELF: a fresh process must resume, not restart.
    delivered.clear()
    check("a RESTART replays nothing", (R._push_spool_recover(), len(delivered)),
          (0, 0))
    spool(3, start=5)
    drain()
    check("...but new records after the restart still arrive", seqs(), [5, 6, 7])

    print(" a consumer that dies mid-delivery loses nothing")
    delivered.clear()
    spool(2, start=8)
    # take one the way the watcher does, then "die" before delivering it
    taken = kv.move(R._PUSH_QUEUE, R._PUSH_WORK)
    check("the taken record is held on the work list", kv.llen(R._PUSH_WORK), 1)
    check("the next process delivers it at start-up", R._push_spool_recover(), 1)
    check("...exactly the record that was in flight",
          [json.loads(taken)["seq"]], seqs())
    check("...and the work list is empty after", kv.llen(R._PUSH_WORK), 0)
    drain()
    check("the rest of the queue follows", seqs(), [8, 9])
    check("a second restart replays none of it", R._push_spool_recover(), 0)

    print(" junk cannot wedge the queue, and nothing is left behind")
    delivered.clear()
    kv.push(R._PUSH_QUEUE, "not json")
    spool(1, start=10)
    drain()
    check("a malformed record is dropped and the next one still arrives",
          seqs(), [10])
    check("the queue is empty once drained", kv.llen(R._PUSH_QUEUE), 0)
    check("so is the work list", kv.llen(R._PUSH_WORK), 0)

    print(" the lobby's push returns 0: queued, not delivered")
    check("a queued push reports no delivery",
          R._push_emit({"kind": "gmode", "gid": 1, "member": 1, "seq": 11}), 0)
    check("...and is waiting for authserv",
          [r["seq"] for r in R._push_spool_pending()], [11])
    R._push_spool_clear()


print("in-process store")
kv.reset(kv.MemoryKV(prefix="spooltest:"))
queue_suite()

print("\nValkey")
pgtest.use_fresh_valkey()
check("the live-state store is Valkey", kv.default().backend, "valkey")
queue_suite()

print(" the lobby in ANOTHER process, authserv here")
delivered.clear()
LOBBY = (
    "import sys; sys.path.insert(0, %r)\n"
    "import responders as R\n"
    "R._PUSH_LOCAL[0] = False\n"
    "for i in range(20):\n"
    "    R._push_emit({'kind': 'gmode', 'gid': 2, 'member': 2, 'seq': i})\n"
    % os.path.abspath(SERVICES))
proc = subprocess.run([sys.executable, "-c", LOBBY], env=dict(os.environ),
                      capture_output=True, text=True, timeout=120)
check("the lobby process ran", proc.returncode, 0)
if proc.returncode:
    print(proc.stdout[-2000:], proc.stderr[-2000:])
deadline = time.time() + 10
while len(delivered) < 20 and time.time() < deadline:
    R._push_spool_drain_one(timeout=0.5)
check("every push crossed the process boundary, in order", seqs(), list(range(20)))
check("and the queue is empty", kv.llen(R._PUSH_QUEUE), 0)
R._push_spool_clear()

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
