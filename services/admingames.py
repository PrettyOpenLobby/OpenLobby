"""The admin panel's proxy to each game's own tool port.

The tools live with their game and run inside its server process, because
they read and change that process's live state (Fantasy Earth's world
builder, Front Mission Online's lobby NPC editor and story gates, the FFXI
bridge's federation worlds and its in-game notices). Each serves a small
token-gated HTTP port of its own. The panel proxies it under /games/<key>/, so:

  - a moderator reaches a tool with their panel login; the tool's token stays
    on the server and never reaches a browser,
  - which moderators may use which tool is a panel permission, checked on
    every request: each path belongs to one of the game's tools
    (adminmodules.Module.tool_for), and that tool's "game.<key>.<id>",
  - every change made through a tool is in the panel's activity log.

Which games have tools, and what they are called, is the game modules'
business (adminmodules/). Where a game's port answers:

    POL_ADMIN_GAME_<KEY>_URL     e.g. http://host.docker.internal:8798
    POL_ADMIN_GAME_<KEY>_TOKEN   that port's token (the game's *_DEVTOOL_TOKEN)

A game with no URL has no tools on this panel. A tool port that binds
loopback cannot be reached from the admin container; bind it where the panel
can reach it and give it a token (the tool refuses a wide bind without one).
"""
import json
import os
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request

import adminmodules

TIMEOUT = float(os.environ.get("POL_ADMIN_GAME_TIMEOUT", "20"))
MAX_BODY = 1 << 20
_KEY = re.compile(r"^[a-z0-9]{1,16}$")


def configured():
    """{key: (url, token)} for every module whose tool port is set."""
    out = {}
    for key, m in adminmodules.load().items():
        port = m.tool_port()
        if port and (m.tools or m.remote):
            out[key] = port
    return out


def title(key):
    m = adminmodules.load().get(key)
    return m.title if m else key


def listing(can):
    """The games with tools this caller may open, for the Games menu:
    each with the tools `can(perm)` allows."""
    have = configured()
    out = []
    for key, m in adminmodules.enabled().items():
        if key not in have:
            continue
        tools = [{"id": t.id, "label": t.label, "path": t.path}
                 for t in m.tools if not t.panel and can(m.perm(t.id))]
        if tools:
            out.append({"key": key, "title": m.title, "short": m.short, "tools": tools})
    return out


def split(path):
    """'/games/fmo/gates?x' -> ('fmo', 'gates'); None when not a game path."""
    if not path.startswith("/games/"):
        return None
    rest = path[len("/games/"):]
    key, _, sub = rest.partition("/")
    if not _KEY.match(key):
        return None
    return key, sub


def _csp(nonce):
    # A tool page draws server data (names, notes) with innerHTML. It is
    # served from the panel's own origin, so the policy only lets the page's
    # own script run: injected markup cannot carry script with it.
    return ("default-src 'self'; script-src 'nonce-%s'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; connect-src 'self'; base-uri 'none'; "
            "form-action 'self'; frame-ancestors 'self'" % nonce)


def forward(key, sub, method, query, body, ctype, actor):
    """Proxy one request to a game's tool port.

    -> (status, headers list, body bytes). Never raises."""
    have = configured()
    if key not in have:
        return _error(404, "This game has no tools set up.", sub, key)
    base, token = have[key]
    q = [(k, v) for k, v in urllib.parse.parse_qsl(query or "", keep_blank_values=True)
         if k != "t"]
    url = base + "/" + urllib.parse.quote(sub, safe="/._-") + (
        "?" + urllib.parse.urlencode(q) if q else "")
    headers = {"X-Devtool-Actor": actor or ""}
    if token:
        headers["X-Devtool-Token"] = token
    if ctype:
        headers["Content-Type"] = ctype
    req = urllib.request.Request(url, data=body if method == "POST" else None,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            status, rtype, rcache, data = (r.status, r.headers.get("Content-Type", ""),
                                           r.headers.get("Cache-Control", ""), r.read(MAX_BODY * 8))
    except urllib.error.HTTPError as e:
        status, rtype, rcache = e.code, e.headers.get("Content-Type", ""), ""
        data = e.read(MAX_BODY)
        if status == 403:
            # the tool refused our token: a configuration fault, not the caller's
            return _error(502, "The %s tools refused the panel's token."
                          % title(key), sub, key)
    except (urllib.error.URLError, OSError, ValueError):
        return _error(502, "The %s tools are not reachable right now."
                      % title(key), sub, key)
    out = [("Content-Type", rtype or "application/octet-stream"),
           ("Cache-Control", rcache or "no-store")]
    if rtype.startswith("text/html"):
        nonce = secrets.token_urlsafe(16)
        data = re.sub(rb"<script(?=[\s>])", ('<script nonce="%s"' % nonce).encode(), data)
        out.append(("Content-Security-Policy", _csp(nonce)))
    return status, out, data


def get_json(key, sub, query, actor):
    """GET a game tool's JSON on the panel's own behalf -> (status, dict). Never raises:
    a game that is not set up or not answering is a status of 404 or 502 and {"error"}."""
    if key not in configured():
        return 404, {"error": "%s has no tools set up on this panel." % title(key)}
    status, _headers, data = forward(key, sub, "GET", query, b"", None, actor)
    try:
        got = json.loads(data or b"{}")
    except ValueError:
        got = {}
    return status, got if isinstance(got, dict) else {}


def call(key, sub, payload, actor):
    """POST a JSON action to a game's tool port on the panel's own behalf (the
    maintenance warning queues an FFXI announcement this way).

    -> (ok, message). Never raises; a game with no tools set up is (False, why)."""
    if key not in configured():
        return False, "%s has no tools set up on this panel." % title(key)
    status, _headers, data = forward(key, sub, "POST", "", json.dumps(payload).encode(),
                                     "application/json", actor)
    try:
        got = json.loads(data or b"{}")
    except ValueError:
        got = {}
    if not isinstance(got, dict):
        got = {}
    msg = got.get("msg") or got.get("error") or "HTTP %d" % status
    return bool(status == 200 and got.get("ok")), msg


def _is_page(key, sub):
    m = adminmodules.load().get(key)
    return bool(m) and any(sub == t.path for t in m.tools if not t.panel)


def _error(status, msg, sub, key=""):
    if _is_page(key, sub):
        page =("<!doctype html><meta charset=utf-8><title>Unavailable</title>"
                "<style>body{margin:0;display:grid;place-items:center;min-height:60vh;"
                "font:15px/1.5 system-ui,sans-serif;background:#14161c;color:#9aa2b4}"
                "@media (prefers-color-scheme:light){body{background:#f4f5f8;color:#4b5563}}"
                "</style><p>%s</p>" % msg.replace("<", "&lt;"))
        return status, [("Content-Type", "text/html; charset=utf-8"),
                        ("Cache-Control", "no-store")], page.encode()
    return status, [("Content-Type", "application/json; charset=utf-8"),
                    ("Cache-Control", "no-store")], json.dumps({"error": msg}).encode()
