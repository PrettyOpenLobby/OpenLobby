"""The Inbox's Game help: players' help requests from inside each game, answered from the panel.

A game module may have a help desk (adminmodules.HelpDesk: a module file's own functions, or a
game's /openlobby/help endpoints through adminmodules.remote). Every desk answers in one shape:

    thread list   [{"id", "subject", "who", "at", "closed", "waiting", "count", "category"}]
                  "waiting": the player wrote last; "at": unix seconds of the first message;
                  optional "player": the login's id (what players.json calls "id"), and
                  "last": unix seconds of the newest message (the Inbox sorts by it)
    one thread    {"id", "subject", "closed", "who": {"name", "online"},
                   "messages": [{"from": "player"|"staff", "subject", "text", "at"}]}
    reply/close   (ok, message)

Seeing and answering a game's requests needs its help permission (Module.perm("help")). Every
call here answers (status, body) and never raises: a desk that does not answer is named in
"problems" and the other games still show.
"""
import concurrent.futures

import adminmodules

DESK_TIMEOUT = 6.0
TEXT_MAX = 4000


def _desks(can, owner):
    return [m for m in adminmodules.enabled().values()
            if getattr(m, "help", None) is not None and (owner or can(m.perm("help")))]


def _str(v, n=200):
    return " ".join(str(v if v is not None else "").split())[:n]


def _row(m, t):
    return {"game": m.key, "game_title": m.title, "id": _str(t.get("id"), 40),
            "subject": _str(t.get("subject"), 120) or "(no subject)", "who": _str(t.get("who"), 60),
            "at": t.get("at"), "closed": bool(t.get("closed")), "waiting": bool(t.get("waiting")),
            "count": t.get("count"), "category": _str(t.get("category"), 60),
            "player": _str(t.get("player"), 128)}


def listing(can, owner, open_only=True):
    desks = _desks(can, owner)
    rows, problems = [], []
    if desks:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(desks))) as pool:
            futs = {pool.submit(m.help.list, m, open_only): m for m in desks}
            done, pending = concurrent.futures.wait(futs, timeout=DESK_TIMEOUT)
            for f in done:
                m = futs[f]
                try:
                    rows += [_row(m, t) for t in (f.result() or []) if isinstance(t, dict)]
                except Exception:          # noqa: BLE001 -- one game down is a note
                    problems.append("%s: its help desk is not answering right now" % m.title)
            for f in pending:
                problems.append("%s: its help desk took too long" % futs[f].title)
    # the ones waiting for us first, then newest
    rows.sort(key=lambda r: (not r["waiting"] or r["closed"], -(r["at"] or 0)))
    return 200, {"threads": rows, "problems": sorted(problems),
                 "games": [{"key": m.key, "title": m.title} for m in desks]}


def _desk(game, can, owner):
    m = adminmodules.enabled().get(game) if isinstance(game, str) else None
    if m is None or getattr(m, "help", None) is None:
        return None, (404, {"error": "That game has no help desk here."})
    if not owner and not can(m.perm("help")):
        return None, (403, {"error": "You don't have permission to do that.", "forbidden": True})
    return m, None


def thread(game, tid, can, owner):
    m, err = _desk(game, can, owner)
    if err:
        return err
    try:
        t = m.help.thread(m, str(tid or ""))
    except LookupError as exc:
        return 404, {"error": str(exc) or "That request is gone."}
    except Exception as exc:                      # noqa: BLE001
        return 502, {"error": "%s's help desk did not answer (%s)." % (m.title, exc.__class__.__name__)}
    if not isinstance(t, dict):
        return 404, {"error": "That request is gone."}
    who = t.get("who") if isinstance(t.get("who"), dict) else {"name": t.get("who")}
    return 200, {"game": m.key, "game_title": m.title, "id": _str(t.get("id"), 40),
                 "subject": _str(t.get("subject"), 120), "closed": bool(t.get("closed")),
                 "who": {"name": _str(who.get("name"), 60), "online": who.get("online")},
                 "messages": [{"from": "staff" if x.get("from") == "staff" else "player",
                               "subject": _str(x.get("subject"), 120), "text": str(x.get("text") or "")[:TEXT_MAX],
                               "at": x.get("at")}
                              for x in (t.get("messages") or []) if isinstance(x, dict)]}


def act(body, actor, can, owner):
    """{"game", "id", "action": "reply"|"close", "text"?, "close"?} -> (status, body)."""
    m, err = _desk(body.get("game"), can, owner)
    if err:
        return err
    tid, action = str(body.get("id") or ""), body.get("action")
    if not tid:
        return 400, {"error": "Which request?"}
    try:
        if action == "reply":
            text = str(body.get("text") or "").replace("\r\n", "\n").strip()
            if not text:
                return 400, {"error": "Write the answer first."}
            if len(text) > TEXT_MAX:
                return 400, {"error": "That answer is too long."}
            ok, msg = m.help.reply(m, tid, text, bool(body.get("close")), actor)
        elif action == "close":
            ok, msg = m.help.close(m, tid, actor)
        else:
            return 400, {"error": "Unknown action."}
    except Exception as exc:                      # noqa: BLE001
        return 502, {"error": "%s's help desk did not answer (%s)." % (m.title, exc.__class__.__name__)}
    return (200 if ok else 409), ({"ok": True, "msg": str(msg)} if ok else {"error": str(msg)})
