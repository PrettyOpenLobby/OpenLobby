"""The admin panel's Calendar: everything the games have scheduled, and the panel's own entries.

Each game module may have a calendar (adminmodules: Module.calendar, a game's own
/openlobby/calendar.json, or sources.eventcal / sources.kv_calendar for the games the panel
already reads). The panel's own entries (a match day, maintenance, a community night) are kept
in the panel's settings as "calendar_entries" and edited here.

Every call answers (status, body) and never raises: a game that does not answer is listed in
"problems" and the rest of the calendar still shows.
"""
import concurrent.futures
import json
import re
import secrets
import time

import adminmodules
import adminusers

SETTING = "calendar_entries"
MAX_SPAN = 100 * 86400          # one request: a month view with its edge weeks, or a long list
MAX_ENTRIES = 500
GAME_TIMEOUT = 5.0
_ID = re.compile(r"^[a-z0-9]{6,32}$")
KINDS = ("event", "maintenance", "match", "community", "reminder")


def _span(lo, hi):
    try:
        lo, hi = int(lo), int(hi)
    except (TypeError, ValueError):
        return None
    if hi <= lo or hi - lo > MAX_SPAN:
        return None
    return lo, hi


def _own(conn):
    try:
        rows = json.loads(adminusers.get_setting(conn, SETTING) or "[]")
    except ValueError:
        rows = []
    return [r for r in rows if isinstance(r, dict) and r.get("id")] if isinstance(rows, list) else []


def _game_entries(mod, lo, hi):
    out = []
    for e in mod.calendar(mod, lo, hi) or []:
        if not isinstance(e, dict) or e.get("end", 0) <= lo or e.get("start", 0) >= hi:
            continue
        out.append({"source": "game", "game": mod.key, "id": "%s:%s" % (mod.key, e.get("id")),
                    "title": e.get("title") or "Event", "start": int(e["start"]), "end": int(e["end"]),
                    "kind": e.get("kind") or "event", "about": e.get("about") or "",
                    "times": e.get("times") or [], "all_day": False, "editable": False})
    return out


def gather(conn, lo, hi, can, owner):
    """{"entries", "games", "problems"} for [lo, hi). `can(perm)` says what the caller may see:
    a game's entries need its game.<key> permission, the panel's own need "calendar"."""
    span = _span(lo, hi)
    if span is None:
        return 400, {"error": "Pick a range of up to %d days." % (MAX_SPAN // 86400)}
    lo, hi = span
    mods = [m for m in adminmodules.enabled().values()
            if getattr(m, "calendar", None) and (owner or can("game.%s" % m.key))]
    entries, problems = [], []
    if mods:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(mods))) as pool:
            futs = {pool.submit(_game_entries, m, lo, hi): m for m in mods}
            done, pending = concurrent.futures.wait(futs, timeout=GAME_TIMEOUT)
            for f in done:
                m = futs[f]
                try:
                    entries += f.result()
                except Exception:          # noqa: BLE001 -- a game down is a note, not an error
                    problems.append("%s: its calendar is not answering right now" % m.title)
            for f in pending:
                problems.append("%s: its calendar took too long" % futs[f].title)
    for r in _own(conn):
        if r.get("end", 0) > lo and r.get("start", 0) < hi:
            entries.append(dict(r, source="panel", id="panel:" + r["id"], editable=True))
    entries.sort(key=lambda e: (e["start"], e["end"], e["title"]))
    games = [{"key": m.key, "title": m.title, "short": m.short}
             for m in adminmodules.enabled().values() if owner or can("game.%s" % m.key)]
    return 200, {"entries": entries, "games": games, "problems": sorted(problems), "from": lo, "to": hi}


def _clean(body, games):
    title = " ".join(str(body.get("title") or "").split())[:80]
    if not title:
        raise ValueError("Give it a title.")
    try:
        start, end = int(body.get("start")), int(body.get("end"))
    except (TypeError, ValueError):
        raise ValueError("Give it a start and an end.")
    if end < start:
        raise ValueError("It ends before it starts.")
    if end - start > 366 * 86400:
        raise ValueError("An entry can last a year at most.")
    game = str(body.get("game") or "")
    if game and game not in games:
        raise ValueError("That game is not on this server.")
    kind = str(body.get("kind") or "event")
    if kind not in KINDS:
        raise ValueError("Unknown kind.")
    return {"title": title, "start": start, "end": end, "game": game, "kind": kind,
            "all_day": bool(body.get("all_day")),
            "about": " ".join(str(body.get("about") or "").split())[:500]}


def change(conn, body, user):
    """{"action": "add"|"edit"|"delete", "id"?, ...fields}. -> (status, body)."""
    act = body.get("action")
    rows = _own(conn)
    games = set(adminmodules.enabled())
    rid = str(body.get("id") or "")
    if rid.startswith("panel:"):
        rid = rid[len("panel:"):]
    try:
        if act == "add":
            if len(rows) >= MAX_ENTRIES:
                return 400, {"error": "The calendar holds %d entries; delete some old ones first." % MAX_ENTRIES}
            row = dict(_clean(body, games), id=secrets.token_hex(6), by=user, at=int(time.time()))
            rows.append(row)
        elif act in ("edit", "delete"):
            if not _ID.match(rid):
                return 400, {"error": "Unknown entry."}
            hit = next((r for r in rows if r["id"] == rid), None)
            if hit is None:
                return 404, {"error": "That entry is gone."}
            if act == "delete":
                rows.remove(hit)
                row = hit
            else:
                row = hit
                row.update(_clean(body, games), edited_by=user, edited_at=int(time.time()))
        else:
            return 400, {"error": "Unknown action."}
    except ValueError as exc:
        return 400, {"error": str(exc)}
    rows.sort(key=lambda r: r["start"])
    adminusers.set_setting(conn, SETTING, json.dumps(rows, separators=(",", ":")))
    return 200, {"ok": True, "id": "panel:" + row["id"], "title": row["title"]}
