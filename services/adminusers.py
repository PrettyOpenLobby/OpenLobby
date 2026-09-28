"""Moderator logins, the admin panel's audit log, and who made each code.

The admin panel has ONE owner login (accounts.admin_cred, plus the env
break-glass pair). This module adds MODERATORS: separate logins, each with a
fixed set of permissions the panel checks on every request (see PERMS and
admin.Handler._permit). The owner creates and manages them on the Security tab.

It lives in its own database, `data/admin.db`, and not in accounts.db, on
purpose. accounts.py is imported by login and authsess, and prod's git-sync
restarts both whenever it changes -- which ends every live game. Nothing in
here is read by any service but the panel, so it has no business forcing
that restart.

Stdlib only, like the rest of services/. Passwords use the same PBKDF2 helper
and policy as the owner credential (accounts.hash_password,
accounts.check_admin_password_policy).
"""
import json
import os
import sqlite3
import threading
import time

import accounts

DB = os.environ.get(
    "POL_ADMIN_DB",
    os.path.join(os.environ.get("POL_DATA_DIR", "/data"), "admin.db"))

#: What a moderator can be allowed to do. The key is what the panel checks; the
#: label is what the owner sees. Anything not covered by one of these is the
#: OWNER's alone -- creating/deleting accounts, grants, passwords and tokens,
#: outside mail, news, PML, and managing moderators.
PERMS = {
    "gm": "GM desk: answer GM calls, the duty switch, chat",
    "codes": "Make registration codes (and see the ones they made)",
    "reports": "Read user reports and tester issue reports",
    "accounts_view": "Look up accounts (read-only)",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS moderator (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pw_hash       TEXT NOT NULL,
    pw_salt       TEXT NOT NULL,
    perms         TEXT NOT NULL DEFAULT '',
    code_limit    INTEGER,              -- codes per rolling 24 h; NULL = no limit
    disabled      INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    created_by    TEXT,
    pw_changed_at REAL NOT NULL         -- part of a session's credential tie
);
CREATE TABLE IF NOT EXISTS audit (
    id      INTEGER PRIMARY KEY,
    at      REAL NOT NULL,
    actor   TEXT NOT NULL,
    role    TEXT NOT NULL,
    action  TEXT NOT NULL,
    target  TEXT,
    detail  TEXT,
    ok      INTEGER NOT NULL,
    addr    TEXT
);
CREATE INDEX IF NOT EXISTS audit_at ON audit(at);
CREATE INDEX IF NOT EXISTS audit_actor ON audit(actor);
CREATE TABLE IF NOT EXISTS code_origin (
    code  TEXT PRIMARY KEY COLLATE NOCASE,
    by    TEXT NOT NULL,
    role  TEXT NOT NULL,
    at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS code_origin_by ON code_origin(by, at);
CREATE TABLE IF NOT EXISTS setting (
    key   TEXT PRIMARY KEY,
    value TEXT
);
-- One row per browser that asked for GM-call alerts (Web Push).
CREATE TABLE IF NOT EXISTS push_sub (
    endpoint   TEXT PRIMARY KEY,
    p256dh     TEXT NOT NULL,
    auth       TEXT NOT NULL,
    username   TEXT NOT NULL,
    role       TEXT NOT NULL,
    uid        INTEGER,
    origin     TEXT,
    ua         TEXT,
    created_at REAL NOT NULL,
    last_ok    REAL,
    last_error TEXT
);
-- GM-call tickets already announced, so a restart never alerts twice.
CREATE TABLE IF NOT EXISTS alerted (
    ticket  TEXT PRIMARY KEY,
    at      REAL NOT NULL,
    result  TEXT
);
"""

#: Columns added after the first release of this file: (table, column, DDL).
_MIGRATIONS = (
    ("moderator", "titles",
     "ALTER TABLE moderator ADD COLUMN titles TEXT NOT NULL DEFAULT ''"),
    ("code_origin", "expires_at",
     "ALTER TABLE code_origin ADD COLUMN expires_at REAL"),
    ("code_origin", "expired",
     "ALTER TABLE code_origin ADD COLUMN expired INTEGER NOT NULL DEFAULT 0"),
)

_SCHEMA_DONE = set()
_LOCK = threading.Lock()


def connect(path=None):
    path = path or DB
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    key = os.path.abspath(path)
    if key not in _SCHEMA_DONE:
        with _LOCK:
            conn.executescript(SCHEMA)
            for table, col, ddl in _MIGRATIONS:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
                if col not in cols:
                    conn.execute(ddl)
            conn.commit()
            _SCHEMA_DONE.add(key)
    return conn


class ModError(ValueError):
    """A request the owner can fix: shown to them as-is."""


def clean_perms(perms):
    got = [p for p in (perms or []) if p in PERMS]
    return sorted(set(got))


def perms_of(row):
    return [p for p in (row["perms"] or "").split(",") if p in PERMS]


def fingerprint(row):
    """Ties a session to this moderator's CURRENT password: a reset changes it."""
    return "mod:%d:%s" % (row["id"], row["pw_changed_at"])


def _check_username(name):
    name = (name or "").strip()
    if not name:
        raise ModError("Username must not be empty.")
    if len(name) > 64:
        raise ModError("Username must be at most 64 characters.")
    if any(c.isspace() for c in name):
        raise ModError("Username must not contain spaces.")
    return name


def _check_limit(v):
    if v is None or v == "":
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ModError("Code limit must be a whole number, or blank for none.")
    if n < 0 or n > 10000:
        raise ModError("Code limit must be between 0 and 10000.")
    return n


def _check_password(pw):
    msg = accounts.check_admin_password_policy(pw)
    if msg:
        raise ModError(msg)


def list_mods(conn):
    return conn.execute("SELECT * FROM moderator ORDER BY username").fetchall()


def get_mod(conn, mod_id):
    return conn.execute("SELECT * FROM moderator WHERE id=?", (mod_id,)).fetchone()


def find_mod(conn, username):
    return conn.execute("SELECT * FROM moderator WHERE username=?",
                        ((username or "").strip(),)).fetchone()


def clean_titles(titles):
    """Content codes a moderator may put on a code; empty = any."""
    out = set()
    for t in titles or []:
        try:
            out.add(int(t))
        except (TypeError, ValueError):
            raise ModError("Titles must be content numbers.")
    return sorted(out)


def titles_of(row):
    raw = row["titles"] if "titles" in row.keys() else ""
    return [int(t) for t in (raw or "").split(",") if t.strip().isdigit()]


def create_mod(conn, username, password, perms, code_limit=None, by=None,
               reserved=(), titles=None):
    """Add a moderator. `reserved` = names that must not be reused (the owner's)."""
    username = _check_username(username)
    if username.lower() in {r.lower() for r in reserved if r}:
        raise ModError("That username belongs to the owner login.")
    if find_mod(conn, username):
        raise ModError("A moderator with that username already exists.")
    _check_password(password)
    h, s = accounts.hash_password(password)
    now = time.time()
    cur = conn.execute(
        "INSERT INTO moderator (username, pw_hash, pw_salt, perms, code_limit,"
        " created_at, created_by, pw_changed_at) VALUES (?,?,?,?,?,?,?,?)",
        (username, h, s, ",".join(clean_perms(perms)), _check_limit(code_limit),
         now, by, now))
    conn.execute("UPDATE moderator SET titles=? WHERE id=?",
                 (",".join(map(str, clean_titles(titles))), cur.lastrowid))
    conn.commit()
    return get_mod(conn, cur.lastrowid)


def update_mod(conn, mod_id, perms=None, code_limit=..., disabled=None,
               titles=None):
    row = get_mod(conn, mod_id)
    if row is None:
        raise ModError("No such moderator.")
    if perms is not None:
        conn.execute("UPDATE moderator SET perms=? WHERE id=?",
                     (",".join(clean_perms(perms)), mod_id))
    if code_limit is not ...:
        conn.execute("UPDATE moderator SET code_limit=? WHERE id=?",
                     (_check_limit(code_limit), mod_id))
    if disabled is not None:
        conn.execute("UPDATE moderator SET disabled=? WHERE id=?",
                     (1 if disabled else 0, mod_id))
    if titles is not None:
        conn.execute("UPDATE moderator SET titles=? WHERE id=?",
                     (",".join(map(str, clean_titles(titles))), mod_id))
    conn.commit()
    return get_mod(conn, mod_id)


def set_password(conn, mod_id, password):
    if get_mod(conn, mod_id) is None:
        raise ModError("No such moderator.")
    _check_password(password)
    h, s = accounts.hash_password(password)
    # max() so two resets inside one clock tick still change the fingerprint
    row = get_mod(conn, mod_id)
    stamp = max(time.time(), float(row["pw_changed_at"]) + 0.001)
    conn.execute("UPDATE moderator SET pw_hash=?, pw_salt=?, pw_changed_at=?"
                 " WHERE id=?", (h, s, stamp, mod_id))
    conn.commit()
    return get_mod(conn, mod_id)


def delete_mod(conn, mod_id):
    n = conn.execute("DELETE FROM moderator WHERE id=?", (mod_id,)).rowcount
    conn.commit()
    return n > 0


def check_login(conn, username, password):
    """The moderator row if (username, password) is right and not disabled."""
    row = find_mod(conn, username)
    if row is None or row["disabled"]:
        return None
    if not accounts.check_password(password, row["pw_hash"], row["pw_salt"]):
        return None
    return row


# --------------------------------------------------------------------------- #
# audit log
# --------------------------------------------------------------------------- #
def audit(conn, actor, role, action, target=None, detail=None, ok=True,
          addr=None):
    if isinstance(detail, (dict, list)):
        detail = json.dumps(detail, ensure_ascii=False, sort_keys=True)
    conn.execute(
        "INSERT INTO audit (at, actor, role, action, target, detail, ok, addr)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (time.time(), actor or "?", role or "?", action,
         (str(target)[:200] if target not in (None, "") else None),
         (detail[:600] if detail else None), 1 if ok else 0, addr))
    conn.commit()


def audit_list(conn, limit=200, actor=None, before=None):
    q, args = "SELECT * FROM audit", []
    where = []
    if actor:
        where.append("actor = ? COLLATE NOCASE")
        args.append(actor)
    if before:
        where.append("at < ?")
        args.append(float(before))
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY at DESC, id DESC LIMIT ?"
    args.append(max(1, min(1000, int(limit))))
    return conn.execute(q, args).fetchall()


def audit_actors(conn):
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT actor FROM audit ORDER BY actor COLLATE NOCASE")]


# --------------------------------------------------------------------------- #
# who made each registration code
# --------------------------------------------------------------------------- #
def code_origin_add(conn, code, by, role, expires_at=None):
    conn.execute("INSERT OR REPLACE INTO code_origin (code, by, role, at, expires_at)"
                 " VALUES (?,?,?,?,?)", (code, by, role, time.time(), expires_at))
    conn.commit()


def code_origins(conn):
    """code (upper) -> {"by": ..., "expires_at": ...}"""
    return {r["code"].upper(): {"by": r["by"], "expires_at": r["expires_at"]}
            for r in conn.execute("SELECT code, by, expires_at FROM code_origin")}


def codes_due_to_expire(conn, now=None):
    return [r["code"] for r in conn.execute(
        "SELECT code FROM code_origin WHERE expires_at IS NOT NULL"
        " AND expires_at <= ? AND expired = 0", (now or time.time(),))]


def mark_expired(conn, code):
    conn.execute("UPDATE code_origin SET expired=1 WHERE code=?", (code,))
    conn.commit()


# --------------------------------------------------------------------------- #
# settings, push subscriptions, alert bookkeeping
# --------------------------------------------------------------------------- #
def get_setting(conn, key, default=None):
    r = conn.execute("SELECT value FROM setting WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def set_setting(conn, key, value):
    if value is None:
        conn.execute("DELETE FROM setting WHERE key=?", (key,))
    else:
        conn.execute("INSERT OR REPLACE INTO setting (key, value) VALUES (?,?)",
                     (key, str(value)))
    conn.commit()


def push_add(conn, sub, username, role, uid, origin, ua):
    keys = sub.get("keys") or {}
    if not str(sub.get("endpoint", "")).startswith("https://") \
            or not keys.get("p256dh") or not keys.get("auth"):
        raise ModError("Not a valid push subscription.")
    conn.execute(
        "INSERT OR REPLACE INTO push_sub (endpoint, p256dh, auth, username, role,"
        " uid, origin, ua, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (sub["endpoint"], keys["p256dh"], keys["auth"], username, role, uid,
         origin, (ua or "")[:200], time.time()))
    conn.commit()


def push_remove(conn, endpoint):
    n = conn.execute("DELETE FROM push_sub WHERE endpoint=?", (endpoint,)).rowcount
    conn.commit()
    return n


def push_list(conn):
    return conn.execute("SELECT * FROM push_sub ORDER BY created_at").fetchall()


def push_result(conn, endpoint, ok, error=None):
    if ok:
        conn.execute("UPDATE push_sub SET last_ok=?, last_error=NULL WHERE endpoint=?",
                     (time.time(), endpoint))
    else:
        conn.execute("UPDATE push_sub SET last_error=? WHERE endpoint=?",
                     ((error or "")[:200], endpoint))
    conn.commit()


def alerted_all(conn):
    return {r[0] for r in conn.execute("SELECT ticket FROM alerted")}


def alerted_add(conn, ticket, result):
    conn.execute("INSERT OR REPLACE INTO alerted (ticket, at, result) VALUES (?,?,?)",
                 (ticket, time.time(), result))
    conn.commit()


def alerted_recent(conn, limit=10):
    return conn.execute("SELECT * FROM alerted ORDER BY at DESC LIMIT ?",
                        (limit,)).fetchall()


def codes_made_since(conn, by, since):
    return conn.execute("SELECT COUNT(*) FROM code_origin WHERE by = ?"
                        " COLLATE NOCASE AND at >= ?", (by, since)).fetchone()[0]
