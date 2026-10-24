"""Reading a game's live status into the one shape the panel draws.

A module's `live` is a callable taking the module and returning a status:

    {"up":       True / False / None (unknown),
     "online":   players on now, or None,
     "players":  [{"name", "where", "detail"}],     at most MAX_ROWS
     "rooms":    [{"name", "detail", "state"}],     rooms, tables, matches...
     "rooms_label": what a room is called in this game ("tables"),
     "summary":  one line about what is going on,
     "facts":    [{"label", "value"}],              a few more numbers
     "error":    why the status could not be read, or None,
     "source":   where it came from, for the module's About box,
     "at":       when it was read,
     "update":   the game's own update report, when its status JSON has one
                 (unchecked here; services/adminupdates.py cleans it)}

`board(...)` builds one from a status JSON (the game's own live.json or a
board's /state.json; the side games share one layout, see generic()) and the
live-session markers the POL titles publish. A module passes `parse` to pick
out what only its game has.
"""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

MAX_ROWS = 200
MAX_BYTES = 4 << 20
TIMEOUT = 3.0
MARKER_FRESH = 60.0
MAX_UPDATE = 64 << 10      # the most a status's "update" object may hold, as JSON


def fetch(where):
    """The JSON at a URL or in a file. Raises OSError/ValueError."""
    if where.startswith(("http://", "https://")):
        req = urllib.request.Request(where, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = r.read(MAX_BYTES + 1)
    else:
        with open(where, "rb") as f:
            data = f.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("status document is too large")
    return json.loads(data)


def marker_count(names):
    """(players, newest stamp) summed over fresh live-session markers; players
    is None when no marker is fresh."""
    try:
        import live_sessions
    except Exception:                          # noqa: BLE001 -- no store here
        return None, None
    total, fresh, newest = 0, False, None
    now = time.time()
    for n in names:
        d = live_sessions.read_marker(n)
        if not d:
            continue
        stamp = float(d.get("stamp") or 0)
        if 0 <= now - stamp < MARKER_FRESH:
            fresh = True
            total += int(d.get("count") or 0)
            newest = max(newest or 0, stamp)
    return (total if fresh else None), newest


def plural(n, word, words=None):
    """'1 player', '3 players' (`words` when the plural is not word + s)."""
    return "%s %s" % (n, word if n == 1 else (words or word + "s"))


def _text(v, limit=80):
    return " ".join(str(v).split())[:limit] if v not in (None, "") else ""


def _player(p):
    if isinstance(p, str):
        return {"name": _text(p), "where": "", "detail": ""}
    if not isinstance(p, dict):
        return None
    name = p.get("name") or p.get("handle") or p.get("nick") or p.get("char")
    if not name:
        return None
    where = (p.get("where") or p.get("zone") or p.get("place") or p.get("room")
             or p.get("lobby") or p.get("area") or "")
    bits = []
    for k in ("job", "cls", "level", "clan", "game"):
        if p.get(k) not in (None, ""):
            bits.append(str(p[k]))
    return {"name": _text(name, 40), "where": _text(where), "detail": _text(" ".join(bits))}


def _room(r):
    if not isinstance(r, dict):
        return None
    name = r.get("name") or r.get("title") or (("Room %s" % r["id"]) if "id" in r else "")
    if not name:
        return None
    n, cap = r.get("players"), r.get("max") or r.get("capacity")
    if isinstance(n, list):
        n = len(n)
    detail = []
    if n is not None:
        detail.append("%s%s %s" % (n, "/%s" % cap if cap else "", "player" if n == 1 and not cap else "players"))
    for k in ("mode", "stage", "rule", "course"):
        if r.get(k):
            detail.append(_text(r[k], 30))
    return {"name": _text(name, 50), "detail": ", ".join(detail),
            "state": _text(r.get("state") or r.get("status") or "", 20)}


def generic(doc):
    """The fields every status JSON here agrees on: up, error, online (an int,
    a list of players, or {"total", "players"}), players, rooms, matches."""
    st = {"up": doc.get("up", True) if isinstance(doc, dict) else None,
          "online": None, "players": [], "rooms": [], "rooms_label": "rooms",
          "summary": "", "facts": [], "error": None}
    if not isinstance(doc, dict):
        st["error"] = "status is not a JSON object"
        return st
    if doc.get("error"):
        st["error"] = _text(doc["error"], 200)
    on = doc.get("online")
    players = doc.get("players")
    if isinstance(on, dict):
        players = players or on.get("players")
        on = on.get("total")
    if isinstance(on, list):
        players = players if players is not None else on
        on = len(on)
    if isinstance(on, bool) or not isinstance(on, (int, float)):
        on = doc.get("count") if isinstance(doc.get("count"), int) else None
    st["online"] = int(on) if on is not None else (len(players) if isinstance(players, list) else None)
    st["players"] = [x for x in map(_player, players or []) if x][:MAX_ROWS] \
        if isinstance(players, list) else []
    rooms = doc.get("rooms")
    if isinstance(rooms, list):
        st["rooms"] = [x for x in map(_room, rooms) if x][:MAX_ROWS]
    matches = doc.get("matches")
    if isinstance(matches, list) and matches:
        st["facts"].append({"label": "matches playing", "value": len(matches)})
    # the game's own update report (docs/admin-modules.md), passed on as it came when it is an
    # object of a sane size; services/adminupdates.py checks every field before showing it
    upd = doc.get("update")
    if isinstance(upd, dict):
        try:
            if len(json.dumps(upd)) <= MAX_UPDATE:
                st["update"] = upd
        except (TypeError, ValueError):
            pass
    return st


def board(parse=None, markers=(), default="", counted=()):
    """A `live` for MODULE: the status JSON at POL_ADMIN_GAME_<KEY>_LIVE (or
    `default`), read with generic() then `parse(doc, status)`, and the player
    count from `markers` when the JSON has none or cannot be read. `counted`
    are (marker, label) pairs whose count is something other than players
    (Tetra Master's marker counts matches); they become facts."""
    def read(m):
        st = _read(m)
        for name, label in counted:
            n, _stamp = marker_count([name])
            if n is None:
                continue
            if st is None:
                st = {"up": True, "online": None, "players": [], "rooms": [], "rooms_label": "rooms",
                      "summary": "", "facts": [], "error": None, "source": "live-session marker"}
            st["facts"].append({"label": label, "value": n})
        return st

    def _read(m):
        where = m.env("LIVE") or default
        st = None
        if where:
            try:
                doc = fetch(where)
                st = generic(doc)
                if parse:
                    parse(doc, st)
                st["source"] = "status JSON"
            except (OSError, ValueError, urllib.error.URLError) as exc:
                st = {"up": False, "online": None, "players": [], "rooms": [],
                      "rooms_label": "rooms", "summary": "", "facts": [],
                      "error": "status JSON unreadable (%s)" % _text(getattr(exc, "reason", exc), 120),
                      "source": "status JSON"}
        if markers:
            n, _stamp = marker_count(markers)
            if st is None:
                if n is None:
                    return None
                st = {"up": True, "online": n, "players": [], "rooms": [], "rooms_label": "rooms",
                      "summary": "", "facts": [], "error": None, "source": "live-session marker"}
            elif n is not None and (st["online"] is None or st.get("error")):
                st["online"], st["up"] = n, True
                st["source"] += " and live-session marker"
        return st
    return read


# -- calendars ---------------------------------------------------------------- #
# A module's calendar is fn(module, lo, hi) -> [entry]: what the game has scheduled between unix
# seconds lo and hi. An entry is {"id", "title", "start", "end"} (unix seconds) plus optional
# "kind" (a short word: cup, campaign, festival...), "about" (a sentence) and "times" (the
# [start, end] windows inside a long entry, such as a cup's one-hour sessions).

def eventcal(game, kinds=None):
    """The event calendar the game reads (services/eventcal.py). A season split into sessions
    becomes one entry carrying its sessions as "times"."""
    def cal(m, lo, hi):
        import eventcal as E
        seasons = {}
        out = []
        # wide enough that a season crossing the range's edge keeps all its sessions
        for w in E.windows(game, lo - 8 * 86400, hi + 8 * 86400):
            if w.get("season"):
                s = seasons.get(w["season"])
                if s is None:
                    s = seasons[w["season"]] = {
                        "id": w["season"], "title": _text(w.get("name"), 80) or w["season"],
                        "start": int(w["season_start"]), "end": int(w["season_end"]),
                        "kind": (kinds or {}).get(w.get("season_kind"), w.get("season_kind") or "event"),
                        "times": []}
                    out.append(s)
                s["times"].append([int(w["start"]), int(w["end"])])
                continue
            out.append({"id": w.get("id") or "%s-%d" % (game, w["start"]),
                        "title": _text(w.get("name"), 80) or "Event",
                        "start": int(w["start"]), "end": int(w["end"]),
                        "kind": (kinds or {}).get(w.get("kind"), w.get("kind") or "event")})
        return [e for e in out if e["end"] > lo and e["start"] < hi]
    return cal


def kv_calendar(key, kinds=None):
    """A list a game's own process keeps in the core's live-state store (polcore.kv) as
    {"at", "events": [{"source", "key", "name", "start", "end", "detail"}]}."""
    def cal(m, lo, hi):
        from polcore import kv
        doc = kv.default().get_json(key) or {}
        out = []
        for e in (doc or {}).get("events") or []:
            try:
                start, end = int(e["start"]), int(e["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if end <= lo or start >= hi:
                continue
            src = _text(e.get("source"), 20)
            out.append({"id": "%s-%s" % (src, _text(e.get("key"), 60)), "title": _text(e.get("name"), 80),
                        "start": start, "end": end, "kind": (kinds or {}).get(src, src or "event")})
        return out
    return cal


def tool_calendar(path="calendar.json"):
    """The calendar a game serves on its own tool port (the panel's proxy, admingames.get_json):
    GET <path>?from=&to= answering {"events": [{"id", "title", "start", "end", "kind", "about",
    "times"}]}. A game with no tools set up on this panel has no calendar; one that does not
    answer raises, so the Calendar names it."""
    def cal(m, lo, hi):
        import admingames
        if m.key not in admingames.configured():
            return []
        status, doc = admingames.get_json(m.key, path, "from=%d&to=%d" % (int(lo), int(hi)), "panel")
        if status != 200:
            raise OSError(doc.get("error") or doc.get("msg") or "HTTP %d" % status)
        out = []
        for e in doc.get("events") or []:
            if not isinstance(e, dict):
                continue
            try:
                start, end = int(e["start"]), int(e["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if end <= lo or start >= hi:
                continue
            times = []
            for t in (e.get("times") if isinstance(e.get("times"), list) else [])[:200]:
                try:
                    times.append([int(t[0]), int(t[1])])
                except (TypeError, ValueError, IndexError, KeyError):
                    pass
            out.append({"id": _text(e.get("id"), 80) or "%s-%d" % (m.key, start),
                        "title": _text(e.get("title"), 80) or "Event", "start": start,
                        "end": max(end, start), "kind": _text(e.get("kind"), 20) or "event",
                        "about": _text(e.get("about"), 300), "times": times})
        return out
    return cal


# -- update reports ------------------------------------------------------------ #
# A module's update is fn(module) -> the "update" object (docs/admin-modules.md), asked with every
# live read. A report that has to ask GitHub is too slow for that, so background() runs the real
# check in a thread and answers from what it found last.

_GH = {"client": None, "api": None}
_gh_lock = threading.Lock()


def github():
    """The GitHub client update checks share (deployer/updatecheck.py's, without a token, every
    answer kept an hour), at POL_ADMIN_GITHUB_API (tests point it at a stand-in)."""
    try:
        import updatecheck
    except ImportError:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sys.path.append(os.path.join(here, "deployer"))
        import updatecheck
    api = os.environ.get("POL_ADMIN_GITHUB_API", "").strip().rstrip("/") or updatecheck.API
    with _gh_lock:
        if _GH["client"] is None or _GH["api"] != api:
            _GH["client"], _GH["api"] = updatecheck.GitHub(api=api), api
        return _GH["client"]


def background(check, ttl=3600.0, retry=300.0, pending=None):
    """An `update` for MODULE that runs check(module) -> report in a thread at most every `ttl`
    seconds and answers at once with the last report; `pending(module)` (or a plain "unknown")
    until the first one is in. A check that raised, or whose report says "retry": true (it could
    not ask, as opposed to finding the answer unknown), runs again after `retry` seconds."""
    state = {"at": 0.0, "report": None, "running": False, "retry": False}
    lock = threading.Lock()

    def run(m):
        try:
            got = check(m)
            again = isinstance(got, dict) and got.pop("retry", None) is True
        except Exception as exc:                       # noqa: BLE001 -- the report says so
            got = {"update": "unknown", "why": "The update check for %s failed (%s)."
                   % (m.title, exc.__class__.__name__)}
            again = True
        with lock:
            state.update(at=time.time(), report=got, running=False, retry=again)

    def update(m):
        now = time.time()
        with lock:
            rep = state["report"]
            gap = retry if state["retry"] else ttl
            if not state["running"] and (rep is None or now - state["at"] >= gap):
                state["running"] = True
                threading.Thread(target=run, args=(m,), daemon=True,
                                 name="update-" + m.key).start()
        if rep is not None:
            return rep
        return pending(m) if pending else {"update": "unknown",
                                           "why": "The update check for %s is running." % m.title}
    update.state = state
    return update
