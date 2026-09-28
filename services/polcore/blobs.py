"""Saved resources in PostgreSQL: the `blob` table (migration 0002).

A blob is a small binary object (a save, a lobby list, a message, a title's
own record file) addressed by two strings:

    scope   whose it is. A decimal member id for per-member data, `shared`,
            `s<subject hex>` for a lobby list, `mail` for messages, or any
            name a title chooses for its own objects (`tm`, `jan`, ...).
    path    its name within that scope.

A scope that is a member id links the row to that member (`member_id`), so
deleting the member deletes its saves. The link is made only when the member
exists; a row for an unknown id is still stored under its scope, and
`delete_member` removes it by scope as well.

Every write is one statement or one transaction, so a reader never sees a half
written object, which is what plain `open("wb")` on the shared volume could
not promise.

    put(scope, path, data)                  store (insert or replace)
    get(scope, path) -> bytes | None
    stat(scope, path) -> Info | None        size and updated_at, no data
    exists(scope, path) -> bool
    delete(scope, path) -> bool
    rename(scope, path, new_scope, new_path) -> bool   replaces the target
    listing(scope=None, prefix=None, suffix=None, path=None) -> [Info]
    delete_prefix(scope, prefix="") -> [Info]
    delete_member(member_ids) -> [Info]

`Info` is (scope, path, size, updated_at) with updated_at in epoch seconds,
the way the code that used to read files compared mtimes.

The resource store in the core (core/resourcestore.py) keeps its old file
names as the key: the part of the name before the first dot is the scope and
the rest is the path, with `m.` meaning the `mail` scope. `file_name` and
`split_name` convert between the two, and are what an import of an old
`resources/` directory uses.
"""
import collections

from . import db

Info = collections.namedtuple("Info", "scope path size updated_at")

#: The file-name prefix the mail scope used on disk (`m.<token>.bin`).
MAIL_SCOPE = "mail"
_MAIL_NAME_HEAD = "m"


def file_name(scope, path):
    """The resources/ file name a row corresponds to: `<scope>.<path>`."""
    head = _MAIL_NAME_HEAD if scope == MAIL_SCOPE else scope
    return "%s.%s" % (head, path)


def split_name(name):
    """(scope, path) for a resources/ file name, or None if it has no dot."""
    head, dot, rest = name.partition(".")
    if not dot or not head or not rest:
        return None
    return (MAIL_SCOPE if head == _MAIL_NAME_HEAD else head), rest


def _member_of(scope):
    return int(scope) if scope.isdigit() else None


def _info(row):
    return Info(row["scope"], row["path"], int(row["size"]),
                float(row["updated_at"]))


# A member link only when the member exists, decided inside the statement so a
# member deleted a moment earlier cannot make the insert fail.
_PUT = ("INSERT INTO blob (scope, path, member_id, data, updated_at)"
        " VALUES (%s, %s, (SELECT id FROM member WHERE id = %s), %s,"
        " clock_timestamp())"
        " ON CONFLICT (scope, path) DO UPDATE SET data = EXCLUDED.data,"
        " member_id = EXCLUDED.member_id, updated_at = EXCLUDED.updated_at")

_COLS = ("scope, path, octet_length(data) AS size,"
         " extract(epoch FROM updated_at) AS updated_at")


def put(scope, path, data, conn=None):
    """Store `data` (bytes) under (scope, path), replacing what was there."""
    db.execute(_PUT, (str(scope), str(path), _member_of(str(scope)),
                      bytes(data)), conn=conn)
    return True


def get(scope, path, conn=None):
    """The stored bytes, or None."""
    row = db.query_one("SELECT data FROM blob WHERE scope = %s AND path = %s",
                       (str(scope), str(path)), conn=conn)
    return None if row is None else bytes(row["data"])


def stat(scope, path, conn=None):
    """Info for one object, or None when there is none."""
    row = db.query_one("SELECT " + _COLS + " FROM blob"
                       " WHERE scope = %s AND path = %s",
                       (str(scope), str(path)), conn=conn)
    return None if row is None else _info(row)


def exists(scope, path, conn=None):
    return stat(scope, path, conn=conn) is not None


def delete(scope, path, conn=None):
    """Remove one object. True when there was one."""
    return db.execute("DELETE FROM blob WHERE scope = %s AND path = %s",
                      (str(scope), str(path)), conn=conn) > 0


def rename(scope, path, new_scope, new_path, conn=None):
    """Move an object to a new name, replacing anything already there, and
    keeping its updated_at (the way os.replace kept a file's mtime). One
    transaction. False when the source does not exist."""
    scope, path = str(scope), str(path)
    new_scope, new_path = str(new_scope), str(new_path)
    if (scope, path) == (new_scope, new_path):
        return exists(scope, path, conn=conn)
    with db.transaction(conn=conn) as c:
        row = db.query_one("SELECT 1 FROM blob WHERE scope = %s AND path = %s"
                           " FOR UPDATE", (scope, path), conn=c)
        if row is None:
            return False
        db.execute("DELETE FROM blob WHERE scope = %s AND path = %s",
                   (new_scope, new_path), conn=c)
        db.execute("UPDATE blob SET scope = %s, path = %s,"
                   " member_id = (SELECT id FROM member WHERE id = %s)"
                   " WHERE scope = %s AND path = %s",
                   (new_scope, new_path, _member_of(new_scope), scope, path),
                   conn=c)
    return True


def _like(text):
    return (text.replace("\\", "\\\\").replace("%", "\\%")
            .replace("_", "\\_"))


def _where(scope, prefix, suffix, path):
    sql, args = [], []
    if scope is not None:
        sql.append("scope = %s")
        args.append(str(scope))
    if path is not None:
        sql.append("path = %s")
        args.append(str(path))
    if prefix:
        sql.append("path LIKE %s")
        args.append(_like(prefix) + "%")
    if suffix:
        sql.append("path LIKE %s")
        args.append("%" + _like(suffix))
    return (" WHERE " + " AND ".join(sql)) if sql else "", args


def listing(scope=None, prefix=None, suffix=None, path=None, conn=None):
    """Info for every object matching all the given filters, ordered by scope
    and path. `prefix`/`suffix` match the path; `path` matches it exactly (in
    any scope when `scope` is None). No data is read."""
    where, args = _where(scope, prefix, suffix, path)
    return [_info(r) for r in db.query(
        "SELECT " + _COLS + " FROM blob" + where + " ORDER BY scope, path",
        args, conn=conn)]


def delete_prefix(scope, prefix="", conn=None):
    """Remove every object in `scope` whose path starts with `prefix`.
    Returns Info for what was removed."""
    where, args = _where(scope, prefix, None, None)
    return [_info(r) for r in db.query(
        "DELETE FROM blob" + where + " RETURNING " + _COLS, args, conn=conn)]


def delete_member(member_ids, conn=None):
    """Remove every object of these members: their scopes, and any row linked
    to them. Returns Info for what was removed."""
    ids = [int(m) for m in member_ids]
    if not ids:
        return []
    return [_info(r) for r in db.query(
        "DELETE FROM blob WHERE scope = ANY(%s) OR member_id = ANY(%s)"
        " RETURNING " + _COLS,
        ([str(m) for m in ids], ids), conn=conn)]
