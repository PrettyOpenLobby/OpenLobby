#!/usr/bin/env python3
"""The admin panel's game modules (services/adminmodules), offline.

    python tools/admin_modules_test.py

Checks that the built-in modules load, that a path under /games/<key>/ is
owned by the right tool, that permissions come out per game, tool and action
(and the old per-game grant still means all of them), that a deployment's own
module folder is read and its mistakes reported rather than fatal, that
POL_ADMIN_MODULES picks what a server shows, that the status readers turn the
games' JSON into the panel's shape, and that "{core}" in container names
becomes the deployment's compose project, and that FFXI's account window lookup asks the
bridge's Characters tool by member id. No database; the one network call is to a stand-in on
loopback.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, os.pardir, "services")))

for k in [k for k in os.environ if k.startswith("POL_ADMIN_")]:
    del os.environ[k]

import adminmodules as M  # noqa: E402
from adminmodules import sources  # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


def reload(**env):
    for k in [k for k in os.environ if k.startswith("POL_ADMIN_")]:
        del os.environ[k]
    os.environ.update(env)
    M._AVAIL["keys"] = None
    M._live_cache.clear()
    return M.load(reload=True)


print("built-in modules")
mods = reload()
chk("the PlayOnline titles load, in order", list(mods), list(M.BUILTIN))
chk("with nothing to complain about", M.problems(), [])
ffxi = mods["ffxi"]
chk("the root tool owns what no other claims",
    [ffxi.tool_for(p).id for p in ("", "fed.json", "worlds/add", "providers/remove")],
    ["federation"] * 4)
chk("a tool owns its page, its JSON and everything under it",
    [ffxi.tool_for(p).id for p in ("chars", "chars.json", "chars/data.json", "notices/announce")],
    ["chars", "chars", "chars", "notices"])
chk("a longer path is not mistaken for a shorter tool's",
    ffxi.tool_for("charsx").id, "federation")
chk("a game without a root tool owns nothing else",
    mods["fmo"].tool_for("gates/edit").id, "gates")

print("permissions")
perms = M.all_perms()
chk("one to see the game, one per tool, one per action, one for its server logs",
    sorted(p for p in perms if p.startswith("game.ffxi")),
    ["game.ffxi", "game.ffxi.ah", "game.ffxi.announce", "game.ffxi.bonanza", "game.ffxi.chars",
     "game.ffxi.events", "game.ffxi.federation", "game.ffxi.notices", "game.ffxi.serverlogs"])
chk("a panel page keeps its own permission and adds none",
    (mods["viewer"].perm("pml"), "game.viewer.pml" in perms), ("pml", False))
chk("the old per-game grant expands to the whole game",
    sorted(M.expand_legacy(["games_fmo", "gm", "games_nope"])),
    ["game.fmo", "game.fmo.coliseum", "game.fmo.gates", "game.fmo.npcs", "game.fmo.war",
     "games_nope", "gm"])
chk("one tool is enough to see the game",
    M.can_see("fmo", lambda p: p == "game.fmo.gates"), True)
chk("the Viewer is seen through the PML permission",
    M.can_see("viewer", lambda p: p == "pml"), True)
chk("nothing is nothing", M.can_see("fmo", lambda p: False), False)
d = mods["ffxi"].describe(lambda p: p in ("game.ffxi", "game.ffxi.chars"))
chk("a description is cut to the caller's tools and actions",
    ([t["id"] for t in d["tools"]], d["actions"]), (["chars"], []))

print("which modules a server shows")
chk("with nothing set up, the Viewer only", list(M.enabled()), ["viewer"])
reload(POL_ADMIN_GAME_FMO_URL="http://127.0.0.1:9")
chk("a tool URL is enough", sorted(M.enabled()), ["fmo", "viewer"])
reload(POL_ADMIN_MODULES="ffxi,tm")
chk("an explicit list is taken as it is", sorted(M.enabled()), ["ffxi", "tm"])

print("a deployment's own modules")
tmp = tempfile.mkdtemp(prefix="adminmods-")
with open(os.path.join(tmp, "orbit.py"), "w") as f:
    f.write("from adminmodules import Module, Service, Tool, sources\n"
            "MODULE = Module('orbit', 'Orbit Racers', platform='PS2 HDD',\n"
            "    tools=[Tool('laps', 'Lap times', 'laps')],\n"
            "    services=[Service(['orbit-game-1', '{core}-orbitboard-1'], {7000: 'game'})],\n"
            "    live=sources.board())\n")
with open(os.path.join(tmp, "broken.py"), "w") as f:
    f.write("raise RuntimeError('half-written')\n")
with open(os.path.join(tmp, "twin.py"), "w") as f:
    f.write("from adminmodules import Module\nMODULE = Module('ffxi', 'Not FFXI')\n")
with open(os.path.join(tmp, "badtool.py"), "w") as f:
    f.write("from adminmodules import Module, Tool\n"
            "MODULE = Module('sneaky', 'Sneaky', tools=[Tool('x', 'X', '../etc')])\n")
mods = reload(POL_ADMIN_MODULES_DIR=tmp, POL_ADMIN_CORE_PROJECT="lobbyhost")
chk("its module loads after the built-ins", list(mods)[-1], "orbit")
chk("...with its own permissions", "game.orbit.laps" in M.all_perms(), True)
probs = M.problems()
chk("a file that fails is reported, not fatal", any("broken.py" in p for p in probs), True)
chk("a key a built-in has is refused", (any("twin.py" in p for p in probs),
                                        mods["ffxi"].title), (True, "Final Fantasy XI"))
chk("a tool path that climbs out is refused",
    (any("badtool.py" in p for p in probs), "sneaky" in mods), (True, False))
groups = {g["id"]: g for g in M.service_groups()}
chk("its services become a server-check group, {core} filled in",
    (groups["orbit"]["containers"], groups["orbit"]["ports"]),
    (["orbit-game-1", "lobbyhost-orbitboard-1"], {7000: "game"}))
chk("a built-in's too", groups["ffxi"]["containers"][0], "lobbyhost-connect-1")
chk("a group with no warning gets a plain one",
    groups["orbit"]["warning"], "Everyone in Orbit Racers is disconnected.")

print("the old POL_ADMIN_GAMES_EXTRA")
mods = reload(POL_ADMIN_GAMES_EXTRA=json.dumps(
    {"kart": {"title": "Kart Club", "short": "KC", "tools": [{"id": "home", "label": "Home"}]},
     "fmo": {"title": "FMO again", "tools": [{"id": "x", "label": "X"}]},
     "Bad Key": {"title": "x", "tools": [{"id": "a", "label": "A"}]}}))
chk("an entry becomes a tools-only module", (mods["kart"].title, mods["kart"].tools[0].id),
    ("Kart Club", "home"))
chk("...and cannot replace a built-in", [t.id for t in mods["fmo"].tools], ["npcs", "gates", "coliseum", "war"])
chk("...and one that does not fit is reported", any("Bad Key" in p for p in M.problems()), True)

print("live status")
st = sources.generic({"up": True, "online": 2, "players": [
    {"name": "Wren", "place": "Port Lumen", "level": 13, "job": "Priest"}, {"nope": 1}],
    "rooms": [{"id": 7, "players": ["a", "b"], "max": 4, "state": "playing"}],
    "matches": [{}]})
chk("a count, players and rooms", (st["online"], st["players"], st["rooms"]),
    (2, [{"name": "Wren", "where": "Port Lumen", "detail": "Priest 13"}],
     [{"name": "Room 7", "detail": "2/4 players", "state": "playing"}]))
chk("matches become a fact", st["facts"], [{"label": "matches playing", "value": 1}])
chk("a list of players is the count",
    sources.generic({"online": [{"name": "Ash", "zone": "Rolling Hills"}]})["online"], 1)
chk("{total, players} is the count",
    sources.generic({"online": {"total": 3, "players": ["Ash", "Bo"]}})["online"], 3)
chk("a board's own error is passed on",
    sources.generic({"up": False, "error": "world unreachable"})["error"], "world unreachable")
path = os.path.join(tmp, "orbit-live.json")
with open(path, "w") as f:
    json.dump({"title": "Orbit Racers", "up": True, "online": 1,
               "players": [{"name": "Ash", "room": "Lap 3"}], "rooms": []}, f)
reload(POL_ADMIN_MODULES_DIR=tmp, POL_ADMIN_GAME_ORBIT_LIVE=path)
got = M.live("orbit")
chk("a module reads its game's status file",
    (got["online"], got["players"][0]["where"], got["source"], got["error"]),
    (1, "Lap 3", "status JSON", None))
chk("...and is cached", M.live("orbit") is got, True)
os.environ["POL_ADMIN_GAME_ORBIT_LIVE"] = os.path.join(tmp, "missing.json")
got = M.live("orbit", fresh=True)
chk("a status that cannot be read says so", (got["up"], "unreadable" in got["error"]),
    (False, True))
chk("several at once", sorted(M.live_all(["orbit", "viewer"])), ["orbit", "viewer"])
chk("a module with no live source has none", M.live("viewer"), None)

print("JongHoLow's GM notice and kick, as its game band reads them")
mods = reload()
jan = mods["jan"]
HOLO = os.path.abspath(os.path.join(HERE, os.pardir, os.pardir, "hippaulholo", "services"))
if os.path.isdir(HOLO):
    sys.path.insert(0, HOLO)
    from janworld import gmnotice  # noqa: E402  -- HippaulHoLo's own reader
    gmnotice.clear()
    ok, msg = jan.action("notice").run(jan, {"text": "Tables close in 10 minutes."}, "op")
    doc = gmnotice._read()
    chk("a notice from the panel is a post the game band reads",
        (ok, [(p["op"], p["text"], p["member"]) for p in doc.get("posts", [])]),
        (True, [("open", "Tables close in 10 minutes.", 0)]))
    recs = gmnotice.pending_for(7)
    chk("...and becomes the record a member's client is sent", len(recs), 1)
    chk("the maintenance warning finds it as JongHoLow's announce",
        jan.action("announce").id, "notice")
    import adminmodules.jan as janmod  # noqa: E402
    real = janmod._members
    janmod._members = lambda polid="", handles="": [7] if handles == "Lex" else []
    try:
        ok, msg = jan.action("kick").run(jan, {"handles": "Lex"}, "op")
        last = gmnotice._read()["posts"][-1]
        chk("a kick is posted for that one member", (ok, last["op"], last["member"]), (True, "kick", 7))
        chk("a name with no member is refused, nothing posted",
            (jan.action("kick").run(jan, {"handles": "Nobody"}, "op")[0],
             len(gmnotice._read()["posts"])), (False, 2))
    finally:
        janmod._members = real
    chk("an empty notice is refused", jan.action("notice").run(jan, {"text": "  "}, "op")[0], False)
    gmnotice.clear()
else:
    print("  (HippaulHoLo is not beside this checkout; skipped)")

print("FFXI in the account window")
import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

ASKED = []


class _Chars(BaseHTTPRequestHandler):
    """The bridge's Characters tool, as far as /chars/player.json goes."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        ASKED.append((self.path, self.headers.get("X-Devtool-Token")))
        members = self.path.partition("members=")[2]
        doc = {"characters": [{"name": "Arbor", "detail": "BLU 67/RDM 33, Port Jeuno", "online": False,
                               "facts": [{"label": "main job", "value": "BLU 67"}]}], "note": ""}             if "7" in members.replace("%2C", ",").split(",") else {}
        body = json.dumps(doc).encode()
        self.send_response(503 if members == "9" else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


srv = ThreadingHTTPServer(("127.0.0.1", 0), _Chars)
threading.Thread(target=srv.serve_forever, daemon=True).start()
mods = reload(POL_ADMIN_MODULES="ffxi", POL_ADMIN_GAME_FFXI_URL="http://127.0.0.1:%d" % srv.server_address[1],
              POL_ADMIN_GAME_FFXI_TOKEN="ffxi-tok")
ffxi = mods["ffxi"]
got = ffxi.player(ffxi, {"polid": "ABCD1234", "handles": ["Arbor"], "members": ["3", "7"]})
chk("the account's characters come from the Characters tool",
    [c["name"] for c in (got or {}).get("characters", [])], ["Arbor"])
chk("...asked by member id alone, with the panel's token",
    ASKED[-1], ("/chars/player.json?members=3%2C7", "ffxi-tok"))
chk("an account whose members have no characters is nothing",
    ffxi.player(ffxi, {"polid": "ABCD1234", "handles": ["Birch"], "members": ["3"]}), None)
n = len(ASKED)
chk("an account with no members is nothing, without asking",
    (ffxi.player(ffxi, {"polid": "", "handles": ["Arbor"], "members": []}), len(ASKED)), (None, n))
chk("a member id that is not a number is not sent",
    (ffxi.player(ffxi, {"polid": "", "handles": [], "members": ["x1"]}), len(ASKED)), (None, n))
try:
    ffxi.player(ffxi, {"polid": "", "handles": [], "members": ["9"]})
    chk("a tool that cannot read its database is a game not answering", "answered", "raised")
except RuntimeError:
    chk("a tool that cannot read its database is a game not answering", "raised", "raised")
srv.shutdown()
mods = reload(POL_ADMIN_MODULES="ffxi")
chk("without the bridge's tools set up there is nothing to ask",
    mods["ffxi"].player(mods["ffxi"], {"polid": "", "handles": [], "members": ["7"]}), None)

print("a module file's calendar from its game's tool port")
CAL_ASKED = []


class _Cal(BaseHTTPRequestHandler):
    """A game's tool port, as far as /calendar.json goes: two yearly runs, one with two parts."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        CAL_ASKED.append((self.path, self.headers.get("X-Devtool-Token")))
        doc = {"events": [
            {"id": "FEST_001-1797000000", "title": "Winter Fair", "start": 1797000000, "end": 1798300000,
             "kind": "limited quest", "about": "In 2 parts.", "times": [[1797000000, 1797600000],
                                                                       [1797600000, 1798300000]]},
            {"id": "LATE_001-1799000000", "title": "Outside the range", "start": 1799000000, "end": 1799100000},
            {"title": "No dates"}]}
        body = json.dumps(doc).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


import tempfile  # noqa: E402
CAL_MODS = tempfile.mkdtemp(prefix="calmods-")
with open(os.path.join(CAL_MODS, "kart.py"), "w") as f:
    f.write("from adminmodules import Module, Tool, sources\n"
            "MODULE = Module('kart', 'Kart Club', tools=[Tool('laps', 'Lap times', 'laps')],\n"
            "                calendar=sources.tool_calendar())\n")
srv = ThreadingHTTPServer(("127.0.0.1", 0), _Cal)
threading.Thread(target=srv.serve_forever, daemon=True).start()
mods = reload(POL_ADMIN_MODULES_DIR=CAL_MODS, POL_ADMIN_MODULES="kart",
              POL_ADMIN_GAME_KART_URL="http://127.0.0.1:%d" % srv.server_address[1],
              POL_ADMIN_GAME_KART_TOKEN="kart-tok")
kart = mods["kart"]
lo, hi = 1796083200, 1798761600
got = kart.calendar(kart, lo, hi)
chk("the game's runs in the range, its parts as times",
    [(e["id"], e["title"], e["start"], e["end"], e["kind"], e["times"]) for e in got],
    [("FEST_001-1797000000", "Winter Fair", 1797000000, 1798300000, "limited quest",
      [[1797000000, 1797600000], [1797600000, 1798300000]])])
chk("...asked for that range with the panel's token",
    CAL_ASKED[-1], ("/calendar.json?from=%d&to=%d" % (lo, hi), "kart-tok"))
srv.shutdown()
mods = reload(POL_ADMIN_MODULES_DIR=CAL_MODS, POL_ADMIN_MODULES="kart")
chk("without its tools set up there is nothing to ask", mods["kart"].calendar(mods["kart"], lo, hi), [])

print("all checks passed" if not bad else "%d check(s) FAILED" % bad)
sys.exit(1 if bad else 0)
