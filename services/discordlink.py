#!/usr/bin/env python3
"""discordlink.py -- the PlayOnline <-> Discord link store (2026-09-13).

Who is linked to which POL member, the one-time codes that make a link, and
the bridge's own bookkeeping (which messages it has already DM'd, what a Reply
button answers). Shared by two processes:

  * `polbridge.py` MINTS a code when someone types `/playonline link`, and
    reads the links to decide who gets a DM;
  * `ucscgi.py`'s account portal REDEEMS the code, behind the member's own
    POL password (kinou 90) -- the only place a link is ever made, so the bot
    never sees a password and a code alone proves nothing.

ITS OWN DATABASE FILE, NOT accounts.db, and that is deliberate: a change to
accounts.py restarts login + authsess on deploy (deploy/pol-git-sync), which
drops every live session. Nothing here is account data the game needs, so it
stays out of the file the game runs on. Same journal rule as accounts.db
(`POL_SQLITE_JOURNAL`, default TRUNCATE) -- WAL on the Windows dev bind mount
has already corrupted one database.

Ground rules for the bridge: the link is made in the portal, never with a
password given to the bot, and the bot must never override real presence.
"""
import os
import re
import secrets
import sqlite3
import time

DEFAULT_DB = os.environ.get("POL_DISCORD_LINK_DB", "/data/discord_links.db")

#: How long a `/playonline link` code stays redeemable, in seconds.
CODE_TTL = int(os.environ.get("POL_DISCORD_CODE_TTL", "600"))

#: How long a Reply button keeps working after its DM was sent.
REPLY_TTL = int(os.environ.get("POL_DISCORD_REPLY_TTL", str(14 * 86400)))

#: No 0/O, 1/I -- a code is read off one screen and typed on another, often
#: with a controller. 32 symbols x 8 = 40 bits, alive for ten minutes.
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LEN = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS link (
    discord_id   TEXT PRIMARY KEY,
    member_id    INTEGER NOT NULL UNIQUE,
    discord_name TEXT,
    linked_at    REAL NOT NULL,
    notify       INTEGER NOT NULL DEFAULT 1,
    dm_channel   TEXT
);
CREATE TABLE IF NOT EXISTS code (
    code         TEXT PRIMARY KEY,
    discord_id   TEXT NOT NULL,
    discord_name TEXT,
    created_at   REAL NOT NULL
);
-- A message file the bridge has finished with (DM'd, or decided not to).
CREATE TABLE IF NOT EXISTS notified (
    name TEXT PRIMARY KEY,
    at   REAL NOT NULL
);
-- What a Reply button answers: from which of the member's handles, to whom.
-- peer_guid is TEXT because a guid is a u64 and SQLite integers are signed.
CREATE TABLE IF NOT EXISTS reply (
    id         TEXT PRIMARY KEY,
    member_id  INTEGER NOT NULL,
    handle_id  INTEGER NOT NULL,
    peer_guid  TEXT NOT NULL,
    peer_name  TEXT,
    subject    TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sent (
    member_id INTEGER NOT NULL,
    at        REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


def connect(path=None):
    """Open (creating if needed) the link DB with its schema applied."""
    path = path or os.environ.get("POL_DISCORD_LINK_DB", DEFAULT_DB)
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    mode = os.environ.get("POL_SQLITE_JOURNAL", "TRUNCATE")
    try:
        conn.execute(f"PRAGMA journal_mode={mode}")
    except sqlite3.Error:
        pass
    conn.executescript(SCHEMA)
    return conn


# --------------------------------------------------------------------------- #
# codes and links
# --------------------------------------------------------------------------- #

def normalise_code(text):
    """What a player typed -> the stored form. Case and separators are free."""
    return re.sub(r"[^A-Z0-9]", "", str(text or "").upper())


def format_code(code):
    """ABCDEFGH -> ABCD-EFGH, the way it is shown."""
    code = normalise_code(code)
    return f"{code[:4]}-{code[4:]}" if len(code) == CODE_LEN else code


def new_code(conn, discord_id, discord_name=None, now=None):
    """A fresh code for this Discord user. Any older code of theirs is void."""
    now = time.time() if now is None else now
    conn.execute("DELETE FROM code WHERE discord_id = ? OR created_at < ?",
                 (str(discord_id), now - CODE_TTL))
    while True:
        code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))
        try:
            conn.execute("INSERT INTO code (code, discord_id, discord_name, created_at)"
                         " VALUES (?,?,?,?)", (code, str(discord_id), discord_name, now))
            break
        except sqlite3.IntegrityError:
            continue
    conn.commit()
    return format_code(code)


def redeem(conn, code, member_id, now=None):
    """Spend a code on a member. Returns the new link row, or None.

    Single use, and only inside CODE_TTL. A member has at most one Discord
    account and a Discord account at most one member, so whatever either side
    was linked to before is replaced.
    """
    now = time.time() if now is None else now
    code = normalise_code(code)
    if len(code) != CODE_LEN:
        return None
    row = conn.execute("SELECT * FROM code WHERE code = ?", (code,)).fetchone()
    if row is None:
        return None
    conn.execute("DELETE FROM code WHERE code = ?", (code,))
    if now - float(row["created_at"]) > CODE_TTL:
        conn.commit()
        return None
    conn.execute("DELETE FROM link WHERE member_id = ? OR discord_id = ?",
                 (int(member_id), row["discord_id"]))
    conn.execute("INSERT INTO link (discord_id, member_id, discord_name, linked_at)"
                 " VALUES (?,?,?,?)",
                 (row["discord_id"], int(member_id), row["discord_name"], now))
    conn.commit()
    return link_by_member(conn, member_id)


def link_by_discord(conn, discord_id):
    return conn.execute("SELECT * FROM link WHERE discord_id = ?",
                        (str(discord_id),)).fetchone()


def link_by_member(conn, member_id):
    return conn.execute("SELECT * FROM link WHERE member_id = ?",
                        (int(member_id),)).fetchone()


def unlink_member(conn, member_id):
    n = conn.execute("DELETE FROM link WHERE member_id = ?", (int(member_id),)).rowcount
    conn.commit()
    return n > 0


def unlink_discord(conn, discord_id):
    n = conn.execute("DELETE FROM link WHERE discord_id = ?", (str(discord_id),)).rowcount
    conn.execute("DELETE FROM code WHERE discord_id = ?", (str(discord_id),))
    conn.commit()
    return n > 0


def set_notify(conn, discord_id, on):
    n = conn.execute("UPDATE link SET notify = ? WHERE discord_id = ?",
                     (1 if on else 0, str(discord_id))).rowcount
    conn.commit()
    return n > 0


def set_dm_channel(conn, discord_id, channel_id):
    conn.execute("UPDATE link SET dm_channel = ? WHERE discord_id = ?",
                 (str(channel_id) if channel_id else None, str(discord_id)))
    conn.commit()


# --------------------------------------------------------------------------- #
# the bridge's bookkeeping
# --------------------------------------------------------------------------- #

def is_notified(conn, name):
    return conn.execute("SELECT 1 FROM notified WHERE name = ?", (name,)).fetchone() is not None


def mark_notified(conn, names, now=None):
    now = time.time() if now is None else now
    if isinstance(names, str):
        names = [names]
    conn.executemany("INSERT OR IGNORE INTO notified (name, at) VALUES (?,?)",
                     [(n, now) for n in names])
    conn.commit()


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
    return row["v"] if row else default


def set_meta(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?,?)", (key, str(value)))
    conn.commit()


def new_reply(conn, member_id, handle_id, peer_guid, peer_name, subject, now=None):
    """Remember what one DM's Reply button answers; returns its short id.

    Discord custom ids are capped at 100 characters, so the button carries this
    id and nothing else -- never a guid or a name a user could edit.
    """
    now = time.time() if now is None else now
    conn.execute("DELETE FROM reply WHERE created_at < ?", (now - REPLY_TTL,))
    rid = secrets.token_urlsafe(9)
    conn.execute("INSERT INTO reply (id, member_id, handle_id, peer_guid, peer_name,"
                 " subject, created_at) VALUES (?,?,?,?,?,?,?)",
                 (rid, int(member_id), int(handle_id), str(int(peer_guid)), peer_name,
                  subject, now))
    conn.commit()
    return rid


def get_reply(conn, rid, now=None):
    now = time.time() if now is None else now
    row = conn.execute("SELECT * FROM reply WHERE id = ?", (str(rid),)).fetchone()
    if row is None or now - float(row["created_at"]) > REPLY_TTL:
        return None
    return row


def note_sent(conn, member_id, now=None):
    now = time.time() if now is None else now
    conn.execute("DELETE FROM sent WHERE at < ?", (now - 86400,))
    conn.execute("INSERT INTO sent (member_id, at) VALUES (?,?)", (int(member_id), now))
    conn.commit()


def sent_since(conn, member_id, window, now=None):
    now = time.time() if now is None else now
    return conn.execute("SELECT COUNT(*) FROM sent WHERE member_id = ? AND at >= ?",
                        (int(member_id), now - window)).fetchone()[0]
