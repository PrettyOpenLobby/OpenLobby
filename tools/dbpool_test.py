"""Does the account connection keep the contract the account code was written to?

`accounts.connect()` returns a `polcore.db.CompatConnection`: the shape of a
sqlite3 connection over a PostgreSQL pool. Every function in accounts.py, and
every caller of them, was written against sqlite3's rules, so the move to
PostgreSQL is only safe while those rules still hold. This pins them, and the
pool behaviour the SQLite version of this suite pinned:

  1. **A returned connection carries nothing forward.** An uncommitted WRITE
     parked on a pooled connection would surface later as a phantom row, and an
     undrained read must not hold anything the next writer waits on. close()
     rolls back, and this checks both directions.
  2. **A connection is held only while a transaction is open.** Between
     statements it goes back to the pool, so a handler that keeps its
     connection object for a whole request (46 call sites in the lobby do) does
     not keep a pool slot, and one nested inside another cannot run it dry.
  3. **Threads do not share a checked-out connection.** The lobby is a thread
     per client. Concurrent writers must each hold their OWN server connection
     while their transaction is open, and all see one consistent database.
  4. **sqlite3's transaction rules.** The first write opens a transaction that
     lasts until commit(); `with conn:` commits or rolls back and does not close;
     a statement that fails inside a transaction undoes only itself (SQLite's
     rule; PostgreSQL alone would abort the whole transaction, and code that
     catches an IntegrityError and carries on would fail on its next line).
  5. **Types as SQLite took them**: an int compared with a TEXT column, a bool
     stored in an INTEGER flag, a SUM that comes back as an int.
  6. **A lock wait ends.** SQLite gave up after its 10 s busy timeout; a writer
     blocked behind a forgotten transaction must fail after POL_DB_LOCK_TIMEOUT
     instead of hanging for ever.

Plus the plain things: rows read by position and by name, and `pool_drain()`
really closes the idle connections.

Needs PostgreSQL (tools/pgtest.py).
"""
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "services"))
sys.path.insert(0, HERE)

# Short, so section 6 does not take ten seconds. Read when polcore.db is
# imported, hence before it.
os.environ["POL_DB_LOCK_TIMEOUT"] = "1s"
os.environ["POL_DB_POOL"] = "8"

import pgtest                                                      # noqa: E402

pgtest.use_fresh_database()

import accounts                                                    # noqa: E402
from polcore import db as pdb                                      # noqa: E402

FAILS = []


def check(ok, label, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  --  {detail}"
                                                       if detail else ""))
    if not ok:
        FAILS.append(label)


def main():
    print("pool basics ->")
    accounts.pool_drain()
    a = accounts.connect()
    check(a._pg is None, "an idle connection holds no pool slot")
    a.execute("SELECT 1").fetchone()
    check(a._pg is None, "...nor after a read")
    a.execute("CREATE TABLE pooltest (v INTEGER, s TEXT, flag INTEGER)")
    check(a._pg is None, "...nor after DDL (it runs on its own, as in sqlite3)")
    a.execute("INSERT INTO pooltest (v) VALUES (0)")
    check(a._pg is not None and a.in_transaction,
          "a write opens a transaction and holds a connection for it")
    a.commit()
    check(a._pg is None and not a.in_transaction,
          "commit() ends the transaction and hands the connection back")
    a.execute("DELETE FROM pooltest")
    a.commit()
    a.close()
    check(sum(accounts.pool_stats().values()) >= 1,
          "the pool holds the connection afterwards", repr(accounts.pool_stats()))

    print("\nnothing carries forward across a close ->")
    conn = accounts.connect()
    conn.execute("INSERT INTO pooltest (v) VALUES (1)")
    conn.close()                                    # no commit -- must roll back
    conn = accounts.connect()
    n = conn.execute("SELECT count(*) FROM pooltest").fetchone()[0]
    conn.close()
    check(n == 0, "an uncommitted write is rolled back, not parked", f"{n} row(s)")
    conn = accounts.connect()
    conn.execute("INSERT INTO pooltest (v) VALUES (2)")
    conn.commit()
    conn.close()
    conn = accounts.connect()
    n = conn.execute("SELECT count(*) FROM pooltest").fetchone()[0]
    conn.close()
    check(n == 1, "a committed write still lands", f"{n} row(s)")
    conn = accounts.connect()
    conn.execute("SELECT * FROM pooltest")          # deliberately not drained
    conn.close()
    writer = accounts.connect()
    try:
        writer.execute("UPDATE pooltest SET v = 3 WHERE v = 2")
        writer.commit()
        wrote, detail = True, ""
    except pdb.Error as exc:
        wrote, detail = False, repr(exc)
    writer.execute("UPDATE pooltest SET v = 2 WHERE v = 3")
    writer.commit()
    writer.close()
    check(wrote, "an undrained cursor does not block the next writer", detail)
    # A connection DROPPED with a transaction open (no close, no commit) gives
    # its server connection back when it is collected, rolled back.
    before = pdb.get_pool().get_stats().get("pool_available", 0)
    lost = accounts.connect()
    lost.execute("INSERT INTO pooltest (v) VALUES (99)")
    del lost
    after = pdb.get_pool().get_stats().get("pool_available", 0)
    conn = accounts.connect()
    n = conn.execute("SELECT count(*) FROM pooltest WHERE v = 99").fetchone()[0]
    conn.close()
    check(n == 0 and after >= before,
          "a dropped connection's transaction is rolled back and its slot returned",
          f"{n} row(s); available {before} -> {after}")

    print("\nconcurrency (the lobby is a thread per connection) ->")
    N = 6
    pids, errors = [], []
    lock = threading.Lock()
    # A BARRIER, so all six hold a transaction at once; otherwise they merely
    # interleave and "each got its own" proves nothing.
    gate = threading.Barrier(N, timeout=10)

    def worker(i):
        try:
            db = accounts.connect()
            db.execute("INSERT INTO pooltest (v) VALUES (%s)", (100 + i,))
            pid = db.execute("SELECT pg_backend_pid()").fetchone()[0]
            with lock:
                pids.append(pid)
            gate.wait()                             # everybody holds one NOW
            db.commit()
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
    check(not errors, f"{N} concurrent writers all completed", "; ".join(errors[:3]))
    check(len(set(pids)) == len(pids) == N,
          "each held its OWN server connection while its transaction was open",
          f"{len(pids)} transactions, {len(set(pids))} backends")
    conn = accounts.connect()
    n = conn.execute("SELECT count(*) FROM pooltest WHERE v >= 100").fetchone()[0]
    conn.execute("DELETE FROM pooltest WHERE v >= 100")
    conn.commit()
    conn.close()
    check(n == N, "and every one of their writes landed", f"{n} row(s)")

    print("\nsqlite3's transaction rules ->")
    conn = accounts.connect()
    with conn:
        conn.execute("INSERT INTO pooltest (v) VALUES (10)")
    check(not conn.in_transaction, "`with conn:` commits on the way out")
    try:
        with conn:
            conn.execute("INSERT INTO pooltest (v) VALUES (11)")
            raise KeyError("boom")
    except KeyError:
        pass
    got = sorted(r[0] for r in conn.execute(
        "SELECT v FROM pooltest WHERE v IN (10, 11)"))
    check(got == [10], "...and rolls back on an exception", repr(got))
    conn.execute("CREATE UNIQUE INDEX pooltest_v ON pooltest (v)")
    conn.execute("INSERT INTO pooltest (v) VALUES (20)")
    try:
        conn.execute("INSERT INTO pooltest (v) VALUES (20)")
        dup = False
    except pdb.IntegrityError:
        dup = True
    check(dup, "a duplicate raises IntegrityError")
    conn.execute("INSERT INTO pooltest (v) VALUES (21)")
    conn.commit()
    got = sorted(r["v"] for r in conn.execute(
        "SELECT v FROM pooltest WHERE v IN (20, 21)"))
    check(got == [20, 21],
          "a failed statement undoes only itself; the transaction goes on",
          repr(got))
    conn.close()

    print("\ntypes as SQLite took them ->")
    conn = accounts.connect()
    conn.execute("INSERT INTO pooltest (v, s, flag) VALUES (%s, %s, %s)",
                 (30, "7", True))
    conn.commit()
    row = conn.execute("SELECT v, s, flag FROM pooltest WHERE s = %s", (7,)).fetchone()
    check(row is not None and row["flag"] == 1 and row[0] == 30,
          "an int matches a TEXT column and a bool lands in an INTEGER flag",
          repr(row))
    total = conn.execute("SELECT SUM(v::bigint) FROM pooltest WHERE v = 30").fetchone()[0]
    check(total == 30 and type(total) is int, "a SUM over a bigint is an int",
          repr(total))
    check(dict(row) == {"v": 30, "s": "7", "flag": 1} and row.keys() == ["v", "s", "flag"]
          and tuple(row) == (30, "7", 1) and row["V"] == 30,
          "rows read by position, by name (any case), as a dict and as a tuple")
    conn.close()

    print("\na lock wait ends ->")
    holder = accounts.connect()
    holder.execute("UPDATE pooltest SET s = 'held' WHERE v = 30")   # not committed
    waiter = accounts.connect()
    t0 = time.monotonic()
    try:
        waiter.execute("UPDATE pooltest SET s = 'waited' WHERE v = 30")
        timed_out = False
    except pdb.OperationalError:
        timed_out = True
    waited = time.monotonic() - t0
    waiter.rollback()
    holder.rollback()
    check(timed_out and waited < 5,
          "a writer blocked by an open transaction fails instead of hanging",
          f"after {waited:.1f}s")

    print("\nthe drain ->")
    freed = accounts.pool_drain()
    check(freed > 0 and not sum(accounts.pool_stats().values()),
          "pool_drain() really closes the idle connections", f"{freed} closed")
    conn = accounts.connect()
    check(conn.execute("SELECT count(*) FROM pooltest").fetchone()[0] > 0,
          "and the next connect opens a fresh pool")
    conn.close()

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s): " + ", ".join(FAILS))
        return 1
    print("all pool checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
