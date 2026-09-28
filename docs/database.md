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
and the Discord links. So are the resources the lobby stores for the client:
saves, lobby lists and POL messages (the `blob` table, below). Some state still
lives in files on the `pol-data` volume while the title plugins move over, so
keep backing that volume up as well. One file there stays on purpose: `login-pw.key`, the key the
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
Every container that touches the database is its own process with its own
pool, so the server has to accept the sum. The bundled PostgreSQL is started
with `max_connections=200` (the `command` of the `postgres` service): ten
containers at the default pool take 80, and the title plugins' containers,
tools run with `docker compose exec` and a `psql` session need room beside
them. If you raise `POL_DB_POOL` or add containers, raise `max_connections`
to match; on a server you bring yourself, set it there.

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

A few more answer questions titles used to ask with their own SQL:

```python
accounts.friend_row_by_id(conn, friend_id)       # (peer_name, kind) or None
accounts.member_created_list(conn)               # [(member_id, created_at)]
accounts.member_content_id_map(conn, code, any_status=True)
                                                 # {member_id: content_id}
accounts.handle_client_guid(conn, handle_id)     # int (0 if unseen) or None
accounts.open_session(conn, member_id, ..., created_at=None)
```

`member_content_id_map` picks each member's Content ID the way
`member_content_id` does: the primary handle first, then the oldest handle,
then slot 0. `created_at` on `open_session` takes a datetime (naive means
UTC) or ISO-8601 text, so a test can open a session that is already old
instead of rewriting the row afterwards.

`polcore.kv` has the same methods whichever backend is behind it: `get`,
`set` with a `ttl` in seconds, `setnx` for a lock with a timeout, hashes
(`hset`, `hgetall`), a FIFO queue (`push`, and `pop` with an optional
blocking timeout) and `publish` / `subscribe`. Values are text; `set_json`
and `get_json` handle structured values.

## Live state in Valkey

Everything below is live state: it is rebuilt by the services as players log
in and play, and each key expires on its own. Key names are shown without
`POL_KV_PREFIX` (default `pol:`).

| Key | What it holds | Expires |
| --- | --- | --- |
| `authsess:s:<session id>` | a login session's IV, key and member, JSON (bytes as `{"__b": hex}`), written by `authsess`, read by `login` | 12 hours after the session's last auth event |
| `authsess:ver` | a counter bumped on every session write, so a reader re-reads only when it moves | never (a counter) |
| `authstamp:ip:<address>` | the session tokens issued to an address, `[[stamp, issued_at], ...]` | 12 hours after the newest |
| `authstamp:ver` | the same kind of counter, for the stamps | never |
| `titlezone:<member id>` | hash `zone`, `at`, and `lease` for a zone the server inferred | the lease (default 900 s) or 12 hours, plus a minute |
| `memberstatus:<member id>` | hash `code`, `at`: the 4:5 presence status | 12 hours, plus a minute |
| `rooms:live` | the room registry `authsess` publishes for the room browser in `login`, JSON | `POL_ROOM_RESTORE_WINDOW` (default 6 hours) after the last change |
| `contentauth:rows` | the per-login content auth values (contentauth.py), JSON list | `POL_CONTENT_AUTH_TTL` (default 24 hours) after the last one |
| `push:queue` | pushes the lobby queued for `authsess` to deliver, one JSON record each | when delivered |
| `push:work` | the push `authsess` is delivering right now | when delivered |
| `live:<service>` | a service's live-session count, `{"count", "stamp"}` (live_sessions.py) | `POL_LIVE_SESSIONS_TTL` (default a day) after the last publish |
| `clientbuild:<address>` | hash `<region>/<product>` to `{"version", "seen"}`, the build a client announced to the patch server | `POL_CLIENT_BUILDS_TTL` (default 30 days) |

A title-zone or status change is also published on the `presence` channel,
which `authsess` subscribes to so a change reaches friends' lists at once.

The push queue is delivered at least once. `authsess` moves a record from
`push:queue` to `push:work`, delivers it, and only then removes it; if it
dies in between, the next `authsess` delivers what it finds on `push:work`
when it starts. A record is never delivered twice by a process that stays up.

To look at the live state by hand:

```
docker compose exec valkey valkey-cli --scan --pattern 'pol:*'
docker compose exec valkey valkey-cli get pol:rooms:live
docker compose exec login python live_sessions.py list
```

### Live-session markers and the deploy gate

A service that holds live sessions calls
`live_sessions.start_heartbeat(service, count_fn)` once. It publishes
`live:<service>` every 10 seconds. The functions in `services/live_sessions.py`:

```python
live_sessions.marker_key(service)                  # "live:<service>"
live_sessions.write_marker(service, count, extra=None)
live_sessions.read_marker(service)                 # {"count", "stamp", ...} or None
live_sessions.read_count(service, stale=None)      # int, or None if not fresh
live_sessions.live_markers()                       # {service: record}
live_sessions.start_heartbeat(service, count_fn, interval=10.0)
live_sessions.thread_count(prefix)
```

`read_count` treats a marker older than `stale` seconds (default
`POL_PRESENCE_STALE`, 180) as unknown and returns None; `stale=0` accepts any
age. A deploy script, which cannot import Python, asks a running container:

```
docker compose exec -T login python live_sessions.py count fmo
```

It prints the count, or nothing when there is no fresh marker, and exits 0
either way. Defer the restart when it prints a number above 0.

The admin panel's Overview reads the markers of Tetra Master (`tm`),
Janhourou (`authsess-jan`), Front Mission Online (`fmo`) and Fantasy Earth
(`felobby`, `feworld`).

## Saved resources: the blob table

The lobby's resource store (`services/core/resourcestore.py`) keeps every
object the client stores or fetches by path in the `blob` table: saves, lobby
lists and POL messages. Each is written in one statement, so a reader sees
the old object or the new one and never part of either.

A row is addressed by `scope` and `path`:

- a decimal member id for per-member data. Such a row is linked to the member
  (`member_id`) when the member exists, and deleting the account deletes it.
- `shared` for a member-scoped path written with no session member.
- `s<subject hex>` for a lobby list keyed by the subject the client names.
- `mail` for POL messages, one object per message whoever reads it.
- any name a title plugin picks for its own records.

The core keeps the file names these objects had under `resources/`. The part
of the name before the first dot is the scope and the rest is the path, with
`m.` standing for the `mail` scope: `7.U_g_TM0DataFile.bin` is scope `7`,
path `U_g_TM0DataFile.bin`, and `m.<token>.bin.read` is scope `mail`, path
`<token>.bin.read`.

### Using it from a title plugin

Title plugins that still write their own files under `POL_RESOURCE_DIR` (the
Tetra Master collections, prizes and auctions, the Janhourou stats) move onto
`polcore.blobs`, which ships in the base image:

```python
from polcore import blobs

blobs.put(scope, path, data)                  # insert or replace, bytes
blobs.get(scope, path)                        # bytes, or None
blobs.stat(scope, path)                       # Info or None, without the data
blobs.exists(scope, path)
blobs.delete(scope, path)                     # True if there was one
blobs.rename(scope, path, new_scope, new_path)   # replaces the target
blobs.listing(scope=None, prefix=None, suffix=None, path=None)   # [Info]
blobs.delete_prefix(scope, prefix="")         # [Info] of what went
blobs.delete_member(member_ids)               # [Info] of what went
blobs.file_name(scope, path)                  # "<scope>.<path>", "m." for mail
blobs.split_name(name)                        # (scope, path), or None
```

`Info` is `(scope, path, size, updated_at)`, with `updated_at` in epoch
seconds so it compares the way a file's mtime did. `listing` matches every
filter given: `prefix` and `suffix` on the path (literally; `%` and `_` are
not wildcards), `path` exactly, in one scope or all of them. A glob such as
`<member>.tm_collection.json` for every member becomes
`blobs.listing(path="tm_collection.json")`, and `*.json` in one scope becomes
`blobs.listing(scope=..., suffix=".json")`.

Put per-member records in the member's scope (`str(member_id)`), so that
deleting the account removes them with everything else. A record that belongs
to no member goes in a scope named after the title, for example `tm` or
`jan`. Every call takes `conn=` to run inside a `polcore.db.transaction()`.

Deleting an account (`accounts.delete_polid`, the `del-account` command)
removes the member's rows in the same transaction as the rest of the account
through `accounts.purge_member_files(member_ids, conn=None)`, which deletes
every row scoped to those member ids or linked to them and returns their old
file names. It does not touch the filesystem.

## Moving an existing /data

A server that ran a release from before PostgreSQL keeps its accounts in
SQLite files and its saves as files under `/data`. `tools/db_import.py` copies
them into the database once. It never writes to the old files, so the volume
stays a complete copy of the old server until you delete it.

What it reads and where each part goes:

| Old file | New home |
| --- | --- |
| `accounts.db` | the account tables of `0001_accounts.sql`, with every id kept |
| `accounts.db`, table `web_login` | `web_login` (`0004_web_login.sql`): the website usernames of the website sign-in service, which kept its own table in `accounts.db` |
| `admin.db` | the `admin_*` tables (`moderator` becomes `admin_moderator`, and so on) |
| `discord_links.db` | the `discord_*` tables |
| `resources/<name>` (each top-level file) | a `blob` row: `blobs.split_name(name)` gives scope and path, the bytes are the file, `updated_at` is its mtime |
| `resources/tmrank/<f>` (Tetra Master's weekly lists) | a `blob` row in scope `tmrank`, path `<f>`, the same way |
| `resources/content-profiles.json` | stays a file for now (core/pfc.py) |
| `resources/*.bak`, `resources/*.bak-*` | not imported; an operator's backup copy is not a record |
| `resources/janevent.json`, `resources/jan-rank-snapshot.json` | not imported here; Janhourou's own import reads them (step 6) |
| `auth-sessions.json` | not imported; `authsess:s:<sid>` is refilled at the next login |
| `auth-stamps.json` | not imported; `authstamp:ip:<address>` (a client running across the move logs in again) |
| `title-zone.json`, `member-status.json` | not imported; `titlezone:<member>` and `memberstatus:<member>` |
| `rooms-live.json` | not imported; `rooms:live` |
| `content-auth.json` | not imported; `contentauth:rows` |
| `/logs/push-spool.jsonl` and its `.offset` | not imported; `push:queue` (drain the old spool first by letting `authsess` run) |
| `<service>-sessions-live.json`, `tm-matches-live.json` | not imported; `live:<service>` (`live:tm` for Tetra Master) |
| `/logs/client-builds.json` | not imported; `clientbuild:<address>`, refilled at each client's next launch |

`web_login` belongs to the website sign-in service, which creates and reads
it and deletes a member's row itself when it deletes the account. The
table's foreign key to `member` deletes the row with the member as well. As
in the account tables, a row whose member is gone is skipped and reported.

Everything live is left behind: the services rebuild it within minutes of
starting. The title repositories' own files are theirs to import, and the
tool ends by printing the commands for each of them.

### The walkthrough

The commands below run from the `openlobby` checkout. The volume names start
with the compose project's name, which is the directory's name unless you set
one; `docker volume ls` shows them.

1. Stop everything. Stop each title with its own compose command (the one in
   its README, ending in `stop`), then the core:

   ```
   docker compose stop
   ```

   If the old `authsess` had pushes waiting in `/logs/push-spool.jsonl`, let
   it run until the spool is empty before you stop it. The new queue lives
   in Valkey and does not read the old file.

2. Back up both volumes, so the move can be undone:

   ```
   docker run --rm -v openlobby_pol-data:/data:ro -v "$PWD":/backup alpine \
       tar czf /backup/pol-data.tgz -C /data .
   docker run --rm -v openlobby_pol-logs:/logs:ro -v "$PWD":/backup alpine \
       tar czf /backup/pol-logs.tgz -C /logs .
   ```

3. Update the checkout, build the new image and start only the database:

   ```
   git pull
   docker compose build
   docker compose up -d postgres
   ```

4. Run the importer in the new image, with the tools directory mounted. Try
   it with `--dry-run` first, which reads everything, writes nothing and
   prints what the real run would do:

   ```
   docker compose run --rm --no-deps -v "$PWD/tools:/tools:ro" \
       --entrypoint python login /tools/db_import.py /data --logs /logs \
       --i-stopped-the-stack --dry-run
   ```

   Then run the same command without `--dry-run`, and with
   `--report /logs/db-import.txt` to keep a copy of the report on the logs
   volume. `--i-stopped-the-stack` is needed because the source is `/data`,
   the path the running containers use. Without it the tool refuses a source
   that looks live, which also covers a SQLite file with a non-empty `-wal` or
   `-journal` beside it and files written in the last ten minutes. To import
   from a copy of the volume on another machine, run
   `python tools/db_import.py <copy> --database-url postgresql://...` there.

5. Read the report. Each source database is imported in one transaction,
   and a source that fails is rolled back and named at the end. When
   `accounts.db` fails, the saves are not imported either, because they link
   to members. Beside the row counts per table the report lists:

   - rows skipped because their parent row is gone. SQLite did not enforce
     most foreign keys and PostgreSQL does, so a friend of a deleted handle,
     or a session of a deleted member, cannot come across. They are counted
     by table and reference.
   - references cleared with the row kept, where the column may be empty and
     deleting the parent would have emptied it: a friend's `peer_handle`, a
     group member's handle, a registration code's `redeemed_by`. The code
     keeps `redeemed_at`, so it stays spent.
   - values a column cannot hold, such as text in a number column, by rowid.
   - the tables left out: `handle_profile_by_member` and
     `handle_content_pre_slots`, the copies two old schema rebuilds kept, and
     anything else that is not part of the new schema.
   - saves of members that no longer exist, which are stored but linked to
     nobody, and title files found in `resources/`.
   - the sequences it moved. Ids are kept, and each id sequence is then set
     past the highest id, or past SQLite's own counter when that is higher,
     so the id of a deleted member is never handed out again.

   The tool refuses to import into tables that already hold rows. A second
   run over the same database finds nothing missing, changes nothing and says
   so. `--merge` imports into a database that is already in use: it adds only
   the rows whose key is not there yet and lists the keys whose contents
   differ, keeping the database's version of those. The Content ID counter
   always ends at the higher of the two values.

6. Import each title's files, from that title's checkout beside `openlobby`,
   with the commands the importer printed, while only `postgres` runs. Each
   title's README sets `DC` to its compose command: for the bridge, Front
   Mission Online and Dirge of Cerberus

   ```
   DC="docker compose --project-directory ../openlobby --env-file ../openlobby/.env --env-file .env -f ../openlobby/docker-compose.yml -f docker-compose.yml"
   ```

   and for Fantasy Earth, Janhourou and Tetra Master the same without the
   two `--env-file` options. The order matters:

   - The core import comes first, because the titles' rows refer to members.
   - FINAL FANTASY XI, before the new bridge starts for the first time. A
     bridge that starts on an empty `ffxi_idmap` pairs every character
     afresh, and a character the Viewer knows under another Content ID gets
     POL-0001. While the table is empty and `/data/ffxi_idmap.json` still
     holds pairings, the bridge does not open its ports and logs what to run
     (`FFXI_IDMAP_START_EMPTY=1` overrides that). From `crystalbridge`:

     ```
     $DC run --rm --no-deps --entrypoint python bridge ffxidb.py import idmap /data/ffxi_idmap.json
     $DC run --rm --no-deps -v crystalbridge_bridge-state:/state:ro --entrypoint python bridge ffxidb.py import accounts /state/ffxi_accounts.json
     ```

   - Fantasy Earth, from `crystalring`:

     ```
     $DC run --rm --no-deps --entrypoint python feworld fedb.py import fe_db /data/fe.db
     $DC run --rm --no-deps --entrypoint python feworld fedb.py import fe_mail_db /data/fe_mail.db
     $DC run --rm --no-deps --entrypoint python feworld fedb.py import world /data
     ```

   - Front Mission Online, from `crystalfront`, before `fmo` first starts.
     `fmo_characters.json`, `fmowar.json` and `fmo_sector_wins.json` need no
     command: the service imports each the first time it finds its table
     empty, so leave them in `/data`. Import `fmo.db` first, or the service
     fills the pilot table from the older `fmo_characters.json` and the
     import of `fmo.db` is refused. The second command matters only where
     the City Control board posted to Discord:

     ```
     $DC run --rm --no-deps --entrypoint python fmo fmodb.py import fmo_db /data/fmo.db
     $DC run --rm --no-deps -v crystalfront_fmo-board-state:/state:ro --entrypoint python fmo fmodb.py import board_state /state
     ```

   - Janhourou, from `crystalholo`. Each member's record
     (`resources/<member>.jan_stats.json`) needs no command: step 4 copied
     it into the `blob` table, where the title reads it. The third command
     matters only where the web board posted to Discord:

     ```
     $DC run --rm --no-deps --entrypoint python jan janstore.py import event /data/resources/janevent.json
     $DC run --rm --no-deps --entrypoint python jan janstore.py import rank_snapshot /data/resources/jan-rank-snapshot.json
     $DC run --rm --no-deps -v openlobby_jan-board-state:/state:ro --entrypoint python jan janstore.py import board_state /state
     ```

   - Tetra Master, from `crystalmaster`. The collections, saves, prize
     records, auction records and weekly lists need no command: step 4
     copied them into the `blob` table, where the title reads them. The
     third command matters only where the web board posted to Discord:

     ```
     $DC run --rm --no-deps --entrypoint python tmrank tmstore.py import event_state /data/tm-event-state.json
     $DC run --rm --no-deps --entrypoint python tmrank tmstore.py import champion /data/tm-champion.json
     $DC run --rm --no-deps -v openlobby_tm-board-state:/state:ro --entrypoint python tmrank tmstore.py import board_state /state
     ```

   - Dirge of Cerberus, from `crystaldirge`, while the `doc` responder is
     stopped: `$DC run --rm --entrypoint python doc docdb.py import <store>
     /logs/doc-<store>.json` for each store file the importer lists.

   The `-v` volumes are the board and bridge state volumes the titles used
   before. The bridge and Front Mission Online ran as compose projects of
   their own, so theirs start with `crystalbridge_` and `crystalfront_`;
   the Janhourou and Tetra Master boards already ran in the core's project,
   so theirs start with `openlobby_`. `docker volume ls` shows the names on
   your host.
   Every title import only reads its source, runs in one transaction,
   prints what it imported and each row it could not map, and refuses a
   table that already holds rows unless given `--merge`, which adds only
   the keys the table lacks. `--dry-run` prints the same report and writes
   nothing, and a second run changes nothing.

7. Start the stack and the titles:

   ```
   docker compose up -d
   ```

   and each title with the command in its README. The services apply any
   migration still pending as they start.

If something is wrong, stop the stack, remove the database volume
(`docker volume rm openlobby_pol-pgdata`), start `postgres` again and run the
importer again from step 4. The old files are untouched, and the backups from
step 2 hold them as well.

## Tests

```
python tests/test_polcore_db.py
python tests/test_polcore_kv.py
python tests/test_polcore_blobs.py
python tests/test_live_markers.py
python tests/test_db_import.py
```

All of them run as part of `python tools/run_all.py`. They need the Python packages
from the image (`pip install "psycopg[binary]" psycopg-pool valkey`) and
Docker: `tools/pgtest.py` starts a throwaway `postgres:17-alpine` on a free
loopback port, gives each test its own empty database, and removes the
container when the test exits. The Valkey test does the same with
`valkey/valkey:8-alpine`, and checks the in-memory backend with the same
assertions.

`test_db_import.py` builds its old `/data` tree with the SQLite account code
the importer reads from, which it takes from git (the commit before the move
to PostgreSQL, named in the test). A shallow clone lacks it; run
`git fetch --unshallow` first.

Without Docker, set `POL_TEST_DATABASE_URL` to a PostgreSQL server the tests
may create databases on, and `POL_TEST_VALKEY_URL` to a Valkey server they
may write keys to. With neither, the first two report SKIP and pass;
`POL_TEST_REQUIRE_DB=1` turns that into a failure, and CI sets it.

Suites whose processes share live state (the push queue across processes,
the login resume) call `pgtest.use_fresh_valkey()`, which points
`POL_VALKEY_URL` at a throwaway Valkey with a key prefix of its own. The
rest keep live state in memory, as a single process does.

A suite that touches the account code or the resource store calls
`pgtest.use_fresh_database()` at the top, which creates an empty database,
points `POL_DATABASE_URL` at it for the suite and for any server the suite
starts, and drops it when the suite exits. Those suites fail, rather than
skip, when there is no database, because without one there is nothing to
test. `run_all.py` starts one PostgreSQL and one Valkey container for the
whole run and hands every suite their addresses, so a full run pays for two
containers.
