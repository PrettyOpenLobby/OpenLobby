#!/usr/bin/env python3
"""The player picker: a game's player search through the panel (/api/games/players) and
the PlayOnline account search the panel's own forms use (/api/accounts/find).

    python tools/admin_players_test.py

A made-up game, Kart Club, describes itself through tools/olport.py and searches its
racers; a module file, Bowling Lanes, searches its own; a third, Dart Club, has no search.
Checks that the owner and a moderator holding any of a game's permissions can search it and
get the game's rows back cleaned (trimmed, at most 50, online kept as true, false or null);
that a moderator without the game's permission gets 403 and a game without search 404; that
a bad limit is 400; that /api/modules says which games search; that a game whose search fails
is a 502 naming only the kind of failure; and that the account search finds PlayOnline
accounts by ID or handle, signed-in ones first, for anyone who may view accounts.
Throwaway PostgreSQL (tools/pgtest.py); the panel runs as a subprocess.
"""
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.abspath(os.path.join(HERE, os.pardir, "services"))
sys.path.insert(0, HERE)
sys.path.insert(0, SERVICES)

import pgtest  # noqa: E402

pgtest.use_fresh_database()

import olport  # noqa: E402

TOKEN = "kart-token-0123456789"
port = olport.AdminPort("kart", "Kart Club", token=TOKEN)
RACERS = [("17", "Arden", True), ("4", "Ardley", False), ("9", "Mira", True), ("31", "Tobin", None)]
ASKED = []


@port.players
def players(q, limit):
    ASKED.append((q, limit))
    if q == "boom":
        raise KeyError("secret detail that must not leak")
    if q == "many":
        return [{"id": " %d " % i, "label": "  Racer   %d " % i, "detail": "x" * 300, "online": "yes"}
                for i in range(80)] + [{"id": "", "label": "no id"}, "junk"]
    hits = [r for r in RACERS if q.lower() in r[1].lower() or q == r[0]]
    hits.sort(key=lambda r: r[2] is not True)
    return [{"id": i, "label": n, "detail": "Class A", "online": on} for i, n, on in hits]


@port.action("mute", "Mute a racer", fields=[{"id": "racer", "label": "Racer", "type": "player"}])
def mute(params, actor):
    return True, "Muted %s." % params.get("racer")


_h, kport = port.start()

MODS = tempfile.mkdtemp(prefix="adminplayers-mods-")
with open(os.path.join(MODS, "bowl.py"), "w") as f:
    f.write("from adminmodules import Action, Module\n"
            "def find(m, q, limit):\n"
            "    rows = [{'id': 'b1', 'label': 'Pinsetter', 'detail': 'league 2', 'online': False}]\n"
            "    return [r for r in rows if q.lower() in r['label'].lower()][:limit]\n"
            "MODULE = Module('bowl', 'Bowling Lanes', present=True, players=find,\n"
            "    actions=[Action('warn', 'Warn a bowler', lambda m, p, a: (True, 'ok'),\n"
            "                    fields=[{'id': 'who', 'label': 'Bowler', 'type': 'player'}])])\n")
with open(os.path.join(MODS, "dart.py"), "w") as f:
    f.write("from adminmodules import Module\n"
            "MODULE = Module('dart', 'Dart Club', present=True)\n")

os.environ["POL_ADMIN_MODULES"] = "kart,bowl,dart"
os.environ["POL_ADMIN_MODULES_DIR"] = MODS
os.environ["POL_ADMIN_GAME_KART_URL"] = "http://127.0.0.1:%d" % kport
os.environ["POL_ADMIN_GAME_KART_TOKEN"] = TOKEN

import accounts  # noqa: E402
import adminusers  # noqa: E402
from admin_http import Client, free_port, start_panel, stop  # noqa: E402

OWNER, OWNER_PW = "op", "correct horse 1"
bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got, "" if ok else "  (want %r)" % (want,)))


def main():
    tmp = tempfile.mkdtemp(prefix="adminplayers-")
    db = accounts.connect()
    accounts.set_admin_cred(db, OWNER, OWNER_PW)
    db.commit()
    # one permission in the game is enough to search it (adminmodules.can_see)
    adminusers.create_mod(db, "gm_kart", "pass for gm_kart", ["game.kart.mute"], by=OWNER)
    adminusers.create_mod(db, "gm_view", "pass for gm_view", ["accounts_view"], by=OWNER)
    for polid, handles in (("KARTPOL1", ["Arden", "ArdenAlt"]), ("KARTPOL2", ["Wren"]), ("ARDPOL03", [])):
        accounts.create_polid(db, polid, "pw-account")
        if handles:
            mid = accounts.add_member(db, polid, polid.lower(), "pw-member")
            for h in handles:
                accounts.set_handle(db, mid, h)
            if polid == "KARTPOL2":
                db.execute("INSERT INTO session (token, member_id, created_at, expires_at)"
                           " VALUES ('tok-wren', %s, '2026-10-22T00:00:00Z', %s)",
                           (mid, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))))
    db.commit()
    db.close()
    proc, url = start_panel(tmp, SERVICES, free_port(),
                            env_extra={k: os.environ[k] for k in os.environ if k.startswith("POL_ADMIN_")})
    try:
        owner, gm_kart, gm_view = Client(url), Client(url), Client(url)
        owner.login(OWNER, OWNER_PW)
        gm_kart.login("gm_kart", "pass for gm_kart")
        gm_view.login("gm_view", "pass for gm_view")

        print("which games search")
        mods = {m["key"]: m for m in owner.call("api/modules")[1].get("modules", [])}
        chk("/api/modules says which games have a player search",
            {k: m.get("players") for k, m in mods.items()}, {"kart": True, "bowl": True, "dart": False})
        chk("...and the player fields keep their type",
            [f["type"] for f in mods["kart"]["actions"][0]["fields"]], ["player"])

        print("a game that describes itself")
        code, d = owner.call("api/games/players?game=kart&q=ard&limit=5")
        chk("the owner searches Kart Club: its rows, online first",
            (code, d.get("players"), ASKED[-1]),
            (200, [{"id": "17", "label": "Arden", "detail": "Class A", "online": True},
                   {"id": "4", "label": "Ardley", "detail": "Class A", "online": False}], ("ard", 5)))
        code, d = gm_kart.call("api/games/players?game=kart&q=tob")
        chk("a moderator with one of the game's permissions may search it, limit 20 by default",
            (code, [(p["label"], p["online"]) for p in d.get("players", [])], ASKED[-1]),
            (200, [("Tobin", None)], ("tob", 20)))
        n = len(ASKED)
        chk("a moderator without the game's permission gets 403 and the game is not asked",
            (gm_view.call("api/games/players?game=kart&q=ard")[0], len(ASKED) - n), (403, 0))
        chk("a bad limit is 400",
            [owner.call("api/games/players?game=kart&limit=" + x)[0] for x in ("0", "51", "x")], [400, 400, 400])
        code, d = owner.call("api/games/players?game=kart&q=many&limit=50")
        rows = d.get("players", [])
        chk("rows are cleaned: at most 50, trimmed, cut, odd online read as null, no empty ids",
            (code, len(rows), rows[0]["id"], rows[0]["label"], len(rows[0]["detail"]), rows[0]["online"]),
            (200, 50, "0", "Racer 0", 120, None))
        code, d = owner.call("api/games/players?game=kart&q=boom")
        chk("a search that fails is a 502 that does not carry the game's exception text",
            (code, "secret" in str(d)), (502, False))
        code, d = owner.call("api/modules/action", {"key": "kart", "action": "mute", "params": {"racer": "17"}})
        chk("the picked id is what the action gets", (code, d.get("msg")), (200, "Muted 17."))

        print("a module file")
        code, d = owner.call("api/games/players?game=bowl&q=pin")
        chk("a module file's players= answers",
            (code, d.get("players")), (200, [{"id": "b1", "label": "Pinsetter", "detail": "league 2", "online": False}]))
        chk("a game without search is 404", owner.call("api/games/players?game=dart&q=a")[0], 404)
        chk("a game that is not there is refused", owner.call("api/games/players?game=nope&q=a")[0] in (403, 404), True)

        print("PlayOnline accounts")
        code, d = owner.call("api/accounts/find?q=ard")
        chk("by handle or ID; the id is the PlayOnline ID, the label the handle with it",
            (code, sorted((p["id"], p["label"], p["online"]) for p in d.get("players", []))),
            (200, [("ARDPOL03", "ARDPOL03", False), ("KARTPOL1", "Arden (KARTPOL1)", False)]))
        chk("...the detail names the other handles",
            [p["detail"] for p in d.get("players", []) if p["id"] == "KARTPOL1"], ["PlayOnline ID KARTPOL1, also ArdenAlt"])
        code, d = gm_view.call("api/accounts/find?q=")
        chk("empty q: the signed-in account first", (code, d["players"][0]["id"], d["players"][0]["online"]),
            (200, "KARTPOL2", True))
        chk("a LIKE wildcard is matched as itself", owner.call("api/accounts/find?q=%25")[1].get("players"), [])
        chk("the limit", (len(owner.call("api/accounts/find?q=&limit=1")[1]["players"]),
                          owner.call("api/accounts/find?limit=99")[0]), (1, 400))
        chk("a moderator who may not view accounts cannot search them",
            gm_kart.call("api/accounts/find?q=ard")[0], 403)
    finally:
        stop(proc)
        port.stop()
    print("all checks passed" if not bad else "%d check(s) FAILED" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
