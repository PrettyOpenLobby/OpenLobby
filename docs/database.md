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

The services are moving onto this layer in stages. Until that work lands they
still read and write `accounts.db` on the `pol-data` volume, so keep backing
that volume up as before.

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
may write keys to. With neither, the suites report SKIP and pass;
`POL_TEST_REQUIRE_DB=1` turns that into a failure, and CI sets it.
