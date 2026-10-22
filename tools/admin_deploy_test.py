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
import hashlib
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
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
UPLOADS = []
UPLOAD_CAP = 3 << 20


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
                                               "state": "running", "created": "2026-10-21T22:00:00Z", "started": "2026-10-21T22:00:01Z", "ended": None,
                                               "exit": None, "summary": "", "missing": []}]})
        if self.path == "/api/jobs/j2":
            return self._reply(200, {"id": "j2", "action": "install", "game": "kart", "state": "running",
                                     "log": LOG.decode(), "log_size": len(LOG), "missing": []})
        if self.path.startswith("/api/browse?path="):
            rel = urllib.parse.unquote(self.path.split("=", 1)[1])
            SEEN.append(("BROWSE", rel))
            return self._reply(200, {"root": "/clients", "path": rel, "parent": None if not rel else "",
                                     "dirs": [] if rel else ["Kart Club"], "files": [{"name": "data.bin", "size": 4}],
                                     "matches": [{"game": "kart", "item": "disc", "label": "Kart Club: disc data"}]
                                     if rel == "Kart Club" else []})
        if self.path == "/api/uploads":
            return self._reply(200, {"uploads": [{"id": "u1", "name": "kart.zip", "created": "2026-10-21T22:00:00Z",
                                                  "last_import": None, "size": 9, "files": 1, "matches": []}]})
        if self.path.startswith("/api/jobs/j2/log?offset="):
            off = int(self.path.rsplit("=", 1)[1])
            return self._reply(200, LOG[off:], "text/plain", [("X-Job-State", "running"),
                                                               ("X-Log-Offset", str(off)), ("X-Log-Size", str(len(LOG)))])
        self._reply(404, {"error": "not found"})

    def do_DELETE(self):
        if not self._auth():
            return
        self.send_response(204 if self.path == "/api/uploads/u1" else 404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _upload(self):
        # read as it comes, a chunk at a time, the way the real deployer streams to disk
        n, got, h = int(self.headers.get("Content-Length") or 0), 0, hashlib.sha256()
        while got < n:
            chunk = self.rfile.read(min(65536, n - got))
            if not chunk:
                break
            got += len(chunk)
            h.update(chunk)
        UPLOADS.append({"length": self.headers.get("Content-Length"), "chunked": self.headers.get("Transfer-Encoding"),
                        "name": self.headers.get("X-Filename"), "got": got, "sha": h.hexdigest()})
        if got > UPLOAD_CAP:
            return self._reply(413, {"max": UPLOAD_CAP})
        return self._reply(201, {"upload": "u2", "size": got, "files": 3,
                                 "matches": [{"game": "kart", "item": "disc", "label": "Kart Club: disc data"}]})

    def do_POST(self):
        if not self._auth():
            return
        if self.path == "/api/uploads":
            return self._upload()
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        SEEN.append(("BODY", body))
        if STATE["busy"]:
            return self._reply(409, {"job": "j2"})
        if body.get("game") not in ("kart", "core"):
            return self._reply(400, {})
        if body.get("action") == "import" and (body.get("source") or {}).get("subdir") == "wrong":
            return self._reply(400, {"error": "That folder is not the Kart Club disc.",
                                     "missing": [{"path": "kart/data.bin", "hint": "copy it from your disc"}]})
        return self._reply(202, {"job": "j3"})


def upload(client, data, name):
    req = urllib.request.Request(client.url + "api/deploy/upload", data=data, method="POST",
                                 headers={"Content-Type": "application/octet-stream",
                                          "X-Filename": urllib.parse.quote(name, safe="")})
    try:
        r = client.op.open(req, timeout=30)
        raw, code = r.read(), r.status
    except urllib.error.HTTPError as exc:
        raw, code = exc.read(), exc.code
    return code, json.loads(raw or b"{}")


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
        chk("its log from an offset, with the state and where to read next",
            (code, lg.get("text"), lg.get("state"), lg.get("next")),
            (200, LOG[16:].decode(), "running", len(LOG)))
        chk("a job id that is not one is refused", owner.call("api/deploy/job?id=../x")[0], 400)

        print("client data import")
        code, b = owner.call("api/deploy/browse?path=Kart%20Club")
        chk("browsing a clients folder, with what it looks like",
            (code, b.get("path"), [m["label"] for m in b.get("matches", [])]), (200, "Kart Club", ["Kart Club: disc data"]))
        n = len(SEEN)
        chk("a folder outside the clients folder never reaches the deployer",
            [owner.call("api/deploy/browse?path=" + urllib.parse.quote(p))[0] for p in ("../etc", "/etc", "a/../../b")],
            [400, 400, 400])
        chk("...not one of them", [s for s in SEEN[n:] if s[0] == "BROWSE"], [])
        chk("the uploads list", [u["id"] for u in owner.call("api/deploy/uploads")[1].get("uploads", [])], ["u1"])
        chk("a moderator cannot browse, list, upload or delete",
            (gm.call("api/deploy/browse?path=")[0], gm.call("api/deploy/uploads")[0],
             upload(gm, b"x" * 10, "a.zip")[0], gm.call("api/deploy/upload/delete", {"upload": "u1"})[0]),
            (403, 403, 403, 403))
        chk("...and nothing they sent reached the deployer", UPLOADS, [])
        data = os.urandom(2 << 20) + b"end"
        code, out = upload(owner, data, "カート.zip")
        chk("an upload goes through", (code, out.get("upload"), out.get("size")), (201, "u2", len(data)))
        up = UPLOADS[-1] if UPLOADS else {}
        chk("...whole, byte for byte, with its length and not chunked",
            (up.get("got"), up.get("sha"), up.get("length"), up.get("chunked")),
            (len(data), hashlib.sha256(data).hexdigest(), str(len(data)), None))
        chk("...with its name percent-encoded as the browser sent it",
            urllib.parse.unquote(up.get("name") or ""), "カート.zip")
        rows = owner.call("api/audit")[1].get("rows") or []
        chk("...and in the activity log", any("uploaded" in json.dumps(r, ensure_ascii=False) and "カート.zip" in json.dumps(r, ensure_ascii=False) for r in rows), True)
        code, out = upload(owner, b"y" * (UPLOAD_CAP + 1), "big.zip")
        chk("an archive over the deployer's cap comes back with a sentence and the cap",
            (code, out.get("max"), bool(out.get("error"))), (413, UPLOAD_CAP, True))
        n = len(SEEN)
        code, out = owner.call("api/deploy/action", {"action": "import", "game": "kart", "item": "disc",
                                                      "source": {"upload": "u2", "subdir": "Kart Club"}})
        chk("an import from an upload", (code, out.get("job")), (202, "j3"))
        chk("...reaches the deployer as the spec says", [s[1] for s in SEEN[n:] if s[0] == "BODY"],
            [{"action": "import", "game": "kart", "item": "disc", "source": {"upload": "u2", "subdir": "Kart Club"}}])
        code, out = owner.call("api/deploy/action", {"action": "import", "game": "kart", "item": "disc",
                                                      "source": {"upload": "u2", "subdir": "wrong"}})
        chk("the wrong folder: the deployer's missing files come back",
            (code, [m["path"] for m in out.get("missing", [])]), (400, ["kart/data.bin"]))
        chk("an import from a clients folder", owner.call("api/deploy/action", {
            "action": "import", "game": "kart", "item": "disc", "source": {"path": "Kart Club"}})[0], 202)
        n = len(SEEN)
        chk("an import with no source, or one that escapes, never reaches the deployer",
            [owner.call("api/deploy/action", dict({"action": "import", "game": "kart", "item": "disc"}, **x))[0]
             for x in ({}, {"source": {"path": "../x"}}, {"source": {"upload": "u2", "subdir": "/etc"}},
                       {"source": {"upload": "../u2"}}, {"item": "../x", "source": {"path": "a"}})],
            [400, 400, 400, 400, 400])
        chk("...not one of them", [s for s in SEEN[n:] if s[0] == "BODY"], [])
        chk("deleting an upload", owner.call("api/deploy/upload/delete", {"upload": "u1"}), (200, {"ok": True}))
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
