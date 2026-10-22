"""People: the admin panel's record of one human, across PlayOnline and the games.

A person (table person, migration 0015) has logins (person_link): the PlayOnline account
("pol", the POL ID), a game's login (under the game's account system, the key game_link
uses; a game without one uses its panel module key, adminmodules.Module.accounts) or a
Discord user ("discord"). Logins come two ways:

- proven: the player showed they are theirs. A PlayOnline account is its own; the logins
  its members linked on the website (game_link, 0014) and through the Discord bot
  (discord_link, discordlink.py) are read from those tables every time, never copied.
- staff: a moderator linked it by hand, with a reason, and it says who and when.

One login belongs to one person: a login already someone's is refused with that person's
name. Merging moves one person's links and notes into another and leaves the merged row
behind as a pointer (merged_into). A person for a PlayOnline account is made the first
time the panel needs it (person_for_polid), so accounts that existed before need nothing.

Without the PlayOnline core (POL_ADMIN_CORE=none) there are no accounts, sessions, game_link
or discord_link rows to read: persons are made by staff and hold staff links only.

The functions take a database connection and leave committing to the caller; they raise
PeopleError with a sentence the panel shows as it is.
"""
import time

import adminmodules

POL, DISCORD = "pol", "discord"
NAME_MAX, WHY_MAX, NOTE_MAX, KEY_MAX = 80, 300, 2000, 128
LIST_MAX = 100


class PeopleError(ValueError):
    """A refusal the panel shows as it is. `status` is the HTTP status; `extra` goes in the answer."""

    def __init__(self, msg, status=400, **extra):
        super().__init__(msg)
        self.status, self.extra = status, extra


def _core():
    return not adminmodules.core_off()


def _clean(v, n):
    return " ".join(str(v if v is not None else "").split())[:n]


def _like(q):
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --------------------------------------------------------------------------- #
# account systems and their names
# --------------------------------------------------------------------------- #
def systems():
    """{system: [module, ...]}: the enabled games under each account system."""
    out = {}
    for m in adminmodules.enabled().values():
        out.setdefault(getattr(m, "accounts", "") or m.key, []).append(m)
    return out


def system_title(system, mods=None):
    if system == POL:
        return "PlayOnline"
    if system == DISCORD:
        return "Discord"
    mods = systems() if mods is None else mods
    got = mods.get(system)
    return ", ".join(m.title for m in got) if got else system


# --------------------------------------------------------------------------- #
# finding a person
# --------------------------------------------------------------------------- #
def get(db, pid):
    """The person row for id `pid`, following merges, or None."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    for _ in range(20):
        row = db.execute("SELECT * FROM person WHERE id = %s", (pid,)).fetchone()
        if row is None or row["merged_into"] is None:
            return row
        pid = row["merged_into"]
    return None


def polid_of(db, pid):
    r = db.execute("SELECT key FROM person_link WHERE person_id = %s AND system = %s ORDER BY at, id LIMIT 1",
                   (int(pid), POL)).fetchone()
    return r["key"] if r else None


def _pol_handles(db, polid):
    return [r["handle_name"] for r in db.execute(
        "SELECT h.handle_name FROM handle h JOIN member m ON m.id = h.member_id"
        " WHERE m.polid = %s ORDER BY h.is_primary DESC, h.id", (polid,))]


def _pol_members(db, polid):
    return [r["id"] for r in db.execute("SELECT id FROM member WHERE polid = %s ORDER BY id", (polid,))]


def person_for_polid(db, polid, by=None):
    """The person for a PlayOnline account, made now if this is the first time it is
    needed. None when there is no such account (or no PlayOnline core)."""
    if not _core():
        return None
    polid = _clean(polid, 32)
    r = db.execute("SELECT person_id FROM person_link WHERE system = %s AND key = %s",
                   (POL, polid)).fetchone()
    if r:
        return get(db, r["person_id"])
    acct = db.execute("SELECT polid FROM polid WHERE polid = %s", (polid,)).fetchone()
    if acct is None:
        return None
    polid = acct["polid"]
    handles = _pol_handles(db, polid)
    now = time.time()
    try:
        pid = db.execute("INSERT INTO person (name, created_at, created_by) VALUES (%s,%s,%s)"
                         " RETURNING id", (handles[0] if handles else polid, now, by)).fetchone()["id"]
        db.execute("INSERT INTO person_link (person_id, system, key, name, how, \"by\", why, at)"
                   " VALUES (%s,%s,%s,%s,'proven',NULL,'',%s)", (pid, POL, polid, polid, now))
        db.commit()
    except Exception:                       # noqa: BLE001 -- another request made it first
        db.rollback()
        r = db.execute("SELECT person_id FROM person_link WHERE system = %s AND key = %s",
                       (POL, polid)).fetchone()
        if not r:
            raise
        pid = r["person_id"]
    return get(db, pid)


def create(db, name, by):
    name = _clean(name, NAME_MAX)
    if not name:
        raise PeopleError("Give the person a name.")
    return db.execute("INSERT INTO person (name, created_at, created_by) VALUES (%s,%s,%s)"
                      " RETURNING *", (name, time.time(), by)).fetchone()


def rename(db, person, name):
    name = _clean(name, NAME_MAX)
    if not name:
        raise PeopleError("Give the person a name.")
    db.execute("UPDATE person SET name = %s WHERE id = %s", (name, person["id"]))
    return name


# --------------------------------------------------------------------------- #
# links
# --------------------------------------------------------------------------- #
def _proven(db, polid):
    """The logins a PlayOnline account's members proved: game_link and discord_link rows."""
    out = []
    members = _pol_members(db, polid)
    if not members:
        return out
    try:
        for r in db.execute("SELECT game, account_key, account_name, linked_at FROM game_link"
                            " WHERE member_id = ANY(%s) ORDER BY game, account_key", (members,)):
            out.append({"system": r["game"], "key": r["account_key"], "name": r["account_name"],
                        "how": "proven", "source": "website", "at": r["linked_at"]})
    except Exception:                       # noqa: BLE001 -- no game_link table: none proven there
        db.rollback()
    try:
        for r in db.execute("SELECT discord_id, discord_name, linked_at FROM discord_link"
                            " WHERE member_id = ANY(%s)", (members,)):
            out.append({"system": DISCORD, "key": r["discord_id"],
                        "name": r["discord_name"] or r["discord_id"],
                        "how": "proven", "source": "bot", "at": r["linked_at"]})
    except Exception:                       # noqa: BLE001
        db.rollback()
    return out


def links(db, person):
    """Every login of the person: its own rows (the PlayOnline account and staff links),
    then what the account's members proved."""
    rows = [dict(r) for r in db.execute(
        "SELECT id, system, key, name, how, \"by\", why, at FROM person_link WHERE person_id = %s"
        " ORDER BY (system = %s) DESC, at, id", (person["id"], POL))]
    out = [dict(r, source="account" if r["system"] == POL else "staff") for r in rows]
    pol = next((r["key"] for r in rows if r["system"] == POL), None)
    if pol and _core():
        have = {(r["system"], r["key"]) for r in rows}
        out += [p for p in _proven(db, pol) if (p["system"], p["key"]) not in have]
    return out


def owner_of(db, system, key):
    """Who a login belongs to already: (person row or None, sentence) or (None, None)."""
    r = db.execute("SELECT person_id FROM person_link WHERE system = %s AND key = %s",
                   (system, key)).fetchone()
    if r:
        p = get(db, r["person_id"])
        return p, "%s already belongs to %s." % ("That login", p["name"] if p else "another person")
    if not _core():
        return None, None
    polid = None
    try:
        if system == DISCORD:
            m = db.execute("SELECT m.polid FROM discord_link d JOIN member m ON m.id = d.member_id"
                           " WHERE d.discord_id = %s", (key,)).fetchone()
        else:
            m = db.execute("SELECT m.polid FROM game_link g JOIN member m ON m.id = g.member_id"
                           " WHERE g.game = %s AND g.account_key = %s", (system, key)).fetchone()
        polid = m["polid"] if m else None
    except Exception:                       # noqa: BLE001 -- no such table: nobody proved it
        db.rollback()
    if not polid:
        return None, None
    p = person_for_polid(db, polid)
    who = p["name"] if p else polid
    return p, "The player of %s proved that login is theirs (PlayOnline ID %s)." % (who, polid)


def link(db, person, system, key, name, why, by):
    """A staff link. -> the new row. Refused when the login is someone's already."""
    system, key = _clean(system, 32), str(key or "").strip()[:KEY_MAX]
    name, why = _clean(name, NAME_MAX) or key, _clean(why, WHY_MAX)
    if not system or not key:
        raise PeopleError("Pick the account to link.")
    if system == POL:
        raise PeopleError("A PlayOnline account is a person of its own: merge the two people instead.")
    if not why:
        raise PeopleError("Say why you are linking it: it is kept in the history.")
    other, said = owner_of(db, system, key)
    if other is not None and other["id"] == person["id"]:
        raise PeopleError("%s is linked to %s already." % (name, person["name"]), 409)
    if said:
        raise PeopleError(said + " Merge the two people if they are the same person.", 409,
                          person={"id": other["id"], "name": other["name"]} if other else None)
    return db.execute("INSERT INTO person_link (person_id, system, key, name, how, \"by\", why, at)"
                      " VALUES (%s,%s,%s,%s,'staff',%s,%s,%s) RETURNING *",
                      (person["id"], system, key, name, by, why, time.time())).fetchone()


def unlink(db, person, link_id, why):
    why = _clean(why, WHY_MAX)
    if not why:
        raise PeopleError("Say why you are unlinking it: it is kept in the history.")
    try:
        link_id = int(link_id)
    except (TypeError, ValueError):
        raise PeopleError("No such link.", 404)
    row = db.execute("SELECT * FROM person_link WHERE id = %s AND person_id = %s",
                     (link_id, person["id"])).fetchone()
    if row is None:
        raise PeopleError("No such link.", 404)
    if row["how"] != "staff":
        raise PeopleError("Only links staff made can be removed here. A proven login is removed"
                          " by the player, or with the account.")
    db.execute("DELETE FROM person_link WHERE id = %s", (link_id,))
    return row


def merge(db, into, other, force=False):
    """Move `other` into `into`: its links and notes. Refused when both have a PlayOnline
    account, unless `force` (the owner's)."""
    if into["id"] == other["id"]:
        raise PeopleError("That is the same person.")
    a, b = polid_of(db, into["id"]), polid_of(db, other["id"])
    if a and b and not force:
        raise PeopleError("Both have a PlayOnline account (%s and %s), so they are two people."
                          " Only the owner can merge them anyway." % (a, b), 409, both_pol=True)
    db.execute("UPDATE person_link SET person_id = %s WHERE person_id = %s", (into["id"], other["id"]))
    db.execute("UPDATE person_note SET person_id = %s WHERE person_id = %s", (into["id"], other["id"]))
    db.execute("UPDATE person SET merged_into = %s WHERE merged_into = %s", (into["id"], other["id"]))
    db.execute("UPDATE person SET merged_into = %s WHERE id = %s", (into["id"], other["id"]))


def note(db, person, text, by):
    text = str(text or "").strip()[:NOTE_MAX]
    if not text:
        raise PeopleError("Write the note first.")
    return db.execute("INSERT INTO person_note (person_id, \"by\", at, text) VALUES (%s,%s,%s,%s)"
                      " RETURNING *", (person["id"], by, time.time(), text)).fetchone()


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #
def _merged_ids(db, pid):
    """The person and everyone merged into them, at any depth."""
    ids, todo = [pid], [pid]
    while todo and len(ids) < 200:
        got = [r["id"] for r in db.execute("SELECT id FROM person WHERE merged_into = ANY(%s)", (todo,))]
        todo = [i for i in got if i not in ids]
        ids += todo
    return ids


def who(db, person, rows=None):
    """The player lookup's `who` (adminplayers.lookup) for every login of the person."""
    rows = links(db, person) if rows is None else rows
    pol = next((r["key"] for r in rows if r["system"] == POL), None)
    handles, members = [], []
    if pol and _core():
        handles, members = _pol_handles(db, pol), [str(m) for m in _pol_members(db, pol)]
    out = {}
    for r in rows:
        if r["system"] != POL:
            out.setdefault(r["system"], []).append({"key": r["key"], "name": r["name"]})
    return {"polid": pol or "", "handles": handles, "members": members, "links": out}


def _online(db, polid):
    if not polid or not _core():
        return None, None
    r = db.execute(
        "SELECT EXISTS (SELECT 1 FROM session s JOIN member m ON m.id = s.member_id"
        "  WHERE m.polid = %s AND s.expires_at > %s) AS online,"
        " (SELECT max(last_login_at) FROM member WHERE polid = %s) AS seen",
        (polid, _now_iso(), polid)).fetchone()
    return bool(r["online"]), r["seen"]


def history(db, person, polid, limit=100):
    """Panel activity about the person (link, unlink, merge, note, rename) and about their
    PlayOnline account, newest first."""
    ids = _merged_ids(db, person["id"])
    targets = ["person:%d" % i for i in ids] + ([polid.lower()] if polid else [])
    rows = db.execute("SELECT at, actor, action, detail, ok, target FROM admin_audit"
                      " WHERE lower(target) = ANY(%s) AND ok = 1 ORDER BY at DESC, id DESC LIMIT %s",
                      (targets, int(limit))).fetchall()
    return [{"at": r["at"], "by": r["actor"], "text": r["action"], "detail": r["detail"] or "",
             "kind": "people" if (r["target"] or "").startswith("person:") else "account"}
            for r in rows if not (r["action"] or "").startswith("added a note about ")]


def notes(db, person):
    return [{"id": r["id"], "by": r["by"], "at": r["at"], "text": r["text"]} for r in db.execute(
        "SELECT * FROM person_note WHERE person_id = %s ORDER BY at DESC, id DESC", (person["id"],))]


def _fill(field, system, rows, pol, handles):
    """What a game action's field gets for this person (a kick or ban sent from their page)."""
    mine = [r for r in rows if r["system"] == system]
    fid = str(field.get("id") or "")
    if fid == "polid":
        return pol or ""
    if fid == "handles":
        return ",".join(handles)
    if field.get("type") == "player":
        return mine[0]["key"] if mine else ""
    return None                             # anything else (a name, how long, why) staff type in


def page(db, person, can, owner):
    """Everything the person page shows. `can(perm)`: the caller's permissions."""
    rows = links(db, person)
    pol = next((r["key"] for r in rows if r["system"] == POL), None)
    see_pol = owner or can("accounts_view")
    online, seen = _online(db, pol)
    handles = _pol_handles(db, pol) if pol and _core() else []
    mods = systems()
    mine = {r["system"] for r in rows}
    out_links = []
    for r in rows:
        if r["system"] == POL and not see_pol:
            out_links.append({"system": POL, "title": "PlayOnline", "how": "proven", "source": "account",
                              "key": "", "name": "", "hidden": True})
            continue
        out_links.append({"id": r.get("id") if r["how"] == "staff" else None, "system": r["system"],
                          "title": system_title(r["system"], mods), "key": r["key"], "name": r["name"],
                          "how": r["how"], "source": r["source"], "by": r.get("by"),
                          "why": r.get("why") or "", "at": r.get("at")})
    actions, games = [], []
    for m in adminmodules.enabled().values():
        sysk = getattr(m, "accounts", "") or m.key
        if sysk in mine and (owner or adminmodules.can_see(m.key, can)):
            games.append({"key": m.key, "title": m.title})
        for a in m.actions:
            if a.kind not in ("kick", "ban") or not (owner or can(m.perm(a.id))):
                continue
            if sysk not in mine and not (pol and a.kind == "kick"):
                continue
            params, ask = {}, []
            for f in a.fields:
                if not (isinstance(f, dict) and f.get("id")):
                    continue
                v = _fill(f, sysk, rows, pol, handles)
                if v is None:
                    ln = [r for r in rows if r["system"] == sysk]
                    ask.append({"id": f["id"], "label": f.get("label") or f["id"],
                                "default": ln[0]["name"] if ln else (handles[0] if handles else "")})
                else:
                    params[f["id"]] = v
            if pol:
                params.setdefault("polid", pol)
                params.setdefault("handles", ",".join(handles))
            actions.append({"game": m.key, "title": m.title, "id": a.id, "label": a.label,
                            "kind": a.kind, "danger": a.danger, "params": params, "ask": ask})
    targets = []
    for m in adminmodules.enabled().values():
        if m.players is not None and (owner or adminmodules.can_see(m.key, can)):
            targets.append({"game": m.key, "system": getattr(m, "accounts", "") or m.key,
                            "title": m.title, "search": True})
    targets.append({"game": "", "system": DISCORD, "title": "Discord", "search": False})
    return {"id": person["id"], "name": person["name"], "created_at": person["created_at"],
            "created_by": person["created_by"], "pol": bool(pol),
            "polid": pol if see_pol else None, "handles": handles if see_pol else [],
            "online": online, "last_seen": seen, "links": out_links, "games": games,
            "actions": actions, "link_targets": targets, "notes": notes(db, person),
            "history": history(db, person, pol if see_pol else None),
            "core": _core()}


def search(db, q, online_only=False, see_pol=True, limit=LIST_MAX):
    """The People list: persons and PlayOnline accounts matching `q` (a person's name, a
    POL ID or handle, any linked login's name or key), online ones first."""
    q = _clean(q, 64)
    pat = _like(q)
    core = _core()
    mods = systems()
    people = {}
    sql = ("SELECT p.id, p.name, p.created_at,"
           " (SELECT key FROM person_link l WHERE l.person_id = p.id AND l.system = %s"
           "  ORDER BY l.at, l.id LIMIT 1) AS polid"
           " FROM person p WHERE p.merged_into IS NULL")
    args = [POL]
    if q:
        sql += (" AND (p.name ILIKE %s ESCAPE '\\' OR EXISTS (SELECT 1 FROM person_link l"
                " WHERE l.person_id = p.id AND (l.system <> %s OR %s)"
                " AND (l.key ILIKE %s ESCAPE '\\' OR l.name ILIKE %s ESCAPE '\\')))")
        args += [pat, POL, bool(see_pol), pat, pat]
    sql += " ORDER BY p.id DESC LIMIT 500"
    for r in db.execute(sql, args):
        people[r["id"]] = dict(r)
    by_pol = {}
    if core:
        known = [r["polid"] for r in people.values() if r["polid"]]
        if see_pol:
            where, args = "", [_now_iso()]
            if q:
                where = (" WHERE p.polid ILIKE %s ESCAPE '\\'"
                         " OR EXISTS (SELECT 1 FROM handle h JOIN member m ON m.id = h.member_id"
                         "  WHERE m.polid = p.polid AND h.handle_name ILIKE %s ESCAPE '\\')"
                         " OR EXISTS (SELECT 1 FROM game_link g JOIN member m ON m.id = g.member_id"
                         "  WHERE m.polid = p.polid AND (g.account_name ILIKE %s ESCAPE '\\'"
                         "  OR g.account_key ILIKE %s ESCAPE '\\'))"
                         " OR EXISTS (SELECT 1 FROM discord_link d JOIN member m ON m.id = d.member_id"
                         "  WHERE m.polid = p.polid AND (d.discord_name ILIKE %s ESCAPE '\\'"
                         "  OR d.discord_id = %s))")
                args += [pat, pat, pat, pat, pat, q]
            sql = ("SELECT p.polid,"
                   " EXISTS (SELECT 1 FROM session s JOIN member m ON m.id = s.member_id"
                   "  WHERE m.polid = p.polid AND s.expires_at > %s) AS online,"
                   " (SELECT max(last_login_at) FROM member m WHERE m.polid = p.polid) AS seen,"
                   " p.created_at FROM polid p" + where +
                   " ORDER BY online DESC, seen DESC NULLS LAST, p.created_at DESC LIMIT %s")
            args.append(int(limit))
            for r in db.execute(sql, args):
                by_pol[r["polid"]] = dict(r)
        missing = [p for p in known if p not in by_pol]
        if missing:
            for r in db.execute(
                    "SELECT p.polid,"
                    " EXISTS (SELECT 1 FROM session s JOIN member m ON m.id = s.member_id"
                    "  WHERE m.polid = p.polid AND s.expires_at > %s) AS online,"
                    " (SELECT max(last_login_at) FROM member m WHERE m.polid = p.polid) AS seen,"
                    " p.created_at FROM polid p WHERE p.polid = ANY(%s)", (_now_iso(), missing)):
                by_pol[r["polid"]] = dict(r)
    pols = list(by_pol)
    handles, proven = {}, {}
    if pols:
        for r in db.execute("SELECT m.polid, h.handle_name FROM handle h JOIN member m ON m.id = h.member_id"
                            " WHERE m.polid = ANY(%s) ORDER BY h.is_primary DESC, h.id", (pols,)):
            handles.setdefault(r["polid"], []).append(r["handle_name"])
        try:
            for r in db.execute("SELECT m.polid, g.game FROM game_link g JOIN member m ON m.id = g.member_id"
                                " WHERE m.polid = ANY(%s)", (pols,)):
                proven.setdefault(r["polid"], set()).add(r["game"])
        except Exception:                   # noqa: BLE001
            db.rollback()
        try:
            for r in db.execute("SELECT m.polid FROM discord_link d JOIN member m ON m.id = d.member_id"
                                " WHERE m.polid = ANY(%s)", (pols,)):
                proven.setdefault(r["polid"], set()).add(DISCORD)
        except Exception:                   # noqa: BLE001
            db.rollback()
    persons_of_pol = {r["polid"]: r for r in people.values() if r["polid"]}
    if pols:
        for r in db.execute("SELECT p.id, p.name, p.created_at, l.key AS polid FROM person_link l"
                            " JOIN person p ON p.id = l.person_id WHERE l.system = %s AND l.key = ANY(%s)"
                            " AND p.merged_into IS NULL", (POL, pols)):
            persons_of_pol.setdefault(r["polid"], dict(r))
            people.setdefault(r["id"], dict(r))
    staff = {}
    if people:
        for r in db.execute("SELECT person_id, system, how FROM person_link WHERE person_id = ANY(%s)"
                            " AND system <> %s ORDER BY at, id", (list(people), POL)):
            staff.setdefault(r["person_id"], []).append((r["system"], r["how"]))
    out = []

    def accounts_of(pid, pol):
        seen_sys, acc = {}, []
        if pol:
            acc.append({"system": POL, "title": "PlayOnline", "staff": False})
        for s in sorted(proven.get(pol, ())) if pol else ():
            seen_sys[s] = len(acc)
            acc.append({"system": s, "title": system_title(s, mods), "staff": False})
        for s, how in staff.get(pid, []) if pid else []:
            if s in seen_sys:
                continue
            seen_sys[s] = len(acc)
            acc.append({"system": s, "title": system_title(s, mods), "staff": how == "staff"})
        return acc

    done = set()
    for pol, r in by_pol.items():
        p = persons_of_pol.get(pol)
        if p and p["id"] in done:
            continue                        # a person merged with two accounts (the owner's force)
        hs = handles.get(pol, [])
        out.append({"id": p["id"] if p else None, "polid": pol if see_pol else None, "pol": True,
                    "name": p["name"] if p else (hs[0] if hs else pol), "handles": hs if see_pol else [],
                    "accounts": accounts_of(p["id"] if p else None, pol),
                    "online": bool(r["online"]), "last_seen": r["seen"]})
        if p:
            done.add(p["id"])
    for pid, r in people.items():
        if pid in done or (r["polid"] and r["polid"] in by_pol):
            continue
        if r["polid"] and not core:
            continue
        out.append({"id": pid, "polid": (r["polid"] if see_pol else None), "pol": bool(r["polid"]),
                    "name": r["name"], "handles": [], "accounts": accounts_of(pid, None) if not r["polid"]
                    else [{"system": POL, "title": "PlayOnline", "staff": False}] + accounts_of(pid, None),
                    "online": None, "last_seen": None, "created_at": r["created_at"]})
    if online_only:
        out = [r for r in out if r["online"]]
    out.sort(key=lambda r: (r["online"] is not True, -(_ts(r["last_seen"]) or 0), -(r["id"] or 0)))
    return out[:limit]


def _ts(iso):
    if not iso:
        return None
    try:
        return time.mktime(time.strptime(str(iso)[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def logins(db, game, rows, can_see_person=True):
    """A game's player search rows, each marked with the person it already belongs to."""
    m = adminmodules.enabled().get(game)
    system = (getattr(m, "accounts", "") or game) if m else game
    out = []
    for r in rows:
        other, _said = owner_of(db, system, r["id"])
        r = dict(r, system=system)
        if other is not None:
            r["person"] = {"id": other["id"], "name": other["name"]}
        out.append(r)
    return out
