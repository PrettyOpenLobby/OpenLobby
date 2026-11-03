#!/usr/bin/env python3
"""News > Community sites: fan sites for the Community Site Tool banner (admincomsites.py).

    python tools/admin_comsites_test.py

A panel without POL_COMTOOL_URL has no such page: the session says so, the list answers
enabled false and saving is refused with a sentence. A panel with one: the owner registers
sites, numbered from 1001 and never reusing a removed one's number, each answer carrying the two embed codes for that id; a name, an
address, a game, a skin or an owner account that does not fit is refused with a sentence; a
site is changed in place, an unknown one is 404; a moderator with comsites does all of it, a
moderator without gets 403; a removed site is gone, removing it again is 404; every change is
in the activity log; and the public view of the table carries no staff note and no owner.
Throwaway PostgreSQL (tools/pgtest.py); the panels run as subprocesses.
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
os.environ.pop("POL_COMTOOL_URL", None)

import accounts  # noqa: E402
import admincomsites  # noqa: E402
import adminusers  # noqa: E402
from admin_http import Client, free_port, start_panel, stop  # noqa: E402

OWNER, OWNER_PW = "op", "kart club owner 1"
SITE = "https://lobby.example.org"
bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got, "" if ok else "  (want %r)" % (want,)))


def sql(q, args=()):
    db = accounts.connect()
    try:
        cur = db.execute(q, args)
        rows = [dict(r) for r in cur.fetchall()] if cur.description else []
        db.commit()
        return rows
    finally:
        db.close()


def audit_since(n, want=0):
    """The log since row n. The panel writes it after the answer has gone out,
    so wait up to two seconds for `want` rows."""
    for _ in range(40):
        rows = sql("SELECT 1 FROM admin_audit WHERE id > %s", (n,))
        if len(rows) >= want:
            break
        time.sleep(0.05)
    return [(r["actor"], r["action"], bool(r["ok"]))
            for r in sql("SELECT * FROM admin_audit WHERE id > %s ORDER BY id", (n,))]


def audit_top():
    return (sql("SELECT coalesce(max(id), 0) AS n FROM admin_audit") or [{"n": 0}])[0]["n"]


def seed():
    db = accounts.connect()
    accounts.set_admin_cred(db, OWNER, OWNER_PW)
    db.commit()
    adminusers.create_mod(db, "mod_sites", "pass for mod_sites", ["comsites"], by=OWNER)
    adminusers.create_mod(db, "mod_news", "pass for mod_news", ["news_edit"], by=OWNER)
    lex = accounts.register_account(db, "Lex", "abc12345")
    db.commit()
    db.close()
    return lex["polid"]


def without(tmp, base_env):
    print("a panel without POL_COMTOOL_URL")
    proc, url = start_panel(tmp, SERVICES, free_port(), env_extra=base_env)
    try:
        owner = Client(url)
        owner.login(OWNER, OWNER_PW)
        chk("the session: no community sites", owner.call("api/session")[1].get("comtool"), False)
        code, r = owner.call("api/comsites")
        chk("  the list: not enabled, no rows", (code, r.get("enabled"), r.get("sites")), (200, False, []))
        code, r = owner.call("api/comsites", {"name": "Kart Fans", "url": "https://kart.example.org"})
        chk("  saving: refused with a sentence", (code, r.get("error")), (409, "Set POL_COMTOOL_URL first."))
    finally:
        stop(proc)


def with_site(tmp, base_env, lex):
    print("a panel with POL_COMTOOL_URL")
    proc, url = start_panel(tmp, SERVICES, free_port(), env_extra=dict(base_env, POL_COMTOOL_URL=SITE + "/"))
    try:
        owner, msites, mnews = Client(url), Client(url), Client(url)
        owner.login(OWNER, OWNER_PW)
        msites.login("mod_sites", "pass for mod_sites")
        mnews.login("mod_news", "pass for mod_news")
        chk("the session: community sites on", owner.call("api/session")[1].get("comtool"), True)
        code, r = owner.call("api/comsites")
        chk("  the list: enabled, where the banner comes from, seven games, nineteen skins, 12-18 custom",
            (code, r["enabled"], r["base"], len(r["games"]), r["skins"], r["custom"], r["sites"]),
            (200, True, SITE, 7, 19, [12, 13, 14, 15, 16, 17, 18], []))
        chk("  the games without an SE skin start on a custom one",
            {g["key"]: g["skin"] for g in r["games"] if g["key"] in ("tetra", "jan", "doc")},
            {"tetra": 12, "jan": 14, "doc": 16})

        n = audit_top()
        code, a = owner.call("api/comsites", {"name": "  Kart   Fans ", "url": "https://kart.example.org/",
                                              "game": "ffxi", "about": "Lap times and tips", "note": "met at the cup"})
        chk("a new site: number 1001, the name tidied, the game's own skin",
            (code, a.get("id"), a.get("name"), a.get("game"), a.get("skin"), a.get("listed"), a.get("featured")),
            (200, 1001, "Kart Fans", "ffxi", None, True, False))
        chk("  its code: the script form names the site and this id",
            a["code"]["script"], '<script src="%s/comtool/comTool"></script>\n<script>comTool(\'1001\');</script>' % SITE)
        chk("  and the tag form", a["code"]["tag"],
            '<div class="comtool" data-site="1001"></div>\n<script src="%s/comtool/comTool" async></script>' % SITE)
        chk("  made by the owner", a.get("made_by"), OWNER)
        chk("  in the activity log", audit_since(n, 1), [(OWNER, "saved a community site", True)])
        code, b = owner.call("api/comsites", {"name": "Derby Den", "url": "http://derby.example.org",
                                              "game": "fmo", "skin": 5, "featured": True, "owner_polid": lex})
        chk("a second: 1002, its skin, featured, owned by Lex",
            (code, b.get("id"), b.get("skin"), b.get("featured"), b.get("owner_polid")), (200, 1002, 5, True, lex))

        for what, body, err in (
                ("no name", {"name": " ", "url": "https://x.example.org"}, "Give the site a name of 1 to 60 characters."),
                ("a long name", {"name": "x" * 61, "url": "https://x.example.org"}, "Give the site a name of 1 to 60 characters."),
                ("an address without http", {"name": "X", "url": "kart.example.org"}, "The address must start with http:// or https://."),
                ("a script address", {"name": "X", "url": "javascript:alert(1)"}, "The address must start with http:// or https://."),
                ("an unknown game", {"name": "X", "url": "https://x.example.org", "game": "kart"}, "Pick one of the listed games."),
                ("skin 19", {"name": "X", "url": "https://x.example.org", "skin": 19}, "The skin is a number from 0 to 18."),
                ("a skin that is not a number", {"name": "X", "url": "https://x.example.org", "skin": "blue"}, "The skin is a number from 0 to 18."),
                ("an unknown account", {"name": "X", "url": "https://x.example.org", "owner_polid": "NOPE2345"}, "No account has that PlayOnline ID.")):
            code, r = owner.call("api/comsites", body)
            chk("refused: " + what, (code, r.get("error")), (400, err))
        chk("  none of them saved", len(owner.call("api/comsites")[1]["sites"]), 2)

        code, r = owner.call("api/comsites", dict(a, name="Kart Fans Club", skin=13, listed=False, note=""))
        chk("a change in place, to a custom skin (migration 0027 makes room for it)",
            (code, r.get("id"), r.get("name"), r.get("skin"), r.get("listed"), r.get("note")),
            (200, 1001, "Kart Fans Club", 13, False, ""))
        code, r = owner.call("api/comsites", {"id": 9999, "name": "Gone", "url": "https://x.example.org"})
        chk("  an unknown site: 404", (code, r.get("error")), (404, "That site is gone."))
        chk("  the list, newest first", [s["id"] for s in owner.call("api/comsites")[1]["sites"]], [1002, 1001])

        code, r = msites.call("api/comsites", {"name": "Mod Made", "url": "https://mod.example.org", "game": "fe"})
        chk("a moderator with comsites registers one", (code, r.get("id"), r.get("made_by")), (200, 1003, "mod_sites"))
        chk("  and lists them", len(msites.call("api/comsites")[1].get("sites") or []), 3)
        chk("a moderator without: 403 to look", mnews.call("api/comsites")[0], 403)
        chk("  and to save", mnews.call("api/comsites", {"name": "X", "url": "https://x.example.org"})[0], 403)
        chk("  and to remove", mnews.call("api/comsites/delete", {"id": 1001})[0], 403)

        n = audit_top()
        code, r = owner.call("api/comsites/delete", {"id": 1003})
        chk("removed", (code, r), (200, {"ok": True, "id": 1003}))
        chk("  again: 404", owner.call("api/comsites/delete", {"id": 1003})[0], 404)
        chk("  in the activity log, the failed try too", audit_since(n, 2),
            [(OWNER, "removed a community site", True), (OWNER, "removed a community site", False)])
        chk("  the next new site does not get the removed one's number",
            owner.call("api/comsites", {"name": "Next", "url": "https://next.example.org"})[1].get("id"), 1004)

        db = accounts.connect()
        try:
            rows = admincomsites.public_rows(db)
        finally:
            db.close()
        chk("the public view: no note, no owner, no maker",
            sorted(rows[0]), ["about", "featured", "game", "id", "listed", "name", "skin", "url"])
        chk("  every site in it, unlisted ones included (the banner still needs their record)",
            [(s["id"], s["listed"]) for s in rows], [(1001, False), (1002, True), (1004, True)])
    finally:
        stop(proc)


def main():
    lex = seed()
    tmp = tempfile.mkdtemp(prefix="admincomsites-")
    base_env = {"POL_ADMIN_BIND": "127.0.0.1", "POL_ADMIN_LIVE_TTL": "0",
                "POL_ADMIN_GITHUB_API": "http://127.0.0.1:%d" % free_port(), "POL_ADMIN_UPDATE_REPO": "off"}
    without(tmp, base_env)
    with_site(tmp, base_env, lex)
    print("admin_comsites_test: %d failed" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
