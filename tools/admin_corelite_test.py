#!/usr/bin/env python3
"""The admin panel without the PlayOnline core (POL_ADMIN_CORE=none): one game's own panel.

    python tools/admin_corelite_test.py

A self-hoster runs one game server that describes itself over the admin protocol; here that is
a made-up game, Kart Club, served by tools/olport.py with a status, an announce action, a tool
page, a calendar and a help desk. The panel starts on an EMPTY database and creates what it needs
itself, signs in with POL_ADMIN_PASSWORD alone, and runs with no live-state store (POL_VALKEY_URL
unset, so polcore.kv keeps it in memory) and no www tree. Once it is up, the PlayOnline tables
(accounts, handles, codes, sessions, mail...) are DROPPED, so any path that still reads them fails
here instead of on a self-hoster's server.

Checks that the login, moderators, activity log, settings, the game (its status, tool page,
actions), the calendar (the game's entries and the panel's own) and Game help all work; that the
maintenance warning reaches the game's announce action with no PlayOnline step; that every
PlayOnline route answers 404 with a plain sentence; and that no PlayOnline permission is listed,
held or grantable. Throwaway PostgreSQL (tools/pgtest.py); the panel runs as a subprocess.
"""
import glob
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.abspath(os.path.join(HERE, os.pardir, "services"))
sys.path.insert(0, HERE)

import pgtest  # noqa: E402

pgtest.use_fresh_database()
for _k in ("POL_VALKEY_URL", "POL_ADMIN_MODULES_DIR", "POL_ADMIN_GAMES_EXTRA"):
    os.environ.pop(_k, None)            # the in-memory store; nothing but the one game
for _k in [k for k in os.environ if k.startswith("POL_ADMIN_GAME_")]:
    os.environ.pop(_k)

import olport  # noqa: E402
from admin_http import Client, free_port, start_panel, stop  # noqa: E402

TOKEN = "kart-token-0123456789"
OWNER, OWNER_PW = "op", "kart club owner 1"
POL_PERMS = {"gm", "codes", "codes_void", "reports", "accounts_view", "accounts_manage",
             "accounts_delete", "news_edit", "news_publish", "pml"}

port = olport.AdminPort("kart", "Kart Club", short="Kart", platform="PC", token=TOKEN)
SAID, ANSWERS = [], []
NOW = int(time.time())
THREADS = {"7": {"subject": "Stuck in the pit lane", "who": "Arden", "at": NOW - 600, "closed": False,
                 "messages": [{"from": "player", "text": "My kart will not leave.", "at": NOW - 600}]}}


@port.status
def status():
    return {"up": True, "online": 3, "summary": "3 racing", "rooms": [{"name": "Lobby A", "players": 3}]}


@port.action("banner", "Banner message", kind="announce", fields=[{"id": "text", "label": "Message"}])
def banner(params, actor):
    SAID.append((params.get("text"), actor))
    return True, "Shown in every lobby."


@port.tool("laps", "Lap times", "laps")
def laps(method, sub, query, body, actor):
    return 200, "text/html; charset=utf-8", b"<h1>Lap times</h1><p>Best lap 1:02.4</p>"


@port.calendar
def calendar(lo, hi):
    return [{"id": "gp-1", "title": "Pumpkin Grand Prix", "start": NOW + 86400, "end": NOW + 90000,
             "kind": "event", "about": "Pumpkin karts only."}]


def _reply(tid, text, close, actor):
    ANSWERS.append((tid, text, close, actor))
    THREADS[tid]["messages"].append({"from": "staff", "text": text, "at": NOW})
    THREADS[tid]["closed"] = THREADS[tid]["closed"] or close
    return True, "Answer sent."


port.help_desk(
    lambda open_only: [{"id": k, "subject": t["subject"], "who": t["who"], "at": t["at"],
                        "closed": t["closed"], "waiting": t["messages"][-1]["from"] == "player",
                        "count": len(t["messages"])}
                       for k, t in THREADS.items() if not (open_only and t["closed"])],
    lambda tid: THREADS.get(tid) and {"id": tid, "subject": THREADS[tid]["subject"],
                                      "closed": THREADS[tid]["closed"],
                                      "who": {"name": THREADS[tid]["who"], "online": True},
                                      "messages": THREADS[tid]["messages"]},
    _reply, lambda tid, actor: (True, "Closed."))

#: Every PlayOnline route: refused without the core, whoever asks.
REFUSED_GET = ["api/accounts", "api/account-detail?polid=KART0001", "api/account-games?polid=KART0001",
               "api/account-clients?polid=KART0001", "api/account-footprint?polid=KART0001",
               "api/codes", "api/content-names", "api/gm-calls", "api/gm-desk", "api/gm-notices",
               "api/gm-players", "api/face-sheet", "api/reports", "api/issues", "api/issue-file",
               "api/news", "api/news/outputs", "api/news/art", "api/pml-list", "api/pml-load",
               "api/pml-refs", "api/pml-resolve", "api/page", "api/push/key", "api/bots",
               "art/menu/logo.png"]
REFUSED_POST = ["api/codes", "api/codes/random", "api/codes/void", "api/account-create",
                "api/account-password", "api/account-state", "api/account-info", "api/account-kick",
                "api/account-clear-token", "api/account-extmail", "api/account-delete", "api/grant",
                "api/revoke", "api/gm-control", "api/gm-say", "api/gm-ticket", "api/gm-notices",
                "api/gm-discord", "api/push/subscribe", "api/push/test", "api/push/unsubscribe",
                "api/triage", "api/report-reply", "api/news", "api/news/render", "api/news/publish",
                "api/pml-expand", "api/bots"]
#: The PlayOnline tables: dropped once the panel has made them, so a read of one fails.
POL_TABLES = ["member", "handle", "polid", "regcode", "session", "mail", "game_link", "login_alias",
              "web_login", "friend", "profile", "content"]

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got, "" if ok else "  (want %r)" % (want,)))


def drop_pol_tables():
    import psycopg
    with psycopg.connect(os.environ["POL_DATABASE_URL"], autocommit=True) as c:
        have = {r[0] for r in c.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
        for t in POL_TABLES:
            if t in have:
                c.execute('DROP TABLE "%s" CASCADE' % t)
        left = {r[0] for r in c.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
    return have, left


def main():
    tmp = tempfile.mkdtemp(prefix="admincorelite-")
    _h, kport = port.start()
    env = {"POL_ADMIN_CORE": "none", "POL_ADMIN_MODULES": "kart", "POL_ADMIN_BIND": "127.0.0.1",
           "POL_ADMIN_GAME_KART_URL": "http://127.0.0.1:%d" % kport, "POL_ADMIN_GAME_KART_TOKEN": TOKEN,
           "POL_ADMIN_USER": OWNER, "POL_ADMIN_PASSWORD": OWNER_PW}
    proc, url = start_panel(tmp, SERVICES, free_port(), env_extra=env)
    try:
        print("boot")
        have, left = drop_pol_tables()
        chk("the panel made its own tables on an empty database",
            {"admin_cred", "admin_moderator", "admin_audit", "admin_setting"} <= have, True)
        chk("the PlayOnline tables are gone for the rest of the test", set(POL_TABLES) & left, set())
        anon = Client(url)
        chk("signed out, the API wants a login", anon.call("api/accounts")[0], 401)
        chk("a wrong password is refused", anon.login(OWNER, "nope nope nope"), 401)

        owner = Client(url)
        chk("the owner signs in with POL_ADMIN_PASSWORD", owner.login(OWNER, OWNER_PW), 200)
        code, s = owner.call("api/session")
        labels = s.get("perm_labels") or {}
        chk("the session says there is no PlayOnline core", (code, s.get("core"), s.get("role")),
            (200, "none", "owner"))
        chk("no PlayOnline permission is listed", sorted(POL_PERMS & set(labels)), [])
        chk("...nor held by the owner", sorted(POL_PERMS & set(s.get("perms") or [])), [])
        chk("no built-in PlayOnline title's either", sorted(p for p in labels if p.startswith("game.")
                                                            and not p.startswith("game.kart")), [])
        chk("the panel's own and the game's remain",
            all(p in labels for p in ("calendar", "calendar_edit", "audit", "health", "settings",
                                      "game.kart", "game.kart.laps", "game.kart.banner", "game.kart.help")),
            True)

        print("the game")
        code, m = owner.call("api/modules")
        chk("only Kart Club loads", (code, [x["key"] for x in m.get("modules", [])],
                                     [x["key"] for x in m.get("found", [])]), (200, ["kart"], ["kart"]))
        kart = (m.get("modules") or [{}])[0]
        chk("...with its tool and its action", ([t["id"] for t in kart.get("tools", [])],
                                                [a["id"] for a in kart.get("actions", [])]),
            (["laps"], ["banner"]))
        chk("...and its live status", (kart.get("live") or {}).get("online"), 3)
        code, page = owner.call("games/kart/laps")
        chk("its tool page is proxied", (code, "Best lap 1:02.4" in str(page)), (200, True))
        code, out = owner.call("api/modules/action", {"key": "kart", "action": "banner",
                                                      "params": {"text": "Hello racers"}})
        chk("its action runs", (code, out.get("msg"), SAID[-1:]),
            (200, "Shown in every lobby.", [("Hello racers", OWNER)]))
        chk("the Games list has it", [g["key"] for g in owner.call("api/games")[1].get("games", [])], ["kart"])

        print("calendar")
        new = {"action": "add", "title": "Club night", "game": "kart", "kind": "community",
               "start": NOW + 2 * 86400, "end": NOW + 2 * 86400 + 7200, "about": "Bring a friend."}
        code, out = owner.call("api/calendar", new)
        chk("the panel's own entry is added", (code, out.get("ok")), (200, True))
        code, d = owner.call("api/calendar?from=%d&to=%d" % (NOW, NOW + 7 * 86400))
        chk("the game's entry and the panel's own show",
            (code, sorted(e["title"] for e in d.get("entries", [])), d.get("problems")),
            (200, ["Club night", "Pumpkin Grand Prix"], []))

        print("Game help")
        code, d = owner.call("api/help")
        chk("the game's desk is listed", (code, [(t["id"], t["game_title"]) for t in d.get("threads", [])]),
            (200, [("7", "Kart Club")]))
        code, out = owner.call("api/help", {"game": "kart", "id": "7", "action": "reply",
                                            "text": "Press start twice.", "close": True})
        chk("an answer reaches the game", (code, ANSWERS), (200, [("7", "Press start twice.", True, OWNER)]))

        print("maintenance warning")
        code, d = owner.call("api/maintenance")
        chk("no title pages, the game that can announce", (code, d.get("titles"), d.get("files"), d.get("games")),
            (200, [], [], {"kart": "Kart Club"}))
        code, d = owner.call("api/maintenance", {"action": "notice", "subject": "Down", "text": "Soon."})
        chk("no notice before Play to show", (code, d.get("core")), (404, "none"))
        n = len(SAID)
        t0 = time.time()
        code, d = owner.call("api/maintenance", {"action": "message", "subject": "Maintenance",
                                                 "text": "Back at 10:00 UTC.", "games": ["kart"]})
        chk("the warning reaches the game's announce, and nothing else",
            (code, d.get("games"), SAID[n:]),
            (200, {"kart": {"ok": True, "msg": "Shown in every lobby."}},
             [("Maintenance: Back at 10:00 UTC.", OWNER)]))
        chk("...at once, with no PlayOnline mail step to wait for", time.time() - t0 < 5, True)
        chk("a warning into no game is refused",
            owner.call("api/maintenance", {"action": "message", "subject": "x", "text": "y", "games": []})[0], 400)

        print("PlayOnline routes")
        leaks = []
        for p in REFUSED_GET:
            code, d = owner.call(p)
            if code != 404 or not isinstance(d, dict) or "without PlayOnline" not in d.get("error", ""):
                leaks.append(("GET", p, code))
        for p in REFUSED_POST:
            code, d = owner.call(p, {"polid": "KART0001", "code": "AAAA-BBBB-CCCC-DDDD-EEEE"})
            if code != 404 or not isinstance(d, dict) or "without PlayOnline" not in d.get("error", ""):
                leaks.append(("POST", p, code))
        chk("every one answers 404 with a plain sentence", leaks, [])
        code, d = owner.call("api/online")
        chk("who is online: no PlayOnline sessions", (code, d), (200, {"games": [], "accounts": None}))
        code, d = owner.call("api/health")
        chk("the Overview probes no PlayOnline ports", (code, d.get("services")), (200, []))
        code, d = owner.call("api/ops")
        chk("no server checker: the panel says so", (code, d), (200, {"available": False}))

        print("moderators")
        code, out = owner.call("api/mods", {"action": "create", "username": "racedesk",
                                            "password": "race desk pass 1",
                                            "perms": ["gm", "accounts_view", "codes", "game.kart",
                                                      "game.kart.help", "calendar"]})
        chk("a moderator is added", code, 200)
        row = next((x for x in owner.call("api/mods")[1].get("mods", []) if x["username"] == "racedesk"), {})
        chk("...without the PlayOnline permissions asked for", sorted(row.get("perms") or []),
            ["calendar", "game.kart", "game.kart.help"])
        mod = Client(url)
        chk("the moderator signs in", mod.login("racedesk", "race desk pass 1"), 200)
        code, s = mod.call("api/session")
        chk("...holding only what is left", sorted(s.get("perms") or []), ["calendar", "game.kart", "game.kart.help"])
        chk("...sees the game's desk", [t["game"] for t in mod.call("api/help?open=0")[1].get("threads", [])], ["kart"])
        chk("...not the accounts", mod.call("api/accounts")[0], 404)
        chk("...and cannot announce without the game's announce permission",
            mod.call("api/maintenance", {"action": "message", "subject": "x", "text": "y",
                                         "games": ["kart"]})[0], 502)
        chk("...which also leaves the game off the warning's list", mod.call("api/maintenance")[1].get("games"), {})

        print("activity log")
        rows = owner.call("api/audit")[1].get("rows") or []
        did = " | ".join(r.get("action", "") for r in rows)
        chk("the game's action, help answer and the warning are logged",
            all(w in did for w in ("banner message in Kart Club", "game help:", "announced maintenance in Kart Club")),
            True)
    finally:
        stop(proc)
        port.stop()
    log = ""
    for f in glob.glob(os.path.join(tmp, "admin-*.log")):
        with open(f, encoding="utf-8", errors="replace") as fh:
            log += fh.read()
    print("the panel's log")
    chk("it says the core is off", "core: none" in log, True)
    chk("no error, and no read of a PlayOnline table",
        [ln for ln in log.splitlines() if "Traceback" in ln or "does not exist" in ln or "rror" in ln], [])
    print("all checks passed" if not bad else "%d check(s) FAILED" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
