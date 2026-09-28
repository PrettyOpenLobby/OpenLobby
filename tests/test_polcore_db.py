#!/usr/bin/env python3
"""polcore.db against a real PostgreSQL: pool, transactions, advisory locks,
upsert and the migration runner.

    python tests/test_polcore_db.py

Needs psycopg 3 and either Docker (a throwaway postgres:17-alpine, see
tools/pgtest.py) or POL_TEST_DATABASE_URL. Without both it reports SKIP and
exits 0, unless POL_TEST_REQUIRE_DB=1, which turns the skip into a failure
(CI sets it so a missing database cannot pass as green).
"""
import os
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


def skip(why):
    if os.environ.get("POL_TEST_REQUIRE_DB") == "1":
        print("FAIL: no database for the test (%s) and POL_TEST_REQUIRE_DB=1" % why)
        sys.exit(1)
    print("SKIP: %s" % why)
    sys.exit(0)


try:
    import psycopg
    from polcore import db
except ImportError as exc:
    skip("psycopg is not installed (%s)" % exc)

import pgtest  # noqa: E402

if not os.environ.get("POL_TEST_DATABASE_URL") and not pgtest.docker_available():
    skip("no Docker daemon and POL_TEST_DATABASE_URL is unset")


def fresh(pool_size=None):
    """A new database, with polcore.db pointed at it."""
    cm = pgtest.database()
    url = cm.__enter__()
    db.configure(url, pool_size=pool_size)
    return cm


def done(cm):
    db.close()
    cm.__exit__(None, None, None)


# --------------------------------------------------------------------------- #
print("pool: bounded, shared by threads, dict rows")
cm = fresh(pool_size=3)
pids, errors = set(), []


def borrow():
    try:
        with db.connect() as c:
            pids.add(c.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])
            time.sleep(0.2)
    except Exception as exc:
        errors.append(exc)


threads = [threading.Thread(target=borrow) for _ in range(9)]
for t in threads:
    t.start()
for t in threads:
    t.join()
chk("nine borrowers, no errors", errors, [])
chk("never more than POL_DB_POOL connections", len(pids) <= 3, True)
chk("dict rows", db.query_one("SELECT 1 AS one"), {"one": 1})
chk("query_one on no rows", db.query_one("SELECT 1 WHERE false"), None)

# --------------------------------------------------------------------------- #
print("transaction(): commit, rollback on exception, read only")
db.execute("CREATE TABLE t (k TEXT PRIMARY KEY, n INTEGER NOT NULL)")
with db.transaction() as c:
    db.execute("INSERT INTO t VALUES ('a', 1)", conn=c)
chk("committed row visible", db.query("SELECT k, n FROM t"), [{"k": "a", "n": 1}])


class Boom(Exception):
    pass


try:
    with db.transaction() as c:
        db.execute("INSERT INTO t VALUES ('b', 2)", conn=c)
        chk("row visible inside its own transaction",
            db.query_one("SELECT n FROM t WHERE k = 'b'", conn=c), {"n": 2})
        raise Boom()
except Boom:
    pass
chk("rolled back after the exception", db.query_one("SELECT n FROM t WHERE k = 'b'"), None)

try:
    with db.transaction(write=False) as c:
        db.execute("INSERT INTO t VALUES ('c', 3)", conn=c)
    ro = "write allowed"
except psycopg.errors.ReadOnlySqlTransaction:
    ro = "refused"
chk("write inside write=False", ro, "refused")

try:
    with db.connect() as c:
        db.lock(c, "x")
    outside = "allowed"
except RuntimeError:
    outside = "refused"
chk("lock() outside a transaction", outside, "refused")

with db.transaction() as c:
    db.execute("INSERT INTO t VALUES ('d', 4)", conn=c)
    try:
        with db.transaction(conn=c):
            db.execute("INSERT INTO t VALUES ('e', 5)", conn=c)
            raise Boom()
    except Boom:
        pass
chk("nested transaction is a savepoint",
    [r["k"] for r in db.query("SELECT k FROM t WHERE k IN ('d','e') ORDER BY k")], ["d"])

# --------------------------------------------------------------------------- #
print("advisory lock serialises read-modify-write (with a negative control)")
db.execute("CREATE TABLE counter (id INTEGER PRIMARY KEY, n INTEGER NOT NULL)")


def bump(lock):
    with db.transaction(lock=lock) as c:
        n = db.query_one("SELECT n FROM counter WHERE id = 1", conn=c)["n"]
        time.sleep(0.3)
        db.execute("UPDATE counter SET n = %s WHERE id = 1", (n + 1,), conn=c)


def race(lock):
    db.execute("DELETE FROM counter")
    db.execute("INSERT INTO counter VALUES (1, 0)")
    ts = [threading.Thread(target=bump, args=(lock,)) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return db.query_one("SELECT n FROM counter WHERE id = 1")["n"]


chk("without a lock the second update is lost (control)", race(None), 1)
chk("with lock='counter' both updates land", race("counter"), 2)
chk("lock_key is stable and signed 64-bit",
    (db.lock_key("counter") == db.lock_key("counter"),
     -2**63 <= db.lock_key("counter") < 2**63), (True, True))

# --------------------------------------------------------------------------- #
print("upsert")
db.execute("CREATE TABLE u (a INTEGER, b TEXT, c TEXT, v BYTEA,"
           " PRIMARY KEY (a, b))")
chk("insert", db.upsert("u", {"a": 1, "b": "x", "c": "one", "v": b"\x00\xff"},
                        key=("a", "b")), 1)
chk("conflict updates the other columns",
    db.upsert("u", {"a": 1, "b": "x", "c": "two", "v": b"\x01"}, key=("a", "b"),
              returning="c"), {"c": "two"})
chk("update=() keeps the existing row",
    db.upsert("u", {"a": 1, "b": "x", "c": "three", "v": b""}, key=["a", "b"],
              update=(), returning="c"), None)
chk("update=[...] overwrites only those",
    db.upsert("u", {"a": 1, "b": "x", "c": "four", "v": b"\x02"}, key=("a", "b"),
              update=["v"], returning=("c", "v")), {"c": "two", "v": b"\x02"})
chk("bytes round-trip as bytes", db.query_one("SELECT v FROM u")["v"], b"\x02")
done(cm)

# --------------------------------------------------------------------------- #
print("migrations: 0001 and 0002 apply, then nothing is pending")
files = db.migration_files()
names = [f[1] for f in files]
chk("the shipped files", names[:2], ["0001_accounts", "0002_state"])
cm = fresh()
chk("first migrate applies all", db.migrate(log=lambda m: None), names)
chk("second migrate applies none", db.migrate(log=lambda m: None), [])
chk("recorded once each",
    [r["name"] for r in db.query("SELECT name FROM schema_migrations ORDER BY version")],
    names)
tables = {r["t"] for r in db.query(
    "SELECT table_name AS t FROM information_schema.tables"
    " WHERE table_schema = 'public'")}
want = {"polid", "member", "handle", "login_alias", "deleted_handle", "content",
        "handle_content", "content_id_seq", "friend", "group_member", "session",
        "handle_profile", "profile", "regcode", "mail", "admin_cred",
        "login_token_client", "ext_mail_log", "login_digest_client", "login_fail",
        "list_stamp", "handle_content_trimmed", "content_character", "blob",
        "schema_migrations"}
chk("every table exists", sorted(want - tables), [])

cols = {(r["table_name"], r["column_name"]) for r in db.query(
    "SELECT table_name, column_name FROM information_schema.columns"
    " WHERE table_schema = 'public'")}
for tc in [("member", "login_pw_sealed"), ("member", "ext_mail"),
           ("member", "token_arm_until"), ("handle", "client_guid"),
           ("friend", "client_ref"), ("friend", "wire_ref"),
           ("group_member", "pending"), ("handle_content", "slot")]:
    chk("column %s.%s (from the upgrade list / rebuilds)" % tc, tc in cols, True)

print("migrations: same tables and columns as the SQLite accounts.db")
# accounts.py still builds the SQLite schema (SCHEMA + its upgrade list and
# rebuilds). While it does, 0001 must carry every table and column it makes,
# so a column added there without a migration here fails this line.
import sqlite3  # noqa: E402
import accounts  # noqa: E402
if getattr(accounts, "SCHEMA", "").lstrip().upper().startswith("PRAGMA"):
    tmpdb = tempfile.mkdtemp()
    try:
        sconn = accounts.connect(os.path.join(tmpdb, "accounts.db"))
        lite = {}
        for (t,) in sconn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"
                                  " AND name NOT LIKE 'sqlite_%'").fetchall():
            lite[t] = {r[1] for r in sconn.execute("PRAGMA table_info(%s)" % t)}
        sconn.close()
        accounts.pool_drain()
    finally:
        shutil.rmtree(tmpdb, ignore_errors=True)
    pg = {}
    for (t, c) in cols:
        pg.setdefault(t, set()).add(c)
    chk("SQLite tables missing from Postgres", sorted(set(lite) - set(pg)), [])
    chk("SQLite columns missing from Postgres",
        sorted("%s.%s" % (t, c) for t in lite for c in lite[t] - pg.get(t, set())), [])
    chk("Postgres columns SQLite does not have (in shared tables)",
        sorted("%s.%s" % (t, c) for t in lite for c in pg.get(t, set()) - lite[t]), [])
else:
    print("  (accounts.py no longer builds a SQLite schema; comparison skipped)")

print("migrations: the schema behaves like accounts.py's")
now = "2026-09-27T00:00:00Z"
with db.transaction() as c:
    db.execute("INSERT INTO polid (polid, pw_hash, pw_salt, created_at, updated_at)"
               " VALUES ('ABCD2345', 'h', 's', %s, %s)", (now, now), conn=c)
    mid = db.query_one(
        "INSERT INTO member (polid, member_no, login_name, pw_hash, pw_salt, created_at)"
        " VALUES ('ABCD2345', 1, 'LOGIN1', 'h', 's', %s) RETURNING id", (now,),
        conn=c)["id"]
    hid = db.query_one("INSERT INTO handle (member_id, handle_name, created_at)"
                       " VALUES (%s, 'Tester', %s) RETURNING id", (mid, now),
                       conn=c)["id"]
    db.execute("INSERT INTO handle_content (handle_id, content_code, slot, content_id,"
               " linked_at) VALUES (%s, 1, 0, '30000001', %s)", (hid, now), conn=c)
    db.execute("INSERT INTO handle_content (handle_id, content_code, slot, content_id,"
               " linked_at) VALUES (%s, 1, 1, NULL, %s), (%s, 1, 2, NULL, %s)",
               (hid, now, hid, now), conn=c)
chk("identity ids start at 1", (mid, hid), (1, 1))
try:
    with db.transaction() as c:
        db.execute("INSERT INTO handle_content (handle_id, content_code, slot,"
                   " content_id, linked_at) VALUES (%s, 1, 3, '30000001', %s)",
                   (hid, now), conn=c)
    dup = "accepted"
except psycopg.errors.UniqueViolation:
    dup = "refused"
chk("a Content ID on two rows", dup, "refused")
chk("case-folded handle lookup",
    db.query_one("SELECT id FROM handle WHERE lower(handle_name) = lower(%s)",
                 ("TESTER",)), {"id": hid})
db.execute("INSERT INTO mail (box, member_id, uidl, raw, received_at)"
           " VALUES ('box', %s, 'u1', %s, %s)", (mid, b"\x00raw\xff", now))
chk("mail.raw is bytes", db.query_one("SELECT raw FROM mail")["raw"], b"\x00raw\xff")
db.upsert("blob", {"scope": str(mid), "path": "save.bin", "member_id": mid,
                   "data": b"\x01\x02"}, key=("scope", "path"))
db.execute("DELETE FROM polid WHERE polid = 'ABCD2345'")
chk("deleting the POL ID cascades to member, handle, links, mail, blobs",
    [db.query_one("SELECT count(*) AS n FROM %s" % t)["n"]
     for t in ("member", "handle", "handle_content", "mail", "blob")], [0] * 5)
done(cm)

# --------------------------------------------------------------------------- #
print("migrations: two runners at once on an empty database")
cm = fresh()
results, errors = [], []
gate = threading.Barrier(2)


def run_migrate():
    try:
        gate.wait()
        results.append(db.migrate(log=lambda m: None))
    except Exception as exc:
        errors.append(repr(exc))


ts = [threading.Thread(target=run_migrate) for _ in range(2)]
for t in ts:
    t.start()
for t in ts:
    t.join()
chk("no errors", errors, [])
chk("every file applied exactly once across both",
    sorted(n for r in results for n in r), sorted(names))
chk("schema_migrations rows", db.query_one(
    "SELECT count(*) AS n FROM schema_migrations")["n"], len(names))
done(cm)

# --------------------------------------------------------------------------- #
print("migrations: a failing file changes nothing and is not recorded")
cm = fresh()
tmp = tempfile.mkdtemp()
try:
    with open(os.path.join(tmp, "0001_ok.sql"), "w") as fh:
        fh.write("CREATE TABLE ok1 (x INTEGER);\n")
    with open(os.path.join(tmp, "0002_broken.sql"), "w") as fh:
        fh.write("CREATE TABLE half (x INTEGER);\nTHIS IS NOT SQL;\n")
    try:
        db.migrate(directory=tmp, log=lambda m: None)
        failed = "no error"
    except db.MigrationError:
        failed = "MigrationError"
    chk("broken file raises", failed, "MigrationError")
    chk("the good one stays applied",
        [r["name"] for r in db.query("SELECT name FROM schema_migrations")], ["0001_ok"])
    chk("the broken one left no table",
        db.query_one("SELECT to_regclass('half') IS NULL AS gone")["gone"], True)
    with open(os.path.join(tmp, "0003-bad-name.sql"), "w") as fh:
        fh.write("SELECT 1;\n")
    try:
        db.migration_files(tmp)
        named = "accepted"
    except db.MigrationError:
        named = "refused"
    chk("a misnamed file", named, "refused")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
    done(cm)

print("\n%s" % ("all passed" if not bad else "%d FAILED" % bad))
sys.exit(1 if bad else 0)
