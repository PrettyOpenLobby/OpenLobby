#!/usr/bin/env python3
"""The admin panel's side of the deployer (services/admindeploy.py), against a stand-in deployer
that answers the contract in docs/deployer.md.

    python tools/admin_deploy_test.py

Checks that the owner sees the catalog and the jobs, that a moderator cannot reach any of it,
that an action goes to the deployer with the panel's token and lands in the activity log, that a
busy deployer's 409 comes back with a sentence, that a job's log is read from an offset, that a
wrong token and an unreachable deployer read as "not available", and that with no deployer set
up the page is simply off. Throwaway PostgreSQL (tools/pgtest.py); the panel runs as a
subprocess on a loopback port.
"""
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.abspath(os.path.join(HERE, os.pardir, "services"))
sys.path.insert(0, HERE)
sys.path.insert(0, SERVICES)

import pgtest  # noqa: E402

pgtest.use_fresh_database()

import accounts  # noqa: E402
import adminusers  # noqa: E402
from admin_http import Client, free_port, start_panel, stop  # noqa: E402

OWNER, OWNER_PW = "op", "correct horse 1"
TOKEN = "deployer-token-0123456789"
bad = 0
SEEN = []
STATE = {"busy": False}
LOG = b"cloning kart...\nok\nenabling kart\n"


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got, "" if ok else "  (want %r)" % (want,)))


class Deployer(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, code, obj, ctype="application/json", extra=()):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in extra:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        SEEN.append((self.command, self.path, self.headers.get("Authorization")))
        if self.headers.get("Authorization") != "Bearer " + TOKEN:
            self._reply(401, {"error": "unauthorized"})
            return False
        return True

    def do_GET(self):
        if not self._auth():
            return
        if self.path == "/api/games":
            return self._reply(200, {"games": [
                {"key": "kart", "name": "Kart Club", "admin_module": "kart", "installed": False,
                 "enabled": False, "running": None, "data_ok": None,
                 "missing": [{"path": "kart/data.bin", "hint": "copy it from your disc"}],
                 "admin_env": ["POL_ADMIN_GAME_KART_URL", "POL_ADMIN_GAME_KART_TOKEN"], "ports": [7000],
                 "notes": ""}], "core": {"running": True}, "options": {"ghcr": False}})
        if self.path.startswith("/api/jobs?"):
            return self._reply(200, {"jobs": [{"id": "j2", "action": "install", "game": "kart",
                                               "state": "running", "started": 1, "ended": None,
                                               "exit": None, "summary": "", "missing": []}]})
        if self.path == "/api/jobs/j2":
            return self._reply(200, {"id": "j2", "action": "install", "game": "kart", "state": "running",
                                     "log": LOG.decode(), "log_size": len(LOG), "missing": []})
        if self.path.startswith("/api/jobs/j2/log?offset="):
            off = int(self.path.rsplit("=", 1)[1])
            return self._reply(200, LOG[off:], "text/plain", [("X-Job-State", "running")])
        self._reply(404, {"error": "not found"})

    def do_POST(self):
        if not self._auth():
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        SEEN.append(("BODY", body))
        if STATE["busy"]:
            return self._reply(409, {"job": "j2"})
        if body.get("game") not in ("kart", "core"):
            return self._reply(400, {})
        return self._reply(202, {"job": "j3"})


def main():
    dep = ThreadingHTTPServer(("127.0.0.1", free_port()), Deployer)
    threading.Thread(target=dep.serve_forever, daemon=True).start()
    dep_url = "http://127.0.0.1:%d" % dep.server_address[1]
    tmp = tempfile.mkdtemp(prefix="admindeploy-")
    db = accounts.connect()
    accounts.set_admin_cred(db, OWNER, OWNER_PW)
    db.commit()
    adminusers.create_mod(db, "gm_all", "pass for gm_all", sorted(adminusers.perm_labels()), by=OWNER)
    db.close()
    env = {"POL_DEPLOYER_URL": dep_url, "POL_DEPLOYER_TOKEN": TOKEN}
    proc, url = start_panel(tmp, SERVICES, free_port(), env_extra=env)
    try:
        owner, gm = Client(url), Client(url)
        owner.login(OWNER, OWNER_PW)
        gm.login("gm_all", "pass for gm_all")
        code, ov = owner.call("api/deploy")
        chk("the owner sees the catalog", (code, ov.get("available"), [g["key"] for g in ov.get("games", [])]),
            (200, True, ["kart"]))
        chk("...with the missing data and the admin settings to add",
            (ov["games"][0]["missing"][0]["path"], ov["games"][0]["admin_env"][0]),
            ("kart/data.bin", "POL_ADMIN_GAME_KART_URL"))
        chk("...and the running job first", [j["id"] for j in ov["jobs"]], ["j2"])
        chk("the panel sends the deployer its token",
            all(a == "Bearer " + TOKEN for m, p, a in [s for s in SEEN if s[0] != "BODY"]), True)
        chk("even a moderator with every permission cannot see it",
            (gm.call("api/deploy")[0], gm.call("api/deploy/job?id=j2")[0]), (403, 403))
        chk("nor run an action", gm.call("api/deploy/action", {"action": "install", "game": "kart"})[0], 403)
        n = len(SEEN)
        code, out = owner.call("api/deploy/action", {"action": "install", "game": "kart"})
        chk("an action goes to the deployer", (code, out, [s for s in SEEN[n:] if s[0] == "BODY"]),
            (202, {"job": "j3"}, [("BODY", {"action": "install", "game": "kart"})]))
        rows = owner.call("api/audit")[1].get("rows") or []
        chk("...and is in the activity log", any("deployer to install kart" in json.dumps(r) for r in rows), True)
        chk("an action or game the panel does not know never reaches the deployer",
            (owner.call("api/deploy/action", {"action": "rm -rf", "game": "kart"})[0],
             owner.call("api/deploy/action", {"action": "install", "game": "../etc"})[0]), (400, 400))
        STATE["busy"] = True
        code, out = owner.call("api/deploy/action", {"action": "update", "game": "core"})
        chk("a busy deployer's 409 comes back with the running job and a sentence",
            (code, out.get("job"), "still running" in (out.get("error") or "")), (409, "j2", True))
        STATE["busy"] = False
        code, j = owner.call("api/deploy/job?id=j2")
        chk("a job with its log", (code, j.get("state"), j.get("log_size")), (200, "running", len(LOG)))
        code, lg = owner.call("api/deploy/log?id=j2&offset=16")
        chk("its log from an offset, with the state", (code, lg.get("text"), lg.get("state")),
            (200, LOG[16:].decode(), "running"))
        chk("a job id that is not one is refused", owner.call("api/deploy/job?id=../x")[0], 400)
    finally:
        stop(proc)
    for name, extra in (("a wrong token", {"POL_DEPLOYER_TOKEN": "wrong"}),
                        ("an unreachable deployer", {"POL_DEPLOYER_URL": "http://127.0.0.1:%d" % free_port()}),
                        ("no deployer set up", {"POL_DEPLOYER_URL": ""})):
        proc, url = start_panel(tmp, SERVICES, free_port(), env_extra=dict(env, **extra))
        try:
            owner = Client(url)
            owner.login(OWNER, OWNER_PW)
            chk("%s reads as not available" % name, owner.call("api/deploy")[1].get("available"), False)
        finally:
            stop(proc)
    dep.shutdown()
    print("all checks passed" if not bad else "%d check(s) FAILED" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
