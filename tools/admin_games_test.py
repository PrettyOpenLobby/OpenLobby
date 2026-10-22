#!/usr/bin/env python3
"""The Games section of the admin panel, over HTTP.

    python tools/admin_games_test.py

A stand-in game tool port (token-gated, like the real ones) runs beside the
panel. Checks that a moderator holding a game's permission reaches that
game's tools and no other's, that the tool's token is added by the panel and
never handed to the browser, that a tool page gets a script policy with a
nonce on its own script, that a change made through a tool is in the activity
log, and that an unreachable or misconfigured tool is reported as such.

Permissions are per tool (adminmodules): a moderator given one of a game's
tools reaches that tool's page and endpoints and not the others'. The old
per-game grant "games_<key>" still reads as every tool of that game.

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

# A title this deployment adds without changing admingames.py; set before the
# panel modules load, as the admin container's environment would be.
EXTRA = json.dumps({"xg": {"title": "Extra Game", "short": "XG",
                           "tools": [{"id": "home", "label": "Home", "path": ""},
                                     {"id": "gates", "label": "Gates", "path": "gates"}]},
                    "bad key": {"title": "x", "tools": []}})
os.environ["POL_ADMIN_GAMES_EXTRA"] = EXTRA

import accounts  # noqa: E402
import adminusers  # noqa: E402
from admin_http import Client, free_port, start_panel, stop  # noqa: E402

OWNER, OWNER_PW = "op", "correct horse 1"
TOKEN = "tool-token-0123456789"
PAGE = b"<!doctype html><title>tool</title><div id=x></div><script>var a = 1;</script>"
SAVE_PIECE = bytes(range(256)) * 256          # 64 KiB; the save is 32 of them, 2 MB
bad = 0
SEEN = []
# A game that describes itself (adminmodules/remote.py, the OpenLobby admin
# protocol): the panel is given its URL and token and nothing else.
KART = {"v": 1, "key": "kart", "title": "Kart Club", "short": "Kart", "platform": "PS2 HDD",
        "tools": [{"id": "laps", "label": "Lap times", "path": "laps"}],
        "actions": [{"id": "banner", "label": "Banner message", "kind": "announce",
                     "fields": [{"id": "text", "label": "Message"}]},
                    {"id": "kick", "label": "Kick", "kind": "kick", "danger": True}],
        "services": [{"containers": ["kart-game-1"], "ports": {"7000": "game"}}]}


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
        if self.path == "/openlobby/module.json":
            return self._reply(200, json.dumps(KART).encode(), "application/json")
        if self.path == "/openlobby/status.json":
            return self._reply(200, json.dumps(
                {"up": True, "online": 2, "players": [{"name": "Wren", "room": "Lap 3"}, "Ash"],
                 "summary": "Grand prix tonight", "rooms_label": "races",
                 "facts": [{"label": "laps run", "value": 41}]}).encode(), "application/json")
        if self.path.startswith("/openlobby/actions/"):
            ok = self.path.endswith(("/banner", "/kick"))
            return self._reply(200, json.dumps({"ok": ok, "msg": "shown" if ok else "no"}).encode(),
                               "application/json")
        if self.path == "/gates/save.bin":
            # a download bigger than POL_ADMIN_GAME_MAX_REPLY_MB below, with no length
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", 'attachment; filename="Wren.sav"')
            self.end_headers()
            for _ in range(32):
                self.wfile.write(SAVE_PIECE)
            return
        if self.path == "/gates/big.json":
            return self._reply(200, b'{"x": "' + b"a" * (2 << 20) + b'"}', "application/json")
        if self.path.split("?")[0] in ("/", "/gates"):
            return self._reply(200, PAGE, "text/html; charset=utf-8")
        return self._reply(200, json.dumps({"path": self.path}).encode(), "application/json")

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle(self.rfile.read(int(self.headers.get("Content-Length") or 0)))


def raw(c, path, body=None, timeout=15):
    """(status, headers, bytes, final url) with the client's cookies."""
    req = urllib.request.Request(c.url + path.lstrip("/"),
                                 data=None if body is None else json.dumps(body).encode(),
                                 method="GET" if body is None else "POST",
                                 headers={"Content-Type": "application/json"})
    try:
        r = c.op.open(req, timeout=timeout)
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
                        ("gm_xg", ["games_xg"]), ("gm_none", ["gm"]),
                        ("gm_gates", ["game.fmo.gates"]),
                        ("gm_laps", []), ("gm_banner", [])):
        adminusers.create_mod(db, name, "pass for " + name, perms, by=OWNER)
    # this process has not met Kart Club (the panel will ask it), so its
    # grants go straight into the table, as the owner's form would save them
    for name, perms in (("gm_laps", "game.kart.laps"), ("gm_banner", "game.kart.banner")):
        db.execute("UPDATE admin_moderator SET perms = %s WHERE username = %s", (perms, name))
    db.commit()
    db.close()
    env = {"POL_ADMIN_GAME_FMO_URL": tool_url, "POL_ADMIN_GAME_FMO_TOKEN": TOKEN,
           # FE: configured, but with the wrong token
           "POL_ADMIN_GAME_FE_URL": tool_url, "POL_ADMIN_GAME_FE_TOKEN": "wrong",
           # an extra title (POL_ADMIN_GAMES_EXTRA), on the same stand-in tool
           "POL_ADMIN_GAMES_EXTRA": EXTRA,
           "POL_ADMIN_GAME_XG_URL": tool_url, "POL_ADMIN_GAME_XG_TOKEN": TOKEN,
           # a game known only by its URL
           "POL_ADMIN_GAME_KART_URL": tool_url, "POL_ADMIN_GAME_KART_TOKEN": TOKEN,
           # small, so a 2 MB answer shows which kind is passed on and which refused
           "POL_ADMIN_GAME_MAX_REPLY_MB": "1"}
    proc, url = start_panel(tmp, SERVICES, free_port(), env_extra=env)
    try:
        c = {}
        for name in ("gm_fmo", "gm_fe", "gm_xg", "gm_none", "gm_gates", "gm_laps", "gm_banner"):
            c[name] = Client(url)
            chk("%s signs in" % name, c[name].login(name, "pass for " + name), 200)
        owner = Client(url)
        chk("owner signs in", owner.login(OWNER, OWNER_PW), 200)

        _code, g = c["gm_fmo"].call("api/games")
        chk("the FMO moderator is offered FMO only", [x["key"] for x in g["games"]], ["fmo"])
        chk("...with all of its tools",
            [t["id"] for t in g["games"][0]["tools"]], ["npcs", "gates", "coliseum", "war"])
        chk("a moderator with no game permission is offered none",
            c["gm_none"].call("api/games")[1]["games"], [])
        chk("the owner is offered every configured game",
            sorted(x["key"] for x in owner.call("api/games")[1]["games"]), ["fe", "fmo", "kart", "xg"])
        _code, g = c["gm_xg"].call("api/games")
        chk("an extra title is offered to its moderator, with its tools",
            [(x["key"], x["title"], [t["id"] for t in x["tools"]]) for x in g["games"]],
            [("xg", "Extra Game", ["home", "gates"])])
        chk("...whose tools' permissions the panel lists",
            ("game.xg" in adminusers.PERMS, "game.xg.gates" in adminusers.PERMS), (True, True))
        chk("an old per-game grant reads as every tool of that game",
            sorted(adminusers.perms_of({"perms": "games_fmo,gm"})),
            ["game.fmo", "game.fmo.coliseum", "game.fmo.gates", "game.fmo.npcs", "game.fmo.war", "gm"])
        st, _h, body, _u = raw(c["gm_xg"], "games/xg/gates")
        chk("...and its page opens through the panel", (st, SEEN[-1]["path"]), (200, "/gates"))
        chk("the FMO moderator cannot open the extra title", raw(c["gm_fmo"], "games/xg/")[0], 403)

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

        st, hd, body, _u = raw(c["gm_fmo"], "games/fmo/gates/save.bin")
        chk("a download larger than the reply limit is passed on whole",
            (st, len(body), body == SAVE_PIECE * 32), (200, 32 * len(SAVE_PIECE), True))
        chk("...with its type and file name",
            (hd.get("Content-Type"), hd.get("Content-Disposition")),
            ("application/octet-stream", 'attachment; filename="Wren.sav"'))
        st, _h, body, _u = raw(c["gm_fmo"], "games/fmo/gates/big.json")
        chk("JSON over the limit is still refused, never cut short",
            (st, b"more than 1 MB" in body), (502, True))
        st, _h, body, _u = raw(c["gm_fmo"], "games/fmo/gates")
        chk("pages still come whole, with their nonce", (st, b"<script nonce=" in body), (200, True))

        st, _h, body, _u = raw(c["gm_fe"], "games/fe/")
        chk("a tool that refuses the panel's token reads as a panel fault",
            (st, b"refused" in body), (502, True))
        chk("a game the panel does not know is not found", raw(owner, "games/nope/")[0], 404)

        # one tool, not the whole game
        _code, g = c["gm_gates"].call("api/games")
        chk("a moderator with one tool is offered just that tool",
            [(x["key"], [t["id"] for t in x["tools"]]) for x in g["games"]], [("fmo", ["gates"])])
        chk("...opens its page", raw(c["gm_gates"], "games/fmo/gates")[0], 200)
        chk("...and its endpoints", raw(c["gm_gates"], "games/fmo/gates.json")[0], 200)
        chk("...and can change through it",
            raw(c["gm_gates"], "games/fmo/gates/edit", {"op": "byte"})[0], 200)
        chk("...but not the game's other tool (the NPC editor at the root)",
            (raw(c["gm_gates"], "games/fmo/")[0], raw(c["gm_gates"], "games/fmo/state")[0],
             raw(c["gm_gates"], "games/fmo/edit", {"x": 1})[0]), (403, 403, 403))

        # the game modules list
        _code, m = owner.call("api/modules")
        keys = [x["key"] for x in m["modules"]]
        chk("the owner's module list has the games with tools set up",
            all(k in keys for k in ("fe", "fmo", "xg")), True)
        chk("...and the PlayOnline Viewer, which the panel serves itself", "viewer" in keys, True)
        chk("...and, for the owner, what did not load (the bad extra entry)",
            ["bad key" in x for x in m.get("problems") or []], [True])
        _code, m = c["gm_gates"].call("api/modules")
        chk("a moderator's module list is cut to the games they may see",
            [x["key"] for x in m["modules"]], ["fmo"])
        chk("...with only their tools", [t["id"] for t in m["modules"][0]["tools"]], ["gates"])
        chk("...and no problems list", "problems" in m, False)

        # a game that plugs in by its URL alone
        _code, m = owner.call("api/modules")
        kart = next((x for x in m["modules"] if x["key"] == "kart"), None)
        chk("a game known only by its URL describes itself",
            kart and (kart["title"], kart["platform"], [t["id"] for t in kart["tools"]],
                      [a["id"] for a in kart["actions"]]),
            ("Kart Club", "PS2 HDD", ["laps"], ["banner", "kick"]))
        live = kart["live"] if kart else {}
        chk("...and reports who is playing",
            (live.get("online"), [p["name"] for p in live.get("players", [])], live.get("summary"),
             live.get("rooms_label"), live.get("facts")),
            (2, ["Wren", "Ash"], "Grand prix tonight", "races", [{"label": "laps run", "value": 41}]))
        chk("...its permissions are listed",
            all(p in owner.call("api/session")[1].get("perm_labels", {}) for p in
                ("game.kart", "game.kart.laps", "game.kart.banner")), True)
        st, _h, _b, _u = raw(c["gm_laps"], "games/kart/laps")
        chk("a moderator with its tool opens the tool", st, 200)
        SEEN.clear()
        st, _h, body, _u = raw(c["gm_banner"], "api/modules/action",
                               {"key": "kart", "action": "banner", "params": {"text": "Lap race at 9"}})
        chk("a moderator with its action runs it",
            (st, json.loads(body), SEEN[-1]["path"], SEEN[-1]["actor"], json.loads(SEEN[-1]["body"])),
            (200, {"ok": True, "msg": "shown"}, "/openlobby/actions/banner", "gm_banner",
             {"text": "Lap race at 9"}))
        chk("...and it is in the activity log",
            any("banner message in Kart Club" in json.dumps(r) for r in
                (owner.call("api/audit")[1].get("rows") or [])), True)
        chk("a moderator without the action cannot run it",
            raw(c["gm_laps"], "api/modules/action", {"key": "kart", "action": "banner"})[0], 403)
        n = len(SEEN)
        chk("...nor reach the game's own channel through the tool proxy",
            (raw(c["gm_laps"], "games/kart/openlobby/actions/banner", {"text": "x"})[0],
             raw(owner, "games/kart/openlobby/module.json")[0], len(SEEN) - n), (403, 403, 0))
        chk("an action the game does not have is not found",
            raw(owner, "api/modules/action", {"key": "kart", "action": "nope"})[0], 404)
        chk("the owner's Modules page says where each game came from",
            {x["key"]: x["origin"] for x in owner.call("api/modules")[1]["found"]
             if x["key"] in ("kart", "fmo", "xg")},
            {"kart": "game", "fmo": "built-in", "xg": "legacy"})

        # Kick and the maintenance message reach the games too, even with the
        # PlayOnline sign-in service down (there is none in this test)
        _code, acct = owner.call("api/account-create", {"handle": "Lumen", "password": "KartPass123"})
        SEEN.clear()
        st, _h, body, _u = raw(owner, "api/account-kick", {"polid": acct["polid"]})
        got = json.loads(body)
        kicks = [x for x in SEEN if x["path"] == "/openlobby/actions/kick"]
        chk("an account's Kick asks every game that can kick",
            (st, got.get("games", {}).get("kart"), len(kicks)),
            (503, {"ok": True, "msg": "shown"}, 1))
        chk("...with the POL ID and the account's handles",
            json.loads(kicks[0]["body"]) if kicks else None,
            {"polid": acct["polid"], "handles": "Lumen"})
        _code, m = owner.call("api/maintenance")
        # FE is configured here (its token is wrong) and has a System message action
        chk("the maintenance form lists the games that can announce", m.get("games"),
            {"fe": "Fantasy Earth", "kart": "Kart Club"})
        SEEN.clear()
        st, _h, body, _u = raw(owner, "api/maintenance", {"action": "message", "subject": "Down",
                                                          "text": "Back at 10:00", "games": ["kart"]},
                               timeout=40)
        said = [json.loads(x["body"]) for x in SEEN if x["path"] == "/openlobby/actions/banner"]
        chk("the maintenance message is announced in the game even when the mail cannot go",
            (st, json.loads(body).get("games", {}).get("kart", {}).get("ok"), said),
            (503, True, [{"text": "Down: Back at 10:00", "urgent": True}]))
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
