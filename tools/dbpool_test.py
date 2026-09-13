"""Does the account-DB connection pool reuse connections WITHOUT changing anything?

`accounts.connect()` hands out pooled connections and `close()` returns them to a
free list (see the pool note in `accounts.py`). That is a performance fix -- this
server measured much slower than SE's original on almost everything before it --
and the whole constraint on it is **do not change behaviour
while optimizing**. This pins the three ways a pool silently could:

  1. **The journal mode is untouched.** WAL on the
     Windows DEV bind mount truncated the database, so `JOURNAL_MODE` is TRUNCATE
     and must stay TRUNCATE after a pooled round-trip. A pool that quietly
     converted the file would reintroduce the one failure this project has
     actually lost data to. (Prod is an Ubuntu VM on an ordinary filesystem,
     where WAL is fine -- but the mode is shared config, so dev decides it.)
  2. **A returned connection carries nothing forward.** An unfinished read
     transaction parked on an idle connection blocks the next writer with no
     statement to blame; an uncommitted WRITE parked there would surface later as
     a phantom row. `_pool_put` rolls back, and this checks both directions.
  3. **Threads do not share a checked-out connection.** The lobby is a thread per
     connection, which is why the pool exists at all and why it opens with
     `check_same_thread=False`. Concurrent workers must each get their OWN
     connection while they hold it, and must all still see one consistent
     database.

Plus the plain thing: that it actually pools (a second connect reuses the first),
that `POL_DB_POOL=0` really disables it, and that `pool_drain()` lets a caller get
the file closed when it needs the file rather than the speed.
"""
import os
import sqlite3
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "services"))

DB = os.path.join(tempfile.mkdtemp(prefix="dbpool-"), "accounts.db")
os.environ["POL_ACCOUNTS_DB"] = DB
# FORCED ON, BEFORE `accounts` IS IMPORTED. `_POOL_MAX` is read from the
# environment at import time, so anyone running the suite with
# POL_DB_POOL=0 -- the kill switch, and a perfectly reasonable thing to be
# testing -- would otherwise see this file report "the pool does not pool" as a
# FAILURE. A suite whose verdict depends on the ambient environment is a suite
# that lies. The kill switch is still exercised, at the bottom, by setting
# `_POOL_MAX` directly.
os.environ["POL_DB_POOL"] = "8"

import accounts                                                    # noqa: E402

FAILS = []


def check(ok, label, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  --  {detail}"
                                                       if detail else ""))
    if not ok:
        FAILS.append(label)


def main():
    print("pool basics ->")
    accounts.pool_drain()
    a = accounts.connect(DB)
    ident = id(a)
    a.close()
    b = accounts.connect(DB)
    check(id(b) == ident, "close() returns the connection to the pool",
          f"{ident} vs {id(b)}")
    # While A is CHECKED OUT, a second connect must not hand out the same one.
    c = accounts.connect(DB)
    check(id(c) != id(b), "a checked-out connection is not handed out twice")
    b.close()
    c.close()
    check(sum(accounts.pool_stats().values()) >= 2,
          "both are idle in the pool afterwards",
          repr(accounts.pool_stats()))

    print("\nthe journal mode is untouched ->")
    conn = accounts.connect(DB)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
    conn.close()
    check(mode == accounts.JOURNAL_MODE.lower(),
          f"still {accounts.JOURNAL_MODE.lower()} after a pooled round-trip",
          f"is {mode!r} -- see accounts-db-wal-hazard")

    print("\nnothing carries forward across a close ->")
    conn = accounts.connect(DB)
    conn.execute("CREATE TABLE IF NOT EXISTS pooltest (v INTEGER)")
    conn.commit()
    conn.close()
    # An UNCOMMITTED write must not survive being parked.
    conn = accounts.connect(DB)
    conn.execute("INSERT INTO pooltest (v) VALUES (1)")
    conn.close()                                    # no commit -- must roll back
    conn = accounts.connect(DB)
    n = conn.execute("SELECT count(*) FROM pooltest").fetchone()[0]
    conn.close()
    check(n == 0, "an uncommitted write is rolled back, not parked", f"{n} row(s)")
    # A committed one must, obviously, survive.
    conn = accounts.connect(DB)
    conn.execute("INSERT INTO pooltest (v) VALUES (2)")
    conn.commit()
    conn.close()
    conn = accounts.connect(DB)
    n = conn.execute("SELECT count(*) FROM pooltest").fetchone()[0]
    conn.close()
    check(n == 1, "a committed write still lands", f"{n} row(s)")
    # An UNDRAINED cursor is the deadlock case: the read transaction it leaves
    # open must not ride the connection back into the pool.
    conn = accounts.connect(DB)
    conn.execute("SELECT * FROM pooltest")          # deliberately not drained
    conn.close()
    writer = accounts.connect(DB)
    try:
        writer.execute("INSERT INTO pooltest (v) VALUES (3)")
        writer.commit()
        wrote = True
    except sqlite3.OperationalError as exc:
        wrote, detail = False, repr(exc)
    else:
        detail = ""
    writer.execute("DELETE FROM pooltest WHERE v = 3")
    writer.commit()
    writer.close()
    check(wrote, "an undrained cursor does not block the next writer", detail)

    print("\nconcurrency (the lobby is a thread per connection) ->")
    N = 8
    seen, errors = [], []
    lock = threading.Lock()
    # A BARRIER, NOT A SLEEP AND NOT JUST `start()` BEFORE `join()`. Without one
    # the workers merely INTERLEAVE: an early thread finishes and returns its
    # connection before a later one checks out, so the pool legitimately hands
    # the same object to both -- 8 checkouts, 4 objects -- and the run proves
    # nothing about exclusivity. The barrier makes all eight hold at once, which
    # is the only arrangement under which "each got its own" is a real claim.
    gate = threading.Barrier(N, timeout=10)

    def worker(_i):
        try:
            db = accounts.connect(DB)
            db.execute("SELECT count(*) FROM pooltest").fetchone()
            with lock:
                seen.append(id(db))
            gate.wait()                             # everybody holds one NOW
            db.execute("SELECT count(*) FROM handle").fetchone()
            db.close()
        except Exception as exc:                    # noqa: BLE001 -- report it
            with lock:
                errors.append(repr(exc))
            try:
                gate.abort()
            except Exception:
                pass

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    check(not errors, f"{N} concurrent workers all completed",
          "; ".join(errors[:3]))
    check(len(set(seen)) == len(seen),
          "each held its OWN connection while it held one",
          f"{len(seen)} checkouts, {len(set(seen))} distinct")

    print("\nthe kill switch and the drain ->")
    freed = accounts.pool_drain()
    check(freed > 0 and not sum(accounts.pool_stats().values()),
          "pool_drain() really closes the idle connections", f"{freed} closed")
    saved = accounts._POOL_MAX
    try:
        accounts._POOL_MAX = 0                      # what POL_DB_POOL=0 gives
        d = accounts.connect(DB)
        first = id(d)
        d.close()
        e = accounts.connect(DB)
        reused = id(e) == first
        e.close()
        check(not reused and not sum(accounts.pool_stats().values()),
              "POL_DB_POOL=0 closes for real and pools nothing")
    finally:
        accounts._POOL_MAX = saved

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s): " + ", ".join(FAILS))
        return 1
    print("all pool checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
