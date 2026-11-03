#!/usr/bin/env python3
"""Moderator permissions for every tab of the admin panel, over HTTP.

    python tools/admin_perms_test.py

For each permission the owner can tick -- account management and deletion,
voiding codes, writing and publishing news, PML Preview, the activity log,
service status, alert/check settings -- a moderator holding ONLY that
permission may use its endpoints and is refused the others. Moderator
management, the owner sign-in, the update channels and the dev access codes stay the owner's
whatever is ticked.

Then the game permissions (adminmodules.CATEGORIES), on two made-up games: each tool and
action of a game belongs to one of the seven (a module can name it), a moderator given one
reaches that kind of tool in every game and nothing else, any of them also shows the game,
a game left out of one is left out, a game added later is covered at once, and a game's
updates stay the owner's. A moderator saved per game before then keeps exactly what they
had until the owner saves them, is shown in the new form, and is saved in it.

The browser side (which tabs each moderator sees) is not driven here.

Uses a throwaway PostgreSQL database (tools/pgtest.py) and a temporary data
directory; the panel runs as a subprocess on a loopback port.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.abspath(os.path.join(HERE, os.pardir, "services"))
sys.path.insert(0, HERE)
sys.path.insert(0, SERVICES)

import pgtest  # noqa: E402

pgtest.use_fresh_database()

MODDIR = tempfile.mkdtemp(prefix="adminperms-mods-")
#: Two made-up games. Kart Club has one tool or action of every kind, and one ("spin") that
#: says its permission itself where the words would guess another.
KART = """from adminmodules import Action, HelpDesk, Module, Service, Tool

def _run(m, params, actor):
    return True, "done"

MODULE = Module("kart", "Kart Club", short="Kart", present=True,
    tools=[Tool("laps", "Lap times", "laps"), Tool("faq", "FAQ", "faq"),
           Tool("racelog", "Race log", "racelog")],
    actions=[Action("banner", "Banner message", _run, kind="announce"),
             Action("kick", "Kick", _run, kind="kick"),
             Action("ban", "Ban", _run), Action("unmute", "Unmute", _run),
             Action("boost", "Give a boost", _run, fields=[{"id": "who", "label": "Racer", "type": "player"}]),
             Action("spin", "Spin out a racer", _run, perm="kick")],
    help=HelpDesk(lambda m, o: [], lambda m, i: {}, lambda *a: (True, ""), lambda *a: (True, "")),
    services=[Service(["kart-game-1"])])
"""
ORBIT = """from adminmodules import Action, Module, Tool

def _run(m, params, actor):
    return True, "done"

MODULE = Module("orbit", "Orbit Run", short="Orbit", present=True,
    tools=[Tool("track", "Track editor", "track")],
    actions=[Action("kick", "Disconnect", _run, kind="kick")])
"""
#: a third, put in place while the panel runs: covered by the game permissions at once
COMET = """from adminmodules import Action, Module

def _run(m, params, actor):
    return True, "done"

MODULE = Module("comet", "Comet Cup", short="Comet", present=True,
    actions=[Action("kick", "Kick", _run, kind="kick"), Action("gift", "Give a gift", _run)])
"""
for name, text in (("kart.py", KART), ("orbit.py", ORBIT)):
    with open(os.path.join(MODDIR, name), "w", encoding="utf-8") as f:
        f.write(text)
os.environ["POL_ADMIN_MODULES_DIR"] = MODDIR
os.environ["POL_ADMIN_MODULES"] = "kart,orbit"

import accounts  # noqa: E402
import adminmodules  # noqa: E402
import adminusers  # noqa: E402
from admin_http import Client, free_port, start_panel, stop  # noqa: E402

OWNER, OWNER_PW = "op", "correct horse 1"
bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


#: One moderator per permission, and one request only that permission allows.
PROBES = {
    "accounts_manage": ("POST", "api/account-kick", {"polid": "NOPE"}),
    "accounts_delete": ("POST", "api/account-delete", {"polid": "NOPE"}),
    "codes_void": ("POST", "api/codes/void", {"code": "NOPE"}),
    "news_edit": ("GET", "api/news", None),
    "pml": ("GET", "api/pml-list", None),
    "audit": ("GET", "api/audit", None),
    "health": ("GET", "api/health", None),
    "settings": ("GET", "api/alerts", None),
}


#: game moderators: what is saved, then {game: (tools, actions)} they are offered
GAME_MODS = {
    "g_kick": ["games.kick"],
    "g_kick_not": ["games.kick", "games.kick:not:orbit"],
    "g_see": ["games.see"],
    "g_edit": ["games.edit", "games.edit:not:kart"],
    "g_most": ["games.see", "games.message", "games.help", "games.ban", "games.logs"],
    # saved per game: Kart Club's kick and lap times, nothing in Orbit Run
    "g_old": ["game.kart", "game.kart.kick", "game.kart.laps"],
}
GAME_WANT = {
    "g_kick": {"kart": ([], ["kick", "spin"]), "orbit": ([], ["kick"])},
    "g_kick_not": {"kart": ([], ["kick", "spin"])},
    "g_see": {"kart": ([], []), "orbit": ([], [])},
    "g_edit": {"orbit": (["track"], [])},
    "g_most": {"kart": (["faq", "racelog"], ["banner", "ban", "unmute"]), "orbit": ([], [])},
    "g_old": {"kart": (["laps"], ["kick"])},
}


def offered(c):
    code, r = c.call("api/modules")
    return {m["key"]: ([t["id"] for t in m["tools"]], [a["id"] for a in m["actions"]])
            for m in (r.get("modules") or [])} if code == 200 else code


def game_perms(url):
    print("game permissions")
    kart = adminmodules.load()["kart"]
    chk("every tool and action of a game belongs to one",
        {p.split(".", 2)[-1] if p.count(".") == 2 else "(the game)": c
         for p, c in adminmodules.game_perms(kart).items()},
        {"(the game)": "see", "laps": "edit", "faq": "help", "racelog": "logs", "banner": "message",
         "kick": "kick", "ban": "ban", "unmute": "ban", "boost": "edit", "spin": "kick", "help": "help",
         "serverlogs": "logs"})
    owner = Client(url)
    owner.login(OWNER, OWNER_PW)
    cs = {}
    for name in GAME_MODS:
        c = Client(url)
        if c.login(name, "pass for " + name) != 200:
            chk("%s signs in" % name, False, True)
        cs[name] = c
    for name, want in GAME_WANT.items():
        chk("%s is offered just its kind of tools and actions" % name, offered(cs[name]), want)
    every = [(k, t) for k, (tools, acts) in {"kart": (["laps", "faq", "racelog"], ["banner", "kick", "ban",
             "unmute", "boost", "spin"]), "orbit": (["track"], ["kick"])}.items() for t in tools + acts]
    for name, want in GAME_WANT.items():
        got_tools, got_acts = [], []
        for key, item in every:
            if item in ("laps", "faq", "racelog", "track"):
                code = cs[name].call("games/%s/%s" % (key, item))[0]
                (got_tools if code != 403 else []).append("%s.%s" % (key, item))
            else:
                code = cs[name].call("api/modules/action", {"key": key, "action": item, "params": {}})[0]
                (got_acts if code != 403 else []).append("%s.%s" % (key, item))
        allowed = sorted("%s.%s" % (k, i) for k, (t, a) in want.items() for i in t + a)
        chk("...and the panel lets %s use those and refuses the rest" % name,
            sorted(got_tools + got_acts), allowed)
    chk("no moderator may update a game",
        sorted(n for n, c in cs.items() if c.call("api/updates/apply", {"key": "kart"})[0] != 403), [])

    code, r = owner.call("api/mods")
    rows = {m["username"]: m for m in r.get("mods", [])}
    chk("the editor lists the seven game permissions", [k for k in r.get("perm_labels", {}) if k.startswith("games.")],
        ["games.see", "games.message", "games.kick", "games.ban", "games.help", "games.edit", "games.logs"])
    chk("...and the games each one has",
        {g["key"]: g["cats"] for g in r.get("perm_games", []) if g["key"] in ("kart", "orbit")},
        {"kart": ["see", "message", "kick", "ban", "help", "edit", "logs"], "orbit": ["see", "kick", "edit"]})
    # the built-in titles are found too (not shown here): every one is left out as well
    ours = lambda perms: [x for x in perms if ":not:" not in x or x.split(":not:")[1] in ("kart", "orbit")]
    chk("a moderator saved per game is shown in the new form", ours(rows["g_old"]["perms"]),
        ["games.edit", "games.edit:not:orbit", "games.kick", "games.kick:not:orbit", "games.see",
         "games.see:not:orbit"])
    chk("...every other game left out of seeing", sorted(x.split(":not:")[1] for x in rows["g_old"]["perms"]
                                                        if x.startswith("games.see:not:")),
        sorted(k for k in adminmodules.load() if k != "kart"))
    chk("...and their session says the same, beside what it comes to",
        (cs["g_old"].call("api/session")[1].get("grants"), cs["g_old"].call("api/session")[1].get("perms")),
        (rows["g_old"]["perms"], ["game.kart", "game.kart.kick", "game.kart.laps"]))
    code, _r = owner.call("api/mods", {"action": "update", "id": rows["g_old"]["id"], "perms": rows["g_old"]["perms"]})
    chk("the owner saves them as shown", code, 200)
    saved = accounts.connect().execute("SELECT perms FROM admin_moderator WHERE username = 'g_old'").fetchone()[0]
    chk("...and the new form is what is stored", saved.split(","), rows["g_old"]["perms"])
    chk("...which now gives all of that kind in Kart Club, nothing in Orbit Run", offered(cs["g_old"]),
        {"kart": (["laps"], ["kick", "boost", "spin"])})
    code, _r = owner.call("api/mods", {"action": "update", "id": rows["g_see"]["id"],
                                       "perms": ["games.see", "games.see:not:kart", "games.nope", "gm",
                                                 "games.kick:not:kart"]})
    saved = accounts.connect().execute("SELECT perms FROM admin_moderator WHERE username = 'g_see'").fetchone()[0]
    chk("a save keeps only what the panel knows, and leaves out games only of what is given",
        (code, saved), (200, "games.see,games.see:not:kart,gm"))
    chk("...and the game left out is gone", offered(cs["g_see"]), {"orbit": ([], [])})
    with open(os.path.join(MODDIR, "comet.py"), "w", encoding="utf-8") as f:
        f.write(COMET)
    adminmodules.load(reload=True)
    chk("a game added later is covered at once, and only as far as given",
        (sorted(x for x in adminmodules.granted(["games.kick"]) if ".comet" in x),
         sorted(x for x in adminmodules.granted(GAME_MODS["g_kick_not"]) if ".comet" in x),
         sorted(x for x in adminmodules.granted(["games.see"]) if ".comet" in x)),
        (["game.comet", "game.comet.kick"], ["game.comet", "game.comet.kick"], ["game.comet"]))
    return cs


def main():
    tmp = tempfile.mkdtemp(prefix="adminperms-")
    db = accounts.connect()
    accounts.set_admin_cred(db, OWNER, OWNER_PW)
    db.commit()
    for perm in list(PROBES) + ["news_publish"]:
        adminusers.create_mod(db, "m_" + perm, "pass for " + perm, [perm], by=OWNER)
    adminusers.create_mod(db, "m_news_both", "pass for both",
                          ["news_edit", "news_publish"], by=OWNER)
    for name, perms in GAME_MODS.items():
        adminusers.create_mod(db, name, "pass for " + name, perms, by=OWNER)
    db.close()
    proc, url = start_panel(tmp, SERVICES, free_port())
    try:
        game_perms(url)
        mods = {}
        for perm in list(PROBES) + ["news_publish", "news_both"]:
            c = Client(url)
            code = c.login("m_" + perm,
                           "pass for " + ("both" if perm == "news_both" else perm))
            if code != 200:
                chk("moderator %s signs in" % perm, code, 200)
            mods[perm] = c

        def status(c, probe):
            method, path, body = probe
            return c.call(path, body if method == "POST" else None)[0]

        for perm, probe in PROBES.items():
            chk("%s may use %s" % (perm, probe[1]), status(mods[perm], probe) != 403, True)
            others = sorted(p for p, c in mods.items()
                            if p not in (perm, "news_both") and status(c, probe) != 403)
            chk("...and no other single permission may", others, [])

        chk("a news writer cannot publish",
            mods["news_edit"].call("api/news/publish", {"dry_run": False})[0], 403)
        chk("...but can see what a publish would change",
            mods["news_edit"].call("api/news/publish", {"dry_run": True})[0] != 403, True)
        item = {"title": "Lap times", "date": "2026/10/22", "body": "Kart Club's grand prix"}
        code, got = mods["news_edit"].call("api/news/render", {"view": "snews", "items": [item]})
        chk("a news writer's preview renders, not a server error (%s)" % str(got)[:120],
            code != 500 and "error" not in got, True)
        chk("...and a view that does not exist is a 400",
            mods["news_edit"].call("api/news/render", {"view": "nope", "items": [item]})[0], 400)
        chk("a writer who may publish gets past the permission check",
            mods["news_both"].call("api/news/publish", {"dry_run": False})[0] != 403, True)
        chk("the PML moderator can load a page's art path",
            mods["pml"].call("art/wh000.pol.com/pml/top/nothing.png")[0] != 403, True)
        chk("the news writer can too (the preview draws with it)",
            mods["news_edit"].call("art/wh000.pol.com/pml/top/nothing.png")[0] != 403, True)
        chk("the activity-log moderator cannot",
            mods["audit"].call("art/wh000.pol.com/pml/top/nothing.png")[0], 403)
        for perm, c in mods.items():
            if c.call("api/mods")[0] != 403 or c.call(
                    "api/mods", {"action": "create", "username": "x" + perm,
                                 "perms": ["pml"]})[0] != 403:
                chk("moderator management stays the owner's (%s)" % perm, False, True)
        chk("no moderator can change the owner sign-in",
            sorted(p for p, c in mods.items()
                   if c.call("api/credentials",
                             {"username": "x", "password": "y" * 12})[0] != 403), [])
        chk("no moderator can see or change the Discord bot tokens",
            sorted(p for p, c in mods.items()
                   if c.call("api/bots")[0] != 403
                   or c.call("api/bots", {"bot": "tm", "action": "delete"})[0] != 403), [])
        chk("no moderator can read or switch the update channel",
            sorted(p for p, c in mods.items()
                   if c.call("api/deploy/channel")[0] != 403
                   or c.call("api/deploy/channel", {"channel": "stable"})[0] != 403), [])
        chk("no moderator can switch a paired game's update channel",
            sorted(p for p, c in mods.items()
                   if c.call("api/updates/channel", {"key": "kart", "channel": "dev"})[0] != 403), [])
        chk("no moderator can see, make or revoke dev access codes",
            sorted(p for p, c in mods.items()
                   if c.call("api/devcodes")[0] != 403
                   or c.call("api/devcodes", {"action": "make", "count": 1, "note": "x"})[0] != 403
                   or c.call("api/devcodes", {"action": "revoke", "code": "ABCD-EFGH-JKMN"})[0] != 403), [])
        _code, sess = mods["pml"].call("api/session")
        chk("the session lists the new permission's label",
            "PML Preview" in json.dumps(sess.get("perm_labels", {})), True)
    finally:
        stop(proc)
    print("all checks passed" if not bad else "%d check(s) FAILED" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
