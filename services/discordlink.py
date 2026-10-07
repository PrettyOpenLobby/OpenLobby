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


# --------------------------------------------------------------------------- #
# announcement channels + ping roles
# --------------------------------------------------------------------------- #

def bind_announce(conn, guild_id, channel_id, baseline_serial=0, now=None):
    """Bind (or move) this guild's announcement channel. `baseline_serial` is
    the highest existing serial; anything at or below it is treated as already
    posted so a bind never dumps the backlog. Moving to a new channel keeps the
    baseline: the previous channel's posts stand."""
    now = time.time() if now is None else now
    existing = conn.execute("SELECT last_serial FROM discord_announce"
                            " WHERE guild_id = %s",
                            (str(guild_id),)).fetchone()
    baseline = int(baseline_serial or 0)
    if existing is not None and int(existing["last_serial"]) > baseline:
        baseline = int(existing["last_serial"])
    conn.execute("INSERT INTO discord_announce (guild_id, channel_id, last_serial,"
                 " bound_at) VALUES (%s,%s,%s,%s)"
                 " ON CONFLICT (guild_id) DO UPDATE SET"
                 " channel_id = excluded.channel_id,"
                 " last_serial = excluded.last_serial,"
                 " bound_at = excluded.bound_at",
                 (str(guild_id), str(channel_id), baseline, now))
    conn.commit()
    return announce_row(conn, guild_id)


def unbind_announce(conn, guild_id):
    n = conn.execute("DELETE FROM discord_announce WHERE guild_id = %s",
                     (str(guild_id),)).rowcount
    conn.commit()
    return n > 0


def announce_row(conn, guild_id):
    return conn.execute("SELECT * FROM discord_announce WHERE guild_id = %s",
                        (str(guild_id),)).fetchone()


def announce_rows(conn):
    return conn.execute("SELECT * FROM discord_announce").fetchall()


def bump_announce_serial(conn, guild_id, serial):
    """Move a channel's last_serial forward. Only forward: a lower value from a
    stale republish must not resend what we already put in the channel."""
    conn.execute("UPDATE discord_announce SET last_serial = %s"
                 " WHERE guild_id = %s AND last_serial < %s",
                 (int(serial), str(guild_id), int(serial)))
    conn.commit()


def set_announce_role(conn, guild_id, content, role_id):
    """Set (role_id truthy) or clear (role_id falsy) the ping role for one
    content id in this guild."""
    if role_id:
        conn.execute("INSERT INTO discord_announce_role (guild_id, content, role_id)"
                     " VALUES (%s,%s,%s)"
                     " ON CONFLICT (guild_id, content) DO UPDATE"
                     " SET role_id = excluded.role_id",
                     (str(guild_id), str(content), str(role_id)))
    else:
        conn.execute("DELETE FROM discord_announce_role"
                     " WHERE guild_id = %s AND content = %s",
                     (str(guild_id), str(content)))
    conn.commit()


def announce_roles(conn, guild_id):
    """{content: role_id} for one guild."""
    rows = conn.execute("SELECT content, role_id FROM discord_announce_role"
                        " WHERE guild_id = %s",
                        (str(guild_id),)).fetchall()
    return {r["content"]: r["role_id"] for r in rows}


# --------------------------------------------------------------------------- #
# GM Call console: alerts channel per guild, one thread per open call
# --------------------------------------------------------------------------- #

def bind_gm_channel(conn, guild_id, channel_id, role_id=None,
                    baseline_ticket="", bound_by=None, now=None):
    """Bind (or move) this guild's GM-alerts channel. `baseline_ticket` is the
    newest ticket_id already on disk; anything at or below it lexicographically
    is treated as already alerted so a bind never dumps the backlog. Moving to
    a new channel keeps the baseline: the old channel's alerts stand.

    Mirrors bind_announce for the announcement channel."""
    now = time.time() if now is None else now
    existing = conn.execute("SELECT last_ticket FROM discord_gm_channel"
                            " WHERE guild_id = %s",
                            (str(guild_id),)).fetchone()
    baseline = str(baseline_ticket or "")
    if existing is not None and str(existing["last_ticket"]) > baseline:
        baseline = str(existing["last_ticket"])
    conn.execute("INSERT INTO discord_gm_channel (guild_id, channel_id, role_id,"
                 " last_ticket, bound_at, bound_by) VALUES (%s,%s,%s,%s,%s,%s)"
                 " ON CONFLICT (guild_id) DO UPDATE SET"
                 " channel_id = excluded.channel_id,"
                 " role_id = excluded.role_id,"
                 " last_ticket = excluded.last_ticket,"
                 " bound_at = excluded.bound_at,"
                 " bound_by = excluded.bound_by",
                 (str(guild_id), str(channel_id),
                  str(role_id) if role_id else None,
                  baseline, now, bound_by))
    conn.commit()
    return gm_channel_row(conn, guild_id)


def unbind_gm_channel(conn, guild_id):
    n = conn.execute("DELETE FROM discord_gm_channel WHERE guild_id = %s",
                     (str(guild_id),)).rowcount
    conn.commit()
    return n > 0


def gm_channel_row(conn, guild_id):
    return conn.execute("SELECT * FROM discord_gm_channel WHERE guild_id = %s",
                        (str(guild_id),)).fetchone()


def gm_channel_rows(conn):
    return conn.execute("SELECT * FROM discord_gm_channel").fetchall()


def bump_gm_last_ticket(conn, guild_id, ticket_id):
    """Move a channel's last_ticket forward. Only forward: a lower value from a
    replay must not re-alert on tickets we already posted."""
    conn.execute("UPDATE discord_gm_channel SET last_ticket = %s"
                 " WHERE guild_id = %s AND last_ticket < %s",
                 (str(ticket_id), str(guild_id), str(ticket_id)))
    conn.commit()


def gm_alert_get(conn, ticket_id):
    return conn.execute("SELECT * FROM discord_gm_alert WHERE ticket_id = %s",
                        (str(ticket_id),)).fetchone()


def gm_alert_record(conn, ticket_id, room, guild_id, channel_id, message_id, now=None):
    now = time.time() if now is None else now
    conn.execute("INSERT INTO discord_gm_alert (ticket_id, room, guild_id,"
                 " channel_id, message_id, posted_at) VALUES (%s,%s,%s,%s,%s,%s)"
                 " ON CONFLICT (ticket_id) DO UPDATE SET"
                 " room = excluded.room,"
                 " guild_id = excluded.guild_id,"
                 " channel_id = excluded.channel_id,"
                 " message_id = excluded.message_id,"
                 " posted_at = excluded.posted_at",
                 (str(ticket_id), str(room or ""), str(guild_id),
                  str(channel_id), str(message_id), now))
    conn.commit()


def gm_alert_claim(conn, ticket_id, who, now=None):
    now = time.time() if now is None else now
    conn.execute("UPDATE discord_gm_alert SET claimed_by = %s, claimed_at = %s"
                 " WHERE ticket_id = %s", (str(who), now, str(ticket_id)))
    conn.commit()


def gm_alert_unclaim(conn, ticket_id):
    conn.execute("UPDATE discord_gm_alert SET claimed_by = NULL, claimed_at = NULL"
                 " WHERE ticket_id = %s", (str(ticket_id),))
    conn.commit()


def gm_alert_drop(conn, ticket_id):
    """Forget an alert whose message is gone. Only a pointer to a Discord
    message; the ticket and the thread rows are the record."""
    conn.execute("DELETE FROM discord_gm_alert WHERE ticket_id = %s",
                 (str(ticket_id),))
    conn.commit()


def gm_alerts_all(conn):
    return conn.execute("SELECT * FROM discord_gm_alert ORDER BY posted_at").fetchall()


def gm_thread_by_room(conn, room):
    return conn.execute("SELECT * FROM discord_gm_thread"
                        " WHERE room = %s AND closed_at IS NULL",
                        (str(room),)).fetchone()


def gm_thread_by_thread(conn, thread_id):
    return conn.execute("SELECT * FROM discord_gm_thread WHERE thread_id = %s",
                        (str(thread_id),)).fetchone()


def gm_thread_open(conn, room, ticket_id, thread_id, guild_id, parent_id,
                   knocker_id, knocker_name=None, knocker_nick=None,
                   last_relay_at=None, now=None):
    """Bind a room to a fresh Discord thread. The unique index enforces at most
    one open row per room; a prior thread must be closed first."""
    now = time.time() if now is None else now
    seed = now if last_relay_at is None else float(last_relay_at)
    conn.execute("INSERT INTO discord_gm_thread (room, ticket_id, thread_id,"
                 " guild_id, parent_id, knocker_id, knocker_name, knocker_nick,"
                 " created_at, last_relay_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                 (str(room), str(ticket_id), str(thread_id), str(guild_id),
                  str(parent_id), str(knocker_id), knocker_name, knocker_nick,
                  now, seed))
    conn.commit()
    return gm_thread_by_thread(conn, thread_id)


def gm_thread_close(conn, thread_id, now=None):
    now = time.time() if now is None else now
    n = conn.execute("UPDATE discord_gm_thread SET closed_at = %s"
                     " WHERE thread_id = %s AND closed_at IS NULL",
                     (now, str(thread_id))).rowcount
    conn.commit()
    return n > 0


def gm_thread_note_msg(conn, thread_id, last_msg_id):
    conn.execute("UPDATE discord_gm_thread SET last_msg_id = %s"
                 " WHERE thread_id = %s",
                 (str(last_msg_id), str(thread_id)))
    conn.commit()


def gm_thread_note_relay(conn, room, at):
    conn.execute("UPDATE discord_gm_thread SET last_relay_at = %s"
                 " WHERE room = %s AND closed_at IS NULL",
                 (float(at), str(room)))
    conn.commit()


def gm_threads_open(conn):
    return conn.execute("SELECT * FROM discord_gm_thread WHERE closed_at IS NULL"
                        " ORDER BY created_at").fetchall()
