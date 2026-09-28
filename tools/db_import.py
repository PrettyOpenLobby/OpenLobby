#!/usr/bin/env python3
"""Move an existing OpenLobby /data tree (SQLite and files) into PostgreSQL.

    python tools/db_import.py SOURCE [--database-url URL] [--logs DIR]
                              [--dry-run] [--merge] [--i-stopped-the-stack]
                              [--report FILE] [--json FILE]

SOURCE is a copy of the old data volume (or the volume itself, with the stack
stopped). What is read from it:

    accounts.db          every table of migration 0001, ids kept
    admin.db             the admin_* tables (migration 0003)
    discord_links.db     the discord_* tables (migration 0003)
    resources/<name>     one `blob` row per top-level file, named by
                         polcore.blobs.split_name(), updated_at = the file's mtime
    resources/tmrank/<f> one `blob` row per file, scope `tmrank`, path <f>

`resources/content-profiles.json` stays a file, and so do backup copies
(a name that goes on past its extension with `.bak`, `.pre-`, `.stale-` or
`.orig`: `*.json.bak`, `*.bin.bak-*`, `*.json.pre-*`, `*.bin.stale-*`; see
_is_backup) and `janevent.json` and `jan-rank-snapshot.json`, which
Janhourou's own import reads. Nothing live is imported
(sessions, stamps, rooms, the push spool): the services rebuild that in
Valkey. The report lists what was found and left behind. --logs names the old
logs volume, so the report can list what is there too (the push spool, the
client builds, Dirge of Cerberus's stores).

The database is POL_DATABASE_URL unless --database-url is given. Pending core
migrations are applied first.

RULES THE TOOL KEEPS

  * The source is never written: each SQLite file is opened read-only
    (`file:...?mode=ro`), and nothing under SOURCE is created or changed.
  * A source that looks live is refused: the path /data itself, a SQLite file
    with a non-empty -wal or -journal beside it, or a file written in the last
    ten minutes. --i-stopped-the-stack runs anyway, for when the stack is
    stopped and the tool runs inside a container that sees the volume at /data.
  * One transaction per source database (and one for resources). An error
    rolls that one back and is reported; resources are skipped when the
    accounts failed, since saves link to members.
  * A table that already holds rows is not imported into unless --merge is
    given. --merge inserts only the rows whose primary key is not there yet
    (for the three tables without a key, rows not already present), and
    reports keys present in both with different contents.
  * A second run over the same target finds nothing to insert and says so.
  * Rows whose parent is missing (foreign keys PostgreSQL enforces and SQLite
    did not) are reported by table and skipped. A nullable reference whose
    declared action is SET NULL or NO ACTION is cleared instead, which keeps
    the row the way deleting its parent would have. Nothing is dropped
    without a line in the report.
  * Ids are kept. Every identity sequence is then moved past the highest id,
    or past SQLite's own AUTOINCREMENT counter when that is higher, so an id
    that belonged to a deleted member is never handed out again.
  * group_member rows go in in SQLite's rowid order, which numbers `seq` in
    the order the members were first added.
  * The two tables the old account code kept as pre-rebuild copies,
    handle_profile_by_member and handle_content_pre_slots, are not imported;
    the report says when they are there.
  * --dry-run reads the target inside a transaction it rolls back and writes
    nothing, not even migrations.

Exit status: 0 done (or nothing to do), 1 a source failed, 2 refused (target
tables not empty), 3 refused (the source looks live), 4 bad arguments.

After the core import the tool prints what each title repository needs run
for its own files, in the order that matters (docs/database.md, "Moving an
existing /data").
"""
import argparse
import collections
import datetime
import hashlib
import json
import os
import pathlib
import sqlite3
import sys
import textwrap
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def _find_services():
    for cand in (os.environ.get("OPENLOBBY_SERVICES", ""),
                 os.path.join(HERE, os.pardir, "services"), "/app"):
        if cand and os.path.isdir(os.path.join(cand, "polcore")):
            return os.path.abspath(cand)
    return None


_SERVICES = _find_services()
if _SERVICES and _SERVICES not in sys.path:
    sys.path.insert(0, _SERVICES)

from polcore import blobs, db  # noqa: E402

try:
    from psycopg import sql as _sql
except ImportError:                     # polcore.db explains when it is used
    _sql = None

# --------------------------------------------------------------------------- #
# what is read, and where it goes
# --------------------------------------------------------------------------- #
ACCOUNT_TABLES = (
    "polid", "member", "handle", "login_alias", "deleted_handle", "content",
    "handle_content", "content_id_seq", "friend", "group_member", "session",
    "handle_profile", "profile", "regcode", "mail", "admin_cred",
    "login_token_client", "ext_mail_log", "login_digest_client", "login_fail",
    "list_stamp", "handle_content_trimmed", "content_character",
    # the website sign-in service's own table (regapi, not in this repository;
    # migration 0004), which it kept in accounts.db
    "web_login",
)

#: Tables the old accounts.db may hold that are left out on purpose.
ACCOUNT_LEFT_OUT = {
    "handle_profile_by_member":
        "the copy of handle_profile the 2026-08-12 rebuild kept; 0001 leaves it out",
    "handle_content_pre_slots":
        "the copy of handle_content the 2026-09-03 slot rebuild kept; 0001 leaves it out",
}

SOURCES = (
    ("accounts", "accounts.db", {t: t for t in ACCOUNT_TABLES}, ACCOUNT_LEFT_OUT),
    ("admin", "admin.db", {
        "moderator": "admin_moderator", "audit": "admin_audit",
        "code_origin": "admin_code_origin", "setting": "admin_setting",
        "push_sub": "admin_push_sub", "alerted": "admin_alerted",
        "triage": "admin_triage"}, {}),
    ("discord", "discord_links.db", {
        "link": "discord_link", "code": "discord_code",
        "notified": "discord_notified", "reply": "discord_reply",
        "sent": "discord_sent", "meta": "discord_meta"}, {}),
)

RESOURCES = "resources"
PROFILES_FILE = "content-profiles.json"
#: Subdirectories of resources/ whose files are blobs too: the directory name
#: is the scope and the file name the path (Tetra Master's weekly lists).
RESOURCE_SUBDIRS = ("tmrank",)
#: Files in resources/ that a title's own importer owns: {name: the command}.
#: They are listed as not imported and never become blob rows.
TITLE_OWNED_RESOURCES = {
    "janevent.json": "hippaulholo: janstore.py import event",
    "jan-rank-snapshot.json": "hippaulholo: janstore.py import rank_snapshot",
}


#: What starts an operator's suffix after a file's real extension:
#: <name>.bak, <name>.bak-<note>, <name>.pre-<note>, <name>.stale-<note>,
#: <name>.orig.
BACKUP_SUFFIXES = (".bak", ".pre-", ".stale-", ".orig")


def _is_backup(name):
    """An operator's copy of a file, kept beside it on the volume and never a
    record of its own. The name is a whole file name with a dot in it (a
    resource's <scope>.<path>, or <file>.<ext>) followed by a suffix that
    starts with one of BACKUP_SUFFIXES: N.jan_stats.json.bak,
    N.tm_collection.json.pre-lastplayed,
    auction-1.bids.bin.stale-settled-20260820, r1.bin.orig. A name that
    ends in .bak is a copy whatever comes before it. The dot before the
    suffix is what keeps a record whose path starts with one of these words
    (N.bakery.bin: scope N, path bakery.bin) a record."""
    if name.endswith(".bak"):
        return True
    for suffix in BACKUP_SUFFIXES:
        at = name.find(suffix, 1)
        while at > 0:
            if "." in name[1:at]:
                return True
            at = name.find(suffix, at + 1)
    return False

#: Live state that stays behind (docs/database.md): (where, name or suffix, what).
LIVE_FILES = (
    ("data", "auth-sessions.json", "login sessions; refilled at the next login"),
    ("data", "auth-stamps.json", "session stamps; a client running across the move logs in again"),
    ("data", "title-zone.json", "title zones; rebuilt as players play"),
    ("data", "member-status.json", "4:5 status; rebuilt as players play"),
    ("data", "rooms-live.json", "the open rooms"),
    ("data", "content-auth.json", "per-login content auth values"),
    ("data", "tm-matches-live.json", "Tetra Master matches in progress"),
    ("data", "*-sessions-live.json", "a service's live-session count"),
    ("logs", "push-spool.jsonl", "pushes queued before the move; let authsess drain it first"),
    ("logs", "push-spool.jsonl.offset", "the spool's read offset"),
    ("logs", "client-builds.json", "client builds; refilled at each client's next launch"),
)

#: Seconds since the last write under which a source counts as live.
RECENT_S = 600

_LOCK = "polcore.db_import"


# --------------------------------------------------------------------------- #
# the title repositories' own files (read from their READMEs and code)
# --------------------------------------------------------------------------- #
#: The compose invocation each title's README gives, run from its checkout.
_DC_ENV = ("docker compose --project-directory ../openlobby"
           " --env-file ../openlobby/.env --env-file .env"
           " -f ../openlobby/docker-compose.yml -f docker-compose.yml")
_DC = ("docker compose --project-directory ../openlobby"
       " -f ../openlobby/docker-compose.yml -f docker-compose.yml")

DOC_STORES = (
    ("doc-characters.json", "characters"), ("doc-ip-members.json", "ip_members"),
    ("doc-shop.json", "shop"), ("doc-gear.json", "gear"),
    ("doc-playtime.json", "playtime"), ("doc-stats.json", "stats"),
    ("doc-rankings.json", "rankings"), ("doc-units.json", "units"),
)

#: (title, repository, compose, [(where, file, command or None, note)], gaps).
#: `where` is data or logs (the old volumes, checked for the file) or the name
#: of another volume. A command follows "$DC " (the compose invocation); a
#: step with no command has nothing to run. A gap is a durable file the title
#: has no importer for yet. The order is the order they run in
#: (docs/database.md, "Moving an existing /data").
TITLES = (
    ("Final Fantasy XI (LSB bridge)", "hippaulbridge", _DC_ENV, [
        ("data", "ffxi_idmap.json",
         "run --rm --no-deps --entrypoint python bridge ffxidb.py import idmap "
         "/data/ffxi_idmap.json",
         "which LSB character is which Content ID. Import it BEFORE the new "
         "bridge starts for the first time: a bridge on an empty ffxi_idmap "
         "pairs every character afresh, and a character the Viewer knows "
         "under another Content ID gets POL-0001. While the table is empty "
         "and this file holds pairings, the bridge waits and says so."),
        ("crystalbridge_bridge-state volume", "ffxi_accounts.json",
         "run --rm --no-deps -v crystalbridge_bridge-state:/state:ro "
         "--entrypoint python bridge ffxidb.py import accounts "
         "/state/ffxi_accounts.json",
         "which LSB account each member has"),
    ], []),
    ("Fantasy Earth", "hippaulring", _DC, [
        ("data", "fe.db",
         "run --rm --no-deps --entrypoint python feworld fedb.py import fe_db "
         "/data/fe.db", "the characters"),
        ("data", "fe_mail.db",
         "run --rm --no-deps --entrypoint python feworld fedb.py import "
         "fe_mail_db /data/fe_mail.db", "the in-game mail"),
        ("data", "<the world state JSON files>",
         "run --rm --no-deps --entrypoint python feworld fedb.py import world "
         "/data", "the world's state, which the FE services kept as JSON "
         "files in /data"),
    ], []),
    ("Front Mission Online", "hippaulfront", _DC_ENV, [
        ("data", "fmo.db",
         "run --rm --no-deps --entrypoint python fmo fmodb.py import fmo_db "
         "/data/fmo.db",
         "the pilots and squadron insignia. Import it before fmo first "
         "starts, or the service fills the table from the older "
         "fmo_characters.json and this import is refused"),
        ("data", "fmowar.json",
         "run --rm --no-deps --entrypoint python fmo fmodb.py import war "
         "/data/fmowar.json",
         "the war state. The fmo service also imports it when it starts and "
         "finds the table empty; running it here says what came in, and "
         "compares the file with a table that already holds a state"),
        ("crystalfront_fmo-board-state volume", "fmo_*_discord.json, discord_channels.json",
         "run --rm --no-deps -v crystalfront_fmo-board-state:/state:ro "
         "--entrypoint python fmo fmodb.py import board_state /state",
         "the City Control board's Discord bookkeeping; only where the board "
         "posted to Discord"),
        ("data", "fmo_characters.json", None,
         "imported by the fmo service on its first start into an empty "
         "table; leave it in /data"),
        ("data", "fmo_sector_wins.json", None,
         "imported by the fmo service on its first start; leave it in /data"),
    ], []),
    ("Janhourou", "hippaulholo", _DC, [
        ("data", "resources/janevent.json",
         "run --rm --no-deps --entrypoint python jan janstore.py import event "
         "/data/resources/janevent.json", "the event record"),
        ("data", "resources/jan-rank-snapshot.json",
         "run --rm --no-deps --entrypoint python jan janstore.py import "
         "rank_snapshot /data/resources/jan-rank-snapshot.json",
         "the ranking's previous order"),
        ("openlobby_jan-board-state volume", "jan_*_discord.json, discord_channels.json",
         "run --rm --no-deps -v openlobby_jan-board-state:/state:ro "
         "--entrypoint python jan janstore.py import board_state /state",
         "the web board's Discord bookkeeping; only where the board posted "
         "to Discord"),
        ("data", "resources/<member>.jan_stats.json", None,
         "each member's record: a row of the blob table, which this import "
         "fills and HippaulHoLo reads; nothing to run"),
    ], []),
    ("Tetra Master", "hippaulmaster", _DC, [
        ("data", "tm-event-state.json",
         "run --rm --no-deps --entrypoint python tmrank tmstore.py import "
         "event_state /data/tm-event-state.json", "the tournament standings"),
        ("data", "tm-champion.json",
         "run --rm --no-deps --entrypoint python tmrank tmstore.py import "
         "champion /data/tm-champion.json",
         "the weekly champion the card shop names"),
        ("openlobby_tm-board-state volume", "tm_*_discord.json, discord_channels.json",
         "run --rm --no-deps -v openlobby_tm-board-state:/state:ro "
         "--entrypoint python tmrank tmstore.py import board_state /state",
         "the web board's Discord bookkeeping; only where the board posted "
         "to Discord"),
        ("data", "resources/<member>.tm_collection.json, auction-*", None,
         "the collections, saves, prize records and auction records: rows "
         "of the blob table, which this import fills and HippaulMaster "
         "reads; nothing to run"),
        ("data", "resources/tmrank/", None,
         "the weekly lists; this import copies each file into the blob table "
         "as scope tmrank, and the tmrank service rebuilds them "
         "(TM_RANK_AT=now publishes at start)"),
    ], []),
    ("Dirge of Cerberus", "hippauldirge", _DC_ENV, [
        ("logs", fn, "run --rm --no-deps --entrypoint python doc docdb.py import %s /logs/%s"
         % (store, fn), None)
        for fn, store in DOC_STORES
    ], []),
)


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
class TablePlan:
    def __init__(self, source_table, table):
        self.source_table = source_table
        self.table = table
        self.source_rows = 0
        self.target_rows = 0
        self.orphans = collections.Counter()     # "col -> parent.col": n
        self.cleared = collections.Counter()
        self.unconvertible = []                  # (rowid, reason)
        self.duplicates = 0
        self.existing_same = 0
        self.existing_differs = []               # keys
        self.to_insert = []                      # value tuples
        self.inserted = 0
        self.conflicts = []                      # keys a unique index refused
        self.raised = None                       # content_id_seq: (old, new)
        self.dropped_columns = {}                # source col: non-null count
        self.cols = ()

    def as_dict(self):
        return {
            "source_table": self.source_table, "table": self.table,
            "source_rows": self.source_rows, "target_rows_before": self.target_rows,
            "orphans": dict(self.orphans), "cleared": dict(self.cleared),
            "unconvertible": [list(u) for u in self.unconvertible],
            "duplicates": self.duplicates,
            "existing_same": self.existing_same,
            "existing_differs": [_jsonable(k) for k in self.existing_differs],
            "to_insert": len(self.to_insert), "inserted": self.inserted,
            "conflicts": [_jsonable(k) for k in self.conflicts],
            "raised": list(self.raised) if self.raised else None,
            "dropped_columns": self.dropped_columns,
        }


def _jsonable(v):
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()
    return v


class SourceResult:
    def __init__(self, name, path):
        self.name = name
        self.path = path
        self.present = False
        self.tables = []                    # TablePlan
        self.left_out = []                  # (table, rows, why)
        self.missing_tables = []            # target tables with no source table
        self.sequences = []                 # (sequence, old, new)
        self.error = None
        self.skipped = None                 # why the source was not run
        self.committed = False
        self.extra = {}                     # resources: scopes, unlinked, ...

    def changes(self):
        return (sum(t.inserted for t in self.tables)
                + sum(1 for t in self.tables if t.raised)
                + len(self.sequences))

    def planned(self):
        return (sum(len(t.to_insert) for t in self.tables)
                + sum(1 for t in self.tables if t.raised))

    def as_dict(self):
        return {"name": self.name, "path": self.path, "present": self.present,
                "tables": [t.as_dict() for t in self.tables],
                "left_out": [list(x) for x in self.left_out],
                "missing_tables": self.missing_tables,
                "sequences": [list(s) for s in self.sequences],
                "error": self.error, "skipped": self.skipped,
                "committed": self.committed, "extra": self.extra}


# --------------------------------------------------------------------------- #
# the source side
# --------------------------------------------------------------------------- #
def open_readonly(path):
    """A read-only sqlite3 connection. `mode=ro` never creates a journal or a
    -wal file and refuses every write."""
    uri = pathlib.Path(os.path.abspath(path)).as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _sqlite_tables(conn):
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
        " AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def _sqlite_columns(conn, table):
    return [r[1] for r in conn.execute('PRAGMA table_info("%s")'
                                       % table.replace('"', '""'))]


def _sqlite_sequence(conn):
    try:
        return {r[0]: int(r[1]) for r in conn.execute(
            "SELECT name, seq FROM sqlite_sequence")}
    except sqlite3.Error:
        return {}


def live_reasons(src, now=None):
    """Why SOURCE looks like a volume a running stack is using, or []."""
    now = time.time() if now is None else now
    out = []
    norm = os.path.normcase(os.path.realpath(src))
    if norm == os.path.normcase(os.path.realpath("/data")):
        out.append("it is /data, where the containers see the data volume")
    newest, newest_name = 0.0, None
    for _name, fn, _t, _l in SOURCES:
        p = os.path.join(src, fn)
        for side in ("-wal", "-journal"):
            try:
                if os.path.getsize(p + side) > 0:
                    out.append(f"{fn}{side} is not empty (a writer has {fn} "
                               "open, or crashed with it open)")
            except OSError:
                pass
        try:
            m = os.path.getmtime(p)
            if m > newest:
                newest, newest_name = m, fn
        except OSError:
            pass
    res = os.path.join(src, RESOURCES)
    for sub in ("",) + RESOURCE_SUBDIRS:
        try:
            with os.scandir(os.path.join(res, sub)) as it:
                for e in it:
                    try:
                        if e.is_file(follow_symlinks=False):
                            m = e.stat(follow_symlinks=False).st_mtime
                            if m > newest:
                                newest, newest_name = m, "/".join(
                                    p for p in (RESOURCES, sub, e.name) if p)
                    except OSError:
                        pass
        except OSError:
            pass
    if newest and now - newest < RECENT_S:
        out.append(f"{newest_name} was written {int(now - newest)} s ago")
    return out


# --------------------------------------------------------------------------- #
# the target side
# --------------------------------------------------------------------------- #
def _q(name):
    return _sql.Identifier(name)


def _target_columns(conn, table):
    """[(name, data_type, nullable, is_identity)] in table order."""
    return [(r["column_name"], r["data_type"], r["is_nullable"] == "YES",
             r["is_identity"] == "YES") for r in conn.execute(
        "SELECT column_name, data_type, is_nullable, is_identity"
        " FROM information_schema.columns"
        " WHERE table_schema = current_schema() AND table_name = %s"
        " ORDER BY ordinal_position", (table,)).fetchall()]


def _primary_key(conn, table):
    return [r["attname"] for r in conn.execute(
        "SELECT a.attname FROM pg_index i"
        " JOIN pg_attribute a ON a.attrelid = i.indrelid"
        "  AND a.attnum = ANY(i.indkey::int2[])"
        " WHERE i.indrelid = to_regclass(%s) AND i.indisprimary"
        " ORDER BY array_position(i.indkey::int2[], a.attnum)",
        (table,)).fetchall()]


FK = collections.namedtuple("FK", "table cols parent pcols action")


def _foreign_keys(conn):
    rows = conn.execute(
        "SELECT cl.relname AS child, pl.relname AS parent,"
        " c.confdeltype::text AS action,"
        " ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY k(n, o)"
        "   JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.n"
        "   ORDER BY k.o)::text[] AS cols,"
        " ARRAY(SELECT a.attname FROM unnest(c.confkey) WITH ORDINALITY k(n, o)"
        "   JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.n"
        "   ORDER BY k.o)::text[] AS pcols"
        " FROM pg_constraint c"
        " JOIN pg_class cl ON cl.oid = c.conrelid"
        " JOIN pg_class pl ON pl.oid = c.confrelid"
        " JOIN pg_namespace n ON n.oid = cl.relnamespace"
        " WHERE c.contype = 'f' AND n.nspname = current_schema()"
        " ORDER BY c.conname").fetchall()
    return [FK(r["child"], tuple(r["cols"]), r["parent"], tuple(r["pcols"]),
               r["action"]) for r in rows]


def _count(conn, table):
    return conn.execute(_sql.SQL("SELECT count(*) AS n FROM {}").format(
        _q(table))).fetchone()["n"]


def _key_set(conn, table, cols):
    q = _sql.SQL("SELECT {} FROM {}").format(
        _sql.SQL(", ").join(_q(c) for c in cols), _q(table))
    return {tuple(r[c] for c in cols) for r in conn.execute(q).fetchall()}


def _apply_pending_migrations(conn, log):
    """Inside the caller's transaction (dry run): run the core migrations the
    target lacks, so the plan can read the tables. The caller rolls back."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                 " version INTEGER PRIMARY KEY, name TEXT NOT NULL,"
                 " checksum TEXT NOT NULL,"
                 " applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
    have = {r["version"] for r in conn.execute(
        "SELECT version FROM schema_migrations").fetchall()}
    pending = [f for f in db.migration_files() if f[0] not in have]
    for version, name, path in pending:
        text, checksum = db._read_migration(path)
        conn.execute(text)
        conn.execute("INSERT INTO schema_migrations (version, name, checksum)"
                     " VALUES (%s, %s, %s)", (version, name, checksum))
    if pending:
        log("dry run: the target lacks migration(s) %s; planned against them "
            "inside a transaction that is rolled back"
            % ", ".join(n for _v, n, _p in pending))
    return [n for _v, n, _p in pending]


# --------------------------------------------------------------------------- #
# values
# --------------------------------------------------------------------------- #
class Unconvertible(ValueError):
    pass


_INT_RANGE = {"integer": (-(1 << 31), (1 << 31) - 1),
              "smallint": (-(1 << 15), (1 << 15) - 1),
              "bigint": (-(1 << 63), (1 << 63) - 1)}


def coerce(value, data_type):
    """A SQLite value as the target column's type. SQLite stores whatever it
    is given; PostgreSQL checks. Text stays text as it was (timestamps
    included)."""
    if value is None:
        return None
    if data_type in _INT_RANGE:
        if isinstance(value, bool):
            value = int(value)
        elif isinstance(value, float):
            if not value.is_integer():
                raise Unconvertible(f"{value!r} is not a whole number")
            value = int(value)
        elif isinstance(value, str):
            try:
                value = int(value.strip())
            except ValueError:
                raise Unconvertible(f"{value!r} is not an integer") from None
        elif isinstance(value, (bytes, memoryview)):
            raise Unconvertible("bytes in an integer column")
        lo, hi = _INT_RANGE[data_type]
        if not lo <= value <= hi:
            raise Unconvertible(f"{value} does not fit {data_type}")
        return value
    if data_type == "double precision":
        try:
            return float(value)
        except (TypeError, ValueError):
            raise Unconvertible(f"{value!r} is not a number") from None
    if data_type == "text":
        if isinstance(value, str):
            if "\x00" in value:
                raise Unconvertible("a NUL character, which text cannot hold")
            return value
        if isinstance(value, (bytes, memoryview)):
            try:
                return coerce(bytes(value).decode("utf-8"), "text")
            except UnicodeDecodeError:
                raise Unconvertible("bytes that are not UTF-8 in a text column") from None
        return str(value)
    if data_type == "bytea":
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value)
        if isinstance(value, str):
            return value.encode("utf-8")
        raise Unconvertible(f"{value!r} in a bytea column")
    return value


def _norm(value):
    """For comparing a source value with what PostgreSQL returned."""
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, float) and value.is_integer():
        return value
    return value


# --------------------------------------------------------------------------- #
# planning and running one SQLite source
# --------------------------------------------------------------------------- #
def _order(tables, fks):
    """`tables` with every parent before its children (the FKs among them)."""
    deps = {t: {fk.parent for fk in fks if fk.table == t and fk.parent != t
                and fk.parent in tables} for t in tables}
    out, done = [], set()
    while len(out) < len(tables):
        ready = [t for t in tables if t not in done and deps[t] <= done]
        if not ready:                              # a cycle: keep the given order
            ready = [t for t in tables if t not in done][:1]
        for t in ready:
            out.append(t)
            done.add(t)
    return out


def _read_source_rows(sconn, stable, cols):
    q = 'SELECT rowid, %s FROM "%s" ORDER BY rowid' % (
        ", ".join('"%s"' % c.replace('"', '""') for c in cols),
        stable.replace('"', '""'))
    return sconn.execute(q).fetchall()


def plan_table(conn, sconn, stable, table, fks, parent_keys):
    """Everything about one table short of writing it."""
    plan = TablePlan(stable, table)
    tcols = _target_columns(conn, table)
    ttype = {c: t for c, t, _n, _i in tcols}
    tnull = {c: n for c, _t, n, _i in tcols}
    scols = _sqlite_columns(sconn, stable)
    cols = [c for c in scols if c in ttype]
    plan.cols = tuple(cols)
    idx = {c: i for i, c in enumerate(cols)}
    pk = _primary_key(conn, table)
    if not all(c in idx for c in pk):
        pk = []                                     # key not fully in the source
    for c in scols:
        if c not in ttype:
            n = sconn.execute('SELECT count(*) FROM "%s" WHERE "%s" IS NOT NULL'
                              % (stable, c)).fetchone()[0]
            plan.dropped_columns[c] = n

    rows = []
    for raw in _read_source_rows(sconn, stable, cols):
        rowid, vals = raw[0], raw[1:]
        try:
            rows.append((rowid, [coerce(v, ttype[c]) for v, c in zip(vals, cols)]))
        except Unconvertible as exc:
            plan.unconvertible.append((rowid, str(exc)))
    plan.source_rows = len(rows) + len(plan.unconvertible)

    # parents first: a row whose parent is missing is skipped, or loses the
    # reference when the column may be NULL and deleting the parent would
    # have left it NULL
    my_fks = [fk for fk in fks if fk.table == table and all(c in idx for c in fk.cols)]
    kept = []
    for rowid, vals in rows:
        skip = False
        for fk in my_fks:
            key = tuple(vals[idx[c]] for c in fk.cols)
            if any(v is None for v in key):
                continue
            if key in parent_keys.get((fk.parent, fk.pcols), ()):
                continue
            label = "%s -> %s.%s" % (",".join(fk.cols), fk.parent, ",".join(fk.pcols))
            if all(tnull[c] for c in fk.cols) and fk.action in ("n", "a", "r"):
                for c in fk.cols:
                    vals[idx[c]] = None
                plan.cleared[label] += 1
            else:
                plan.orphans[label] += 1
                skip = True
                break
        if not skip:
            kept.append(vals)

    plan.target_rows = _count(conn, table)
    if pk:
        have = _key_set(conn, table, pk) if plan.target_rows else set()
        seen, existing = set(), []
        for vals in kept:
            key = tuple(vals[idx[c]] for c in pk)
            if key in seen:
                plan.duplicates += 1
                continue
            seen.add(key)
            if key in have:
                existing.append((key, vals))
            else:
                plan.to_insert.append(tuple(vals))
        if existing:
            q = _sql.SQL("SELECT {} FROM {}").format(
                _sql.SQL(", ").join(_q(c) for c in cols), _q(table))
            current = {tuple(r[c] for c in pk): tuple(_norm(r[c]) for c in cols)
                       for r in conn.execute(q).fetchall()}
            for key, vals in existing:
                if current.get(key) == tuple(_norm(v) for v in vals):
                    plan.existing_same += 1
                elif table == "content_id_seq":
                    old = current[key][idx["next_id"]]
                    new = vals[idx["next_id"]]
                    if new > old:
                        plan.raised = (old, new)
                    else:
                        plan.existing_same += 1
                else:
                    plan.existing_differs.append(key)
    else:
        have = collections.Counter()
        if plan.target_rows:
            q = _sql.SQL("SELECT {} FROM {}").format(
                _sql.SQL(", ").join(_q(c) for c in cols), _q(table))
            have = collections.Counter(tuple(_norm(r[c]) for c in cols)
                                       for r in conn.execute(q).fetchall())
        for vals in kept:
            t = tuple(_norm(v) for v in vals)
            if have[t] > 0:
                have[t] -= 1
                plan.existing_same += 1
            else:
                plan.to_insert.append(tuple(vals))
    plan.pk = pk
    return plan


def run_table(conn, plan):
    """Insert what the plan found missing. A unique index the plan could not
    see (a login name, a Content ID) refuses a row instead of failing the
    database; those rows are counted and listed."""
    if plan.raised:
        conn.execute("UPDATE content_id_seq SET next_id = %s WHERE id = 1",
                     (plan.raised[1],))
    if not plan.to_insert:
        return
    before = _count(conn, plan.table)
    q = _sql.SQL("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT DO NOTHING").format(
        _q(plan.table), _sql.SQL(", ").join(_q(c) for c in plan.cols),
        _sql.SQL(", ").join(_sql.Placeholder() * len(plan.cols)))
    with conn.cursor() as cur:
        cur.executemany(q, plan.to_insert)
    plan.inserted = _count(conn, plan.table) - before
    if plan.inserted < len(plan.to_insert):
        idx = {c: i for i, c in enumerate(plan.cols)}
        if plan.pk:
            landed = _key_set(conn, plan.table, plan.pk)
            plan.conflicts = [k for k in (tuple(v[idx[c]] for c in plan.pk)
                                          for v in plan.to_insert)
                              if k not in landed]
        else:
            plan.conflicts = [None] * (len(plan.to_insert) - plan.inserted)


def _identity_columns(conn, table):
    return [c for c, _t, _n, ident in _target_columns(conn, table) if ident]


def advance_sequences(conn, tables, floors, apply):
    """Move each identity sequence past the highest id in its table, or past
    SQLite's AUTOINCREMENT counter for it when that is higher. Returns
    [(sequence, old, new)] for the ones that move."""
    moved = []
    for table in tables:
        for col in _identity_columns(conn, table):
            seq = conn.execute("SELECT pg_get_serial_sequence(%s, %s) AS s",
                               (table, col)).fetchone()["s"]
            if not seq:
                continue
            top = conn.execute(_sql.SQL("SELECT max({}) AS m FROM {}").format(
                _q(col), _q(table))).fetchone()["m"] or 0
            if col == "id":
                top = max(top, floors.get(table, 0))
            row = conn.execute(
                "SELECT last_value FROM pg_sequences"
                " WHERE quote_ident(schemaname) || '.' || quote_ident(sequencename) = %s"
                "    OR schemaname || '.' || sequencename = %s", (seq, seq)).fetchone()
            current = (row["last_value"] if row else None) or 0
            if top > current:
                if apply:
                    conn.execute("SELECT setval(%s, %s, true)", (seq, top))
                moved.append((seq, current, top))
    return moved


def import_sqlite(conn, res, table_map, left_out, apply, log):
    """Plan (and with `apply`, run) one SQLite source on `conn`, which is
    inside the caller's transaction."""
    sconn = open_readonly(res.path)
    try:
        stables = _sqlite_tables(sconn)
        fks = _foreign_keys(conn)
        targets = [table_map[s] for s in stables if s in table_map]
        by_target = {table_map[s]: s for s in stables if s in table_map}
        for s in stables:
            if s in table_map:
                continue
            n = sconn.execute('SELECT count(*) FROM "%s"' % s.replace('"', '""')).fetchone()[0]
            res.left_out.append((s, n, left_out.get(s, "no such table in the new schema")))
        res.missing_tables = sorted(set(table_map.values()) - set(targets))
        order = _order(targets, fks)
        parents = {(fk.parent, fk.pcols) for fk in fks if fk.table in order}
        parent_keys = {p: _key_set(conn, p[0], p[1]) for p in parents}
        for table in order:
            plan = plan_table(conn, sconn, by_target[table], table, fks,
                              parent_keys)
            res.tables.append(plan)
            if apply:
                run_table(conn, plan)
            for key in [p for p in parents if p[0] == table]:
                if apply:
                    parent_keys[key] = _key_set(conn, key[0], key[1])
                else:
                    idx = {c: i for i, c in enumerate(plan.cols)}
                    if all(c in idx for c in key[1]):
                        parent_keys[key] |= {tuple(v[idx[c]] for c in key[1])
                                             for v in plan.to_insert}
        floors = {table_map[k]: v for k, v in _sqlite_sequence(sconn).items()
                  if k in table_map}
        res.sequences = advance_sequences(conn, order, floors, apply)
    finally:
        sconn.close()


# --------------------------------------------------------------------------- #
# resources
# --------------------------------------------------------------------------- #
def _scope_kind(scope):
    if scope.isdigit():
        return "member"
    if scope in ("shared", blobs.MAIL_SCOPE):
        return scope
    if scope.startswith("s") and len(scope) > 1 and all(
            ch in "0123456789abcdef" for ch in scope[1:]):
        return "lobby list"
    return "other"


def _resource_files(root):
    """([(DirEntry, scope, path)], [(name, why)]) for resources/: each
    top-level file named <scope>.<path>, and each file of a RESOURCE_SUBDIRS
    directory under that directory's name as its scope."""
    files, skipped = [], []
    with os.scandir(root) as it:
        entries = sorted(it, key=lambda e: e.name)
    for e in entries:
        if e.is_symlink():
            skipped.append((e.name, "a symbolic link"))
        elif e.is_dir() and e.name in RESOURCE_SUBDIRS:
            with os.scandir(e.path) as it2:
                for f in sorted(it2, key=lambda x: x.name):
                    name = e.name + "/" + f.name
                    if f.is_symlink():
                        skipped.append((name, "a symbolic link"))
                    elif f.is_dir():
                        skipped.append((name + "/", "a directory inside %s/" % e.name))
                    elif _is_backup(f.name):
                        skipped.append((name, "a backup copy (.bak, .pre-, .stale-, .orig), not a record"))
                    else:
                        files.append((f, e.name, f.name))
        elif e.is_dir():
            skipped.append((e.name + "/", "a directory (only top-level files and %s "
                            "are resources)" % ", ".join(d + "/" for d in RESOURCE_SUBDIRS)))
        elif e.name == PROFILES_FILE:
            skipped.append((e.name, "stays a file (core/pfc.py)"))
        elif e.name in TITLE_OWNED_RESOURCES:
            skipped.append((e.name, "the title's own import reads it (%s)"
                            % TITLE_OWNED_RESOURCES[e.name]))
        elif _is_backup(e.name):
            skipped.append((e.name, "a backup copy (.bak, .pre-, .stale-, .orig), not a record"))
        elif blobs.split_name(e.name) is None:
            skipped.append((e.name, "not a resource name (no scope before a dot)"))
        else:
            files.append((e,) + blobs.split_name(e.name))
    return files, skipped


def import_resources(conn, res, apply, log, new_members=()):
    files, skipped = _resource_files(res.path)
    plan = TablePlan(RESOURCES + "/", "blob")
    plan.pk = ["scope", "path"]
    plan.cols = ("scope", "path", "member_id", "data", "updated_at")
    plan.source_rows = len(files)
    plan.target_rows = _count(conn, "blob")
    current = {}
    if plan.target_rows:
        current = {(r["scope"], r["path"]): r["h"] for r in conn.execute(
            "SELECT scope, path, md5(data) AS h FROM blob").fetchall()}
    members = {r["id"] for r in conn.execute("SELECT id FROM member").fetchall()}
    members |= set(new_members)          # a dry run: the accounts it would add
    kinds, others, unlinked = collections.Counter(), collections.Counter(), []
    for e, scope, path in files:
        with open(e.path, "rb") as fh:
            data = fh.read()
        mtime = e.stat(follow_symlinks=False).st_mtime
        kind = _scope_kind(scope)
        kinds[kind] += 1
        if kind == "other":
            others[scope] += 1
        mid = int(scope) if scope.isdigit() else None
        if mid is not None and mid not in members:
            unlinked.append(e.name)
        key = (scope, path)
        if key in current:
            if current[key] == hashlib.md5(data).hexdigest():
                plan.existing_same += 1
            else:
                plan.existing_differs.append(key)
            continue
        plan.to_insert.append((scope, path, mid, data, mtime))
    res.tables.append(plan)
    res.left_out.extend((n, None, why) for n, why in skipped)
    res.extra = {"by_scope": dict(kinds), "other_scopes": dict(others),
                 "not_linked": unlinked}
    if apply and plan.to_insert:
        before = plan.target_rows
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO blob (scope, path, member_id, data, updated_at)"
                " VALUES (%s, %s, (SELECT id FROM member WHERE id = %s), %s,"
                " to_timestamp(%s)) ON CONFLICT DO NOTHING", plan.to_insert)
        plan.inserted = _count(conn, "blob") - before
        if plan.inserted < len(plan.to_insert):
            landed = _key_set(conn, "blob", ["scope", "path"])
            plan.conflicts = [(s, p) for s, p, *_r in plan.to_insert
                              if (s, p) not in landed]


# --------------------------------------------------------------------------- #
# the run
# --------------------------------------------------------------------------- #
def _sources(src):
    out = []
    for name, fn, tmap, left in SOURCES:
        r = SourceResult(name, os.path.join(src, fn))
        r.present = os.path.isfile(r.path)
        out.append((r, tmap, left))
    r = SourceResult("resources", os.path.join(src, RESOURCES))
    r.present = os.path.isdir(r.path)
    out.append((r, None, None))
    return out


def run(src, merge=False, dry_run=False, log=print):
    """Import `src`. Returns (results, status) where status is 'done',
    'nothing', 'dry-run', 'refused' or 'failed'."""
    sources = _sources(src)
    # 1. the plan, with nothing kept: every source against the target as it
    #    is, inside one transaction that always rolls back.
    planned = []
    with db.connect() as conn:
        try:
            with conn.transaction():
                _apply_pending_migrations(conn, log if dry_run else (lambda *_: None))
                new_members = set()
                for res, tmap, left in sources:
                    if not res.present:
                        planned.append(res)
                        continue
                    try:
                        with conn.transaction():         # a savepoint each
                            if tmap is None:
                                import_resources(conn, res, False, log,
                                                 new_members=new_members)
                            else:
                                import_sqlite(conn, res, tmap, left, False, log)
                    except Exception as exc:     # noqa: BLE001 -- reported
                        res.error = f"{type(exc).__name__}: {exc}"
                    for t in res.tables:
                        if t.table == "member" and "id" in t.cols:
                            i = t.cols.index("id")
                            new_members |= {v[i] for v in t.to_insert}
                    planned.append(res)
                raise _Rollback()
        except _Rollback:
            pass
    busy = [(res.name, t.table, t.target_rows) for res in planned for t in res.tables
            if t.to_insert and t.target_rows]
    if dry_run:
        return planned, "dry-run"
    if busy and not merge:
        return planned, "refused"
    if all(res.planned() == 0 and not res.sequences for res in planned) \
            and not any(res.error for res in planned):
        return planned, "nothing"

    # 2. the import: migrations for real, then one transaction per source
    db.migrate(log=lambda m: log("  " + m))
    results = []
    accounts_failed = False
    for res0, tmap, left in sources:
        res = SourceResult(res0.name, res0.path)
        res.present = res0.present
        if not res.present:
            results.append(res)
            continue
        if tmap is None and accounts_failed:
            res.skipped = ("not imported: accounts.db failed, and saves link to "
                           "members; fix that and run again")
            results.append(res)
            continue
        try:
            with db.connect() as conn:
                with conn.transaction():
                    db.lock(conn, _LOCK)
                    if tmap is None:
                        import_resources(conn, res, True, log)
                    else:
                        import_sqlite(conn, res, tmap, left, True, log)
            res.committed = True
        except Exception as exc:                # noqa: BLE001 -- reported
            res.error = f"{type(exc).__name__}: {exc}"
            res.committed = False
            if res.name == "accounts":
                accounts_failed = True
        results.append(res)
    status = "failed" if any(r.error for r in results) else (
        "done" if any(r.changes() for r in results) else "nothing")
    return results, status


class _Rollback(Exception):
    pass


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #
def _found(src, logs):
    """{(where, name): exists} for the live and title files."""
    def has(where, name):
        base = src if where == "data" else logs if where == "logs" else None
        if base is None:
            return None
        if "*" in name:
            head, tail = name.split("*", 1)
            try:
                return any(n.startswith(head) and n.endswith(tail)
                           for n in os.listdir(base))
            except OSError:
                return False
        return os.path.exists(os.path.join(base, name))
    return has


def render(src, results, status, merge, dry_run, logs=None):
    L = []
    w = L.append
    w("Import of %s" % src)
    w("")
    for res in results:
        w("== %s (%s)" % (res.name, os.path.relpath(res.path, src)))
        if not res.present:
            w("   not in the source; nothing to import")
            w("")
            continue
        if res.skipped:
            w("   " + res.skipped)
            w("")
            continue
        if res.error:
            w("   FAILED, rolled back: %s" % res.error)
        if res.tables:
            w("   %-24s %7s %7s %7s %8s %8s %8s" % (
                "table", "source", "orphan", "target", "present", "insert",
                "inserted" if not dry_run else ""))
            for t in res.tables:
                w("   %-24s %7d %7d %7d %8d %8d %8s" % (
                    t.table, t.source_rows, sum(t.orphans.values()),
                    t.target_rows, t.existing_same + len(t.existing_differs),
                    len(t.to_insert),
                    "" if dry_run else str(t.inserted if res.committed else 0)))
        orphans = [(t.table, k, n) for t in res.tables for k, n in t.orphans.items()]
        if orphans:
            w("   Skipped, parent missing (PostgreSQL enforces the reference):")
            for table, k, n in orphans:
                w("     %-22s %5d  %s" % (table, n, k))
        cleared = [(t.table, k, n) for t in res.tables for k, n in t.cleared.items()]
        if cleared:
            w("   Reference cleared, row kept (the parent is gone):")
            for table, k, n in cleared:
                w("     %-22s %5d  %s" % (table, n, k))
            if any(t == "regcode" for t, _k, _n in cleared):
                w("     (a registration code keeps redeemed_at, so it stays spent)")
        for t in res.tables:
            if t.unconvertible:
                w("   Skipped, value the column cannot hold: %s %d row(s)"
                  % (t.table, len(t.unconvertible)))
                for rowid, why in t.unconvertible[:10]:
                    w("     rowid %s: %s" % (rowid, why))
            if t.duplicates:
                w("   Skipped, key repeated in the source: %s %d row(s)"
                  % (t.table, t.duplicates))
            if t.conflicts:
                w("   Skipped, refused by a unique index: %s %d row(s) %s"
                  % (t.table, len(t.conflicts),
                     ", ".join(str(k) for k in t.conflicts[:10] if k is not None)))
            if t.existing_differs:
                w("   Kept the target's row, the source's differs: %s %d key(s) %s"
                  % (t.table, len(t.existing_differs),
                     ", ".join(str(k) for k in t.existing_differs[:10])))
            if t.raised:
                w("   content_id_seq: next_id %s -> %s (the higher of the two)"
                  % t.raised)
            if t.dropped_columns:
                w("   Column(s) the target has no place for, in %s: %s" % (
                    t.table, ", ".join("%s (%d non-empty)" % kv
                                       for kv in t.dropped_columns.items())))
        if res.left_out:
            w("   Not imported:")
            for name, n, why in res.left_out:
                w("     %-28s %s%s" % (name, "" if n is None else "%d row(s): " % n, why))
        if res.missing_tables:
            w("   No such table in the source (left empty): %s"
              % ", ".join(res.missing_tables))
        if res.name == "resources" and res.extra:
            w("   By scope: %s" % ", ".join("%s %d" % kv for kv in
                                           sorted(res.extra["by_scope"].items())))
            if res.extra.get("other_scopes"):
                w("   Title records copied by name (the titles read them from"
                  " the blob table): %s" % ", ".join(
                      "%s %d" % kv for kv in sorted(res.extra["other_scopes"].items())))
            if res.extra.get("not_linked"):
                w("   Stored, not linked to a member (that member no longer"
                  " exists): %d, e.g. %s" % (len(res.extra["not_linked"]),
                                             ", ".join(res.extra["not_linked"][:5])))
        for seq, old, new in res.sequences:
            w("   Sequence %s: %s -> %s" % (seq, old, new))
        w("")

    has = _found(src, logs)
    live = [(where, name, what) for where, name, what in LIVE_FILES if has(where, name)]
    if live:
        w("Live state left behind (the services rebuild it in Valkey):")
        for where, name, what in live:
            w("   %s/%s: %s" % (where, name, what))
        w("")

    if dry_run:
        w("Dry run: nothing was written.")
    elif status == "refused":
        busy = sorted({"%s (%d rows)" % (t.table, t.target_rows) for r in results
                       for t in r.tables if t.to_insert and t.target_rows})
        w("REFUSED: these target tables already hold rows and the source has "
          "rows they lack: %s." % ", ".join(busy))
        w("Nothing was written. Import into an empty database, or run again with "
          "--merge to add only the rows whose key is not there yet.")
    elif status == "nothing":
        w("Nothing to import: the target already holds every row in the source. "
          "Nothing was changed.")
    elif status == "failed":
        w("FAILED: %s. Each failed source was rolled back; the others were "
          "committed." % ", ".join(r.name for r in results if r.error))
    else:
        w("Done: %d row(s) written." % sum(t.inserted for r in results
                                          for t in r.tables if r.committed))
    w("")
    w(render_titles(has))
    return "\n".join(L)


def render_titles(has):
    L = []
    w = L.append

    def para(text, indent):
        for line in textwrap.wrap(text, 78 - len(indent)):
            w(indent + line)

    w("Next: each title's own files. Run these from that title's checkout,")
    w("beside openlobby/, with only postgres running. The order matters:")
    w("")
    w("  1. This import, the core, comes first: the titles' rows refer to")
    w("     members.")
    w("  2. FFXI: the bridge's two maps, before the new bridge starts for the")
    w("     first time.")
    w("  3. Fantasy Earth, Front Mission Online, Janhourou and Tetra Master:")
    w("     their files, before each title starts for the first time.")
    w("  4. Dirge of Cerberus: its stores, while the doc responder is stopped.")
    w("  5. Then start the stack and the titles.")
    w("")
    w("Each title's import reads its source without changing it, runs in one")
    w("transaction, refuses a table that already holds rows unless given")
    w("--merge, and writes nothing with --dry-run.")
    w("")
    for title, repo, dc, files, gaps in TITLES:
        w("%s (%s)" % (title, repo))
        if any(cmd for _w, _n, cmd, _note in files):
            w('  DC="%s"' % dc)
        for where, name, cmd, note in files:
            if where in ("data", "logs"):
                shown = "%s/%s" % (where, name)
                found = None if any(ch in name for ch in "<*,") else has(where, name)
            else:
                shown, found = "%s (%s)" % (name, where), None
            w("  %s%s" % (shown, "" if found is None
                          else " [found]" if found else " [not there]"))
            if note:
                para(note, "      ")
            if cmd:
                w("      $DC " + cmd)
        for gap in gaps:
            para("GAP: " + gap, "  ")
        w("")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="db_import.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="the old /data tree (a copy, or the volume "
                                   "with the stack stopped)")
    ap.add_argument("--database-url", help="the target (default POL_DATABASE_URL)")
    ap.add_argument("--logs", help="the old /logs tree, for the report")
    ap.add_argument("--dry-run", action="store_true",
                    help="plan and report; write nothing")
    ap.add_argument("--merge", action="store_true",
                    help="import into tables that already hold rows, adding "
                         "only rows whose key is not there")
    ap.add_argument("--i-stopped-the-stack", dest="stopped", action="store_true",
                    help="run even though the source looks like a live volume")
    ap.add_argument("--report", metavar="FILE", help="also write the report here")
    ap.add_argument("--json", metavar="FILE",
                    help="write the full result as JSON (for scripts and tests)")
    args = ap.parse_args(argv)

    src = os.path.abspath(args.source)
    if not os.path.isdir(src):
        print("error: %s is not a directory" % src, file=sys.stderr)
        return 4
    for p in (args.report, args.json):
        if p and os.path.abspath(p).startswith(src + os.sep):
            print("error: %s is inside the source, which this tool never writes"
                  % p, file=sys.stderr)
            return 4
    if not any(os.path.exists(os.path.join(src, fn)) for _n, fn, _t, _l in SOURCES) \
            and not os.path.isdir(os.path.join(src, RESOURCES)):
        print("error: %s holds none of accounts.db, admin.db, discord_links.db "
              "or resources/; is it the data volume?" % src, file=sys.stderr)
        return 4
    reasons = live_reasons(src)
    if reasons and not args.stopped:
        print("REFUSED: %s looks like a volume a running stack is using:" % src,
              file=sys.stderr)
        for r in reasons:
            print("  - " + r, file=sys.stderr)
        print("Stop the stack (docker compose stop), then run again with "
              "--i-stopped-the-stack. Nothing was read or written.",
              file=sys.stderr)
        return 3
    if args.database_url:
        db.configure(args.database_url)
    try:
        db.database_url()
    except db.DatabaseNotConfigured as exc:
        print("error: %s (or pass --database-url)" % exc, file=sys.stderr)
        return 4

    try:
        results, status = run(src, merge=args.merge, dry_run=args.dry_run,
                              log=lambda m: print(m, flush=True))
    finally:
        db.close()
    text = render(src, results, status, args.merge, args.dry_run, logs=args.logs)
    print(text)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"source": src, "status": status, "merge": args.merge,
                       "dry_run": args.dry_run,
                       "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                       "sources": [r.as_dict() for r in results]}, fh, indent=1)
    if status == "dry-run" and any(r.error for r in results):
        return 1                        # the real run would fail the same way
    return {"done": 0, "nothing": 0, "dry-run": 0, "refused": 2,
            "failed": 1}[status]


if __name__ == "__main__":
    sys.exit(main())
