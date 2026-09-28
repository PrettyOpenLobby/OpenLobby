"""PostgreSQL access for every service: one pool per process, transactions,
advisory locks and the numbered migrations.

    POL_DATABASE_URL   postgresql://user:password@host:5432/dbname (required)
    POL_DB_POOL        most connections this process holds open (default 8)

Connections come out of the pool in autocommit mode with dict rows, so a bare
`query()` is one statement and one implicit transaction. Anything that must be
atomic goes through `transaction()`:

    with db.transaction() as conn:
        row = db.query_one("SELECT next_id FROM content_id_seq WHERE id = 1",
                           conn=conn)
        ...

SQLite serialised every writer with BEGIN IMMEDIATE, and some code in this tree
relies on that (read a counter, then write it back). Postgres does not
serialise transactions that way. Code that needs it names what it serialises:

    with db.transaction(lock="content_id_seq") as conn:
        ...

which takes pg_advisory_xact_lock on a key derived from the name, held until
the transaction ends. Two transactions that name the same key run one after
the other; everything else runs concurrently.

Placeholders are psycopg's: %s positional or %(name)s named. SQLite's ? is not
accepted.

Code written against sqlite3's connection (accounts.py and everything that
calls it) uses `compat_connect()` instead: a `CompatConnection` with that
connection's shape -- execute() returning a cursor, commit(), `with conn:`,
close() -- and rows readable by position and by name. Its docstring lists the
rules it keeps. `accounts.connect()` returns one.

Migrations are the files in polcore/migrations/, named NNNN_name.sql and
applied in order, each in its own transaction, recorded in schema_migrations.
`migrate()` is safe to call from several containers starting at once: each
step runs under an advisory lock and re-reads what is applied after taking it.
A service calls `migrate()` once at start; an operator can run

    python -m polcore.db migrate     (from services/)
    python -m polcore.db status
"""
import contextlib
import hashlib
import os
import re
import sys
import threading
import time

try:
    import psycopg
    from psycopg import sql as _sql
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
except ImportError as _exc:            # the module stays importable for --help
    psycopg = None
    _IMPORT_ERROR = _exc
else:
    _IMPORT_ERROR = None

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "migrations")
_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

#: Advisory lock key for the migration runner. Any other fixed int64 would do;
#: this one is derived like every other named lock so it cannot collide with
#: one by accident.
_MIGRATE_LOCK = "polcore.db.migrate"


class DatabaseNotConfigured(RuntimeError):
    """POL_DATABASE_URL is empty and nothing called configure()."""


class MigrationError(RuntimeError):
    """A migration file failed to apply, or the directory is malformed."""


_state_lock = threading.Lock()
_pool = None
_url = None
_pool_size = None


def _require_driver():
    if psycopg is None:
        raise RuntimeError("polcore.db needs psycopg 3 and psycopg_pool "
                           "(pip install 'psycopg[binary]' psycopg_pool): "
                           f"{_IMPORT_ERROR}")


def database_url():
    """The URL this process uses: configure() wins, then POL_DATABASE_URL."""
    url = _url or os.environ.get("POL_DATABASE_URL", "").strip()
    if not url:
        raise DatabaseNotConfigured(
            "POL_DATABASE_URL is not set (postgresql://user:pw@host:5432/db)")
    return url


def configure(url=None, pool_size=None):
    """Point this process at another database, closing the current pool.

    Tests use it to give each case its own database. `url=None` goes back to
    POL_DATABASE_URL.
    """
    global _url, _pool_size
    close()
    with _state_lock:
        _url = url
        _pool_size = pool_size


def get_pool():
    """The process-wide pool, created on first use."""
    global _pool
    _require_driver()
    if _pool is not None:
        return _pool
    with _state_lock:
        if _pool is None:
            size = _pool_size or int(os.environ.get("POL_DB_POOL", "8") or 8)
            size = max(1, size)
            pool = ConnectionPool(
                database_url(), min_size=1, max_size=size, open=False,
                name="polcore",
                kwargs={"autocommit": True, "row_factory": dict_row},
                # Probe an idle connection before handing it out, so a server
                # restart surfaces here rather than in whichever request drew
                # the dead connection. Only one that has sat idle a while: the
                # account code borrows per statement (CompatConnection), and a
                # probe per borrow doubled the round trips of every read.
                check=_check_if_idle,
                reset=_note_returned,
                timeout=30)
            pool.open(wait=True, timeout=30)
            _pool = pool
    return _pool


#: Seconds a pooled connection may sit unused before it is probed on checkout.
CHECK_IDLE_S = float(os.environ.get("POL_DB_CHECK_IDLE_S", "5") or 0)


def _note_returned(conn):
    conn._pol_returned_at = time.monotonic()


def _check_if_idle(conn):
    at = getattr(conn, "_pol_returned_at", None)
    if at is not None and time.monotonic() - at < CHECK_IDLE_S:
        return
    ConnectionPool.check_connection(conn)


def close():
    """Close the pool (all idle connections). The next call reopens it."""
    global _pool
    with _state_lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.close()


@contextlib.contextmanager
def connect():
    """A pooled connection (autocommit, dict rows), returned on exit.

    A connection handed back inside an open transaction is rolled back by the
    pool, so a forgotten transaction cannot leak into the next borrower.
    """
    with get_pool().connection() as conn:
        yield conn


def lock_key(name):
    """The signed int64 advisory-lock key for `name` (a str or an int)."""
    if isinstance(name, int):
        return name
    digest = hashlib.blake2b(str(name).encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def lock(conn, key):
    """Take a transaction-scoped advisory lock on `key` (str or int).

    Held until the surrounding transaction commits or rolls back. Outside a
    transaction the lock would be released the moment the statement ends,
    which serialises nothing, so that is refused.
    """
    status = conn.info.transaction_status
    if status != psycopg.pq.TransactionStatus.INTRANS:
        raise RuntimeError("db.lock() needs an open transaction; "
                           "use it inside db.transaction()")
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key(key),))


@contextlib.contextmanager
def transaction(write=True, lock=None, conn=None):
    """A connection inside a real transaction: commit on exit, roll back if the
    block raises (the exception propagates).

    write=False makes the transaction READ ONLY, so a stray write fails loudly.
    lock names an advisory lock (str, int, or a sequence of them, taken in
    order) held for the whole transaction; see the module docstring.
    conn nests inside a caller's connection; an inner transaction on a
    connection already in one becomes a savepoint.
    """
    keys = () if lock is None else (
        (lock,) if isinstance(lock, (str, int)) else tuple(lock))
    with contextlib.ExitStack() as stack:
        if conn is None:
            conn = stack.enter_context(connect())
        nested = (conn.info.transaction_status
                  == psycopg.pq.TransactionStatus.INTRANS)
        stack.enter_context(conn.transaction())
        if not write and not nested:
            conn.execute("SET TRANSACTION READ ONLY")
        for key in keys:
            _advisory(conn, key)
        yield conn


_advisory = lock


@contextlib.contextmanager
def _borrow(conn):
    if conn is not None:
        yield conn
    else:
        with connect() as c:
            yield c


def query(sql, params=None, conn=None):
    """All rows as a list of dicts."""
    with _borrow(conn) as c:
        return c.execute(sql, params).fetchall()


def query_one(sql, params=None, conn=None):
    """The first row as a dict, or None."""
    with _borrow(conn) as c:
        return c.execute(sql, params).fetchone()


def execute(sql, params=None, conn=None):
    """Run one statement; returns the affected row count."""
    with _borrow(conn) as c:
        return c.execute(sql, params).rowcount


def execute_many(sql, seq, conn=None):
    """Run one statement per parameter set; returns the total row count."""
    with _borrow(conn) as c:
        with c.cursor() as cur:
            cur.executemany(sql, seq)
            return cur.rowcount


def _cols(value):
    if value is None:
        return None
    return (value,) if isinstance(value, str) else tuple(value)


def upsert(table, row, key, update=None, returning=None, conn=None):
    """INSERT ... ON CONFLICT (key) DO UPDATE, built with quoted identifiers.

    table      table name
    row        {column: value} to insert
    key        the conflict column(s): a str or a sequence
    update     columns to overwrite on conflict; None means every column in
               `row` that is not in `key`, and an empty sequence means
               DO NOTHING (keep the existing row)
    returning  column(s) to return; the result is then the row dict (None
               when DO NOTHING skipped it), otherwise the row count
    """
    if not row:
        raise ValueError("upsert needs at least one column")
    key = _cols(key)
    cols = list(row)
    missing = [k for k in key if k not in row]
    if missing:
        raise ValueError(f"upsert key column(s) not in row: {missing}")
    update = [c for c in cols if c not in key] if update is None else list(update)
    ident = _sql.Identifier
    q = _sql.SQL("INSERT INTO {t} ({c}) VALUES ({v}) ON CONFLICT ({k}) ").format(
        t=_sql.Identifier(*table.split(".")),
        c=_sql.SQL(", ").join(map(ident, cols)),
        v=_sql.SQL(", ").join(_sql.Placeholder() * len(cols)),
        k=_sql.SQL(", ").join(map(ident, key)))
    if update:
        q += _sql.SQL("DO UPDATE SET {}").format(_sql.SQL(", ").join(
            _sql.SQL("{c} = EXCLUDED.{c}").format(c=ident(c)) for c in update))
    else:
        q += _sql.SQL("DO NOTHING")
    ret = _cols(returning)
    if ret:
        q += _sql.SQL(" RETURNING {}").format(_sql.SQL(", ").join(map(ident, ret)))
    with _borrow(conn) as c:
        cur = c.execute(q, [row[k] for k in cols])
        return cur.fetchone() if ret else cur.rowcount


# --------------------------------------------------------------------------- #
# the compat connection: the DB-API shape the account code was written against
# --------------------------------------------------------------------------- #
#: The errors a caller catches, so code that runs SQL needs no psycopg import.
if psycopg is not None:
    Error = psycopg.Error
    IntegrityError = psycopg.IntegrityError
    OperationalError = psycopg.OperationalError
    ProgrammingError = psycopg.ProgrammingError
else:                                   # importable without the driver
    class Error(Exception):
        pass

    class IntegrityError(Error):
        pass

    class OperationalError(Error):
        pass

    class ProgrammingError(Error):
        pass


class Row(tuple):
    """A result row readable by position AND by column name.

    `row[0]`, `row["id"]`, `a, b = row`, `dict(row)` and `row.keys()` all work,
    the way they did on sqlite3.Row, so code written against that keeps working.
    A name lookup falls back to a case-insensitive match (sqlite3.Row's rule);
    PostgreSQL folds unquoted aliases to lower case, so `AS pStatus` comes back
    as `pstatus` and is still found as `row["pStatus"]`. An unknown name raises
    IndexError, as sqlite3.Row did.
    """
    __slots__ = ()
    _names = ()
    _index = {}

    def __getitem__(self, key):
        if isinstance(key, str):
            i = self._index.get(key)
            if i is None:
                i = self._index.get(key.lower())
                if i is None:
                    raise IndexError(f"no column named {key!r}")
            return tuple.__getitem__(self, i)
        return tuple.__getitem__(self, key)

    def keys(self):
        return list(self._names)

    def __repr__(self):
        return "Row(%s)" % ", ".join(
            f"{n}={v!r}" for n, v in zip(self._names, tuple(self)))


_ROW_CLASSES = {}
_ROW_LOCK = threading.Lock()


def _row_class(names):
    cls = _ROW_CLASSES.get(names)
    if cls is None:
        index = {}
        for i, name in enumerate(names):
            index.setdefault(name, i)
        for i, name in enumerate(names):
            index.setdefault(name.lower(), i)
        cls = type("Row", (Row,), {"__slots__": (), "_names": names,
                                   "_index": index})
        with _ROW_LOCK:
            cls = _ROW_CLASSES.setdefault(names, cls)
    return cls


def compat_row(cursor):
    """psycopg row factory producing `Row`."""
    desc = cursor.description
    if not desc:
        return tuple
    cls = _row_class(tuple(d.name for d in desc))
    return cls


if psycopg is not None:
    from psycopg.adapt import Dumper as _Dumper, Loader as _Loader

    class _LooseIntDumper(_Dumper):
        """int and bool sent as untyped literals, so the server types them
        from the column they meet. psycopg would send an int as smallint or
        bigint, and `text_column = 5` then fails with "operator does not
        exist"; a bool would refuse to go into an INTEGER flag column. SQLite
        took both, and so does this."""
        oid = 0

        def dump(self, obj):
            return str(int(obj)).encode("ascii")

    class _NumericLoader(_Loader):
        """numeric results (SUM over a bigint, say) as int or float rather
        than Decimal, which is what SQLite returned for the same query."""

        def load(self, data):
            text = bytes(data).decode("ascii")
            if text.lstrip("-").isdigit():
                return int(text)
            return float(text)


_DML = re.compile(r"^\s*(?:--[^\n]*\n\s*)*(INSERT|UPDATE|DELETE|REPLACE|MERGE)\b",
                  re.IGNORECASE)

#: How long a statement waits for a row lock another transaction holds before
#: it fails. SQLite gave up after its 10 s busy timeout; without this a
#: PostgreSQL writer would wait for ever, which turns one forgotten commit on
#: a pooled connection into a hang instead of an error.
LOCK_TIMEOUT = os.environ.get("POL_DB_LOCK_TIMEOUT", "10s")


class Result:
    """What `CompatConnection.execute` returns: the rows already fetched, and
    the cursor attributes callers read (rowcount, description, fetch*)."""
    __slots__ = ("_rows", "_pos", "rowcount", "description", "lastrowid")

    def __init__(self, rows, rowcount, description):
        self._rows = rows
        self._pos = 0
        self.rowcount = rowcount
        self.description = description
        # PostgreSQL has no rowid; write `INSERT ... RETURNING id` instead.
        self.lastrowid = None

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchmany(self, size=1):
        out = self._rows[self._pos:self._pos + size]
        self._pos += len(out)
        return out

    def fetchall(self):
        out = self._rows[self._pos:]
        self._pos = len(self._rows)
        return out

    def __iter__(self):
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row

    def close(self):
        self._pos = len(self._rows)


class CompatConnection:
    """A connection with the shape of Python's sqlite3.Connection, over the
    pool.

    The account code (and the title plugins) were written against sqlite3:
    `conn.execute(sql, params)` returning a cursor, `conn.commit()`,
    `with conn:` as a transaction, `conn.close()` in a `finally`. This keeps
    that shape so those callers only had to change their SQL:

      * Autocommit until a write. A SELECT outside a transaction runs on its
        own. The first INSERT/UPDATE/DELETE opens a transaction (sqlite3's
        implicit BEGIN) that lasts until commit() or rollback().
      * A pool connection is held only while a transaction is open. Between
        statements it goes back to the pool, so a handler that keeps one of
        these open for a whole request costs nothing while it waits, and
        nesting one inside another cannot run the pool dry.
      * Inside a transaction every statement runs under a savepoint. SQLite
        undid a failed statement and kept the transaction; PostgreSQL would
        abort the whole transaction, so code that catches an IntegrityError
        and carries on would otherwise fail on its next statement.
      * `with conn:` commits on success and rolls back on an exception, and
        does not close (sqlite3's rule).
      * close() rolls back anything uncommitted and hands the connection
        back. The object stays usable (the next statement borrows again),
        and one that is dropped without close() is cleaned up when it is
        garbage collected.
      * Rows are `Row`: by position and by name.
      * Placeholders are %s. A literal % in the SQL text is written %%.

    `begin(lock=...)` opens a transaction now and takes named advisory locks
    in it, for the code that relied on SQLite running one writer at a time.
    """

    def __init__(self):
        self._pg = None
        self._pool = None
        self._fresh = False             # the open transaction is ours and empty
        self._last_broken = False
        self._lock = threading.RLock()

    # -- plumbing -----------------------------------------------------------
    def _acquire(self):
        if self._pg is None:
            pool = get_pool()
            self._pg = pool.getconn()
            self._pool = pool
        return self._pg

    def _release(self):
        pg, pool = self._pg, self._pool
        self._pg = self._pool = None
        if pg is None:
            return
        try:
            if pg.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                pg.rollback()
        except Exception:               # a broken connection: the pool drops it
            pass
        pool.putconn(pg)

    def _in_tx(self):
        return (self._pg is not None and self._pg.info.transaction_status
                in (psycopg.pq.TransactionStatus.INTRANS,
                    psycopg.pq.TransactionStatus.INERROR))

    def _start(self):
        """BEGIN on the held connection (idle, just acquired)."""
        self._pg.execute(f"BEGIN; SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")

    def _cursor(self):
        cur = self._pg.cursor(row_factory=compat_row)
        cur.adapters.register_dumper(int, _LooseIntDumper)
        cur.adapters.register_dumper(bool, _LooseIntDumper)
        cur.adapters.register_loader("numeric", _NumericLoader)
        return cur

    def _run(self, sql, params, many):
        cur = self._cursor()
        try:
            if many:
                cur.executemany(sql, params)
            else:
                cur.execute(sql, params)
            rows = cur.fetchall() if cur.description else []
            desc = (tuple((d.name, None, None, None, None, None, None)
                          for d in cur.description) if cur.description else None)
            return Result(rows, cur.rowcount, desc)
        finally:
            cur.close()

    def _statement(self, sql, params, many=False):
        with self._lock:
            if self._pg is None:
                # A connection that died while it sat in the pool (a database
                # restart) fails its first statement; the pool only probes one
                # that has been idle a while. Nothing has run on it yet, so
                # try once more on another.
                try:
                    return self._statement_on(sql, params, many)
                except OperationalError:
                    if self._pg is not None or not self._last_broken:
                        raise
            return self._statement_on(sql, params, many)

    def _statement_on(self, sql, params, many):
        self._last_broken = False
        self._acquire()
        try:
            if not self._in_tx():
                if not _DML.match(sql):
                    return self._run(sql, params, many)
                self._start()
                self._fresh = True
            if self._fresh:
                # The transaction holds nothing yet, so a failure can simply
                # end it: nothing is lost that a savepoint would have kept.
                try:
                    out = self._run(sql, params, many)
                except BaseException:
                    if not self._pg.broken:
                        self._pg.rollback()
                    raise
                self._fresh = False
                return out
            self._pg.execute("SAVEPOINT pol_stmt")
            try:
                out = self._run(sql, params, many)
            except BaseException:
                if not self._pg.broken:
                    self._pg.execute("ROLLBACK TO SAVEPOINT pol_stmt")
                raise
            self._pg.execute("RELEASE SAVEPOINT pol_stmt")
            return out
        finally:
            if self._pg is not None and self._pg.broken:
                self._last_broken = True
                self._fresh = False
            if not self._in_tx():
                self._release()

    # -- the sqlite3.Connection surface ---------------------------------------
    def execute(self, sql, params=None):
        return self._statement(sql, params)

    def executemany(self, sql, seq):
        seq = list(seq)
        if not seq:
            return Result([], 0, None)
        return self._statement(sql, seq, many=True)

    def executescript(self, script):
        """Commit, then run several statements (no parameters) at once."""
        with self._lock:
            self.commit()
            self._acquire()
            try:
                self._pg.execute(script)
            finally:
                if not self._in_tx():
                    self._release()

    def cursor(self):
        return _CompatCursor(self)

    def begin(self, lock=None):
        """Open a transaction now if none is open, and take the named advisory
        lock(s) in it, held until commit or rollback (see `transaction`)."""
        keys = () if lock is None else (
            (lock,) if isinstance(lock, (str, int)) else tuple(lock))
        with self._lock:
            self._acquire()
            if not self._in_tx():
                self._start()
            self._fresh = False
            for key in keys:
                self._pg.execute("SELECT pg_advisory_xact_lock(%s)",
                                 (lock_key(key),))

    @property
    def in_transaction(self):
        return self._in_tx()

    def commit(self):
        with self._lock:
            if self._pg is not None:
                try:
                    if self._in_tx():
                        self._pg.commit()
                finally:
                    self._fresh = False
                    self._release()

    def rollback(self):
        with self._lock:
            self._fresh = False
            self._release()

    def close(self):
        self.rollback()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False

    def __del__(self):
        try:
            if self._pg is not None:
                self._release()
        except Exception:
            pass


class _CompatCursor:
    """`conn.cursor()` for code that asks for one: execute on it, then fetch."""

    def __init__(self, conn):
        self.connection = conn
        self._res = Result([], -1, None)

    def execute(self, sql, params=None):
        self._res = self.connection.execute(sql, params)
        return self

    def executemany(self, sql, seq):
        self._res = self.connection.executemany(sql, seq)
        return self

    rowcount = property(lambda self: self._res.rowcount)
    description = property(lambda self: self._res.description)
    lastrowid = property(lambda self: None)

    def fetchone(self):
        return self._res.fetchone()

    def fetchmany(self, size=1):
        return self._res.fetchmany(size)

    def fetchall(self):
        return self._res.fetchall()

    def __iter__(self):
        return iter(self._res)

    def close(self):
        self._res = Result([], -1, None)


def compat_connect():
    """A `CompatConnection` on this process's pool (see its docstring)."""
    _require_driver()
    return CompatConnection()


# --------------------------------------------------------------------------- #
# migrations
# --------------------------------------------------------------------------- #
def migration_files(directory=None):
    """[(version, name, path)] in version order. Raises on a duplicate version
    or a file that does not match NNNN_name.sql."""
    directory = directory or MIGRATIONS_DIR
    out, seen = [], {}
    for fn in sorted(os.listdir(directory)):
        if not fn.endswith(".sql"):
            continue
        m = _MIGRATION_NAME.match(fn)
        if not m:
            raise MigrationError(f"migration file {fn!r} is not NNNN_name.sql")
        version = int(m.group(1))
        if version in seen:
            raise MigrationError(f"migrations {seen[version]!r} and {fn!r} "
                                 "share a version number")
        seen[version] = fn
        out.append((version, fn[:-4], os.path.join(directory, fn)))
    return out


def _read_migration(path):
    # CRLF from a Windows checkout must not change the checksum.
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read().replace("\r\n", "\n")
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


_SCHEMA_MIGRATIONS = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    checksum   TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)"""


def applied_migrations(conn=None):
    """{version: {name, checksum, applied_at}} for what this database has."""
    with _borrow(conn) as c:
        exists = c.execute(
            "SELECT to_regclass('schema_migrations') IS NOT NULL AS ok"
        ).fetchone()["ok"]
        if not exists:
            return {}
        return {r["version"]: r for r in c.execute(
            "SELECT version, name, checksum, applied_at FROM schema_migrations")}


def migrate(directory=None, log=None):
    """Apply every pending migration in order. Returns the names applied.

    Each file runs in its own transaction together with its schema_migrations
    row, under an advisory lock, and the applied set is re-read after the lock
    is taken, so a second process that was waiting finds nothing left to do.
    A file whose checksum no longer matches the applied one is reported, not
    re-run: migrations are never edited after they ship.
    """
    log = log or (lambda msg: print(f"[db] {msg}", file=sys.stderr))
    files = migration_files(directory)
    done = []
    with connect() as conn:
        while True:
            with transaction(conn=conn, lock=_MIGRATE_LOCK):
                conn.execute(_SCHEMA_MIGRATIONS)
                have = applied_migrations(conn)
                pending = [f for f in files if f[0] not in have]
                if not pending:
                    break
                version, name, path = pending[0]
                text, checksum = _read_migration(path)
                try:
                    conn.execute(text)
                except psycopg.Error as exc:
                    raise MigrationError(f"{name}: {exc}") from exc
                conn.execute("INSERT INTO schema_migrations (version, name, checksum)"
                             " VALUES (%s, %s, %s)", (version, name, checksum))
            done.append(name)
            log(f"applied migration {name}")
    for version, name, path in files:
        row = have.get(version)
        if row and row["checksum"] != _read_migration(path)[1]:
            log(f"WARNING: {name} changed after it was applied "
                "(checksum differs); it was not re-run")
    return done


def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="python -m polcore.db",
                                 description="Database migrations "
                                 "(uses POL_DATABASE_URL).")
    ap.add_argument("cmd", choices=("migrate", "status"))
    args = ap.parse_args(argv)
    try:
        if args.cmd == "migrate":
            names = migrate()
            print("applied: " + ", ".join(names) if names else "up to date")
        else:
            have = applied_migrations()
            for version, name, _path in migration_files():
                row = have.get(version)
                state = f"applied {row['applied_at']:%Y-%m-%d %H:%M}" if row else "pending"
                print(f"{name:<32} {state}")
        return 0
    except (DatabaseNotConfigured, MigrationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        close()


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
