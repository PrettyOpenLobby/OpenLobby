"""Moderator logins, the admin panel's audit log, and who made each code.

The admin panel has ONE owner login (accounts.admin_cred, plus the env
break-glass pair). This module adds MODERATORS: separate logins, each with a
fixed set of permissions the panel checks on every request (see PERMS and
admin.Handler._permit). The owner creates and manages them on the Security tab.

Its tables are the admin_* ones (polcore/migrations/0003_admin_discord.sql),
in the same PostgreSQL as everything else. It used to be its own SQLite file,
data/admin.db, kept out of accounts.db so that a change here would not make
prod's git-sync restart login and authsess; with the schema in numbered
migrations that reason is gone, and nothing here is read by any service but
the panel.

Passwords use the same PBKDF2 helper and policy as the owner credential
(accounts.hash_password, accounts.check_admin_password_policy).
"""
import json
import time

import accounts

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


def connect(path=None):
    """A connection for the functions below: the same kind accounts.connect()
    returns, with the schema brought up to date. `path` is accepted for old
    callers and ignored."""
    return accounts.connect()


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
    return conn.execute("SELECT * FROM admin_moderator"
                        " ORDER BY lower(username)").fetchall()


def get_mod(conn, mod_id):
    return conn.execute("SELECT * FROM admin_moderator WHERE id=%s", (mod_id,)).fetchone()


def find_mod(conn, username):
    return conn.execute("SELECT * FROM admin_moderator"
                        " WHERE lower(username) = lower(%s)",
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
    mod_id = conn.execute(
        "INSERT INTO admin_moderator (username, pw_hash, pw_salt, perms, code_limit,"
        " created_at, created_by, pw_changed_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"
        " RETURNING id",
        (username, h, s, ",".join(clean_perms(perms)), _check_limit(code_limit),
         now, by, now)).fetchone()["id"]
    conn.execute("UPDATE admin_moderator SET titles=%s WHERE id=%s",
                 (",".join(map(str, clean_titles(titles))), mod_id))
    conn.commit()
    return get_mod(conn, mod_id)


def update_mod(conn, mod_id, perms=None, code_limit=..., disabled=None,
               titles=None):
    row = get_mod(conn, mod_id)
    if row is None:
        raise ModError("No such moderator.")
    if perms is not None:
        conn.execute("UPDATE admin_moderator SET perms=%s WHERE id=%s",
                     (",".join(clean_perms(perms)), mod_id))
    if code_limit is not ...:
        conn.execute("UPDATE admin_moderator SET code_limit=%s WHERE id=%s",
                     (_check_limit(code_limit), mod_id))
    if disabled is not None:
        conn.execute("UPDATE admin_moderator SET disabled=%s WHERE id=%s",
                     (1 if disabled else 0, mod_id))
    if titles is not None:
        conn.execute("UPDATE admin_moderator SET titles=%s WHERE id=%s",
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
    conn.execute("UPDATE admin_moderator SET pw_hash=%s, pw_salt=%s, pw_changed_at=%s"
                 " WHERE id=%s", (h, s, stamp, mod_id))
    conn.commit()
    return get_mod(conn, mod_id)


def delete_mod(conn, mod_id):
    n = conn.execute("DELETE FROM admin_moderator WHERE id=%s", (mod_id,)).rowcount
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
        "INSERT INTO admin_audit (at, actor, role, action, target, detail, ok, addr)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (time.time(), actor or "?", role or "?", action,
         (str(target)[:200] if target not in (None, "") else None),
         (detail[:600] if detail else None), 1 if ok else 0, addr))
    conn.commit()


def audit_list(conn, limit=200, actor=None, before=None):
    q, args = "SELECT * FROM admin_audit", []
    where = []
    if actor:
        where.append("lower(actor) = lower(%s)")
        args.append(actor)
    if before:
        where.append("at < %s")
        args.append(float(before))
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY at DESC, id DESC LIMIT %s"
    args.append(max(1, min(1000, int(limit))))
    return conn.execute(q, args).fetchall()


def audit_actors(conn):
    return [r[0] for r in conn.execute(
        "SELECT actor FROM admin_audit GROUP BY actor"
        " ORDER BY lower(actor), actor")]


# --------------------------------------------------------------------------- #
# who made each registration code
# --------------------------------------------------------------------------- #
def code_origin_add(conn, code, by, role, expires_at=None):
    # The key ignores case (the code column was COLLATE NOCASE), and a repeat
    # replaces the whole row, `expired` included, as SQLite's OR REPLACE did.
    conn.execute('INSERT INTO admin_code_origin (code, "by", role, at, expires_at)'
                 " VALUES (%s,%s,%s,%s,%s)"
                 " ON CONFLICT ((lower(code))) DO UPDATE SET code = excluded.code,"
                 ' "by" = excluded."by", role = excluded.role, at = excluded.at,'
                 " expires_at = excluded.expires_at, expired = 0",
                 (code, by, role, time.time(), expires_at))
    conn.commit()


def code_origins(conn):
    """code (upper) -> {"by": ..., "expires_at": ...}"""
    return {r["code"].upper(): {"by": r["by"], "expires_at": r["expires_at"]}
            for r in conn.execute(
                'SELECT code, "by", expires_at FROM admin_code_origin')}


def codes_due_to_expire(conn, now=None):
    return [r["code"] for r in conn.execute(
        "SELECT code FROM admin_code_origin WHERE expires_at IS NOT NULL"
        " AND expires_at <= %s AND expired = 0", (now or time.time(),))]


def mark_expired(conn, code):
    conn.execute("UPDATE admin_code_origin SET expired=1"
                 " WHERE lower(code) = lower(%s)", (code,))
    conn.commit()


# --------------------------------------------------------------------------- #
# settings, push subscriptions, alert bookkeeping
# --------------------------------------------------------------------------- #
def get_setting(conn, key, default=None):
    r = conn.execute("SELECT value FROM admin_setting WHERE key=%s", (key,)).fetchone()
    return r[0] if r else default


def set_setting(conn, key, value):
    if value is None:
        conn.execute("DELETE FROM admin_setting WHERE key=%s", (key,))
    else:
        conn.execute("INSERT INTO admin_setting (key, value) VALUES (%s,%s)"
                     " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                     (key, str(value)))
    conn.commit()


def push_add(conn, sub, username, role, uid, origin, ua):
    keys = sub.get("keys") or {}
    if not str(sub.get("endpoint", "")).startswith("https://") \
            or not keys.get("p256dh") or not keys.get("auth"):
        raise ModError("Not a valid push subscription.")
    conn.execute(
        "INSERT INTO admin_push_sub (endpoint, p256dh, auth, username, role,"
        " uid, origin, ua, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT (endpoint) DO UPDATE SET p256dh = excluded.p256dh,"
        " auth = excluded.auth, username = excluded.username,"
        " role = excluded.role, uid = excluded.uid, origin = excluded.origin,"
        " ua = excluded.ua, created_at = excluded.created_at,"
        " last_ok = NULL, last_error = NULL",
        (sub["endpoint"], keys["p256dh"], keys["auth"], username, role, uid,
         origin, (ua or "")[:200], time.time()))
    conn.commit()


def push_remove(conn, endpoint):
    n = conn.execute("DELETE FROM admin_push_sub WHERE endpoint=%s", (endpoint,)).rowcount
    conn.commit()
    return n


def push_list(conn):
    return conn.execute("SELECT * FROM admin_push_sub ORDER BY created_at").fetchall()


def push_result(conn, endpoint, ok, error=None):
    if ok:
        conn.execute("UPDATE admin_push_sub SET last_ok=%s, last_error=NULL WHERE endpoint=%s",
                     (time.time(), endpoint))
    else:
        conn.execute("UPDATE admin_push_sub SET last_error=%s WHERE endpoint=%s",
                     ((error or "")[:200], endpoint))
    conn.commit()


def alerted_all(conn):
    return {r[0] for r in conn.execute("SELECT ticket FROM admin_alerted")}


def alerted_add(conn, ticket, result):
    conn.execute("INSERT INTO admin_alerted (ticket, at, result) VALUES (%s,%s,%s)"
                 " ON CONFLICT (ticket) DO UPDATE SET at = excluded.at,"
                 " result = excluded.result",
                 (ticket, time.time(), result))
    conn.commit()


def alerted_recent(conn, limit=10):
    return conn.execute("SELECT * FROM admin_alerted ORDER BY at DESC LIMIT %s",
                        (limit,)).fetchall()


def codes_made_since(conn, by, since):
    return conn.execute('SELECT COUNT(*) FROM admin_code_origin WHERE lower("by")'
                        " = lower(%s) AND at >= %s", (by, since)).fetchone()[0]
