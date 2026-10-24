#!/usr/bin/env python3
"""Games added to the admin panel by hand, and paired through their setup program (adminpair.py).

    python tools/admin_pair_test.py

A made-up game, Kart Club, serves the admin protocol through tools/olport.py (a tool page and an
announce action); a second one, Derby Night, stands at another address to be mistaken for it.

Checks that the owner can test, add, change and remove a game in Settings > Modules, that the
test says what the game describes or a plain sentence when it cannot, that a stored game is a
module like one in the environment (its tools proxied, its actions run, shown even when
POL_ADMIN_MODULES is an explicit list), that its token never comes back to the browser, and that
every change is in the activity log. Then pairing (docs/installs.md): the owner's one-time code,
POST /api/pair with no login (a wrong, used or expired code; a game that calls itself something
else; the happy path, where only the secret's hash is kept), POST /api/pair/update with the right
and a wrong secret and an unknown key, and unpairing, which ends the secret. Then a paired game's
updater (docs/installs.md, "Updates"): every rule of the "updater" block refused with a sentence,
stored as requested, shown on the Modules page, approved only by the owner and only as shown,
requested again when its folder, command or timeout changes, revoked, taken away with null, and
gone with an unpair or a remove, each step in the activity log. Then its update channels
(docs/installs.md, "Update channels"): every rule of "channels" refused, stored and shown, part
of what is approved (a change makes it requested again, a re-send keeps it), the id of an updater
without channels unchanged by them, and the switch the owner's only. A moderator gets 403
for everything here, /api/updates included. Then the rate limit (five tries a minute from one
address, then 429) on a panel with the default, and a panel without the PlayOnline core
(POL_ADMIN_CORE=none) pairing the same way. Throwaway PostgreSQL (tools/pgtest.py); the panels run
as subprocesses.
"""
import hashlib
import json
import os
import re
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.abspath(os.path.join(HERE, os.pardir, "services"))
sys.path.insert(0, HERE)
sys.path.insert(0, SERVICES)

import pgtest  # noqa: E402

pgtest.use_fresh_database()
for _k in ("POL_VALKEY_URL", "POL_ADMIN_MODULES_DIR", "POL_ADMIN_GAMES_EXTRA", "POL_DEPLOYER_URL",
           "POL_DEPLOYER_TOKEN", "OPENLOBBY_REVISION", "POL_ADMIN_PAIR_TRIES"):
    os.environ.pop(_k, None)
for _k in [k for k in os.environ if k.startswith("POL_ADMIN_GAME_")]:
    os.environ.pop(_k)

import olport  # noqa: E402

TOKEN = "kart-token-0123456789"
NEW_TOKEN = "kart-token-after-setup-9876"
SAID = []

kart = olport.AdminPort("kart", "Kart Club", short="Kart", platform="PC", token=TOKEN)
derby = olport.AdminPort("derby", "Derby Night", token="derby-token-0123456789")


@kart.status
def status():
    return {"up": True, "online": 3, "summary": "3 racing"}


@kart.action("banner", "Banner message", kind="announce", fields=[{"id": "text", "label": "Message"}])
def banner(params, actor):
    SAID.append(params.get("text"))
    return True, "Shown in every lobby."


@kart.tool("laps", "Lap times", "laps")
def laps(method, sub, query, body, actor):
    return 200, "text/html; charset=utf-8", b"<h1>Lap times</h1>"


import accounts  # noqa: E402
import adminpair  # noqa: E402
import adminusers  # noqa: E402
from admin_http import Client, free_port, start_panel, stop  # noqa: E402

OWNER, OWNER_PW = "op", "kart club owner 1"
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


def audit_since(n):
    return [(r["actor"], r["action"], bool(r["ok"]))
            for r in sql("SELECT * FROM admin_audit WHERE id > %s ORDER BY id", (n,))]


def audit_top():
    return (sql("SELECT coalesce(max(id), 0) AS n FROM admin_audit") or [{"n": 0}])[0]["n"]


def seed():
    db = accounts.connect()
    accounts.set_admin_cred(db, OWNER, OWNER_PW)
    db.commit()
    adminusers.create_mod(db, "mod_set", "pass for mod_set", ["audit", "settings"], by=OWNER)
    db.close()


def raw_get(client, path):
    """The body as text, to look for what must not be in it."""
    import urllib.request
    return client.op.open(urllib.request.Request(client.url + path), timeout=15).read().decode()


def modules_of(client):
    code, r = client.call("api/modules?fresh=1")
    return {m["key"]: m for m in r.get("modules", [])}, r


def hand_added(owner, mod, kport, dport, dead):
    kurl = "http://127.0.0.1:%d" % kport
    print("Add a game, by hand")
    for path, body in (("api/modules/test", {"key": "kart", "url": kurl, "token": TOKEN}),
                       ("api/modules/game", {"action": "save", "key": "kart", "url": kurl, "token": TOKEN}),
                       ("api/pair/code", {})):
        chk("a moderator cannot POST %s" % path, mod.call(path, body)[0], 403)
    chk("...nor read the update notices", mod.call("api/updates")[0], 403)
    code, r = owner.call("api/modules/test", {"key": "Kart!", "url": kurl, "token": TOKEN})
    chk("a key that does not fit: 400 with a sentence", (code, "lower-case" in r.get("error", "")), (400, True))
    code, r = owner.call("api/modules/test", {"key": "kart", "url": "kart:8820", "token": TOKEN})
    chk("an address without http://: 400", (code, "http://" in r.get("error", "")), (400, True))
    code, r = owner.call("api/modules/test", {"key": "kart", "url": kurl, "token": "wrong-token-000"})
    chk("a wrong token: the game refused it", (code, r.get("error")),
        (400, "The game at %s refused that token." % kurl))
    code, r = owner.call("api/modules/test", {"key": "kart", "url": dead, "token": TOKEN})
    chk("nothing answering: said so", (code, r.get("error", "").startswith("Nothing answered at %s" % dead)),
        (400, True))
    code, r = owner.call("api/modules/test", {"key": "kart", "url": "http://127.0.0.1:%d" % dport,
                                              "token": "derby-token-0123456789"})
    chk("a game that calls itself something else", (code, "calls itself 'derby'" in r.get("error", "")),
        (400, True))
    code, r = owner.call("api/modules/test", {"key": "kart", "url": kurl + "/", "token": TOKEN})
    g = r.get("game") or {}
    chk("the test: what the game describes", (code, g.get("title"), [t["label"] for t in g.get("tools", [])],
                                              [a["label"] for a in g.get("actions", [])]),
        (200, "Kart Club", ["Lap times"], ["Banner message"]))
    chk("a test stores nothing", sql("SELECT count(*) AS n FROM admin_game")[0]["n"], 0)

    n = audit_top()
    code, r = owner.call("api/modules/game", {"action": "save", "key": "kart", "url": kurl, "token": "wrong-token-000"})
    chk("save tests first: a refused token is not stored",
        (code, sql("SELECT count(*) AS n FROM admin_game")[0]["n"]), (400, 0))
    code, r = owner.call("api/modules/game", {"action": "save", "key": "kart", "url": kurl + "/", "token": TOKEN})
    chk("save", (code, r.get("title"), r.get("added")), (200, "Kart Club", True))
    row = sql("SELECT * FROM admin_game WHERE key = 'kart'")[0]
    chk("  stored by hand, by the owner", (row["url"], row["token"], row["title"], row["how"], row["added_by"],
                                           row["pairing_hash"]), (kurl, TOKEN, "Kart Club", "manual", OWNER, None))
    chk("  both in the activity log", audit_since(n),
        [(OWNER, "could not add the game kart", False), (OWNER, "added Kart Club by hand", True)])

    mods, r = modules_of(owner)
    k = mods.get("kart") or {}
    chk("a stored game is a module, with its tools and actions",
        ([t["label"] for t in k.get("tools", [])], [a["label"] for a in k.get("actions", [])], k.get("has_tools")),
        (["Lap times"], ["Banner message"], True))
    chk("  shown although POL_ADMIN_MODULES names only derby", sorted(mods), ["kart"])
    chk("  its live status read with the stored token", ((k.get("live") or {}).get("online")), 3)
    found = {f["key"]: f for f in r.get("found", [])}
    chk("  the Modules page says where its address comes from", (found["kart"]["origin"], found["kart"]["port"]),
        ("game", "stored"))
    added = r.get("added") or []
    chk("  and lists it as added here, the token only as set",
        [(a["key"], a["title"], a["how"], a["token_set"], a["paired"], a["env"], "token" in a) for a in added],
        [("kart", "Kart Club", "manual", True, False, False, False)])
    chk("  the token is nowhere in what the browser gets", TOKEN in raw_get(owner, "api/modules?fresh=1"), False)
    code, page = owner.call("games/kart/laps")
    chk("its tool page is proxied", (code, "Lap times" in str(page)), (200, True))
    code, r = owner.call("api/modules/action", {"key": "kart", "action": "banner", "params": {"text": "Hello"}})
    chk("its action runs", (code, r.get("ok"), SAID[-1:]), (200, True, ["Hello"]))

    n = audit_top()
    code, r = owner.call("api/modules/game", {"action": "save", "key": "kart", "url": kurl, "token": ""})
    chk("saving again with no token keeps the token",
        (code, r.get("added"), sql("SELECT token FROM admin_game WHERE key = 'kart'")[0]["token"]),
        (200, False, TOKEN))
    code, r = owner.call("api/modules/test", {"key": "kart", "url": kurl, "token": ""})
    chk("...and a test with no token uses the stored one", code, 200)
    code, r = owner.call("api/modules/game", {"action": "unpair", "key": "kart"})
    chk("a game added by hand is not paired", (code, r.get("error")), (400, "Kart Club is not paired."))
    code, r = owner.call("api/modules/game", {"action": "remove", "key": "kart"})
    chk("remove", (code, sql("SELECT count(*) AS n FROM admin_game")[0]["n"]), (200, 0))
    chk("  gone from the modules", "kart" in modules_of(owner)[0], False)
    chk("  again: 404", owner.call("api/modules/game", {"action": "remove", "key": "kart"})[0], 404)
    chk("  all in the activity log", [a[1] for a in audit_since(n)],
        ["changed the address or token of Kart Club", "unpaired kart", "removed Kart Club from the panel",
         "removed kart from the panel"])


def pairing(owner, mod, anon, kport, dport):
    kurl = "http://127.0.0.1:%d" % kport
    print("pairing")
    n = audit_top()
    chk("a moderator cannot make a code", mod.call("api/pair/code", {})[0], 403)
    code, r = owner.call("api/pair/code", {})
    pc = r.get("code") or ""
    chk("the owner gets an 8-character code, 10 minutes",
        (code, bool(re.fullmatch("[%s]{8}" % adminpair.CODE_ALPHABET, pc)), r.get("ttl")), (200, True, 600))
    chk("  the panel keeps only its hash", sql("SELECT code_hash FROM admin_pair_code")[0]["code_hash"],
        hashlib.sha256(pc.encode()).hexdigest())
    chk("  waiting", owner.call("api/pair/code?code=" + pc)[1].get("state"), "waiting")
    chk("  a moderator cannot see it", mod.call("api/pair/code?code=" + pc)[0], 403)
    body = {"code": pc, "key": "kart", "title": "Kart Club", "url": kurl, "token": TOKEN}

    chk("not JSON: 400", anon.call("api/pair", {})[0], 400)
    code, r = anon.call("api/pair", dict(body, code="ZZZZZZZZ"))
    chk("a wrong code: 403", (code, r.get("error")),
        (403, "That code is wrong. Check it against the one the panel shows."))
    code, r = anon.call("api/pair", dict(body, url="http://127.0.0.1:%d" % dport, token="derby-token-0123456789"))
    chk("a game that calls itself something else: 400",
        (code, "calls itself 'derby'" in r.get("error", "")), (400, True))
    chk("  and the code is still good", owner.call("api/pair/code?code=" + pc)[1].get("state"), "waiting")
    code, r = anon.call("api/pair", dict(body, token="wrong-token-000"))
    chk("a token the game refuses: 400", (code, "refused that token" in r.get("error", "")), (400, True))
    code, r = anon.call("api/pair", dict(body, key="Kart"))
    chk("a key that does not fit: 400", code, 400)

    typed = (pc[:4] + "-" + pc[4:]).lower()
    code, r = anon.call("api/pair", dict(body, code=typed))
    secret = r.get("pairing") or ""
    chk("the code as typed (lower case, a dash): paired",
        (code, r.get("ok"), r.get("title"), len(secret) >= 43), (200, True, "Kart Club", True))
    row = sql("SELECT * FROM admin_game WHERE key = 'kart'")[0]
    chk("  stored as paired, the secret only as its hash",
        (row["how"], row["token"], row["url"], row["added_by"], row["pairing_hash"],
         secret in json.dumps(row, default=str)),
        ("paired", TOKEN, kurl, OWNER, hashlib.sha256(secret.encode()).hexdigest(), False))
    chk("  the code view says so", owner.call("api/pair/code?code=" + pc)[1],
        {"state": "used", "key": "kart", "title": "Kart Club"})
    mods, r = modules_of(owner)
    chk("  the game is a module now", [t["label"] for t in (mods.get("kart") or {}).get("tools", [])], ["Lap times"])
    chk("  and listed as paired", [(a["how"], a["paired"]) for a in r.get("added", [])], [("paired", True)])
    code, r = anon.call("api/pair", body)
    chk("the same code again: 403, used", (code, r.get("error")),
        (403, "That code was already used. Make a new one in the panel."))
    code, r = owner.call("api/pair/code", {})
    old = r["code"]
    db = accounts.connect()
    db.execute("UPDATE admin_pair_code SET expires_at = %s WHERE code_hash = %s",
               (time.time() - 1, hashlib.sha256(old.encode()).hexdigest()))
    db.commit()
    db.close()
    code, r = anon.call("api/pair", dict(body, code=old))
    chk("an expired code: 403", (code, r.get("error")), (403, "That code has expired. Make a new one in the panel."))
    chk("  the code view says expired", owner.call("api/pair/code?code=" + old)[1].get("state"), "expired")

    print("the setup program changes the token")
    upd = {"key": "kart", "pairing": secret, "token": NEW_TOKEN}
    code, r = anon.call("api/pair/update", dict(upd, pairing="not-the-secret"))
    chk("a wrong secret: 403", (code, r.get("error")),
        (403, "That pairing secret is not this panel's for kart. Pair again."))
    code, r = anon.call("api/pair/update", dict(upd, key="derby"))
    chk("a key the panel does not have: 404", (code, r.get("error")), (404, "This panel has no game with the key derby."))
    code, r = anon.call("api/pair/update", dict(upd, url="ftp://x"))
    chk("a url that does not fit: 400", code, 400)
    kart.token = NEW_TOKEN
    code, r = anon.call("api/pair/update", upd)
    chk("the right secret: 200", (code, r), (200, {"ok": True}))
    chk("  the new token is stored", sql("SELECT token FROM admin_game WHERE key = 'kart'")[0]["token"], NEW_TOKEN)
    code, page = owner.call("games/kart/laps")
    chk("  and used", (code, "Lap times" in str(page)), (200, True))

    code, r = owner.call("api/modules/game", {"action": "unpair", "key": "kart"})
    chk("unpair", (code, sql("SELECT how, pairing_hash FROM admin_game WHERE key = 'kart'")[0]),
        (200, {"how": "manual", "pairing_hash": None}))
    code, r = anon.call("api/pair/update", dict(upd, token=TOKEN))
    chk("  the secret no longer works", code, 403)
    chk("  the game stays", "kart" in modules_of(owner)[0], True)

    got = audit_since(n)
    chk("the activity log", got, [
        (OWNER, "made a pairing code for a setup program", True),
        ("setup program", "a setup program tried to pair kart with a wrong code", False),
        ("setup program", "a setup program could not pair kart: The game at %s answered, but it calls itself "
         "'derby', the panel calls it 'kart'." % ("http://127.0.0.1:%d" % dport), False),
        ("setup program", "a setup program could not pair kart: The game at %s refused that token." % kurl, False),
        ("setup program", "paired Kart Club through its setup program", True),
        ("setup program", "a setup program tried to pair kart with a used code", False),
        (OWNER, "made a pairing code for a setup program", True),
        ("setup program", "a setup program tried to pair kart with an expired code", False),
        ("setup program", "a setup program tried to change the token of Kart Club with a wrong pairing secret", False),
        ("setup program", "a setup program tried to change the token of derby, which is not here", False),
        ("setup program", "Kart Club changed its token through its setup program", True),
        (OWNER, "unpaired Kart Club", True),
        ("setup program", "a setup program tried to change the token of Kart Club with a wrong pairing secret", False)])
    owner.call("api/modules/game", {"action": "remove", "key": "kart"})
    kart.token = TOKEN


UPDATER = {"folder": "C:\\Users\\kart\\Kart Club", "command": ["python", "setup.py", "--update", "--yes"],
           "timeout_minutes": 180}


def updater_row():
    rows = sql("SELECT * FROM admin_game_updater WHERE key = 'kart'")
    return rows[0] if rows else None


def added_updater(owner):
    added = {a["key"]: a for a in modules_of(owner)[1].get("added", [])}
    return (added.get("kart") or {}).get("updater")


def updaters(owner, mod, anon, kport):
    """A paired game's updater (docs/installs.md, "Updates", the deployer-run updater)."""
    kurl = "http://127.0.0.1:%d" % kport
    print("a paired game's updater")
    pc = owner.call("api/pair/code", {})[1]["code"]
    body = {"code": pc, "key": "kart", "title": "Kart Club", "url": kurl, "token": TOKEN}
    long_arg = "x" * 200
    for what, upd, said in (
            ("not an object", ["python"], "updater must be an object"),
            ("an unknown field", dict(UPDATER, env={"A": "1"}), "updater has fields the panel does not know: env."),
            ("a command that is a string", dict(UPDATER, command="python setup.py"), "must be a list of arguments"),
            ("an empty command", dict(UPDATER, command=[]), "must be a list of arguments"),
            ("a command that is not python", dict(UPDATER, command=["sh", "-c", "x"]), 'must start with "python"'),
            ("python with a path", dict(UPDATER, command=["/usr/bin/python3", "x"]), 'must start with "python"'),
            ("17 items", dict(UPDATER, command=["python"] + ["a"] * 16), "more than 16 items"),
            ("an item of 200 characters", dict(UPDATER, command=["python", long_arg]), "under 200 characters"),
            ("a NUL", dict(UPDATER, command=["python", "a\x00b"]), "NUL"),
            ("a number in the command", dict(UPDATER, command=["python", 3]), "must be a string"),
            ("a timeout of 4", dict(UPDATER, timeout_minutes=4), "from 5 to 240"),
            ("a timeout of 241", dict(UPDATER, timeout_minutes=241), "from 5 to 240"),
            ("a timeout of true", dict(UPDATER, timeout_minutes=True), "from 5 to 240"),
            ("a timeout as text", dict(UPDATER, timeout_minutes="60"), "from 5 to 240"),
            ("a relative folder", dict(UPDATER, folder="Kart Club"), "must be a full path"),
            ("a drive root", dict(UPDATER, folder="C:\\"), "not the root of a drive"),
            ("the Linux root", dict(UPDATER, folder="/"), "not the root of a drive"),
            ("a folder with ..", dict(UPDATER, folder="/srv/kart/../etc"), "must not have . or .."),
            ("a system folder", dict(UPDATER, folder="/etc/kart"), "system folder"),
            ("a folder with a control character", dict(UPDATER, folder="/srv/ka\nrt"), "control character"),
            ("a Windows name with a colon", dict(UPDATER, folder="C:\\Kart:Club"), "Windows does not allow")):
        code, r = anon.call("api/pair", dict(body, updater=upd))
        chk("updater with %s: 400 with a sentence" % what, (code, said in r.get("error", "")), (400, True))
    chk("  nothing paired, the code still good", (sql("SELECT count(*) AS n FROM admin_game")[0]["n"],
                                                   owner.call("api/pair/code?code=" + pc)[1].get("state")), (0, "waiting"))

    n = audit_top()
    code, r = anon.call("api/pair", dict(body, updater=UPDATER))
    secret = r.get("pairing") or ""
    chk("pairing with an updater: 200, updater requested", (code, r.get("updater")), (200, "requested"))
    row = updater_row() or {}
    chk("  stored as sent, requested", (row.get("folder"), json.loads(row.get("command") or "null"),
                                        row.get("timeout_minutes"), row.get("state"), row.get("approved_by")),
        (UPDATER["folder"], UPDATER["command"], 180, "requested", None))
    u = added_updater(owner) or {}
    chk("  the Modules page shows it, the command shell-quoted",
        (u.get("folder"), u.get("command"), u.get("shown"), u.get("timeout_minutes"), u.get("state"), len(u.get("id") or "")),
        (UPDATER["folder"], UPDATER["command"], "python setup.py --update --yes", 180, "requested", 16))
    chk("  in the activity log", [a[1] for a in audit_since(n)],
        ["paired Kart Club through its setup program",
         "Kart Club asked to update itself with python setup.py --update --yes"])

    print("approving it")
    chk("a moderator cannot approve", mod.call("api/modules/game", {"action": "approve_updater", "key": "kart",
                                                                   "id": u.get("id")})[0], 403)
    chk("...nor run it", mod.call("api/updates/run", {"key": "kart"})[0], 403)
    code, r = owner.call("api/updates/run", {"key": "kart"})
    chk("a requested updater does not run", (code, r.get("error")), (404, "That game has no approved updater."))
    code, r = owner.call("api/modules/game", {"action": "approve_updater", "key": "kart", "id": "0" * 16})
    chk("approving another updater than the one shown: 409", (code, "changed since" in r.get("error", ""),
                                                             updater_row()["state"]), (409, True, "requested"))
    n = audit_top()
    code, r = owner.call("api/modules/game", {"action": "approve_updater", "key": "kart", "id": u["id"]})
    row = updater_row()
    chk("the owner approves it", (code, r.get("state"), row["state"], row["approved_by"], bool(row["approved_at"])),
        (200, "approved", "approved", OWNER, True))
    chk("  in the activity log", audit_since(n), [(OWNER, "approved the updater of Kart Club", True)])

    print("the setup program sends it again")
    upd = {"key": "kart", "pairing": secret, "token": TOKEN}
    n = audit_top()
    code, r = anon.call("api/pair/update", dict(upd, updater=UPDATER))
    chk("the same updater: stays approved", (code, r.get("updater"), updater_row()["state"]), (200, "approved", "approved"))
    same = dict(UPDATER, folder="c:/Users/kart/Kart Club/")
    for _ in range(3):
        code, r = anon.call("api/pair/update", dict(upd, updater=same))
    chk("  sent again and again, the folder written another way: still approved",
        (code, r.get("updater"), updater_row()["state"], updater_row()["folder"]),
        (200, "approved", "approved", UPDATER["folder"]))
    chk("  and no activity log row for an identical re-send", audit_since(n), [])
    code, r = anon.call("api/pair/update", upd)
    chk("no updater in the request: no change", (code, r, updater_row()["state"]), (200, {"ok": True}, "approved"))
    code, r = anon.call("api/pair/update", dict(upd, updater=dict(UPDATER, command=["python3", "setup.py", "--update"])))
    chk("a changed command: requested again", (code, r.get("updater"), updater_row()["state"],
                                               updater_row()["approved_by"]), (200, "requested", "requested", None))
    chk("  and in the activity log, the change only", [a[1] for a in audit_since(n)],
        ["Kart Club asked to update itself with python3 setup.py --update"])
    for what, change in (("folder", {"folder": "/srv/kart"}), ("timeout", {"timeout_minutes": 30})):
        u = added_updater(owner)
        owner.call("api/modules/game", {"action": "approve_updater", "key": "kart", "id": u["id"]})
        code, r = anon.call("api/pair/update", dict(upd, updater=dict(UPDATER, command=["python3", "setup.py", "--update"],
                                                                      **change)))
        chk("a changed %s: requested again" % what, (code, r.get("updater"), updater_row()["state"]),
            (200, "requested", "requested"))
    code, r = anon.call("api/pair/update", dict(upd, updater={"folder": "/srv/kart", "command": ["python", "u.py"]}))
    chk("no timeout: 60 minutes", (code, updater_row()["timeout_minutes"]), (200, 60))
    code, r = anon.call("api/pair/update", dict(upd, updater={"folder": "/srv/kart", "command": "x"}))
    chk("a bad updater on pair/update: 400, nothing changed",
        (code, json.loads(updater_row()["command"])), (400, ["python", "u.py"]))
    u = added_updater(owner)
    owner.call("api/modules/game", {"action": "approve_updater", "key": "kart", "id": u["id"]})
    pc2 = owner.call("api/pair/code", {})[1]["code"]
    code, r = anon.call("api/pair", dict(body, code=pc2, updater={"folder": "/srv/kart", "command": ["python", "u.py"]}))
    chk("pairing again with the same updater: still approved", (code, r.get("updater"), updater_row()["state"]),
        (200, "approved", "approved"))
    secret = r.get("pairing") or secret
    upd["pairing"] = secret

    n = audit_top()
    chk("a moderator cannot revoke", mod.call("api/modules/game", {"action": "revoke_updater", "key": "kart"})[0], 403)
    code, r = owner.call("api/modules/game", {"action": "revoke_updater", "key": "kart"})
    chk("the owner revokes it", (code, updater_row()["state"]), (200, "requested"))
    chk("  in the activity log", audit_since(n), [(OWNER, "revoked the updater of Kart Club", True)])
    code, r = anon.call("api/pair/update", dict(upd, updater=None))
    chk("updater null: taken away", (code, updater_row()), (200, None))
    anon.call("api/pair/update", dict(upd, updater=UPDATER))
    code, r = owner.call("api/modules/game", {"action": "unpair", "key": "kart"})
    chk("unpairing removes it", (code, updater_row(), added_updater(owner)), (200, None, None))
    owner.call("api/modules/game", {"action": "remove", "key": "kart"})
    pc3 = owner.call("api/pair/code", {})[1]["code"]
    anon.call("api/pair", dict(body, code=pc3, updater=UPDATER))
    code, r = owner.call("api/modules/game", {"action": "remove", "key": "kart"})
    chk("removing the game removes it", (code, updater_row()), (200, None))


def channels(owner, mod, anon, kport):
    """An updater's "channels" (docs/installs.md, "Update channels")."""
    print("an updater's update channels")
    sys.path.insert(0, os.path.join(SERVICES, "deployer"))
    import gameupdater
    kurl = "http://127.0.0.1:%d" % kport
    pc = owner.call("api/pair/code", {})[1]["code"]
    body = {"code": pc, "key": "kart", "title": "Kart Club", "url": kurl, "token": TOKEN}
    for what, chans, said in (
            ("channels that are not a list", "stable,dev", "must be a list of 1 to 4 channel names"),
            ("no channels", [], "must be a list of 1 to 4 channel names"),
            ("five channels", ["stable", "dev", "beta", "next", "old"], "must be a list of 1 to 4"),
            ("an upper-case name", ["stable", "Dev"], "1 to 12 lower-case letters"),
            ("a 13-letter name", ["stable", "d" * 13], "1 to 12 lower-case letters"),
            ("a digit in a name", ["stable", "dev2"], "1 to 12 lower-case letters"),
            ("a name that is not text", ["stable", 2], "1 to 12 lower-case letters"),
            ("a name twice", ["stable", "dev", "dev"], "names a channel twice"),
            ("no stable", ["dev", "beta"], 'must include "stable"')):
        code, r = anon.call("api/pair", dict(body, updater=dict(UPDATER, channels=chans)))
        chk("updater with %s: 400 with a sentence" % what, (code, said in r.get("error", "")), (400, True))
    chk("  nothing paired", sql("SELECT count(*) AS n FROM admin_game")[0]["n"], 0)

    plain, _bad = gameupdater.check(UPDATER)
    old_id = hashlib.sha256(json.dumps([plain["folder"], plain["command"], plain["timeout_minutes"]],
                                       separators=(",", ":")).encode()).hexdigest()[:16]
    chk("an updater without channels keeps the id it had before channels existed",
        gameupdater.fingerprint(plain), old_id)

    withch = dict(UPDATER, channels=["stable", "dev"])
    n = audit_top()
    code, r = anon.call("api/pair", dict(body, updater=withch))
    secret = r.get("pairing") or ""
    chk("pairing with channels: 200, requested", (code, r.get("updater")), (200, "requested"))
    row = updater_row() or {}
    chk("  stored, on stable", (json.loads(row.get("channels") or "null"), row.get("channel")),
        (["stable", "dev"], "stable"))
    u = added_updater(owner) or {}
    chk("  the Modules page shows them and the channel it is on", (u.get("channels"), u.get("channel")),
        (["stable", "dev"], "stable"))
    chk("  its id is not the one without channels", u.get("id") != old_id, True)
    chk("  the activity log says which channels", sql("SELECT detail FROM admin_audit WHERE id > %s ORDER BY id",
                                                       (n,))[-1]["detail"].count('"dev"'), 1)
    chk("a switch before it is approved: 404", owner.call("api/updates/channel", {"key": "kart", "channel": "dev"})[0], 404)
    owner.call("api/modules/game", {"action": "approve_updater", "key": "kart", "id": u["id"]})
    chk("approved", updater_row()["state"], "approved")
    chk("a moderator cannot switch it", mod.call("api/updates/channel", {"key": "kart", "channel": "dev"})[0], 403)
    code, r = owner.call("api/updates/channel", {"key": "kart", "channel": "beta"})
    chk("a channel it does not declare: 400", (code, r.get("error")), (400, "Kart Club offers no such channel."))
    code, r = owner.call("api/updates/channel", {"key": "kart", "channel": "dev"})
    chk("  no deployer here: 503, and the channel stays", (code, updater_row()["channel"]), (503, "stable"))

    upd = {"key": "kart", "pairing": secret, "token": TOKEN}
    code, r = anon.call("api/pair/update", dict(upd, updater=dict(withch)))
    chk("the same channels sent again: still approved", (code, r.get("updater")), (200, "approved"))
    db = accounts.connect()
    db.execute("UPDATE admin_game_updater SET channel = 'dev' WHERE key = 'kart'")
    db.commit()
    db.close()
    n = audit_top()
    code, r = anon.call("api/pair/update", dict(upd, updater=dict(UPDATER, channels=["stable", "dev", "beta"])))
    chk("a changed list: requested again, the channel it is on kept",
        (code, r.get("updater"), updater_row()["state"], updater_row()["channel"]),
        (200, "requested", "requested", "dev"))
    chk("  in the activity log", [a[1] for a in audit_since(n)],
        ["Kart Club asked to update itself with python setup.py --update --yes"])
    u = added_updater(owner)
    owner.call("api/modules/game", {"action": "approve_updater", "key": "kart", "id": u["id"]})
    code, r = anon.call("api/pair/update", dict(upd, updater=dict(UPDATER)))
    chk("channels taken out: requested again, back to stable, no switch offered",
        (code, r.get("updater"), updater_row()["channels"], updater_row()["channel"],
         (added_updater(owner) or {}).get("channels")), (200, "requested", None, "stable", None))
    u = added_updater(owner)
    owner.call("api/modules/game", {"action": "approve_updater", "key": "kart", "id": u["id"]})
    code, r = owner.call("api/updates/channel", {"key": "kart", "channel": "stable"})
    chk("  a switch on an updater without channels: 400", (code, r.get("error")),
        (400, "Kart Club's updater offers no update channels."))
    owner.call("api/modules/game", {"action": "remove", "key": "kart"})


def updates(owner):
    print("update notices")
    code, r = owner.call("api/updates")
    chk("the owner reads them", (code, sorted(r)), (200, ["channel", "checked_at", "checking", "items"]))
    core = next((i for i in r.get("items", []) if i["key"] == "core"), {})
    chk("  the core's own check, GitHub out of reach: unknown", core.get("update"), "unknown")


def main():
    _h, kport = kart.start()
    _h, dport = derby.start()
    dead = "http://127.0.0.1:%d" % free_port()
    seed()
    base = {"POL_ADMIN_BIND": "127.0.0.1", "POL_ADMIN_MODULES": "derby", "POL_ADMIN_LIVE_TTL": "0",
            "POL_ADMIN_GITHUB_API": dead}
    tmp = tempfile.mkdtemp(prefix="adminpair-")
    proc, url = start_panel(tmp, SERVICES, free_port(), env_extra=dict(base, POL_ADMIN_PAIR_TRIES="1000"))
    try:
        owner, mod, anon = Client(url), Client(url), Client(url)
        chk("owner signs in", owner.login(OWNER, OWNER_PW), 200)
        chk("moderator signs in", mod.login("mod_set", "pass for mod_set"), 200)
        hand_added(owner, mod, kport, dport, dead)
        pairing(owner, mod, anon, kport, dport)
        updaters(owner, mod, anon, kport)
        channels(owner, mod, anon, kport)
        updates(owner)
    finally:
        stop(proc)

    print("five tries a minute from one address")
    proc, url = start_panel(tmp, SERVICES, free_port(), env_extra=base)
    try:
        anon = Client(url)
        codes = [anon.call("api/pair", {"code": "ZZZZZZZZ", "key": "kart", "title": "Kart Club",
                                        "url": "http://127.0.0.1:%d" % kport, "token": TOKEN})[0] for _ in range(5)]
        chk("five wrong codes: 403 each", codes, [403] * 5)
        code, r = anon.call("api/pair", {"code": "ZZZZZZZZ"})
        chk("the sixth: 429, with how long to wait", (code, "Try again in" in r.get("error", "")), (429, True))
        chk("  /api/pair/update counts the same tries", anon.call("api/pair/update", {"key": "kart"})[0], 429)
        owner = Client(url)
        owner.login(OWNER, OWNER_PW)
        chk("  the panel itself is not limited", owner.call("api/pair/code", {})[0], 200)
    finally:
        stop(proc)

    print("without the PlayOnline core")
    env = {"POL_ADMIN_CORE": "none", "POL_ADMIN_BIND": "127.0.0.1", "POL_ADMIN_PAIR_TRIES": "1000",
           "POL_ADMIN_LIVE_TTL": "0", "POL_ADMIN_GITHUB_API": dead}
    proc, url = start_panel(tmp, SERVICES, free_port(), env_extra=env)
    try:
        owner, anon = Client(url), Client(url)
        owner.login(OWNER, OWNER_PW)
        chk("no games yet", modules_of(owner)[0], {})
        pc = owner.call("api/pair/code", {})[1].get("code")
        code, r = anon.call("api/pair", {"code": pc, "key": "kart", "title": "Kart Club",
                                         "url": "http://127.0.0.1:%d" % kport, "token": TOKEN})
        chk("a setup program pairs", (code, r.get("title")), (200, "Kart Club"))
        mods, _r = modules_of(owner)
        chk("  the game is shown", (sorted(mods), (mods.get("kart") or {}).get("live", {}).get("online")),
            (["kart"], 3))
        code, r = anon.call("api/pair/update", {"key": "kart", "pairing": r.get("pairing"), "token": TOKEN})
        chk("  and changes its token", code, 200)
        code, r = owner.call("api/updates")
        chk("  the update notices: no core item without the core",
            (code, [i["key"] for i in r.get("items", [])]), (200, []))
    finally:
        stop(proc)
        kart.stop()
        derby.stop()
    print("admin_pair_test: %d failed" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
