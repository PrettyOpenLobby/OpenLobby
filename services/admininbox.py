"""The Inbox: every thread the panel collects, from every source, in one list.

The sources are the PlayOnline core's GM calls, Report User reports and bug reports, and each
game's help desk (adminhelp.py). Each is read the way its own view reads it, with the same
permission: GM calls need "gm", reports and bug reports need "reports", a game's help desk its
help permission. A GM call is confidential: the list carries who filed it and its state, never
its subject or text, and only for a caller who may open the GM call view at all. Without the
core (POL_ADMIN_CORE=none) the Inbox is the help desks alone.

A source is read on a thread of its own and given adminhelp.DESK_TIMEOUT; one that fails or
is slow is named in "problems" and the others still show. Every row has the same shape:

    {"source": "gm"|"report"|"issue"|"help", "id", "game", "game_title", "subject",
     "who", "about", "at", "state": "open"|"answered"|"closed", "waiting",
     "login": {"system", "key", "name"} or None, "person": {"id", "name"} or None}

"at" is the newest activity in unix seconds, and the list is newest first. "waiting" means the
player is waiting for staff. A row's login is resolved to its People person (adminpeople.py)
when the caller holds "people": a PlayOnline account is always someone's, a game login is
someone's when a person has it linked (person_link) or its player proved it (game_link).
"""
import calendar
import concurrent.futures
import time

import adminhelp
import adminmodules
import adminpeople

SOURCES = ("gm", "report", "issue", "help")
SOURCE_TITLE = {"gm": "GM call", "report": "Player report", "issue": "Bug report", "help": "Game help"}
STATES = ("waiting", "open", "answered", "closed", "all")
POL_GAME = "pol"
LIMIT_MAX = 200


def _str(v, n=200):
    return " ".join(str(v if v is not None else "").split())[:n]


def _ts(v):
    """Unix seconds from a number or an ISO 8601 UTC time, else 0."""
    if v is None or v == "":
        return 0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    try:
        return float(s)
    except ValueError:
        pass
    try:
        return float(calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return 0


# --------------------------------------------------------------------------- #
# each source's rows, in the one shape
# --------------------------------------------------------------------------- #
def gm_row(rec):
    """A GM call. Its subject and text stay in the GM call view."""
    st = rec.get("status") if rec.get("status") in ("open", "answered", "closed") else "open"
    who = _str(rec.get("handle") or rec.get("caller"), 60)
    polid = rec.get("polid")
    return {"source": "gm", "id": _str(rec.get("id"), 80), "game": POL_GAME,
            "game_title": _str(rec.get("content_label"), 60) or "PlayOnline",
            "subject": "", "confidential": True, "who": who, "about": "",
            "at": max(_ts(rec.get("received_at")), _ts(rec.get("status_at")), _ts(rec.get("knocked_at"))),
            "state": st, "waiting": st == "open",
            "login": {"system": adminpeople.POL, "key": polid, "name": who} if polid else None}


def report_row(rec):
    st = rec.get("status") or "open"
    replies = [r for r in rec.get("replies") or [] if isinstance(r, dict)]
    state = "closed" if st in ("resolved", "wontfix") else ("answered" if replies else "open")
    sender = str(rec.get("contact") or rec.get("from") or "")
    who = _str(sender.split("@", 1)[0], 60)
    polid = rec.get("polid")
    return {"source": "report", "id": _str(rec.get("id"), 80), "game": POL_GAME,
            "game_title": _str(rec.get("application"), 60) or "PlayOnline",
            "subject": _str(rec.get("subject"), 120) or "(no subject)", "who": who,
            "about": _str(rec.get("suspect"), 60),
            "at": max([_ts(rec.get("received_at")), _ts(rec.get("status_at"))] + [_ts(r.get("at")) for r in replies]),
            "state": state, "waiting": state == "open",
            "login": {"system": adminpeople.POL, "key": polid, "name": who} if polid else None}


def issue_row(rec):
    st = rec.get("status") or "open"
    state = "closed" if st in ("resolved", "wontfix") else "open"
    first = (str(rec.get("description") or "").strip().splitlines() or [""])[0]
    who = _str(rec.get("handle"), 60)
    polid = rec.get("polid")
    return {"source": "issue", "id": _str(rec.get("id"), 96), "game": POL_GAME,
            "game_title": _str(rec.get("title"), 60) or "PlayOnline",
            "subject": _str(first, 120) or "(no description)", "who": who,
            "about": "with logs" if rec.get("client_files") or rec.get("server_files") else "",
            "at": max(_ts(rec.get("received_at")), _ts(rec.get("status_at"))),
            "state": state, "waiting": state == "open",
            "login": ({"system": adminpeople.POL, "key": polid, "name": who} if polid
                      else {"system": adminpeople.POL, "key": "", "name": who, "handle": True} if who else None)}


def help_row(m, t):
    r = adminhelp._row(m, t)
    state = "closed" if r["closed"] else ("open" if r["waiting"] else "answered")
    system = getattr(m, "accounts", "") or m.key
    return {"source": "help", "id": r["id"], "game": m.key, "game_title": m.title,
            "subject": r["subject"], "who": r["who"], "about": r["category"],
            "at": max(_ts(t.get("last")), _ts(r["at"])), "state": state, "waiting": state == "open",
            "login": {"system": system, "key": r.get("player") or "", "name": r["who"],
                      "game": m.key, "search": m.players is not None} if r["who"] or r.get("player") else None}


# --------------------------------------------------------------------------- #
# reading the sources
# --------------------------------------------------------------------------- #
def gather(fetchers, timeout=None):
    """fetchers: [(title, fn)] where fn() -> [row]. Every one runs at once; (rows, problems)."""
    timeout = adminhelp.DESK_TIMEOUT if timeout is None else timeout
    rows, problems = [], []
    if not fetchers:
        return rows, problems
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(fetchers)))
    try:
        futs = {pool.submit(fn): title for title, fn in fetchers}
        done, pending = concurrent.futures.wait(futs, timeout=timeout)
        for f in done:
            try:
                rows += [r for r in (f.result() or []) if isinstance(r, dict)]
            except Exception:              # noqa: BLE001 -- one source down is a note
                problems.append("%s: not answering right now" % futs[f])
        for f in pending:
            problems.append("%s: took too long to answer" % futs[f])
    finally:
        pool.shutdown(wait=False)
    return rows, sorted(problems)


def help_fetchers(can, owner, open_only):
    """One fetcher per help desk the caller may read."""
    out = []
    for m in adminhelp._desks(can, owner):
        out.append(("%s help desk" % m.title,
                    (lambda m=m: [help_row(m, t) for t in (m.help.list(m, open_only) or []) if isinstance(t, dict)])))
    return out


# --------------------------------------------------------------------------- #
# filters, people, paging
# --------------------------------------------------------------------------- #
def _state_ok(row, state):
    if state == "all":
        return True
    if state == "waiting":
        return row["waiting"] and row["state"] != "closed"
    if state == "open":
        return row["state"] != "closed"
    return row["state"] == state


def _person_logins(db, person):
    """What rows of this person look like: {(system, key)}, {(system, lower name)}, POL ID."""
    keys, names = set(), set()
    links = adminpeople.links(db, person)
    pol = next((r["key"] for r in links if r["system"] == adminpeople.POL), None)
    for r in links:
        if r["system"] == adminpeople.POL:
            continue
        keys.add((r["system"], str(r["key"])))
        if r.get("name"):
            names.add((r["system"], str(r["name"]).lower()))
    handles = set()
    if pol and not adminmodules.core_off():
        handles = {h.lower() for h in adminpeople._pol_handles(db, pol)}
    return pol, keys, names, handles


def _mine(row, pol, keys, names, handles):
    lg = row.get("login") or {}
    if not lg:
        return False
    if lg["system"] == adminpeople.POL:
        if lg.get("key"):
            return bool(pol) and lg["key"] == pol
        return bool(handles) and (lg.get("name") or "").lower() in handles
    if lg.get("key") and (lg["system"], str(lg["key"])) in keys:
        return True
    return (lg["system"], (lg.get("name") or "").lower()) in names or \
        (lg["system"], (lg.get("name") or "")) in keys


class Resolver:
    """Finds the person of a row's login. One per request: answers are kept."""

    def __init__(self, db, by):
        self.db, self.by, self.memo = db, by, {}
        self.core = not adminmodules.core_off()

    def _polid_person(self, polid):
        p = adminpeople.person_for_polid(self.db, polid, self.by)
        return {"id": p["id"], "name": p["name"]} if p else None

    def _one(self, rows):
        ids = {r["person_id"] for r in rows}
        return next(iter(ids)) if len(ids) == 1 else None

    def person(self, lg):
        if not lg:
            return None
        k = (lg["system"], lg.get("key") or "", (lg.get("name") or "").lower())
        if k not in self.memo:
            try:
                self.memo[k] = self._find(lg)
            except Exception:                # noqa: BLE001 -- unresolved is shown as the login
                self.db.rollback()
                self.memo[k] = None
        return self.memo[k]

    def _find(self, lg):
        db, system, key, name = self.db, lg["system"], str(lg.get("key") or ""), str(lg.get("name") or "")
        if system == adminpeople.POL:
            if not self.core:
                return None
            if not key and name:
                r = db.execute("SELECT m.polid FROM handle h JOIN member m ON m.id = h.member_id"
                               " WHERE lower(h.handle_name) = lower(%s) LIMIT 2", (name,)).fetchall()
                key = r[0]["polid"] if len(r) == 1 else ""
            return self._polid_person(key) if key else None
        pid = None
        if key:
            r = db.execute("SELECT person_id FROM person_link WHERE system = %s AND key = %s",
                           (system, key)).fetchall()
            pid = self._one(r)
        if pid is None and name:
            r = db.execute("SELECT person_id FROM person_link WHERE system = %s"
                           " AND (lower(name) = lower(%s) OR lower(key) = lower(%s))",
                           (system, name, name)).fetchall()
            pid = self._one(r)
        if pid is not None:
            p = adminpeople.get(db, pid)
            return {"id": p["id"], "name": p["name"]} if p else None
        if not self.core:
            return None
        polids = []
        try:
            if key:
                polids = [r["polid"] for r in db.execute(
                    "SELECT DISTINCT m.polid FROM game_link g JOIN member m ON m.id = g.member_id"
                    " WHERE g.game = %s AND g.account_key = %s", (system, key))]
            if not polids and name:
                polids = [r["polid"] for r in db.execute(
                    "SELECT DISTINCT m.polid FROM game_link g JOIN member m ON m.id = g.member_id"
                    " WHERE g.game = %s AND (lower(g.account_name) = lower(%s)"
                    " OR lower(g.account_key) = lower(%s))", (system, name, name))]
        except Exception:                    # noqa: BLE001 -- no game_link table: nobody proved it
            db.rollback()
            polids = []
        return self._polid_person(polids[0]) if len(polids) == 1 else None


def build(rows, problems, q, db=None, can=lambda p: False, owner=False, by=None, sources=()):
    """Filter, sort, page and resolve. `q`: {"source", "game", "state", "person", "offset", "limit"}."""
    src = q.get("source") or "all"
    game = q.get("game") or ""
    state = q.get("state") if q.get("state") in STATES else "waiting"
    try:
        offset = max(0, int(q.get("offset") or 0))
    except ValueError:
        offset = 0
    try:
        limit = max(1, min(LIMIT_MAX, int(q.get("limit") or 50)))
    except ValueError:
        limit = 50
    see_people = owner or can("people")
    person = None
    if q.get("person") and not see_people:
        return 403, {"error": "You don't have permission to do that.", "forbidden": True}
    if q.get("person") and db is not None:
        person = adminpeople.get(db, q.get("person"))
        if person is None:
            return 404, {"error": "No such person."}
    if person is not None:
        pol, keys, names, handles = _person_logins(db, person)
        rows = [r for r in rows if _mine(r, pol, keys, names, handles)]
    games = {}
    for r in rows:
        games.setdefault(r["game"], r["game_title"] if r["game"] != POL_GAME else "PlayOnline")
    counts = {s: 0 for s in SOURCES}
    for r in rows:
        if r["waiting"] and r["state"] != "closed" and (not game or r["game"] == game):
            counts[r["source"]] += 1
    rows = [r for r in rows if (src == "all" or r["source"] == src) and (not game or r["game"] == game)
            and _state_ok(r, state)]
    rows.sort(key=lambda r: (-(r["at"] or 0), r["source"], r["id"]))
    page = rows[offset:offset + limit]
    res = Resolver(db, by) if (db is not None and see_people) else None
    for r in page:
        r["source_title"] = SOURCE_TITLE[r["source"]]
        r["person"] = res.person(r.get("login")) if res else None
        lg = r.get("login")
        if lg is not None:
            # a POL ID is shown where the source shows it already (GM calls, reports) or to
            # those who may see accounts; the login's name is the player's own
            if lg["system"] == adminpeople.POL and not (owner or can("accounts_view")
                                                        or r["source"] in ("gm", "report")):
                lg = dict(lg, key="")
            r["login"] = dict(lg, title=adminpeople.system_title(lg["system"]))
            if r["source"] != "help" or not (owner or can("people_link")):
                r["login"].pop("search", None)
        if r["source"] == "gm":
            r["subject"] = ""
    return 200, {"threads": page, "total": len(rows), "offset": offset, "limit": limit,
                 "waiting": counts, "problems": problems, "sources": list(sources),
                 "games": [{"key": k, "title": v} for k, v in sorted(games.items(), key=lambda kv: kv[1].lower())],
                 "person": {"id": person["id"], "name": person["name"]} if person is not None else None,
                 "people": see_people, "core": not adminmodules.core_off()}
