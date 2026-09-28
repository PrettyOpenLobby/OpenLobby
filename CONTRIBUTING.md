# Contributing to OpenLobby

OpenLobby is the core of a PlayOnline server reimplementation: login, lobby,
world, mail, accounts and the portal. The game titles are separate repositories
that plug into it. This page says where things are, how to run the checks, and
what a pull request needs.

## Where things are

```
services/
  responders.py     entry point (`python responders.py <modes>`) and a facade
                    over the core package; see below
  core/             the server, one module per concern (core/__init__.py lists them)
  accounts.py       the account database and its command-line tool
  titles.py         the seam a game title plugs into (POL_TITLES)
  ucscgi.py         the in-client sign-up and account portal
  admin.py          the local admin panel (port 8090)
  srvcore.py        logging, config, capture helpers shared by every service
  stub.py           the observation stub (dns / http / tcp modes)
  polcore/          PostgreSQL (db.py, blobs.py) and live state (kv.py)
  live_sessions.py  the live-session markers the deploy gate reads
tools/              self-tests (`*_test.py`, `*_check.py`) and operator tools
tests/              newer self-tests
config/             server.yaml and the portal-era table
```

`core/` is split along the wire: one module per band or per record family.
Reading order for a first visit:

1. `core/main.py`: which mode starts which listener on which port.
2. `core/directory.py`, `core/authserv.py`, `core/ircband.py`: the login
   band, from the redirect to the IRC-style verbs a logged-in client sends.
3. `core/lobbyserver.py`, `core/lobbyreply.py`, `core/paylen.py`: the lobby
   band, where each request is an opcode pair and the reply is a fixed-length
   record.
4. The record families: `friendlist.py`, `friendput.py`, `friendgroups.py`,
   `handlelists.py`, `characters.py`, `profilerecord.py`, `lobbymail.py`,
   `lobbyrooms.py`, `resourcestore.py`.
5. Presence and pushes: `presence.py`, `memberstatus.py`, `titlezone.py`,
   `pushchannel.py`, `pushrecord.py`, `pushspool.py`.

### The facade

`services/responders.py` is where the whole server used to live. It is now a
thin module that imports the `core` package and forwards `responders.<name>`
reads and writes to the module that owns the name. Tools and tests written as
`import responders as R` keep working, including the ones that rebind a name
to fake a session (`R._session_member_id = lambda: 7`): the write lands in the
owning module, so the code under test sees it. New code should import from
`core` directly; `tools/facade_rebind_check.py` proves the forwarding still
holds for every rebinding the tools make.

### Game titles

A title never imports the core. It registers a `titles.Title` subclass and
reaches the core through `titles.core`, whose docstring is the whole contract.
Logic that belongs to one game does not belong in `core/`; if a hook is
missing, add the hook to `titles.py` with a no-op default and use it from the
title.

The account database follows the same rule. `accounts.py` knows content codes
only as numbers; a title that issues one Content ID per character (FFXI)
declares how many on its plugin (`Title.content_slots`), and the account code
mints the extra ids when the title is granted and tops a member's handles up
at login. Operator commands that only make sense for one game live in that
game's plugin, not in `accounts.py`'s command line.

### Where state lives

Anything that must survive a restart goes in PostgreSQL through
`services/polcore/db.py`: the accounts, and saves and other stored objects
through `polcore/blobs.py`. State other containers need to see while players
are online (sessions, presence, rooms, the push queue, live-session counts)
goes in Valkey through `polcore/kv.py`, with an expiry. Do not add a JSON file
on the shared volume for either. Files are only for what the operator edits
by hand. `docs/database.md` lists the keys and the blob scopes in use, and
`tools/db_import.py` moves an old `/data` tree into the database.

The schema is numbered SQL files in `services/polcore/migrations/`. A
migration that has shipped is never edited; a schema change is a new file
with the next number. The titles keep their own sets in the same
`schema_migrations` table, which is keyed by the number alone, so a number
used twice is taken as already applied and silently skipped. Each
repository has its own range:

```
OpenLobby       0001-0999
HippaulRing     1001-1999   fe_* tables
HippaulFront    2001-2999   fmo_*
HippaulMaster   3001-3999   tm_*
HippaulHoLo     4001-4999   jan_*
HippaulDirge    5001-5999   doc_*
HippaulBridge   6001-6999   ffxi_*
```

A title that moves one of its files into PostgreSQL ships an importer for
it, run as `python <store>.py import STORE FILE`. It only reads the source,
runs in one transaction, refuses a table that already holds rows unless
given `--merge`, writes nothing with `--dry-run`, and changes nothing when
run a second time. Its command goes into `TITLES` in `tools/db_import.py`
and into the walkthrough in `docs/database.md`.

`services/live_sessions.py` belongs to the core. A title publishes its
live-session count with `live_sessions.start_heartbeat` or
`live_sessions.write_marker` and never ships a copy of the module; the title
images leave one out of the build and refuse to build if the core's has been
replaced.

## Running the checks

```
python check.py --selftest     # the hygiene scanner can fail (positive controls)
python check.py                # nothing private or proprietary in the tree
python tools/run_all.py        # every self-test; -k <substring> picks a few
```

The suites that touch the database need the Python drivers
(`pip install "psycopg[binary]" psycopg-pool valkey`) and either Docker,
which `tools/pgtest.py` uses to start throwaway PostgreSQL and Valkey
containers, or `POL_TEST_DATABASE_URL` and `POL_TEST_VALKEY_URL` naming
servers the tests may write to. `POL_TEST_REQUIRE_DB=1` makes a missing
database a failure instead of a skip; CI sets it. `tests/test_db_import.py`
rebuilds its old `/data` tree from git at a pinned commit, so a shallow
clone needs `git fetch --unshallow` first. `tools/run_all.py` points
`POL_DATA_DIR`, `POL_RESOURCE_DIR`, `POL_LOG_DIR` and `POL_LOGIN_PW_KEYFILE`
into a temporary directory of its own when they are not set, and removes it
at the end; without that a suite falls back to `/data`, which on Windows is
the root of the current drive. A value you set yourself is used as it is.

Every suite is expected to pass on a clean checkout. On Windows, `resume`
fails when the ports it uses fall in a range Windows reserves for Hyper-V
(`netsh int ipv4 show excludedportrange tcp`); that is the machine, not the
code. GitHub Actions runs the same three commands on every pull request.

A new self-test is registered by hand in `tools/run_all.py`. The list is
explicit on purpose: a suite that is not registered does not run.

## What a pull request needs

- One topic per pull request, with a subject line that says what the server
  now does differently ("Friends: a handle named in the wrong case still
  resolves").
- The checks above green, and a self-test for behaviour that can be pinned
  offline. A change to what a suite pins updates the suite in the same pull
  request.
- No Square Enix content: no client tables, captured server blobs, portal
  pages, art or fonts, and no captured packets in tests. Code that reads such
  data from the user's own install is fine.
- Nothing private: no real addresses or hostnames, no member names or ids.
  A new address becomes an environment knob with a loopback or empty default.
- Plain prose in comments and docs: say what the code does and why; leave out
  session narration.
- Behaviour that exists because a real client needs it keeps a comment saying
  which client and what happens without it. Much of this server is shaped by
  quirks of 2002-era clients, and a reader cannot tell a quirk from a mistake
  without that note.

## Reporting a bug

Open an issue with the client (Viewer build or console), the mode the server
was in, the log lines around the failure (`/logs` in the containers), and what
the client showed (a POL-xxxx code if there was one).
