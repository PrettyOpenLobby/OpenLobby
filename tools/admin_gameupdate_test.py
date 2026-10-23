#!/usr/bin/env python3
"""The panel's Update button for paired games (docs/installs.md, "Updates").

    python tools/admin_gameupdate_test.py

A made-up game, Kart Club, stands in as a plain HTTP server speaking the admin protocol, so the
test sets exactly what it answers. Checks the game-applied update (path B): the "apply",
"state", "message" and "why" fields reaching the Updates list, POST /api/updates/apply sending
POST /openlobby/update with the token, the actor and an empty body, and passing on the game's
202, 409 and 503 with its message as given, any other status as "The game did not take the
update request (HTTP n)"; the owner and a moderator with game.kart.update may press it, one
without may not; then the status poll (/api/updates/state): updating with its message, done
when running.commit equals latest.commit and the update is "current", a failure written to the
activity log once, and a game that stays away (POL_ADMIN_UPDATE_LOST shortened) "has not come
back". Then the deployer-run updater (path A): the panel offers it only once the owner approved
it and a deployer is set up, and what the stand-in deployer receives is the stored, approved
record, never what the browser sent. Throwaway PostgreSQL (tools/pgtest.py); the panel runs as a
subprocess.
"""
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.abspath(os.path.join(HERE, os.pardir, "services"))
sys.path.insert(0, HERE)
sys.path.insert(0, SERVICES)

import pgtest  # noqa: E402

pgtest.use_fresh_database()
for _k in ("POL_VALKEY_URL", "POL_ADMIN_MODULES_DIR", "POL_ADMIN_GAMES_EXTRA", "POL_DEPLOYER_URL",
           "POL_DEPLOYER_TOKEN", "OPENLOBBY_REVISION", "POL_ADMIN_PAIR_TRIES", "POL_ADMIN_MODULES"):
    os.environ.pop(_k, None)
for _k in [k for k in os.environ if k.startswith("POL_ADMIN_GAME_")]:
    os.environ.pop(_k)

import accounts  # noqa: E402
import adminusers  # noqa: E402
from admin_http import Client, free_port, start_panel, stop  # noqa: E402

TOKEN = "kart-token-0123456789"
DEPLOYER_TOKEN = "deployer-token-for-the-test-0001"
OWNER, OWNER_PW = "op", "kart club owner 1"
OLD, NEW = "1a2b3c4d5e6f", "9f8e7d6c5b4a"
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


def audit_since(n):
    return [(r["actor"], r["action"], bool(r["ok"]))
            for r in sql("SELECT * FROM admin_audit WHERE id > %s ORDER BY id", (n,))]


def audit_top():
    return (sql("SELECT coalesce(max(id), 0) AS n FROM admin_audit") or [{"n": 0}])[0]["n"]


class Game:
    """Kart Club's admin port: module.json, status.json with self.update, and POST
    /openlobby/update answered with self.answer (status, body); every update request kept."""

    def __init__(self):
        self.update = None
        self.answer = (202, {"ok": True, "msg": "Fetching the new version."})
        self.asked = []
        game = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if self.headers.get("X-Devtool-Token") != TOKEN:
                    return self.send(403, {"error": "token"})
                if self.path == "/openlobby/module.json":
                    return self.send(200, {"v": 1, "key": "kart", "title": "Kart Club", "platform": "PC"})
                if self.path == "/openlobby/status.json":
                    doc = {"up": True, "online": 2}
                    if game.update is not None:
                        doc["update"] = game.update
                    return self.send(200, doc)
                self.send(404, {"error": "not found"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                if self.path == "/openlobby/update":
                    game.asked.append({"token": self.headers.get("X-Devtool-Token"),
                                       "actor": self.headers.get("X-Devtool-Actor"), "body": body})
                    if self.headers.get("X-Devtool-Token") != TOKEN:
                        return self.send(403, {"error": "token"})
                    return self.send(*game.answer)
                self.send(404, {"error": "not found"})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


class Deployer:
    """A stand-in deployer: answers /api/games and /api/jobs, keeps every POST /api/updater."""

    def __init__(self):
        self.got = []
        dep = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def ok(self):
                return self.headers.get("Authorization") == "Bearer " + DEPLOYER_TOKEN

            def do_GET(self):
                if not self.ok():
                    return self.send(401, {"error": "token"})
                if self.path == "/api/games":
                    return self.send(200, {"games": [], "core": {"running": True}, "options": {}})
                if self.path.startswith("/api/jobs"):
                    return self.send(200, {"jobs": []})
                if self.path.startswith("/api/updates"):
                    return self.send(200, {"checked_at": "2026-10-23T08:00:00Z", "checking": False, "items": []})
                self.send(404, {"error": "not found"})

            def do_POST(self):
                if not self.ok():
                    return self.send(401, {"error": "token"})
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.path == "/api/updater":
                    dep.got.append(body)
                    return self.send(202, {"job": "20261023T080000-abcdef"})
                self.send(404, {"error": "not found"})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


def seed():
    db = accounts.connect()
    accounts.set_admin_cred(db, OWNER, OWNER_PW)
    db.commit()
    for name in ("mod_upd", "mod_see"):
        adminusers.create_mod(db, name, "pass for " + name, ["audit"], by=OWNER)
    db.close()
    # the game's permissions are known to the panel once it has asked the game, not here
    sql("UPDATE admin_moderator SET perms = 'game.kart,game.kart.update' WHERE username = 'mod_upd'")
    sql("UPDATE admin_moderator SET perms = 'game.kart' WHERE username = 'mod_see'")


def item(owner, key="kart"):
    code, r = owner.call("api/updates")
    return next((i for i in r.get("items", []) if i["key"] == key), None)


def report(state=None, update="available", running=OLD, **extra):
    out = {"running": {"commit": running}, "latest": {"commit": NEW}, "behind": 2 if update == "available" else 0,
           "changes": [{"commit": NEW, "subject": "Lap times survive a restart"}] if update == "available" else [],
           "update": update, "how": "Run Kart Club Setup and choose Update.", "apply": True}
    if state:
        out["state"] = state
    out.update(extra)
    return out


def path_b(owner, upd, see, game):
    print("the game applies its own update")
    game.update = dict(report(), apply=False)
    it = item(owner)
    chk("apply false: the how sentence, no way in", (it["update"], it.get("apply"), it.get("via"), it["how"]),
        ("available", None, None, "Run Kart Club Setup and choose Update."))
    code, r = owner.call("api/updates/apply", {"key": "kart"})
    chk("  and Update is refused before the game is asked",
        (code, r.get("error"), len(game.asked)), (409, "Kart Club does not offer to update itself from the panel.", 0))
    game.update = report()
    it = item(owner)
    chk("apply true: kept, the game updates itself", (it.get("apply"), it.get("via")), (True, "game"))
    labels = owner.call("api/mods")[1].get("perm_labels", {})
    chk("game.kart.update is listed with the game's own permissions",
        labels.get("game.kart.update"), "Kart Club: update it, when it can update itself")

    n = audit_top()
    chk("a moderator without game.kart.update: 403", see.call("api/updates/apply", {"key": "kart"})[0], 403)
    chk("  nor the status poll", see.call("api/updates/state?key=kart")[0], 403)
    chk("  and the game was not asked", len(game.asked), 0)
    for status, body, want_code, want_msg in (
            (409, {"ok": False, "msg": "Kart Club is already updating."}, 409, "Kart Club is already updating."),
            (503, {"ok": False, "msg": "A race is on; try after it."}, 503, "A race is on; try after it."),
            (500, {"ok": False, "msg": "boom"}, 502, "The game did not take the update request (HTTP 500)"),
            (200, {"ok": True}, 502, "The game did not take the update request (HTTP 200)")):
        game.answer = (status, body)
        code, r = upd.call("api/updates/apply", {"key": "kart"})
        chk("the game answers %d: %d with %r" % (status, want_code, want_msg), (code, r.get("error")),
            (want_code, want_msg))
    one = game.asked[0]
    chk("POST /openlobby/update: the token, the actor, an empty body", (one["token"], one["actor"], one["body"]),
        (TOKEN, "mod_upd", b""))
    game.answer = (202, {"ok": True, "msg": "Fetching the new version."})
    game.update = report("updating", message="Fetching the new version.")
    code, r = upd.call("api/updates/apply", {"key": "kart"})
    chk("202: taken, the game's message as given", (code, r), (200, {"ok": True, "msg": "Fetching the new version."}))
    chk("all in the activity log", audit_since(n),
        [("mod_upd", "updated Kart Club", False)] * 4 + [("mod_upd", "updated Kart Club", True)])

    print("following it")
    code, s = upd.call("api/updates/state?key=kart")
    chk("updating, with its message", (code, s.get("reachable"), s.get("state"), s.get("message"), s.get("applying"),
                                       s.get("done")),
        (200, True, "updating", "Fetching the new version.", True, False))
    it = item(owner)
    chk("  the Updates list says so", (it.get("state"), it.get("message"), it.get("applying")),
        ("updating", "Fetching the new version.", True))
    game.update = report("updating", message="Building the new image.")
    chk("  the message changes", upd.call("api/updates/state?key=kart")[1].get("message"), "Building the new image.")
    game.update = report(update="current", running=NEW)
    code, s = upd.call("api/updates/state?key=kart")
    chk("running.commit equals latest.commit and current: done", (s.get("done"), s.get("applying"), s.get("state")),
        (True, False, None))
    game.update = report(update="current", running=OLD)
    chk("  current with another commit is not done", upd.call("api/updates/state?key=kart")[1].get("done"), False)

    n = audit_top()
    upd.call("api/updates/apply", {"key": "kart"})
    game.update = report("failed", why="The build failed; the old version still runs.")
    code, s = upd.call("api/updates/state?key=kart")
    chk("a failure", (s.get("state"), s.get("why"), s.get("applying")),
        ("failed", "The build failed; the old version still runs.", False))
    upd.call("api/updates/state?key=kart")
    it = item(owner)
    chk("  the Updates list keeps it", (it.get("state"), it.get("why"), it.get("via")),
        ("failed", "The build failed; the old version still runs.", "game"))
    chk("  written to the activity log once", audit_since(n),
        [("mod_upd", "updated Kart Club", True),
         ("mod_upd", "Kart Club: update failed: The build failed; the old version still runs.", False)])

    game.update = report()
    upd.call("api/updates/apply", {"key": "kart"})
    game.update = report("updating", message="Restarting.")
    upd.call("api/updates/state?key=kart")
    game.stop()
    code, s = upd.call("api/updates/state?key=kart")
    chk("the game away during its update: unreachable, not lost yet", (code, s.get("reachable"), s.get("lost"),
                                                                        s.get("applying")), (200, False, False, True))
    time.sleep(6.5)
    code, s = upd.call("api/updates/state?key=kart")
    chk("  away past POL_ADMIN_UPDATE_LOST: has not come back", (s.get("lost"), s.get("message")),
        (True, "Kart Club has not come back after updating; check its server"))


def path_a(owner, anon, dep):
    print("the deployer runs an approved updater")
    game = Game()
    try:
        code, r = owner.call("api/modules/game", {"action": "remove", "key": "kart"})
        pc = owner.call("api/pair/code", {})[1]["code"]
        updater = {"folder": "C:\\Users\\kart\\Kart Club", "command": ["python", "setup.py", "--update", "--yes"],
                   "timeout_minutes": 180}
        code, r = anon.call("api/pair", {"code": pc, "key": "kart", "title": "Kart Club",
                                         "url": "http://127.0.0.1:%d" % game.port, "token": TOKEN, "updater": updater})
        chk("paired with an updater", (code, r.get("updater")), (200, "requested"))
        game.update = dict(report(), apply=False)
        chk("requested: not offered", item(owner).get("via"), None)
        chk("  nor run", owner.call("api/updates/run", {"key": "kart"})[0], 404)
        added = {a["key"]: a for a in owner.call("api/modules?fresh=1")[1].get("added", [])}
        owner.call("api/modules/game", {"action": "approve_updater", "key": "kart", "id": added["kart"]["updater"]["id"]})
        chk("approved, a deployer set up: the deployer runs it", item(owner).get("via"), "updater")
        game.update = report()
        chk("  but a game that applies its own update wins", item(owner).get("via"), "game")
        game.update = dict(report(), apply=False)
        n = audit_top()
        code, r = owner.call("api/updates/run", {"key": "kart", "folder": "/", "command": ["python", "-c", "evil"],
                                                 "timeout_minutes": 240, "updater": {"folder": "/"}})
        chk("Update: the deployer's job", (code, r.get("job")), (202, "20261023T080000-abcdef"))
        chk("  the deployer got the stored, approved record, not the browser's",
            dep.got, [{"game": "kart", "title": "Kart Club", "updater": updater}])
        chk("  in the activity log", audit_since(n),
            [(OWNER, "asked the deployer to update Kart Club with its updater", True)])
        owner.call("api/modules/game", {"action": "revoke_updater", "key": "kart"})
        chk("revoked: not offered again", (item(owner).get("via"), owner.call("api/updates/run", {"key": "kart"})[0]),
            (None, 404))
    finally:
        game.stop()


def main():
    game = Game()
    dep = Deployer()
    seed()
    dead = "http://127.0.0.1:%d" % free_port()
    env = {"POL_ADMIN_BIND": "127.0.0.1", "POL_ADMIN_LIVE_TTL": "0", "POL_ADMIN_GITHUB_API": dead,
           "POL_ADMIN_PAIR_TRIES": "1000", "POL_ADMIN_UPDATE_LOST": "6", "POL_ADMIN_UPDATE_REPO": "off",
           "POL_DEPLOYER_URL": dep.url, "POL_DEPLOYER_TOKEN": DEPLOYER_TOKEN}
    tmp = tempfile.mkdtemp(prefix="admingameupdate-")
    proc, url = start_panel(tmp, SERVICES, free_port(), env_extra=env)
    try:
        owner, upd, see, anon = Client(url), Client(url), Client(url), Client(url)
        chk("owner signs in", owner.login(OWNER, OWNER_PW), 200)
        code, r = owner.call("api/modules/game", {"action": "save", "key": "kart",
                                                  "url": "http://127.0.0.1:%d" % game.port, "token": TOKEN})
        chk("Kart Club added", code, 200)
        chk("moderators sign in", (upd.login("mod_upd", "pass for mod_upd"), see.login("mod_see", "pass for mod_see")),
            (200, 200))
        path_b(owner, upd, see, game)
        path_a(owner, anon, dep)
    finally:
        stop(proc)
        dep.stop()
    print("admin_gameupdate_test: %d failed" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
