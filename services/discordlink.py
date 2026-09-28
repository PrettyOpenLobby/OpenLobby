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

The tables are the discord_* ones (polcore/migrations/0003_admin_discord.sql),
in the same PostgreSQL as the accounts. They used to be their own SQLite file,
discord_links.db, kept out of accounts.db so a change here would not restart
login and authsess on deploy; with the schema in numbered migrations there is
nothing left to keep apart, and none of it is account data the game reads.

Ground rules for the bridge: the link is made in the portal, never with a
password given to the bot, and the bot must never override real presence.
"""
import os
import re
import secrets
import time

import accounts

#: How long a `/playonline link` code stays redeemable, in seconds.
CODE_TTL = int(os.environ.get("POL_DISCORD_CODE_TTL", "600"))

#: How long a Reply button keeps working after its DM was sent.
REPLY_TTL = int(os.environ.get("POL_DISCORD_REPLY_TTL", str(14 * 86400)))

#: No 0/O, 1/I -- a code is read off one screen and typed on another, often
#: with a controller. 32 symbols x 8 = 40 bits, alive for ten minutes.
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LEN = 8


def connect(path=None):
    """A connection for the functions below (the kind accounts.connect()
    returns), with the schema brought up to date. `path` is accepted for old
    callers and ignored."""
    return accounts.connect()


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
    conn.execute("DELETE FROM discord_code WHERE discord_id = %s OR created_at < %s",
                 (str(discord_id), now - CODE_TTL))
    while True:
        code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))
        try:
            conn.execute("INSERT INTO discord_code (code, discord_id, discord_name, created_at)"
                         " VALUES (%s,%s,%s,%s)", (code, str(discord_id), discord_name, now))
            break
        except accounts.db.IntegrityError:
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
    row = conn.execute("SELECT * FROM discord_code WHERE code = %s", (code,)).fetchone()
    if row is None:
        return None
    conn.execute("DELETE FROM discord_code WHERE code = %s", (code,))
    if now - float(row["created_at"]) > CODE_TTL:
        conn.commit()
        return None
    conn.execute("DELETE FROM discord_link WHERE member_id = %s OR discord_id = %s",
                 (int(member_id), row["discord_id"]))
    conn.execute("INSERT INTO discord_link (discord_id, member_id, discord_name, linked_at)"
                 " VALUES (%s,%s,%s,%s)",
                 (row["discord_id"], int(member_id), row["discord_name"], now))
    conn.commit()
    return link_by_member(conn, member_id)


def link_by_discord(conn, discord_id):
    return conn.execute("SELECT * FROM discord_link WHERE discord_id = %s",
                        (str(discord_id),)).fetchone()


def link_by_member(conn, member_id):
    return conn.execute("SELECT * FROM discord_link WHERE member_id = %s",
                        (int(member_id),)).fetchone()


def unlink_member(conn, member_id):
    n = conn.execute("DELETE FROM discord_link WHERE member_id = %s", (int(member_id),)).rowcount
    conn.commit()
    return n > 0


def unlink_discord(conn, discord_id):
    n = conn.execute("DELETE FROM discord_link WHERE discord_id = %s", (str(discord_id),)).rowcount
    conn.execute("DELETE FROM discord_code WHERE discord_id = %s", (str(discord_id),))
    conn.commit()
    return n > 0


def set_notify(conn, discord_id, on):
    n = conn.execute("UPDATE discord_link SET notify = %s WHERE discord_id = %s",
                     (1 if on else 0, str(discord_id))).rowcount
    conn.commit()
    return n > 0


def set_dm_channel(conn, discord_id, channel_id):
    conn.execute("UPDATE discord_link SET dm_channel = %s WHERE discord_id = %s",
                 (str(channel_id) if channel_id else None, str(discord_id)))
    conn.commit()


# --------------------------------------------------------------------------- #
# the bridge's bookkeeping
# --------------------------------------------------------------------------- #

def is_notified(conn, name):
    return conn.execute("SELECT 1 FROM discord_notified WHERE name = %s", (name,)).fetchone() is not None


def mark_notified(conn, names, now=None):
    now = time.time() if now is None else now
    if isinstance(names, str):
        names = [names]
    conn.executemany("INSERT INTO discord_notified (name, at) VALUES (%s,%s)"
                     " ON CONFLICT DO NOTHING",
                     [(n, now) for n in names])
    conn.commit()


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT v FROM discord_meta WHERE k = %s", (key,)).fetchone()
    return row["v"] if row else default


def set_meta(conn, key, value):
    conn.execute("INSERT INTO discord_meta (k, v) VALUES (%s,%s)"
                 " ON CONFLICT (k) DO UPDATE SET v = excluded.v", (key, str(value)))
    conn.commit()


def new_reply(conn, member_id, handle_id, peer_guid, peer_name, subject, now=None):
    """Remember what one DM's Reply button answers; returns its short id.

    Discord custom ids are capped at 100 characters, so the button carries this
    id and nothing else -- never a guid or a name a user could edit.
    """
    now = time.time() if now is None else now
    conn.execute("DELETE FROM discord_reply WHERE created_at < %s", (now - REPLY_TTL,))
    rid = secrets.token_urlsafe(9)
    conn.execute("INSERT INTO discord_reply (id, member_id, handle_id, peer_guid, peer_name,"
                 " subject, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                 (rid, int(member_id), int(handle_id), str(int(peer_guid)), peer_name,
                  subject, now))
    conn.commit()
    return rid


def get_reply(conn, rid, now=None):
    now = time.time() if now is None else now
    row = conn.execute("SELECT * FROM discord_reply WHERE id = %s", (str(rid),)).fetchone()
    if row is None or now - float(row["created_at"]) > REPLY_TTL:
        return None
    return row


def note_sent(conn, member_id, now=None):
    now = time.time() if now is None else now
    conn.execute("DELETE FROM discord_sent WHERE at < %s", (now - 86400,))
    conn.execute("INSERT INTO discord_sent (member_id, at) VALUES (%s,%s)", (int(member_id), now))
    conn.commit()


def sent_since(conn, member_id, window, now=None):
    now = time.time() if now is None else now
    return conn.execute("SELECT COUNT(*) FROM discord_sent WHERE member_id = %s AND at >= %s",
                        (int(member_id), now - window)).fetchone()[0]
