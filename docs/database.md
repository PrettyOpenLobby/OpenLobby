# Database and live state

The stack keeps its data in two services that run beside the game servers.

PostgreSQL holds everything that has to survive a restart: accounts, handles,
Content IDs, friends, groups, mail, saved resources. Valkey holds live state
that several containers need to see quickly - who is online, login sessions
that expire on their own, the rooms that are open right now and the queue of
pushes waiting for delivery. Nothing durable is written to Valkey, so if its
container is lost the players currently online have to log in again and no
data goes missing.

Both services are part of the default `docker-compose.yml`, so
`docker compose up -d` starts them with the rest of the stack. PostgreSQL
keeps its files in the `pol-pgdata` volume. Valkey keeps nothing on disk.

The account data is in PostgreSQL: POL IDs, members, handles, Content IDs,
friends and groups, mail, sessions, the admin panel's moderators and audit log,
and the Discord links. Some state still lives in files on the `pol-data`
volume while the rest of the services move over, so keep backing that volume
up as well. One file there stays on purpose: `login-pw.key`, the key the
Viewer login passwords are sealed with. It is kept out of the database so that
a copy of the database alone cannot unseal them, and without it no sealed
password can be read again, so back it up with the database.

The bundled PostgreSQL is created with the C collation (`POSTGRES_INITDB_ARGS`
in the compose file), so text sorts byte by byte and `lower()` folds only
ASCII letters, which is what the account code was written against. A server
you bring yourself works with other collations; lists sorted by name may come
out in a different order.

## Settings

`POL_DATABASE_URL` is the connection string every service uses, in the form
`postgresql://user:password@host:5432/dbname`. The compose file sets it to the
bundled `postgres` service. Point it somewhere else to use a database server
you already run.

`POL_DB_PASSWORD` in `.env` is the password the bundled PostgreSQL is created
with (the default is `openlobby`). Choose it before the first start. The
database keeps the password it was initialised with, so changing the variable
later also means running `ALTER USER openlobby PASSWORD '...'` inside it.
Stick to letters and digits, because the password becomes part of a URL.

`POL_DB_POOL` caps how many connections each process keeps open (default 8).

`POL_DB_LOCK_TIMEOUT` is how long a write waits for a row another transaction
holds before it fails (default `10s`, the busy timeout the SQLite version had).

`POL_LOGIN_PW_KEYFILE` moves the password sealing key somewhere other than
`<POL_DATA_DIR>/login-pw.key`; `POL_LOGIN_PW_KEY` replaces the file with a
passphrase.

`POL_VALKEY_URL` is the Valkey server, for example `valkey://valkey:6379/0`.
Redis works as well (`redis://...`). When it is empty a service keeps its live
state in its own memory, which is fine for tests and a single process but
means two containers cannot see each other's state.

`POL_KV_PREFIX` goes in front of every Valkey key and channel (default
`pol:`), so two stacks can share one Valkey server.

## Migrations

The schema lives in `services/polcore/migrations/` as numbered SQL files
(`0001_accounts.sql`, `0002_state.sql`, ...). They are applied in order and
recorded in the `schema_migrations` table. Services that use the database
call `polcore.db.migrate()` at start to apply whatever is pending, and
containers starting at the same moment take turns through an advisory lock,
so nothing runs twice.

To apply or inspect them by hand:

```
docker compose exec login python -m polcore.db migrate
docker compose exec login python -m polcore.db status
```

Outside Docker, run the same commands from `services/` with
`POL_DATABASE_URL` set.

A migration that has shipped is never edited. A schema change is a new file
with the next number. Each file runs in its own transaction, so one that fails
leaves the database as it was and is tried again on the next start.

## Using it from code

`polcore.db` gives a pooled connection that returns rows as dicts:

```python
from polcore import db

row = db.query_one("SELECT * FROM member WHERE login_name = %s", (name,))

with db.transaction(lock="content_id_seq") as conn:
    n = db.query_one("SELECT next_id FROM content_id_seq WHERE id = 1",
                     conn=conn)["next_id"]
    db.execute("UPDATE content_id_seq SET next_id = %s WHERE id = 1",
               (n + 1,), conn=conn)
```

Placeholders are `%s`. The `lock` argument takes a PostgreSQL advisory lock
for the length of the transaction. SQLite ran one writer at a time, and code
that reads a value and writes it back relied on that; with PostgreSQL such
code names a lock, and only transactions naming the same lock wait for each
other. `db.upsert()` builds `INSERT ... ON CONFLICT` for a dict of columns.

### The account connection

`accounts.connect()` returns a `polcore.db.CompatConnection`, the connection
every function in `accounts.py` takes as `conn`. It keeps the shape of the
SQLite connection the code was written against, so a title plugin or a tool
that used to open `accounts.db` itself can move over by changing its SQL and
nothing else:

```python
import accounts

conn = accounts.connect()
try:
    row = conn.execute("SELECT * FROM handle WHERE id = %s", (hid,)).fetchone()
    row["handle_name"], row[0], dict(row)      # by name, by position, as a dict
    conn.execute("UPDATE handle SET is_primary = 1 WHERE id = %s", (hid,))
    conn.commit()
finally:
    conn.close()
```

- A read outside a transaction runs on its own. The first INSERT, UPDATE or
  DELETE opens a transaction, which lasts until `commit()` or `rollback()`.
  `with conn:` commits at the end of the block, or rolls back if it raises,
  and does not close the connection.
- The connection holds a pooled server connection only while a transaction is
  open, so keeping the object for a whole request costs nothing between
  statements.
- Inside a transaction a statement that fails undoes only itself and the
  transaction carries on, as it did under SQLite. The error types are
  `polcore.db.Error`, `IntegrityError` and `OperationalError`.
- `close()` rolls back anything not committed and hands the connection back.
  A connection dropped without `close()` is cleaned up when it is garbage
  collected.
- Integers and booleans are sent untyped, so a number compared with a text
  column, or `True` stored in an integer flag column, works as it did.
- There is no `lastrowid`: write `INSERT ... RETURNING id`. There is no
  `rowid` either; order by a real column.
- `conn.begin(lock="name")` opens a transaction now and takes a named advisory
  lock in it, for code that must not run twice at once (the Content ID counter
  uses it).

`accounts.py` also has the lookups the title servers need, so they do not
read its tables directly: `session_by_ip` and `sessions_by_ip` (which POL
member logged in from this address), `member_content_id_list` and
`content_id_list` (the Content IDs of one member, or of the whole server, for
one title), `member_groups` (the groups a member belongs to),
`member_id_by_handle_name` (a handle typed by a person) and
`record_character_name` (the write side of `character_names`).

`polcore.kv` has the same methods whichever backend is behind it: `get`,
`set` with a `ttl` in seconds, `setnx` for a lock with a timeout, hashes
(`hset`, `hgetall`), a FIFO queue (`push`, and `pop` with an optional
blocking timeout) and `publish` / `subscribe`. Values are text; `set_json`
and `get_json` handle structured values.

## Tests

```
python tests/test_polcore_db.py
python tests/test_polcore_kv.py
```

Both run as part of `python tools/run_all.py`. They need the Python packages
from the image (`pip install "psycopg[binary]" psycopg-pool valkey`) and
Docker: `tools/pgtest.py` starts a throwaway `postgres:17-alpine` on a free
loopback port, gives each test its own empty database, and removes the
container when the test exits. The Valkey test does the same with
`valkey/valkey:8-alpine`, and checks the in-memory backend with the same
assertions.

Without Docker, set `POL_TEST_DATABASE_URL` to a PostgreSQL server the tests
may create databases on, and `POL_TEST_VALKEY_URL` to a Valkey server they
may write keys to. With neither, these two suites report SKIP and pass;
`POL_TEST_REQUIRE_DB=1` turns that into a failure, and CI sets it.

Every other suite that touches the account code calls
`pgtest.use_fresh_database()` at the top, which creates an empty database,
points `POL_DATABASE_URL` at it for the suite and for any server the suite
starts, and drops it when the suite exits. Those suites fail, rather than
skip, when there is no database, because without one there is nothing to
test. `run_all.py` starts one PostgreSQL container for the whole run and
hands every suite its address, so a full run pays for one container.
