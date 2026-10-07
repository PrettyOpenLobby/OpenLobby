#!/usr/bin/env python3
"""The Games section of the admin panel, over HTTP.

    python tools/admin_games_test.py

A stand-in game tool port (token-gated, like the real ones) runs beside the
panel. Checks that a moderator holding a game's permission reaches that
game's tools and no other's, that the tool's token is added by the panel and
never handed to the browser, that a tool page gets a script policy with a
nonce on its own script, that a change made through a tool is in the activity
log, and that an unreachable or misconfigured tool is reported as such.

Uses a throwaway PostgreSQL database (tools/pgtest.py) and a temporary data
directory; the panel runs as a subprocess on a loopback port.
"""
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.abspath(os.path.join(HERE, os.pardir, "services"))
sys.path.insert(0, HERE)
sys.path.insert(0, SERVICES)

import pgtest  # noqa: E402

pgtest.use_fresh_database()

import accounts  # noqa: E402
import adminusers  # noqa: E402
from admin_http import Client, free_port, start_panel, stop  # noqa: E402

OWNER, OWNER_PW = "op", "correct horse 1"
TOKEN = "tool-token-0123456789"
PAGE = b"<!doctype html><title>tool</title><div id=x></div><script>var a = 1;</script>"
bad = 0
SEEN = []


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


class Tool(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, body=b""):
        SEEN.append({"path": self.path, "token": self.headers.get("X-Devtool-Token"),
                     "actor": self.headers.get("X-Devtool-Actor"), "body": body})
        if self.headers.get("X-Devtool-Token") != TOKEN:
            return self._reply(403, b'{"error": "bad or missing token"}', "application/json")
        if self.path.split("?")[0] in ("/", "/gates"):
            return self._reply(200, PAGE, "text/html; charset=utf-8")
        return self._reply(200, json.dumps({"path": self.path}).encode(), "application/json")

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle(self.rfile.read(int(self.headers.get("Content-Length") or 0)))


def raw(c, path, body=None):
    """(status, headers, bytes, final url) with the client's cookies."""
    req = urllib.request.Request(c.url + path.lstrip("/"),
                                 data=None if body is None else json.dumps(body).encode(),
                                 method="GET" if body is None else "POST",
                                 headers={"Content-Type": "application/json"})
    try:
        r = c.op.open(req, timeout=15)
        return r.status, r.headers, r.read(), r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read(), path


def main():
    tool = ThreadingHTTPServer(("127.0.0.1", free_port()), Tool)
    threading.Thread(target=tool.serve_forever, daemon=True).start()
    tool_url = "http://127.0.0.1:%d" % tool.server_address[1]
    tmp = tempfile.mkdtemp(prefix="admingames-")
    db = accounts.connect()
    accounts.set_admin_cred(db, OWNER, OWNER_PW)
    db.commit()
    for name, perms in (("gm_fmo", ["games_fmo"]), ("gm_fe", ["games_fe"]),
                        ("gm_none", ["gm"])):
        adminusers.create_mod(db, name, "pass for " + name, perms, by=OWNER)
    db.close()
    env = {"POL_ADMIN_GAME_FMO_URL": tool_url, "POL_ADMIN_GAME_FMO_TOKEN": TOKEN,
           # FE: configured, but with the wrong token
           "POL_ADMIN_GAME_FE_URL": tool_url, "POL_ADMIN_GAME_FE_TOKEN": "wrong"}
    proc, url = start_panel(tmp, SERVICES, free_port(), env_extra=env)
    try:
        c = {}
        for name in ("gm_fmo", "gm_fe", "gm_none"):
            c[name] = Client(url)
            chk("%s signs in" % name, c[name].login(name, "pass for " + name), 200)
        owner = Client(url)
        chk("owner signs in", owner.login(OWNER, OWNER_PW), 200)

        _code, g = c["gm_fmo"].call("api/games")
        chk("the FMO moderator is offered FMO only", [x["key"] for x in g["games"]], ["fmo"])
        chk("...with both of its tools",
            [t["id"] for t in g["games"][0]["tools"]], ["npcs", "gates"])
        chk("a moderator with no game permission is offered none",
            c["gm_none"].call("api/games")[1]["games"], [])
        chk("the owner is offered every configured game",
            sorted(x["key"] for x in owner.call("api/games")[1]["games"]), ["fe", "fmo"])

        SEEN.clear()
        st, hd, body, _u = raw(c["gm_fmo"], "games/fmo/gates?t=leak&x=1")
        chk("the FMO moderator opens the story gates page", st, 200)
        chk("the tool got the panel's token", SEEN[-1]["token"], TOKEN)
        chk("...and the moderator's name", SEEN[-1]["actor"], "gm_fmo")
        chk("a token in the browser's query is not passed on", SEEN[-1]["path"], "/gates?x=1")
        chk("the page never carries the token", TOKEN.encode() in body, False)
        csp = hd.get("Content-Security-Policy") or ""
        nonce = csp.split("'nonce-")[1].split("'")[0] if "'nonce-" in csp else None
        chk("the page gets a script policy with a nonce", bool(nonce), True)
        chk("...and its own script carries that nonce",
            ('<script nonce="%s">' % nonce).encode() in body, True)
        st, hd, body, final = raw(c["gm_fmo"], "games/fmo")
        chk("/games/fmo lands on the folder, so relative paths resolve",
            (st, final.endswith("/games/fmo/")), (200, True))

        chk("the FE moderator cannot open FMO's tools", raw(c["gm_fe"], "games/fmo/state")[0], 403)
        chk("nor can a moderator without a game permission",
            raw(c["gm_none"], "games/fmo/")[0], 403)
        n = len(SEEN)
        raw(c["gm_none"], "games/fmo/gates/edit", {"op": "byte"})
        chk("a refused request never reaches the tool", len(SEEN), n)

        st, _h, body, _u = raw(c["gm_fmo"], "games/fmo/gates/edit",
                               {"pilot": "a|1", "op": "byte", "index": 173, "value": 99})
        chk("an edit is passed through", (st, SEEN[-1]["path"]), (200, "/gates/edit"))
        chk("...with its body", json.loads(SEEN[-1]["body"])["index"], 173)
        _code, audit = owner.call("api/audit")
        rows = audit.get("rows") or audit.get("entries") or audit.get("audit") or []
        hit = [r for r in rows if "Front Mission Online" in json.dumps(r)]
        chk("the edit is in the activity log", len(hit) >= 1, True)

        st, _h, body, _u = raw(c["gm_fe"], "games/fe/")
        chk("a tool that refuses the panel's token reads as a panel fault",
            (st, b"refused" in body), (502, True))
        chk("a game the panel does not know is not found", raw(owner, "games/nope/")[0], 404)
    finally:
        stop(proc)
        tool.shutdown()
    # an unreachable tool: the same panel, pointed at a closed port
    env["POL_ADMIN_GAME_FMO_URL"] = "http://127.0.0.1:%d" % free_port()
    proc, url = start_panel(tmp, SERVICES, free_port(), env_extra=env)
    try:
        owner = Client(url)
        owner.login(OWNER, OWNER_PW)
        st, _h, body, _u = raw(owner, "games/fmo/")
        chk("an unreachable tool says so, as a page", (st, b"not reachable" in body), (502, True))
    finally:
        stop(proc)
    print("all checks passed" if not bad else "%d check(s) FAILED" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
