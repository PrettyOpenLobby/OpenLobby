#!/usr/bin/env python3
"""People: the panel's record of one human across PlayOnline and the games (adminpeople.py).

    python tools/admin_people_test.py

A made-up game, Kart Club, describes itself through tools/olport.py: its racers sign in with
logins of its own (account system "kartid"), it searches them, answers the player lookup for
the logins it is given, and can kick a racer. Two PlayOnline accounts, one of which proved its
Kart Club login on the website (game_link) and linked Discord (discord_link).

Checks that the person of a PlayOnline account is made the first time it is opened, and once;
that its proven logins are read from game_link and discord_link; that a staff link needs a
reason, is in the activity log and shows who, when and why; that a login that is someone's
already is refused with that person's name, whether staff linked it or the player proved it;
that the Link dialog's search marks the logins taken; that the person page's player lookup is
given the staff links too; the People list (search by name, POL ID, handle, any linked login;
online first; the Online filter); notes; unlinking; merging and its refusals (the same person,
two PlayOnline accounts unless the owner forces it); the person page's actions; and the
permissions: without "people" every People route is 403 and no login leaks, with "people" but
not "people_link" the pages read and nothing changes, without accounts_view no POL ID shows.
Then the same panel without the PlayOnline core (POL_ADMIN_CORE=none, its account tables
dropped): a person is made by hand, a Kart Club login linked, and the game's characters shown.
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
for _k in ("POL_VALKEY_URL", "POL_ADMIN_MODULES_DIR", "POL_ADMIN_GAMES_EXTRA"):
    os.environ.pop(_k, None)
for _k in [k for k in os.environ if k.startswith("POL_ADMIN_GAME_")]:
    os.environ.pop(_k)

import olport  # noqa: E402

TOKEN = "kart-token-0123456789"
port = olport.AdminPort("kart", "Kart Club", token=TOKEN, accounts="kartid")
RACERS = [("17", "Arden", True), ("9", "Mira", False), ("31", "Tobin", None), ("44", "Wren", False)]
WHO, KICKS = [], []


@port.players
def players(q, limit):
    return [{"id": i, "label": n, "detail": "Class A", "online": on}
            for i, n, on in RACERS if q.lower() in n.lower() or q == i][:limit]


@port.player
def player(who):
    WHO.append(who)
    mine = who["links"].get("kartid", [])
    if not mine:
        return None
    by_id = {i: (n, on) for i, n, on in RACERS}
    return {"characters": [{"name": by_id.get(x["key"], (x["name"], None))[0], "detail": "Class A",
                            "online": by_id.get(x["key"], ("", None))[1] is True} for x in mine]}


@port.action("kick", "Kick a racer", kind="kick", fields=[{"id": "racer", "label": "Racer", "type": "player"}])
def kick(params, actor):
    KICKS.append(dict(params))
    return True, "Kicked."


import accounts  # noqa: E402
import adminusers  # noqa: E402
from admin_http import Client, free_port, start_panel, stop  # noqa: E402

OWNER, OWNER_PW = "op", "kart club owner 1"
POL_TABLES = ["member", "handle", "polid", "regcode", "session", "mail", "game_link", "login_alias",
              "web_login", "friend", "profile", "content", "discord_link"]
bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got, "" if ok else "  (want %r)" % (want,)))


def sql(q, args=()):
    db = accounts.connect()
    try:
        return [dict(r) for r in db.execute(q, args).fetchall()]
    finally:
        db.close()


def seed():
    db = accounts.connect()
    accounts.set_admin_cred(db, OWNER, OWNER_PW)
    db.commit()
    adminusers.create_mod(db, "mod_none", "pass for mod_none", ["game.kart"], by=OWNER)
    adminusers.create_mod(db, "mod_read", "pass for mod_read", ["people", "game.kart"], by=OWNER)
    adminusers.create_mod(db, "mod_link", "pass for mod_link",
                          ["people", "people_link", "accounts_view", "game.kart", "game.kart.kick"], by=OWNER)
    ids = {}
    for polid, handles in (("KARTPOL1", ["Arden", "ArdenAlt"]), ("KARTPOL2", ["Wren"])):
        accounts.create_polid(db, polid, "pw-account")
        mid = accounts.add_member(db, polid, polid.lower(), "pw-member")
        ids[polid] = mid
        for h in handles:
            accounts.set_handle(db, mid, h)
    db.execute("UPDATE member SET last_login_at = '2026-10-20T10:00:00Z' WHERE id = %s", (ids["KARTPOL1"],))
    db.execute("INSERT INTO session (token, member_id, created_at, expires_at)"
               " VALUES ('tok-wren', %s, '2026-10-22T00:00:00Z', %s)",
               (ids["KARTPOL2"], time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))))
    db.execute("INSERT INTO game_link (game, account_key, account_name, member_id, linked_at)"
               " VALUES ('kartid', '17', 'Arden', %s, '2026-10-14T12:00:00Z')", (ids["KARTPOL1"],))
    db.execute("INSERT INTO discord_link (discord_id, member_id, discord_name, linked_at)"
               " VALUES ('900001', %s, 'arden.k', %s)", (ids["KARTPOL1"], time.time()))
    db.commit()
    db.close()


def core_on(kport):
    tmp = tempfile.mkdtemp(prefix="adminpeople-")
    env = {"POL_ADMIN_MODULES": "kart", "POL_ADMIN_BIND": "127.0.0.1",
           "POL_ADMIN_GAME_KART_URL": "http://127.0.0.1:%d" % kport, "POL_ADMIN_GAME_KART_TOKEN": TOKEN}
    os.environ.update(env)          # so the moderators' game permissions are known here too
    import adminmodules
    adminmodules.load(reload=True)
    seed()
    proc, url = start_panel(tmp, SERVICES, free_port(), env_extra=env)
    try:
        owner, none, read, link = Client(url), Client(url), Client(url), Client(url)
        owner.login(OWNER, OWNER_PW)
        none.login("mod_none", "pass for mod_none")
        read.login("mod_read", "pass for mod_read")
        link.login("mod_link", "pass for mod_link")

        print("the person of a PlayOnline account")
        chk("no person rows before anyone looks", sql("SELECT count(*) AS n FROM person")[0]["n"], 0)
        code, arden = link.call("api/person?polid=KARTPOL1")
        chk("opening the account makes its person, named by its first handle",
            (code, arden.get("name"), arden.get("pol"), arden.get("polid"), arden.get("handles")),
            (200, "Arden", True, "KARTPOL1", ["Arden", "ArdenAlt"]))
        aid = arden["id"]
        code, again = owner.call("api/person?polid=KARTPOL1")
        chk("...once: opening it again finds the same person",
            (code, again.get("id"), sql("SELECT count(*) AS n FROM person")[0]["n"]), (200, aid, 1))
        chk("an account that is not there is 404", owner.call("api/person?polid=NOPE0000")[0], 404)
        got = {(l["system"], l["key"], l["how"], l["source"], l["title"]) for l in arden["links"]}
        chk("proven logins come from their sources: the account, game_link, discord_link",
            got, {("pol", "KARTPOL1", "proven", "account", "PlayOnline"),
                  ("kartid", "17", "proven", "website", "Kart Club"),
                  ("discord", "900001", "proven", "bot", "Discord")})
        chk("...and are not copied into person_link",
            sql("SELECT system, key FROM person_link ORDER BY id"), [{"system": "pol", "key": "KARTPOL1"}])
        chk("the Link dialog offers the games with a player search, then Discord",
            [(t["game"], t["system"], t["search"]) for t in arden["link_targets"]],
            [("kart", "kartid", True), ("", "discord", False)])

        print("staff links")
        code, d = link.call("api/person", {"action": "link", "id": aid, "system": "kartid", "key": "9",
                                           "name": "Mira", "why": "  "})
        chk("a link needs a reason", (code, "why" in d.get("error", "")), (400, True))
        code, d = link.call("api/person", {"action": "link", "id": aid, "system": "kartid", "key": "9",
                                           "name": "Mira", "why": "asked in Discord, same email"})
        chk("with one it is linked", (code, d.get("id")), (200, aid))
        a = sql("SELECT actor, action, target, detail FROM admin_audit WHERE target = %s ORDER BY id DESC LIMIT 1",
                ("person:%d" % aid,))
        chk("...and it is in the activity log",
            (a[0]["actor"], a[0]["action"], '"why": "asked in Discord, same email"' in (a[0]["detail"] or "")),
            ("mod_link", "linked Kart Club Mira to Arden", True))
        arden = link.call("api/person?id=%d" % aid)[1]
        st = [l for l in arden["links"] if l["how"] == "staff"]
        chk("the page shows it as linked by staff, with who and why",
            [(l["system"], l["key"], l["name"], l["by"], l["why"], isinstance(l["id"], int)) for l in st],
            [("kartid", "9", "Mira", "mod_link", "asked in Discord, same email", True)])
        chk("...and the history has it", [h["text"] for h in arden["history"]], ["linked Kart Club Mira to Arden"])
        chk("a PlayOnline account cannot be staff-linked: that is a merge",
            link.call("api/person", {"action": "link", "id": aid, "system": "pol", "key": "KARTPOL2",
                                     "why": "x"})[0], 400)

        print("one login, one person")
        code, tobin = link.call("api/person", {"action": "create", "name": "Tobin"})
        tid = tobin.get("id")
        chk("staff add a person by hand", (code, isinstance(tid, int)), (200, True))
        code, d = link.call("api/person", {"action": "link", "id": tid, "system": "kartid", "key": "9",
                                           "name": "Mira", "why": "same name"})
        chk("a login staff linked to another person is refused, naming them",
            (code, "Arden" in d.get("error", ""), (d.get("person") or {}).get("id")), (409, True, aid))
        code, d = link.call("api/person", {"action": "link", "id": tid, "system": "kartid", "key": "17",
                                           "name": "Arden", "why": "same name"})
        chk("...and one a player proved is theirs", (code, "Arden" in d.get("error", "")), (409, True))
        code, d = link.call("api/person", {"action": "link", "id": tid, "system": "discord", "key": "900001",
                                           "why": "same name"})
        chk("...Discord too", (code, "Arden" in d.get("error", "")), (409, True))
        code, d = link.call("api/person", {"action": "link", "id": aid, "system": "kartid", "key": "9",
                                           "why": "again"})
        chk("linking it twice to the same person says so", (code, "already" in d.get("error", "")), (409, True))
        code, d = link.call("api/people/logins?game=kart&q=")
        taken = {p["id"]: (p.get("person") or {}).get("name") for p in d.get("players", [])}
        chk("the Link dialog's search says whose logins are taken",
            (code, taken), (200, {"17": "Arden", "9": "Arden", "31": None, "44": None}))
        code, d = link.call("api/person", {"action": "link", "id": tid, "system": "kartid", "key": "31",
                                           "name": "Tobin", "why": "asked at the event"})
        chk("a free login links", code, 200)

        print("in the games")
        n = len(WHO)
        code, d = link.call("api/person/games?id=%d" % aid)
        who = WHO[-1] if len(WHO) > n else {}
        chk("the lookup is given the PlayOnline account and every login, staff links too",
            (code, who.get("polid"), who.get("handles"),
             sorted(x["key"] for x in who.get("links", {}).get("kartid", [])),
             [x["key"] for x in who.get("links", {}).get("discord", [])]),
            (200, "KARTPOL1", ["Arden", "ArdenAlt"], ["17", "9"], ["900001"]))
        chk("...and the game's characters come back",
            sorted(c["name"] for g in d.get("games", []) for c in g["characters"]), ["Arden", "Mira"])
        n = len(WHO)
        code, d = link.call("api/person/games?id=%d" % tid)
        chk("a person without a PlayOnline account is looked up by their links alone",
            (code, WHO[-1]["polid"] if len(WHO) > n else None, WHO[-1]["links"] if len(WHO) > n else None,
             [c["name"] for g in d.get("games", []) for c in g["characters"]]),
            (200, "", {"kartid": [{"key": "31", "name": "Tobin"}]}, ["Tobin"]))

        print("the People list")
        code, d = owner.call("api/people")
        rows = d.get("people", [])
        chk("everyone: the signed-in account first, a person without an account as one",
            (code, [(r["name"], r["polid"], r["online"]) for r in rows]),
            (200, [("Wren", "KARTPOL2", True), ("Arden", "KARTPOL1", False), ("Tobin", None, None)]))
        chk("...an account nobody opened yet has no person row", [r["id"] for r in rows][0], None)
        chk("...the accounts column marks staff links",
            [(a["title"], a["staff"]) for a in rows[1]["accounts"]],
            [("PlayOnline", False), ("Discord", False), ("Kart Club", False)])
        chk("...Tobin's", [(a["title"], a["staff"]) for a in rows[2]["accounts"]], [("Kart Club", True)])
        find = lambda c, q: [r["name"] for r in c.call("api/people?q=" + q)[1].get("people", [])]
        chk("search by a staff-linked login", find(owner, "mira"), ["Arden"])
        chk("...by a Discord name", find(owner, "arden.k"), ["Arden"])
        chk("...by a POL ID", find(owner, "KARTPOL2"), ["Wren"])
        chk("...by a handle", find(owner, "ArdenAlt"), ["Arden"])
        chk("...by a person's name", find(owner, "tob"), ["Tobin"])
        chk("...a LIKE wildcard is itself", find(owner, "%25"), [])
        chk("the Online filter", [r["name"] for r in owner.call("api/people?f=online")[1]["people"]], ["Wren"])

        print("notes and unlinking")
        chk("a note needs text", link.call("api/person", {"action": "note", "id": aid, "text": " "})[0], 400)
        chk("a note", link.call("api/person", {"action": "note", "id": aid,
                                                "text": "Helps new racers on Harbor Loop."})[0], 200)
        arden = link.call("api/person?id=%d" % aid)[1]
        chk("...shows on the page", [(x["by"], x["text"]) for x in arden["notes"]],
            [("mod_link", "Helps new racers on Harbor Loop.")])
        chk("...and is not repeated in the history", [h["text"] for h in arden["history"]],
            ["linked Kart Club Mira to Arden"])
        sid = [l for l in arden["links"] if l["how"] == "staff"][0]["id"]
        chk("unlinking needs a reason", link.call("api/person", {"action": "unlink", "id": aid, "link": sid})[0], 400)
        pid = sql("SELECT id FROM person_link WHERE system = 'pol'")[0]["id"]
        chk("the PlayOnline account is not a staff link to remove",
            link.call("api/person", {"action": "unlink", "id": aid, "link": pid, "why": "x"})[0], 400)
        chk("someone else's link is not found here",
            link.call("api/person", {"action": "unlink", "id": tid, "link": sid, "why": "x"})[0], 404)
        chk("unlink", link.call("api/person", {"action": "unlink", "id": aid, "link": sid, "why": "wrong racer"})[0], 200)
        arden = link.call("api/person?id=%d" % aid)[1]
        chk("...gone, and in the history",
            ([l["key"] for l in arden["links"] if l["system"] == "kartid"], arden["history"][0]["text"]),
            (["17"], "unlinked Kart Club Mira from Arden"))
        link.call("api/person", {"action": "link", "id": aid, "system": "kartid", "key": "9", "name": "Mira",
                                 "why": "it was right after all"})

        print("actions")
        acts = [(a["game"], a["id"], a["params"]) for a in arden["actions"]]
        chk("...nothing left to ask for it", [a["ask"] for a in arden["actions"]], [[]])
        chk("the page offers the game's kick, aimed at the person's login",
            acts, [("kart", "kick", {"racer": "17", "polid": "KARTPOL1", "handles": "Arden,ArdenAlt"})])
        code, d = link.call("api/modules/action", {"key": "kart", "action": "kick", "params": acts[0][2]})
        chk("...and it reaches the game", (code, KICKS[-1].get("racer")), (200, "17"))
        chk("a moderator without the kick permission is not offered it",
            read.call("api/person?id=%d" % aid)[1].get("actions"), [])

        print("merging")
        chk("not into itself", link.call("api/person", {"action": "merge", "id": aid, "other": aid})[0], 400)
        code, d = link.call("api/person", {"action": "merge", "id": aid, "other": tid})
        chk("Tobin into Arden", code, 200)
        arden = link.call("api/person?id=%d" % aid)[1]
        chk("...Tobin's links moved", sorted(l["key"] for l in arden["links"] if l["system"] == "kartid"),
            ["17", "31", "9"])
        code, d = link.call("api/person?id=%d" % tid)
        chk("...Tobin's page leads to Arden", (code, d.get("id"), d.get("name")), (200, aid, "Arden"))
        chk("...the merge and Tobin's history are on Arden's",
            [h["text"] for h in arden["history"]][:1] + sorted(h["text"] for h in arden["history"] if "Tobin" in h["text"]),
            ["merged Tobin into Arden", "added the person Tobin", "linked Kart Club Tobin to Tobin",
             "merged Tobin into Arden"])
        chk("...and Tobin is off the list", "Tobin" in find(owner, "tob"), False)
        code, d = link.call("api/person", {"action": "merge", "id": aid, "other_polid": "KARTPOL2"})
        chk("two PlayOnline accounts are two people", (code, d.get("both_pol")), (409, True))
        code, d = link.call("api/person", {"action": "merge", "id": aid, "other_polid": "KARTPOL2", "force": True})
        chk("...a moderator's force flag does not count", code, 409)
        code, d = owner.call("api/person", {"action": "merge", "id": aid, "other_polid": "KARTPOL2", "force": True})
        chk("...the owner's does", code, 200)
        rows = owner.call("api/people")[1]["people"]
        chk("...and the list shows Arden once, with both accounts' sign-ins",
            [(r["name"], r["online"]) for r in rows], [("Arden", True)])

        print("permissions")
        for path in ("api/people", "api/person?polid=KARTPOL1", "api/person?id=%d" % aid,
                     "api/person/games?id=%d" % aid, "api/people/logins?game=kart&q="):
            code, d = none.call(path)
            chk("without people: %s is 403 and carries no login" % path.split("?")[0],
                (code, any(x in str(d) for x in ("Arden", "900001", "KARTPOL"))), (403, False))
        chk("...nor can they change anything",
            none.call("api/person", {"action": "note", "id": aid, "text": "x"})[0], 403)
        code, d = read.call("api/person?id=%d" % aid)
        chk("people without people_link: the page reads, without the POL ID (no accounts_view)",
            (code, d.get("name"), d.get("polid"), d.get("handles"), d.get("can_link"),
             [l.get("hidden") for l in d["links"] if l["system"] == "pol"]),
            (200, "Arden", None, [], False, [True, True]))
        chk("...the list does not find people by POL ID", find(read, "KARTPOL1"), [])
        chk("...the list shows no POL IDs", {r["polid"] for r in read.call("api/people")[1]["people"]}, {None})
        chk("...no opening an account's person by POL ID", read.call("api/person?polid=KARTPOL1")[0], 404)
        for body in ({"action": "note", "id": aid, "text": "x"}, {"action": "create", "name": "Mira"},
                     {"action": "link", "id": aid, "system": "kartid", "key": "44", "why": "x"},
                     {"action": "merge", "id": aid, "other": tid}):
            chk("...%s is 403" % body["action"], read.call("api/person", body)[0], 403)
        chk("...and the Link dialog's search is 403", read.call("api/people/logins?game=kart&q=")[0], 403)
    finally:
        stop(proc)


def core_off(kport):
    print("without the PlayOnline core")
    pgtest.use_fresh_database()
    tmp = tempfile.mkdtemp(prefix="adminpeople-lite-")
    env = {"POL_ADMIN_CORE": "none", "POL_ADMIN_MODULES": "kart", "POL_ADMIN_BIND": "127.0.0.1",
           "POL_ADMIN_GAME_KART_URL": "http://127.0.0.1:%d" % kport, "POL_ADMIN_GAME_KART_TOKEN": TOKEN,
           "POL_ADMIN_USER": OWNER, "POL_ADMIN_PASSWORD": OWNER_PW}
    proc, url = start_panel(tmp, SERVICES, free_port(), env_extra=env)
    try:
        import psycopg
        with psycopg.connect(os.environ["POL_DATABASE_URL"], autocommit=True) as c:
            have = {r[0] for r in c.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
            for t in POL_TABLES:
                if t in have:
                    c.execute('DROP TABLE "%s" CASCADE' % t)
        chk("the panel made the person tables", {"person", "person_link", "person_note"} <= have, True)
        owner = Client(url)
        chk("owner signs in", owner.login(OWNER, OWNER_PW), 200)
        chk("an empty list", owner.call("api/people")[1], {"people": [], "core": False, "see_pol": False})
        chk("no person from a POL ID", owner.call("api/person?polid=KARTPOL1")[0], 404)
        code, d = owner.call("api/person", {"action": "create", "name": "Mira"})
        mid = d.get("id")
        chk("New person", (code, isinstance(mid, int)), (200, True))
        code, d = owner.call("api/people/logins?game=kart&q=mi")
        chk("the Link dialog searches the game", (code, [p["id"] for p in d.get("players", [])]), (200, ["9"]))
        chk("link a Kart Club login", owner.call("api/person", {"action": "link", "id": mid, "system": "kartid",
                                                               "key": "9", "name": "Mira",
                                                               "why": "she asked at the event"})[0], 200)
        code, d = owner.call("api/person?id=%d" % mid)
        chk("the page: no PlayOnline, the staff link",
            (code, d.get("pol"), d.get("core"), [(l["title"], l["how"]) for l in d.get("links", [])],
             [h["text"] for h in d.get("history", [])]),
            (200, False, False, [("Kart Club", "staff")], ["linked Kart Club Mira to Mira", "added the person Mira"]))
        code, d = owner.call("api/person/games?id=%d" % mid)
        chk("...and her characters", (code, [(g["title"], c["name"]) for g in d.get("games", [])
                                             for c in g["characters"]]), (200, [("Kart Club", "Mira")]))
        rows = owner.call("api/people?q=mira")[1]["people"]
        chk("the list finds her", [(r["name"], r["pol"], [a["title"] for a in r["accounts"]]) for r in rows],
            [("Mira", False, ["Kart Club"])])
        chk("a note", owner.call("api/person", {"action": "note", "id": mid, "text": "Runs the Friday cup."})[0], 200)
        chk("the people permissions are offered without the core",
            {"people", "people_link"} <= set(owner.call("api/mods")[1].get("perm_labels", {})), True)
    finally:
        stop(proc)


def main():
    _h, kport = port.start()
    try:
        core_on(kport)
        core_off(kport)
    finally:
        port.stop()
    print("all checks passed" if not bad else "%d check(s) FAILED" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
