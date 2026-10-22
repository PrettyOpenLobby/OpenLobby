"""The OpenLobby admin protocol: a game server that describes itself.

A game server can plug into an OpenLobby admin panel with no module file:
the deployment sets POL_ADMIN_GAME_<KEY>_URL (and _TOKEN) and the panel asks
that address what the game is. Every request carries the same headers as the
tool proxy (X-Devtool-Token, X-Devtool-Actor).

    GET  <url>/openlobby/module.json     what the game is (version 1):
        {"v": 1, "key": "<key>", "title": "...", "short": "...", "platform": "...",
         "about": "...", "content_ids": [..],
         "tools":    [{"id", "label", "path", "about"}],      pages under <url>/<path>
         "actions":  [{"id", "label", "kind", "about", "danger",
                       "fields": [{"id", "label", "type"}]}],
         "services": [{"name", "containers": [..], "ports": {"<port>": "what"},
                       "warning"}]}
    GET  <url>/openlobby/status.json     who is playing now, in the panel's
                                         shape (adminmodules.sources, top)
    POST <url>/openlobby/actions/<id>    JSON of the action's fields
                                         -> {"ok": true|false, "msg": "..."}

`key` must equal the <KEY> the panel was given. An action of kind "announce"
is used by the maintenance warning, "kick" by an account's Kick. Ids follow
the module rules (adminmodules.ID_RE); anything that does not fit is dropped
and reported on the panel's Modules page.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

from adminmodules import Action, HelpDesk, Module, Service, Tool, sources

PREFIX = "openlobby/"
TIMEOUT = 4.0


def _get(url, token, path):
    req = urllib.request.Request(url + "/" + path, headers={
        "Accept": "application/json", "X-Devtool-Token": token or "",
        "X-Devtool-Actor": "panel"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = r.read(sources.MAX_BYTES + 1)
    if len(data) > sources.MAX_BYTES:
        raise ValueError("too large")
    return json.loads(data)


def _str(v, limit=80):
    return " ".join(str(v or "").split())[:limit]


def _action(key, a):
    aid = _str(a.get("id"), 32)

    def run(m, params, actor):
        import admingames
        return admingames.call(key, PREFIX + "actions/" + aid, params, actor)
    fields = [{"id": _str(f.get("id"), 32), "label": _str(f.get("label")),
               "type": _str(f.get("type") or "text", 16)}
              for f in (a.get("fields") or []) if isinstance(f, dict) and f.get("id")]
    return Action(aid, _str(a.get("label")) or aid, run, kind=_str(a.get("kind"), 32),
                  fields=fields, about=_str(a.get("about"), 200), danger=bool(a.get("danger")))


def _live(m):
    url, token = m.tool_port()
    doc = _get(url, token, PREFIX + "status.json")
    st = sources.generic(doc)
    # a protocol status may already carry the panel's own fields
    if isinstance(doc, dict):
        if isinstance(doc.get("summary"), str):
            st["summary"] = _str(doc["summary"], 200)
        if isinstance(doc.get("rooms_label"), str):
            st["rooms_label"] = _str(doc["rooms_label"], 30)
        st["facts"] += [{"label": _str(f.get("label"), 40), "value": f.get("value")}
                        for f in (doc.get("facts") or []) if isinstance(f, dict)][:12]
    st["source"] = "the game's OpenLobby status"
    return st


def _calendar(m, lo, hi):
    url, token = m.tool_port()
    doc = _get(url, token, PREFIX + "calendar.json?from=%d&to=%d" % (int(lo), int(hi)))
    out = []
    for e in (doc.get("events") if isinstance(doc, dict) else None) or []:
        if not isinstance(e, dict):
            continue
        try:
            start, end = int(e["start"]), int(e["end"])
        except (KeyError, TypeError, ValueError):
            continue
        times = []
        for t in (e.get("times") or [])[:200]:
            try:
                times.append([int(t[0]), int(t[1])])
            except (TypeError, ValueError, IndexError):
                pass
        out.append({"id": _str(e.get("id"), 80) or "%s-%d" % (m.key, start),
                    "title": _str(e.get("title"), 80) or "Event", "start": start, "end": max(end, start),
                    "kind": _str(e.get("kind"), 20), "about": _str(e.get("about"), 300), "times": times})
    return out


def _post(m, path, body, actor):
    url, token = m.tool_port()
    req = urllib.request.Request(url + "/" + PREFIX + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-Devtool-Token": token or "",
                                          "X-Devtool-Actor": str(actor or "")[:64]})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            doc = json.loads(r.read(sources.MAX_BYTES))
    except urllib.error.HTTPError as e:
        if e.code == 403:
            return False, "The game refused the panel's token."
        try:
            doc = json.loads(e.read(4096))
        except ValueError:
            doc = {}
        return False, _str((doc or {}).get("msg") or (doc or {}).get("error"), 300) or "The game answered HTTP %d." % e.code
    return bool(doc.get("ok")), _str(doc.get("msg"), 300)


def _help_list(m, open_only):
    url, token = m.tool_port()
    doc = _get(url, token, PREFIX + "help.json" + ("?open=1" if open_only else ""))
    return [t for t in (doc.get("threads") if isinstance(doc, dict) else None) or [] if isinstance(t, dict)]


def _help_thread(m, tid):
    url, token = m.tool_port()
    try:
        return _get(url, token, PREFIX + "help/thread.json?id=" + urllib.parse.quote(str(tid)))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise LookupError("That request is gone.")
        raise


HELP = HelpDesk(_help_list, _help_thread,
                lambda m, tid, text, close, actor: _post(m, "help/reply", {"id": tid, "text": text, "close": close}, actor),
                lambda m, tid, actor: _post(m, "help/close", {"id": tid}, actor))


def _player(m, who):
    url, token = m.tool_port()
    q = urllib.parse.urlencode({"polid": who.get("polid") or "", "handles": ",".join(who.get("handles") or []),
                                "members": ",".join(str(x) for x in who.get("members") or [])})
    doc = _get(url, token, PREFIX + "player.json?" + q)
    return doc if isinstance(doc, dict) else None


def build(key, url, token):
    """A Module for the game at `url`. Raises OSError/ValueError when the game
    does not answer or its description does not fit."""
    doc = _get(url, token, PREFIX + "module.json")
    if not isinstance(doc, dict) or doc.get("v") != 1:
        raise ValueError("not an OpenLobby module description (v 1)")
    if doc.get("key") != key:
        raise ValueError("it calls itself %r, the panel calls it %r" % (doc.get("key"), key))
    title = _str(doc.get("title"), 60)
    if not title:
        raise ValueError("it has no title")
    tools = [Tool(_str(t.get("id"), 32), _str(t.get("label")) or _str(t.get("id")),
                  str(t.get("path") or ""), about=_str(t.get("about"), 200))
             for t in (doc.get("tools") or []) if isinstance(t, dict) and t.get("id")
             and not str(t.get("path") or "").startswith(PREFIX.rstrip("/"))]
    actions = [_action(key, a) for a in (doc.get("actions") or [])
               if isinstance(a, dict) and a.get("id")]
    services = []
    for s in (doc.get("services") or []):
        if not isinstance(s, dict):
            continue
        ports = {}
        for p, what in (s.get("ports") or {}).items():
            try:
                ports[int(p)] = _str(what, 40)
            except (TypeError, ValueError):
                pass
        ctrs = [_str(c, 128) for c in (s.get("containers") or []) if c]
        if ctrs:
            services.append(Service(ctrs, ports, name=_str(s.get("name")),
                                    warning=_str(s.get("warning"), 200)))
    ids = []
    for c in (doc.get("content_ids") or []):
        try:
            ids.append(int(c))
        except (TypeError, ValueError):
            pass
    m = Module(key, title, short=_str(doc.get("short"), 16), platform=_str(doc.get("platform"), 40),
               about=_str(doc.get("about"), 200), tools=tools, actions=actions,
               services=services or None, live=_live, content_ids=ids)
    if doc.get("calendar"):
        m.calendar = _calendar
    if doc.get("help"):
        m.help = HELP
    if doc.get("player"):
        m.player = _player
    m.remote = True
    return m
