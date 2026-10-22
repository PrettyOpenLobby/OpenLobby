#!/usr/bin/env python3
"""The Inbox: every source's threads in one list (admininbox.py, GET /api/inbox).

    python tools/admin_inbox_test.py

A made-up game, Kart Club, serves a help desk and a player search through tools/olport.py; its
racers sign in with logins of their own (account system "kartid"). A second made-up game, Boat
Club, has a help desk that fails. The PlayOnline core has two GM calls, a Report User report and
a bug report on disk, and two accounts: Arden proved the Kart Club login on the website
(game_link), Wren has a mailbox. Tobin is a person staff made by hand, with a Kart Club login.

Checks that the sources merge into one list, newest activity first; that each row is resolved
to its person (a PlayOnline account through its handle or mailbox, a game login through a staff
link or a proven game_link) or shows the raw login; the filters (source, game, state, person)
and paging; that a source that fails is one line in "problems"; that GM calls stay confidential
(no subject or text in the list, and nothing at all for a moderator without "gm"); that names
need "people"; the People list's possible duplicates; renaming a person; and the same panel
without the PlayOnline core, where the Inbox is the help desks alone.
Throwaway PostgreSQL (tools/pgtest.py); the panel runs as a subprocess.
"""
import json
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

NOW = int(time.time())
TOKEN = "kart-token-0123456789"
SECRET = "ZZQ my neighbour cheats"
kart = olport.AdminPort("kart", "Kart Club", token=TOKEN, accounts="kartid")
boat = olport.AdminPort("boat", "Boat Club", token=TOKEN)
THREADS = {
    "7": {"subject": "Stuck in the pit lane", "who": "Arden", "at": NOW - 3000, "closed": False, "waiting": True},
    "8": {"subject": "Lap times", "who": "Tobin", "player": "31", "at": NOW - 90000, "last": NOW - 80000,
          "closed": False, "waiting": False},
    "9": {"subject": "Old one", "who": "Nobody", "at": NOW - 200000, "closed": True, "waiting": False},
}
RACERS = [("17", "Arden"), ("31", "Tobin"), ("50", "Nobody")]


def _list(open_only):
    return [dict(t, id=k, count=1) for k, t in THREADS.items() if not (open_only and t["closed"])]


def _thread(tid):
    t = THREADS.get(tid)
    return t and {"id": tid, "subject": t["subject"], "closed": t["closed"], "who": {"name": t["who"], "online": None},
                  "messages": [{"from": "player", "text": "help", "at": t["at"]}]}


kart.help_desk(_list, _thread, lambda tid, text, close, actor: (True, "Sent."), lambda tid, actor: (True, "Closed."))


@kart.players
def players(q, limit):
    return [{"id": i, "label": n, "online": None} for i, n in RACERS if q.lower() in n.lower() or q == i][:limit]


def _boat_list(open_only):
    raise RuntimeError("the boat desk is broken")


boat.help_desk(_boat_list, lambda tid: None, lambda *a: (False, "no"), lambda *a: (False, "no"))

import accounts  # noqa: E402
import adminusers  # noqa: E402
from admin_http import Client, free_port, start_panel, stop  # noqa: E402

OWNER, OWNER_PW = "op", "kart club owner 1"
bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got, "" if ok else "  (want %r)" % (want,)))


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def seed_files(tmp):
    gm = os.path.join(tmp, "gm-calls")
    os.makedirs(gm, exist_ok=True)
    for name, handle, subject, at in (("gm-20261022T100000-1.json", "Arden", SECRET, NOW - 1000),
                                      ("gm-20261022T090000-2.json", "Wren", "lost my items", NOW - 5000)):
        with open(os.path.join(gm, name), "w", encoding="utf-8") as f:
            json.dump({"handle": handle, "subject": subject, "body": SECRET + " body", "received_at": iso(at),
                       "request_no": 1, "peer": "192.0.2.10:1000"}, f)
    reports = os.path.join(tmp, "reports")
    os.makedirs(reports, exist_ok=True)
    with open(os.path.join(reports, "20261022T080000-1.json"), "w", encoding="utf-8") as f:
        json.dump({"received_at": iso(NOW - 7000), "from": "wren@pol.com", "subject": "Harassment in the lobby",
                   "fields": {"suspect": "Kaze", "application": "Kart Club", "explanation": "rude"}}, f)
    issues = os.path.join(tmp, "issues", "20261022T070000Z-PC-a1")
    os.makedirs(issues, exist_ok=True)
    with open(os.path.join(issues, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"id": "20261022T070000Z-PC-a1", "received_at": iso(NOW - 10000), "handle": "Arden",
                   "title": "PlayOnline Viewer", "description": "Crash after the cutscene\nmore",
                   "client_files": [{"name": "a.log", "bytes": 3}], "server_files": []}, f)
    return {"POL_GM_CALL_DIR": gm, "POL_GMD_TICKET_DIR": gm, "POL_REPORT_DIR": reports,
            "POL_ISSUE_DIR": os.path.join(tmp, "issues")}


def seed_db():
    db = accounts.connect()
    accounts.set_admin_cred(db, OWNER, OWNER_PW)
    db.commit()
    adminusers.create_mod(db, "mod_all", "pass for mod_all", ["gm", "reports", "people", "people_link",
                                                              "accounts_view", "game.kart", "game.kart.help"], by=OWNER)
    adminusers.create_mod(db, "mod_nogm", "pass for mod_nogm", ["reports", "people", "game.kart",
                                                                "game.kart.help"], by=OWNER)
    adminusers.create_mod(db, "mod_help", "pass for mod_help", ["game.kart", "game.kart.help"], by=OWNER)
    adminusers.create_mod(db, "mod_none", "pass for mod_none", ["accounts_view"], by=OWNER)
    ids = {}
    for polid, handle in (("KARTPOL1", "Arden"), ("KARTPOL2", "Wren")):
        accounts.create_polid(db, polid, "pw-account")
        ids[polid] = accounts.add_member(db, polid, polid.lower(), "pw-member")
        accounts.set_handle(db, ids[polid], handle)
    db.execute("UPDATE member SET mail_address = 'wren@pol.com' WHERE id = %s", (ids["KARTPOL2"],))
    db.execute("INSERT INTO game_link (game, account_key, account_name, member_id, linked_at)"
               " VALUES ('kartid', '17', 'Arden', %s, '2026-10-14T12:00:00Z')", (ids["KARTPOL1"],))
    db.execute("INSERT INTO discord_link (discord_id, member_id, discord_name, linked_at)"
               " VALUES ('900001', %s, 'arden.k', %s)", (ids["KARTPOL1"], time.time()))
    db.commit()
    db.close()


def rows_of(d):
    return [(t["source"], t["id"]) for t in d.get("threads", [])]


def main():
    _h, kport = kart.start()
    _h2, bport = boat.start()
    tmp = tempfile.mkdtemp(prefix="admininbox-")
    env = {"POL_ADMIN_MODULES": "kart,boat", "POL_ADMIN_BIND": "127.0.0.1",
           "POL_ADMIN_GAME_KART_URL": "http://127.0.0.1:%d" % kport, "POL_ADMIN_GAME_KART_TOKEN": TOKEN,
           "POL_ADMIN_GAME_BOAT_URL": "http://127.0.0.1:%d" % bport, "POL_ADMIN_GAME_BOAT_TOKEN": TOKEN}
    env.update(seed_files(tmp))
    os.environ.update(env)
    import adminmodules
    adminmodules.load(reload=True)
    seed_db()
    proc, url = start_panel(tmp, SERVICES, free_port(), env_extra=env)
    try:
        owner, mall, nogm, mhelp, mnone = (Client(url) for _ in range(5))
        owner.login(OWNER, OWNER_PW)
        mall.login("mod_all", "pass for mod_all")
        nogm.login("mod_nogm", "pass for mod_nogm")
        mhelp.login("mod_help", "pass for mod_help")
        mnone.login("mod_none", "pass for mod_none")
        tobin = mall.call("api/person", {"action": "create", "name": "Tobin"})[1]["id"]
        mall.call("api/person", {"action": "link", "id": tobin, "system": "kartid", "key": "31", "name": "Tobin",
                                 "why": "signed up at the Friday cup"})

        print("one list from every source")
        code, d = owner.call("api/inbox?state=all")
        chk("every source, newest activity first", (code, rows_of(d)),
            (200, [("gm", "gm-20261022T100000-1"), ("help", "7"), ("gm", "gm-20261022T090000-2"),
                   ("report", "20261022T080000-1"), ("issue", "20261022T070000Z-PC-a1"), ("help", "8"),
                   ("help", "9")]))
        chk("...the sources the caller may read", [s["key"] for s in d["sources"]], ["gm", "report", "issue", "help"])
        chk("a help desk that fails is one line, and the rest still show",
            [p for p in d["problems"] if "Boat Club" in p] != [] and len(d["threads"]) == 7, True)
        by = {(t["source"], t["id"]): t for t in d["threads"]}
        chk("states: waiting, answered, closed",
            [(by[k]["state"], by[k]["waiting"]) for k in (("help", "7"), ("help", "8"), ("help", "9"))],
            [("open", True), ("answered", False), ("closed", False)])
        chk("the newest message counts as the activity", by[("help", "8")]["at"], NOW - 80000)
        arden = by[("help", "7")]["person"]
        chk("a game login the player proved is their person", (arden or {}).get("name"), "Arden")
        chk("...the same person as their GM call and their bug report",
            (by[("gm", "gm-20261022T100000-1")]["person"], by[("issue", "20261022T070000Z-PC-a1")]["person"]),
            (arden, arden))
        chk("a login staff linked is that person's", by[("help", "8")]["person"], {"id": tobin, "name": "Tobin"})
        chk("a report is its sender's person", (by[("report", "20261022T080000-1")]["person"] or {}).get("name"), "Wren")
        chk("...and says who it is about", by[("report", "20261022T080000-1")]["about"], "Kaze")
        nob = by[("help", "9")]
        chk("a login nobody has shows as it is, ready to link",
            (nob["person"], nob["who"], nob["login"]["system"], nob["login"]["search"]),
            (None, "Nobody", "kartid", True))
        gm = by[("gm", "gm-20261022T100000-1")]
        chk("a GM call's subject and text stay in the GM call view",
            (gm["subject"], SECRET in json.dumps(d)), ("", False))

        print("filters and paging")
        chk("by source", rows_of(owner.call("api/inbox?state=all&source=help")[1]),
            [("help", "7"), ("help", "8"), ("help", "9")])
        chk("waiting on us (the default)", rows_of(owner.call("api/inbox")[1]),
            [("gm", "gm-20261022T100000-1"), ("help", "7"), ("gm", "gm-20261022T090000-2"),
             ("report", "20261022T080000-1"), ("issue", "20261022T070000Z-PC-a1")])
        chk("closed", rows_of(owner.call("api/inbox?state=closed")[1]), [("help", "9")])
        chk("answered", rows_of(owner.call("api/inbox?state=answered")[1]), [("help", "8")])
        code, d = owner.call("api/inbox?state=all&game=kart")
        chk("by game", (rows_of(d), [g["key"] for g in d["games"]]),
            ([("help", "7"), ("help", "8"), ("help", "9")], ["kart", "pol"]))
        p1 = owner.call("api/inbox?state=all&limit=3")[1]
        p2 = owner.call("api/inbox?state=all&limit=3&offset=3")[1]
        p3 = owner.call("api/inbox?state=all&limit=3&offset=6")[1]
        chk("pages of three: every row once, in order",
            (p1["total"], rows_of(p1) + rows_of(p2) + rows_of(p3) == rows_of(owner.call("api/inbox?state=all")[1]),
             len(rows_of(p3))), (7, True, 1))
        chk("the waiting count per source", p1["waiting"], {"gm": 2, "report": 1, "issue": 1, "help": 1})
        code, d = owner.call("api/inbox?state=all&person=%d" % arden["id"])
        chk("one person's cases from every source",
            (code, rows_of(d), d["person"]["name"]),
            (200, [("gm", "gm-20261022T100000-1"), ("help", "7"), ("issue", "20261022T070000Z-PC-a1")], "Arden"))
        chk("...a staff-linked person's", rows_of(owner.call("api/inbox?state=all&person=%d" % tobin)[1]),
            [("help", "8")])
        chk("a person who is not there", owner.call("api/inbox?person=999999")[0], 404)

        print("permissions")
        code, d = nogm.call("api/inbox?state=all")
        chk("without gm: no GM call at all, nothing of its text",
            (code, [s for s, _i in rows_of(d) if s == "gm"], SECRET in json.dumps(d), "Wren" in json.dumps(
                [t for t in d["threads"] if t["source"] == "gm"])), (200, [], False, False))
        chk("...also on a person's page",
            [s for s, _i in rows_of(nogm.call("api/inbox?state=all&person=%d" % arden["id"])[1])], ["help", "issue"])
        code, d = mhelp.call("api/inbox?state=all")
        chk("help permission only: the help desk alone, and no names from People",
            (code, [s["key"] for s in d["sources"]], {t["person"] is None for t in d["threads"]},
             [t["who"] for t in d["threads"]]), (200, ["help"], {True}, ["Arden", "Tobin", "Nobody"]))
        chk("...and no Link shortcut without people_link", any("search" in (t["login"] or {}) for t in d["threads"]),
            False)
        chk("...and no person filter", mhelp.call("api/inbox?state=all&person=%d" % tobin)[0], 403)
        code, d = mnone.call("api/inbox?state=all")
        chk("no source at all: an empty Inbox, not an error", (code, d["threads"], d["sources"]), (200, [], []))
        chk("signed out: refused", Client(url).call("api/inbox")[0], 401)

        print("People: duplicates and renaming")
        ardo = mall.call("api/person", {"action": "create", "name": "Ardo"})[1]["id"]
        mall.call("api/person", {"action": "link", "id": ardo, "system": "discord", "key": "900002",
                                 "name": "arden.k", "why": "asked in the help channel"})
        tob2 = mall.call("api/person", {"action": "create", "name": "tobin"})[1]["id"]
        code, d = mall.call("api/people?f=dupes")
        maybe = {r["id"]: [(m["id"], m["why"]) for m in r["maybe"]] for r in d["people"]}
        chk("the same Discord name on two people",
            (maybe.get(ardo), maybe.get(arden["id"])), ([(arden["id"], "same Discord name")], [(ardo, "same Discord name")]))
        chk("the same name on two people", (maybe.get(tob2), maybe.get(tobin)),
            ([(tobin, "same name")], [(tob2, "same name")]))
        chk("the whole list carries it too",
            [m["id"] for r in mall.call("api/people?q=Ardo")[1]["people"] if r["id"] == ardo for m in r["maybe"]],
            [arden["id"]])
        chk("merging one into the other", mall.call("api/person", {"action": "merge", "id": tobin, "other": tob2})[0], 200)
        chk("...and they are no longer listed", tob2 in [r["id"] for r in mall.call("api/people?f=dupes")[1]["people"]],
            False)
        chk("renaming a person", mall.call("api/person", {"action": "rename", "id": ardo, "name": "Ardo K"})[0], 200)
        hist = [h["text"] for h in mall.call("api/person?id=%d" % ardo)[1]["history"]]
        chk("...is in their history", "renamed Ardo to Ardo K" in hist, True)
        chk("...and needs people_link", nogm.call("api/person", {"action": "rename", "id": ardo, "name": "X"})[0], 403)
    finally:
        stop(proc)

    print("without the PlayOnline core")
    env["POL_ADMIN_CORE"] = "none"
    proc, url = start_panel(tmp, SERVICES, free_port(), env_extra=env)
    try:
        owner = Client(url)
        owner.login(OWNER, OWNER_PW)
        code, d = owner.call("api/inbox?state=all")
        chk("the help desks alone", (code, [s["key"] for s in d["sources"]], rows_of(d)),
            (200, ["help"], [("help", "7"), ("help", "8"), ("help", "9")]))
        chk("...a staff link still finds the person", d["threads"][1]["person"], {"id": tobin, "name": "Tobin"})
        chk("...a login only game_link knew is shown as it is", d["threads"][0]["person"], None)
        chk("...and the broken desk is still one line", any("Boat Club" in p for p in d["problems"]), True)
    finally:
        stop(proc)
        kart.stop()
        boat.stop()
    print("all checks passed" if not bad else "%d check(s) FAILED" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
