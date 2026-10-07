"""The admin panel's Games section: each title's own debugging tools, behind
the panel's sign-in.

The tools themselves live with their game and run inside its server process,
because they read and change that process's live state (Fantasy Earth's world
builder, Front Mission Online's lobby NPC editor and story gates, the FFXI
bridge's federation worlds and providers). Each serves
a small token-gated HTTP port of its own. The panel proxies it under
/games/<key>/, so:

  - a moderator reaches a tool with their panel login; the tool's token stays
    on the server and never reaches a browser,
  - which moderators may use which game's tools is a panel permission
    ("games_<key>", see adminusers.PERMS), checked on every request,
  - every change made through a tool is in the panel's activity log.

Configuration, one pair per game:

    POL_ADMIN_GAME_<KEY>_URL     where the game's tool port answers, e.g.
                                 http://host.docker.internal:8798
    POL_ADMIN_GAME_<KEY>_TOKEN   that port's token (the game's *_DEVTOOL_TOKEN)

A game with no URL is left out of the section. A tool port that binds
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

#: The titles the section knows how to label. `tools` are the pages each
#: game's port serves; `path` is relative to the port's root.
GAMES = {
    "fe": {"title": "Fantasy Earth", "short": "FE",
           "tools": [{"id": "world", "label": "World builder", "path": ""}]},
    "fmo": {"title": "Front Mission Online", "short": "FMO",
            "tools": [{"id": "npcs", "label": "Lobby NPCs", "path": ""},
                      {"id": "gates", "label": "Story gates", "path": "gates"}]},
    "ffxi": {"title": "Final Fantasy XI", "short": "FFXI",
             "tools": [{"id": "federation", "label": "Federation", "path": ""}]},
}

TIMEOUT = float(os.environ.get("POL_ADMIN_GAME_TIMEOUT", "20"))
MAX_BODY = 1 << 20
_KEY = re.compile(r"^[a-z0-9]{1,16}$")


def perm_for(key):
    return "games_" + key


def configured():
    """{key: (url, token)} for every game with a tool URL set."""
    out = {}
    for key in GAMES:
        url = os.environ.get("POL_ADMIN_GAME_%s_URL" % key.upper(), "").strip()
        if url:
            out[key] = (url.rstrip("/"),
                        os.environ.get("POL_ADMIN_GAME_%s_TOKEN" % key.upper(), "").strip())
    return out


def listing(can):
    """The games this caller may open, for the section's menu. `can(perm)`."""
    have = configured()
    return [{"key": k, "title": g["title"], "short": g["short"], "tools": g["tools"]}
            for k, g in GAMES.items() if k in have and can(perm_for(k))]


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
        return _error(404, "This game has no tools set up.", sub)
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
                          % GAMES[key]["title"], sub)
    except (urllib.error.URLError, OSError, ValueError):
        return _error(502, "The %s tools are not reachable right now."
                      % GAMES[key]["title"], sub)
    out = [("Content-Type", rtype or "application/octet-stream"),
           ("Cache-Control", rcache or "no-store")]
    if rtype.startswith("text/html"):
        nonce = secrets.token_urlsafe(16)
        data = re.sub(rb"<script(?=[\s>])", ('<script nonce="%s"' % nonce).encode(), data)
        out.append(("Content-Security-Policy", _csp(nonce)))
    return status, out, data


def _is_page(sub):
    return any(sub == t["path"] for g in GAMES.values() for t in g["tools"])


def _error(status, msg, sub):
    if _is_page(sub):
        page =("<!doctype html><meta charset=utf-8><title>Unavailable</title>"
                "<style>body{margin:0;display:grid;place-items:center;min-height:60vh;"
                "font:15px/1.5 system-ui,sans-serif;background:#14161c;color:#9aa2b4}"
                "@media (prefers-color-scheme:light){body{background:#f4f5f8;color:#4b5563}}"
                "</style><p>%s</p>" % msg.replace("<", "&lt;"))
        return status, [("Content-Type", "text/html; charset=utf-8"),
                        ("Cache-Control", "no-store")], page.encode()
    return status, [("Content-Type", "application/json; charset=utf-8"),
                    ("Cache-Control", "no-store")], json.dumps({"error": msg}).encode()
