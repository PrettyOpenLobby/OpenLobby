#!/usr/bin/env python3
"""tools/olport.py (the game side of the OpenLobby admin protocol) against
the panel's own reader (services/adminmodules/remote.py).

    python tools/olport_test.py

A made-up game serves its description, its status, two actions and a tool
page through olport; the panel's module registry is pointed at it by URL and
token alone, and must list it, read who is playing, run its actions (with the
panel user's name), refuse without the token, and never let a tool path reach
the protocol. No database, no Docker.
"""
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, os.pardir, "services")))
for k in [k for k in os.environ if k.startswith("POL_ADMIN_")]:
    del os.environ[k]

import olport  # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got, "" if ok else "  (want %r)" % (want,)))


TOKEN = "kart-token-0123456789"
SAID = []

print("starting a game's admin port")
try:
    olport.AdminPort("kart", "Kart Club", bind="0.0.0.0")
    chk("a wide bind without a token is refused", False, True)
except ValueError:
    chk("a wide bind without a token is refused", True, True)

port = olport.AdminPort("kart", "Kart Club", short="Kart", platform="PS2 HDD", token=TOKEN,
                        services=[{"containers": ["kart-game-1"], "ports": {"7000": "game"}}])


@port.status
def status():
    return {"up": True, "online": 2, "players": [{"name": "Arden", "room": "Lap 3"}, "Mira"],
            "rooms": [{"name": "Friday race", "players": 2, "max": 4, "state": "racing"}],
            "rooms_label": "races", "summary": "Grand prix tonight"}


@port.action("banner", "Banner message", kind="announce", fields=[{"id": "text", "label": "Message"}])
def banner(params, actor):
    SAID.append((params.get("text"), actor))
    return True, "Shown in every lobby."


@port.action("kick", "Kick", kind="kick", danger=True)
def kick(params, actor):
    raise RuntimeError("secret detail that must not leak")


RACERS = [("17", "Arden", True), ("4", "Ardley", False), ("9", "Mira", True), ("31", "Tobin", None)]
ASKED = []


@port.players
def players(q, limit):
    ASKED.append((q, limit))
    if q == "boom":
        raise KeyError("secret detail that must not leak")
    hits = [r for r in RACERS if q.lower() in r[1].lower() or q == r[0]]
    hits.sort(key=lambda r: r[2] is not True)
    return [{"id": i, "label": n, "detail": "Class A", "online": on} for i, n, on in hits]


@port.tool("laps", "Lap times", "laps")
def laps(method, sub, query, body, actor):
    return 200, "text/html; charset=utf-8", ("<p>%s %s</p>" % (method, sub)).encode()


host, pnum = port.start()
url = "http://127.0.0.1:%d" % pnum


def get(path, token=TOKEN, body=None):
    req = urllib.request.Request(url + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"X-Devtool-Token": token, "X-Devtool-Actor": "lex"},
                                 method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


chk("without the token nothing answers", get("/openlobby/module.json", token="nope")[0], 403)
st, body = get("/openlobby/module.json")
chk("its description", (st, json.loads(body)["key"], [a["id"] for a in json.loads(body)["actions"]]),
    (200, "kart", ["banner", "kick"]))
chk("a failing action says what kind of failure, not its text",
    json.loads(get("/openlobby/actions/kick", body={})[1]), {"ok": False, "msg": "failed: RuntimeError"})
chk("a tool page", get("/laps/edit")[1], b"<p>GET laps/edit</p>")
chk("an unknown action", get("/openlobby/actions/nope", body={})[0], 404)
chk("module.json says it has a player search", json.loads(get("/openlobby/module.json")[1])["players"], True)
st, body = get("/openlobby/players.json?q=ard&limit=5")
chk("players.json: the matches, online first",
    (st, [(p["id"], p["online"]) for p in json.loads(body)["players"]], ASKED[-1]),
    (200, [("17", True), ("4", False)], ("ard", 5)))
st, body = get("/openlobby/players.json")
chk("no q and no limit: everyone the game answers, limit 20", (st, len(json.loads(body)["players"]), ASKED[-1]),
    (200, 4, ("", 20)))
chk("the limit cuts the answer", len(json.loads(get("/openlobby/players.json?limit=1")[1])["players"]), 1)
chk("q is url-decoded", (get("/openlobby/players.json?q=Mi%72a")[0], ASKED[-1]), (200, ("Mira", 20)))
n = len(ASKED)
chk("a bad limit is 400 and never reaches the game",
    ([get("/openlobby/players.json?limit=" + x)[0] for x in ("0", "51", "x", "-1")], len(ASKED) - n),
    ([400, 400, 400, 400], 0))
st, body = get("/openlobby/players.json?q=boom")
chk("a failing search says what kind of failure, not its text",
    (st, json.loads(body), b"secret" in body), (500, {"error": "players failed: KeyError"}, False))
chk("players.json needs the token", get("/openlobby/players.json", token="nope")[0], 403)

print("the panel, given the URL and the token")
os.environ["POL_ADMIN_GAME_KART_URL"] = url
os.environ["POL_ADMIN_GAME_KART_TOKEN"] = TOKEN
import adminmodules as M  # noqa: E402
mods = M.load(reload=True)
kart = mods.get("kart")
chk("the game describes itself to the registry",
    kart and (kart.title, kart.platform, kart.origin, [t.id for t in kart.tools],
              [a.id for a in kart.actions]),
    ("Kart Club", "PS2 HDD", "game", ["laps"], ["banner", "kick"]))
chk("its services become a server check",
    [g["containers"] for g in M.service_groups(["kart"])], [["kart-game-1"]])
live = M.live("kart")
chk("who is playing", (live["online"], [p["name"] for p in live["players"]], live["rooms_label"],
                       live["summary"], live["rooms"][0]["detail"]),
    (2, ["Arden", "Mira"], "races", "Grand prix tonight", "2/4 players"))
ok, msg = kart.action("announce").run(kart, {"text": "Back at 10:00"}, "lex")
chk("the panel's announce runs the game's banner, with the panel user's name",
    (ok, msg, SAID), (True, "Shown in every lobby.", [("Back at 10:00", "lex")]))
chk("the remote module has the player search and says so",
    (kart.players is not None, kart.describe()["players"]), (True, True))
chk("the panel's find_players asks the game and cleans the rows",
    (M.find_players("kart", " ard ", 50), ASKED[-1]),
    ([{"id": "17", "label": "Arden", "detail": "Class A", "online": True},
      {"id": "4", "label": "Ardley", "detail": "Class A", "online": False}], ("ard", 50)))
chk("...and keeps an unknown online as None", M.find_players("kart", "tob", 3)[0]["online"], None)
chk("the panel's kick reaches the game and reports its failure",
    kart.action("kick").run(kart, {"polid": "ABCD1234", "handles": "Arden"}, "lex"),
    (False, "failed: RuntimeError"))

print("a game that adds an action while the panel runs")


@port.action("lights", "Track lights", fields=[{"id": "on", "label": "On"}])
def lights(params, actor):
    return True, "Lights set."


chk("not before the panel asks the game again", [a.id for a in M.enabled()["kart"].actions], ["banner", "kick"])
M._loaded["at"] -= M._REDESCRIBE + 1
chk("...and there after five minutes, without a restart",
    [a.id for a in M.enabled()["kart"].actions], ["banner", "kick", "lights"])

print("a module file for a game that describes itself")
import tempfile  # noqa: E402
mdir = tempfile.mkdtemp(prefix="olport-mods-")
with open(os.path.join(mdir, "kart.py"), "w") as f:
    f.write("from adminmodules import Module, Service, sources\n"
            "MODULE = Module('kart', 'Kart Club (file)', self_described=True,\n"
            "    services=[Service(['kartfile-game-1'])], live=sources.board())\n")
os.environ["POL_ADMIN_MODULES_DIR"] = mdir
kart = M.load(reload=True).get("kart")
chk("the game's own description wins, the file's services stay",
    (kart.title, kart.origin, [a.id for a in kart.actions], [g["containers"] for g in M.service_groups(["kart"])]),
    ("Kart Club", "game", ["banner", "kick", "lights"], [["kartfile-game-1"]]))
port.stop()
kart = M.load(reload=True).get("kart")
chk("with the game down, the file stands in and says why",
    (kart.title, kart.origin, any("stands in" in p for p in M.problems())),
    ("Kart Club (file)", "file", True))
del os.environ["POL_ADMIN_MODULES_DIR"]
host, pnum = port.start()
url = "http://127.0.0.1:%d" % pnum
os.environ["POL_ADMIN_GAME_KART_URL"] = url

print("a wrong token")
os.environ["POL_ADMIN_GAME_KART_TOKEN"] = "wrong"
M.load(reload=True)
chk("the game is not listed, and the reason is", (
    "kart" in M.load(), any("kart" in p for p in M.problems())), (False, True))

print("the copy the services import")
CRLF, LF = bytes((13, 10)), bytes((10,))
with open(os.path.join(HERE, "olport.py"), "rb") as f1:
    published = f1.read().replace(CRLF, LF)
with open(os.path.join(HERE, os.pardir, "services", "olport.py"), "rb") as f2:
    imported = f2.read().replace(CRLF, LF)
chk("services/olport.py is tools/olport.py, byte for byte", published == imported, True)

port.stop()
print("all checks passed" if not bad else "%d check(s) FAILED" % bad)
sys.exit(1 if bad else 0)
