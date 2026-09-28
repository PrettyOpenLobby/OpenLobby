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
                # the dead connection.
                check=ConnectionPool.check_connection,
                timeout=30)
            pool.open(wait=True, timeout=30)
            _pool = pool
    return _pool


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
