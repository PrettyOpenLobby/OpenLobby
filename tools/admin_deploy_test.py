#!/usr/bin/env python3
"""The admin panel's side of the deployer (services/admindeploy.py), against a stand-in deployer
that answers the contract in docs/deployer.md.

    python tools/admin_deploy_test.py

Checks that the owner sees the catalog and the jobs, that a moderator cannot reach any of it,
that an action goes to the deployer with the panel's token and lands in the activity log, that a
busy deployer's 409 comes back with a sentence, that a job's log is read from an offset, that a
wrong token and an unreachable deployer read as "not available", and that with no deployer set
up the page is simply off. The update channel: the owner reads it (by short image names: the
registry's address is not passed on, even from a deployer that sends it), a moderator can neither
read nor switch it, a dev image's source link is a redirect for the owner only, an access code goes to the deployer and nowhere else (not the activity log, not
the panel's own output), a code that does not fit never leaves the panel, the deployer's refusal
comes back with its sentence, and a deployer without channels reads as such. Per server: the
unlock, a key's switch, forgetting the code and the issuer of short codes reach the deployer as
they should, owner only, each in the activity log, and an older deployer reads as one. Throwaway PostgreSQL (tools/pgtest.py); the panel runs as a
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
FEED = {"on": False}
CHANNEL = {"on": True}
CODE = "kart-dev-7Qm2x9Lp4Vb8"
CHANNEL_ANSWER = {"channel": "stable", "unlocked": False, "dev": [], "registry": None, "source": None,
                  "since": None, "registry_default": "registry.example.org/kart", "code_stored": False,
                  "code_ok": None, "code_why": None, "ghcr": True, "problem": None, "images": [
                      {"key": "kart", "name": "Kart Club", "stable": "ghcr.io/example/kart", "dev_image": "kart:main",
                       "dev": "registry.example.org/kart/kart:main", "applies": True, "on": False, "source":
                       "https://registry.example.org/kart/kart", "what": None, "license": "AGPL-3.0-or-later",
                       "note": None, "enabled": True}],
                  "keys": [{"key": "kart", "name": "Kart Club", "on": "stable", "since": None, "enabled": True,
                            "applies": True, "builds_on_core": False, "images": ["kart (dev)"], "services": ["kart"],
                            "note": None, "problem": None, "would": [], "back": None,
                            "registry": "registry.example.org/kart"}],
                  "issuer": {"value": "https://openlobby.fyi", "from": "default", "default": "https://openlobby.fyi",
                             "registry": "registry.example.org/kart"}}


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
        if self.path.startswith("/api/patches/feed"):
            SEEN.append(("FEED", self.path))
            if not FEED["on"]:
                return self._reply(409, {"error": "Patches from the developers are off.",
                                         "feed": False, "enabled": False, "can_enable": True})
            return self._reply(200, {"feed": True, "bundles": [
                {"region": "KC1", "product": "0007", "title": "Kart Club (test)", "latest": "20050101_0",
                 "have": "20040714_0", "status": "behind", "files": 1, "size": 700, "why": None}]})
        if self.path.startswith("/api/patches"):
            SEEN.append(("PATCHES", self.path))
            listing = {"bundles": [{"region": "KC1", "product": "0007", "title": "Kart Club (test)", "aliases": [],
                                    "versions": ["20030429_0", "20040714_0"], "latest": "20040714_0",
                                    "served": True, "selfcheck": "failed", "selfcheck_at": "2026-10-21T12:03:10Z",
                                    "selfcheck_why": "The list the service sends is trimmed on purpose.",
                                    "size": 5000}]}
            if self.path.endswith("recheck=1"):
                return self._reply(202, dict(listing, job="j9"))
            return self._reply(200, listing)
        if self.path == "/api/uploads":
            return self._reply(200, {"uploads": [{"id": "u1", "name": "kart.zip", "created": "2026-10-21T22:00:00Z",
                                                  "last_import": None, "size": 9, "files": 1, "matches": []}]})
        if self.path == "/api/channel":
            if not CHANNEL["on"]:
                return self._reply(404, {"error": "not found"})
            return self._reply(200, CHANNEL_ANSWER)
        if self.path.startswith("/api/channel/source?"):
            SEEN.append(("SOURCE", self.path))
            stable = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("stable", [""])[0]
            if stable == "ghcr.io/example/kart":
                return self._reply(200, {"url": "https://code.example.org/kart/kart", "license": "AGPL-3.0"})
            if stable == "ghcr.io/example/evil":
                return self._reply(200, {"url": "javascript:alert(1)"})
            return self._reply(404, {"error": "No dev image there has a source address."})
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
        if self.path == "/api/patches/feed":
            SEEN.append(("FEEDSET", body))
            if body.get("enabled") and body.get("code") not in (None, "olx2.good"):
                return self._reply(400, {"error": "That access code does not include patches."})
            FEED["on"] = bool(body.get("enabled"))
            return self._reply(200, {"feed": FEED["on"], "enabled": FEED["on"]})
        if self.path == "/api/channel":
            SEEN.append(("CHANNEL", body))
            if "issuer" in body:
                return self._reply(200, {"issuer": {"value": body["issuer"] or "https://openlobby.fyi",
                                                    "from": "state" if body["issuer"] else "default",
                                                    "default": "https://openlobby.fyi"}})
            if "unlock" in body and body.get("unlock") != CODE:
                return self._reply(400, {"error": "The dev builds did not accept the access code."})
            if body.get("channel") == "dev" and body.get("code") != CODE:
                return self._reply(400, {"error": "The dev registry registry.example.org did not accept "
                                                  "the access code. Check it, or ask for a new one."})
            return self._reply(202, {"job": "j7"})
        if str(body.get("action", "")).startswith("patch_"):
            if body.get("action") in ("patch_rollback", "patch_remove") and body.get("product") != "0007":
                return self._reply(404, {})
            return self._reply(202, {"job": "j8"})
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

        print("patch bundles")
        code, p = owner.call("api/deploy/patches")
        chk("the bundles, with their versions and self-check",
            (code, [(b["region"], b["latest"], b["selfcheck"]) for b in p.get("bundles", [])]),
            (200, [("KC1", "20040714_0", "failed")]))
        code, p = owner.call("api/deploy/patches?recheck=1")
        chk("checking them all starts a job", (code, p.get("job")), (200, "j9"))
        chk("a moderator cannot see or touch them",
            (gm.call("api/deploy/patches")[0], gm.call("api/deploy/patch", {"action": "patch_check"})[0]), (403, 403))
        n = len(SEEN)
        code, out = owner.call("api/deploy/patch", {"action": "patch_rollback", "region": "KC1", "product": "0007",
                                                   "to": "20030429_0"})
        chk("a roll back reaches the deployer as the docs say",
            (code, out.get("job"), [s[1] for s in SEEN[n:] if s[0] == "BODY"]),
            (202, "j8", [{"action": "patch_rollback", "region": "KC1", "product": "0007", "to": "20030429_0"}]))
        n = len(SEEN)
        code, out = owner.call("api/deploy/patch", {"action": "patch_build", "region": "KC1", "product": "0007",
                                                   "source": {"upload": "u2", "subdir": "Kart Club"},
                                                   "version": "", "walk": True, "aliases": ["KC2"]})
        chk("a build: an empty version stays the deployer's 'auto', the rest goes through",
            (code, [s[1] for s in SEEN[n:] if s[0] == "BODY"]),
            (202, [{"action": "patch_build", "region": "KC1", "product": "0007",
                    "source": {"upload": "u2", "subdir": "Kart Club"}, "walk": True, "aliases": ["KC2"]}]))
        chk("removing a bundle that is not there is a 404 with a sentence",
            (lambda r: (r[0], bool(r[1].get("error"))))(owner.call("api/deploy/patch", {
                "action": "patch_remove", "region": "KC1", "product": "0001"})), (404, True))
        n = len(SEEN)
        chk("bad regions, products, versions and sources never reach the deployer",
            [owner.call("api/deploy/patch", x)[0] for x in (
                {"action": "patch_remove", "region": "kc1", "product": "0007"},
                {"action": "patch_remove", "region": "KC1", "product": "7"},
                {"action": "patch_rollback", "region": "KC1", "product": "0007", "to": "latest"},
                {"action": "patch_import", "source": {"path": "../x.polpatch"}},
                {"action": "patch_build", "region": "KC1", "product": "0007", "source": {"path": "a"}, "aliases": ["../"]},
                {"action": "patch_wipe"})],
            [400] * 6)
        chk("...not one of them", [s for s in SEEN[n:] if s[0] == "BODY"], [])
        rows = owner.call("api/audit")[1].get("rows") or []
        chk("patch actions are in the activity log", any("rolled a patch bundle back" in json.dumps(r) for r in rows), True)

        print("patch feed")
        code, f = owner.call("api/deploy/patches/feed")
        chk("patches off: the panel reads it as off, with a kept code to turn them on with",
            (code, f.get("feed"), f.get("enabled"), f.get("can_enable")), (200, False, False, True))
        n = len(SEEN)
        code, out = owner.call("api/deploy/patches/feed", {"enabled": True, "code": " olx2.good "})
        chk("turning them on: the code reaches the deployer, trimmed",
            (code, [s[1] for s in SEEN[n:] if s[0] == "FEEDSET"]), (200, [{"enabled": True, "code": "olx2.good"}]))
        rows = owner.call("api/audit")[1].get("rows") or []
        chk("...the activity log says so, and never holds the code",
            (any("turned patches from the developers on" in json.dumps(r) for r in rows),
             any("olx2.good" in json.dumps(r) for r in rows)), (True, False))
        chk("a code the deployer refuses: its sentence", owner.call("api/deploy/patches/feed",
            {"enabled": True, "code": "olx1.nope"})[0], 400)
        chk("bad bodies never reach it", [owner.call("api/deploy/patches/feed", b)[0]
                                          for b in ({}, {"enabled": "yes"}, {"enabled": True, "code": 7})],
            [400, 400, 400])
        chk("a moderator cannot turn them on", gm.call("api/deploy/patches/feed", {"enabled": True})[0], 403)
        FEED["on"] = True
        code, f = owner.call("api/deploy/patches/feed?refresh=1")
        chk("with a feed: what it offers, as the deployer said",
            (code, f.get("feed"), [(b["region"], b["status"], b["files"]) for b in f.get("bundles", [])]),
            (200, True, [("KC1", "behind", 1)]))
        chk("...and the refresh reaches it", SEEN[-1], ("FEED", "/api/patches/feed?refresh=1"))
        chk("a moderator cannot see the feed or fetch from it",
            (gm.call("api/deploy/patches/feed")[0],
             gm.call("api/deploy/patch", {"action": "patch_fetch", "region": "KC1", "product": "0007"})[0]),
            (403, 403))
        n = len(SEEN)
        code, out = owner.call("api/deploy/patch", {"action": "patch_fetch", "region": "KC1", "product": "0007"})
        chk("a fetch reaches the deployer as the docs say",
            (code, [s[1] for s in SEEN[n:] if s[0] == "BODY"]),
            (202, [{"action": "patch_fetch", "region": "KC1", "product": "0007"}]))
        n = len(SEEN)
        chk("a fetch with a bad region never reaches it",
            owner.call("api/deploy/patch", {"action": "patch_fetch", "region": "../", "product": "0007"})[0], 400)
        chk("...not that one", [s for s in SEEN[n:] if s[0] == "BODY"], [])
        rows = owner.call("api/audit")[1].get("rows") or []
        chk("a fetch is in the activity log",
            any("fetched a patch bundle from the developers" in json.dumps(r) for r in rows), True)

        print("update channel")
        code, ch = owner.call("api/deploy/channel")
        chk("the owner reads the channel and its images, by short name",
            (code, ch.get("available"), ch.get("channel"), [r.get("dev_name") for r in ch.get("images", [])]),
            (200, True, "stable", ["kart (dev)"]))
        chk("...and the registry's address is nowhere in the answer, though the deployer sent it",
            ("registry.example.org" in json.dumps(ch), sorted(ch), sorted(ch["images"][0]), sorted(ch["keys"][0])),
            (False, ["available", "channel", "code_ok", "code_stored", "code_why", "dev", "ghcr", "images",
                     "install_id", "issuer", "keys", "paired", "problem", "short_codes", "since", "unlocked"],
             ["applies", "dev_name", "enabled", "has_source", "key", "license", "name", "note", "on", "stable", "what"],
             ["applies", "back", "builds_on_core", "enabled", "images", "key", "name", "note", "on", "problem",
              "services", "since", "would"]))
        chk("...each thing with a dev build has its own switch, and the issuer is said",
            (ch["keys"][0]["key"], ch["keys"][0]["on"], ch["issuer"], ch["paired"]),
            ("kart", "stable", {"value": "https://openlobby.fyi", "from": "default", "default": "https://openlobby.fyi"}, []))

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        jar = [h for h in owner.op.handlers if isinstance(h, urllib.request.HTTPCookieProcessor)][0].cookiejar

        def source(who_jar, stable):
            op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(who_jar), NoRedirect)
            try:
                r = op.open(owner.url + "api/deploy/channel/source?" + urllib.parse.urlencode({"stable": stable}),
                            timeout=15)
                return r.status, r.headers.get("Location")
            except urllib.error.HTTPError as e:
                return e.code, e.headers.get("Location")
        chk("the source link sends the owner to the address the deployer gives",
            source(jar, "ghcr.io/example/kart"), (302, "https://code.example.org/kart/kart"))
        chk("...an image without one, or an address that is not http(s), is a 404",
            (source(jar, "ghcr.io/example/none"), source(jar, "ghcr.io/example/evil")), ((404, None), (404, None)))
        gjar = [h for h in gm.op.handlers if isinstance(h, urllib.request.HTTPCookieProcessor)][0].cookiejar
        n = len(SEEN)
        chk("...and a moderator gets no redirect", source(gjar, "ghcr.io/example/kart"), (403, None))
        chk("...nor reaches the deployer", [s for s in SEEN[n:] if s[0] == "SOURCE"], [])
        n = len(SEEN)
        chk("a moderator with every permission can neither read nor switch it",
            (gm.call("api/deploy/channel")[0],
             gm.call("api/deploy/channel", {"channel": "dev", "code": CODE})[0],
             gm.call("api/deploy/channel", {"channel": "stable"})[0]), (403, 403, 403))
        chk("...and nothing they sent reached the deployer", [s for s in SEEN[n:] if s[0] == "CHANNEL"], [])
        n = len(SEEN)
        chk("a channel or code that does not fit never reaches the deployer",
            [owner.call("api/deploy/channel", x)[0] for x in (
                {"channel": "beta"}, {"channel": "dev"}, {"channel": "dev", "code": "short"},
                {"channel": "dev", "code": "has spaces in it"},
                {"channel": "dev", "code": CODE, "registry": "Not A Registry"},
                {"channel": "dev", "code": CODE, "registry": 7})],
            [400] * 6)
        chk("...not one of them", [s for s in SEEN[n:] if s[0] == "CHANNEL"], [])
        n = len(SEEN)
        code, out = owner.call("api/deploy/channel", {"channel": "dev", "code": " " + CODE + " ",
                                                      "registry": "registry.example.org/kart"})
        chk("switching to Dev starts the deployer's job", (code, out), (202, {"job": "j7"}))
        chk("...the code and registry reach the deployer as sent, trimmed",
            [s[1] for s in SEEN[n:] if s[0] == "CHANNEL"],
            [{"channel": "dev", "code": CODE, "registry": "registry.example.org/kart"}])
        chk("...with the panel's token", all(a == "Bearer " + TOKEN for m, pth, a in
                                             [x for x in SEEN[n:] if len(x) == 3]), True)
        n = len(SEEN)
        owner.call("api/deploy/channel", {"channel": "dev", "code": "abcd efgh jkmn"})
        chk("...a short dev code typed with spaces reaches it as ABCD-EFGH-JKMN",
            [s[1] for s in SEEN[n:] if s[0] == "CHANNEL"], [{"channel": "dev", "code": "ABCD-EFGH-JKMN"}])
        code, out = owner.call("api/deploy/channel", {"channel": "dev", "code": "kart-wrong-code-0000"})
        chk("a code the deployer refuses comes back with its sentence",
            (code, "did not accept" in (out.get("error") or "")), (400, True))
        code, out = owner.call("api/deploy/channel", {"channel": "stable"})
        chk("switching back to Stable", (code, out), (202, {"job": "j7"}))
        rows = owner.call("api/audit")[1].get("rows") or []
        text = json.dumps(rows)
        chk("the switches are in the activity log",
            ("switched to the dev channel" in text, "switched to the stable channel" in text,
             "tried to switch to the dev channel" in text), (True, True, True))
        chk("...and the access code is nowhere in it", CODE in text or "kart-wrong-code-0000" in text, False)

        # per key: unlock once, then each thing on its own
        n = len(SEEN)
        code, out = owner.call("api/deploy/channel", {"unlock": " " + CODE + " "})
        chk("unlocking starts the deployer's job", (code, out), (202, {"job": "j7"}))
        code, out = owner.call("api/deploy/channel", {"unlock": "abcd efgh jkmn"})
        code, out = owner.call("api/deploy/channel", {"key": "kart", "to": "dev"})
        chk("a per-key switch starts the deployer's job", (code, out), (202, {"job": "j7"}))
        code, out = owner.call("api/deploy/channel", {"lock": True})
        code, out = owner.call("api/deploy/channel", {"issuer": "https://codes.example.org/"})
        chk("the issuer is set through the deployer", (code, out.get("issuer", {}).get("from")), (200, "state"))
        owner.call("api/deploy/channel", {"issuer": ""})
        chk("...each reaches the deployer as it should, the code trimmed, a short code as ABCD-EFGH-JKMN",
            [s[1] for s in SEEN[n:] if s[0] == "CHANNEL"],
            [{"unlock": CODE}, {"unlock": "ABCD-EFGH-JKMN"}, {"key": "kart", "to": "dev"}, {"lock": True},
             {"issuer": "https://codes.example.org"}, {"issuer": ""}])
        n = len(SEEN)
        chk("requests that do not fit never reach the deployer",
            [owner.call("api/deploy/channel", x)[0] for x in (
                {"unlock": "short"}, {"key": "Bad Key", "to": "dev"}, {"key": "kart", "to": "beta"},
                {"lock": "yes"}, {"issuer": "http://codes.example.org"}, {"issuer": "codes.example.org"},
                {"issuer": 7})],
            [400] * 7)
        chk("...not one of them", [s for s in SEEN[n:] if s[0] == "CHANNEL"], [])
        n = len(SEEN)
        chk("a moderator can neither switch a key, unlock, forget nor set the issuer",
            [gm.call("api/deploy/channel", x)[0] for x in (
                {"key": "kart", "to": "dev"}, {"unlock": CODE}, {"lock": True}, {"issuer": "https://codes.example.org"})],
            [403] * 4)
        chk("...and nothing they sent reached the deployer", [s for s in SEEN[n:] if s[0] == "CHANNEL"], [])
        code, out = owner.call("api/deploy/channel", {"unlock": "kart-wrong-code-0000"})
        chk("an unlock the deployer refuses comes back with its sentence",
            (code, "did not accept" in (out.get("error") or "")), (400, True))
        rows = owner.call("api/audit")[1].get("rows") or []
        text = json.dumps(rows)
        chk("each is in the activity log",
            ("unlocked the dev builds" in text, "switched kart to Dev" in text, "forgot the dev access code" in text,
             "set the issuer of short dev codes to https://codes.example.org" in text,
             "set the issuer of short dev codes to https://openlobby.fyi" in text,
             "tried to unlock the dev builds" in text), (True,) * 6)
        chk("...and no access code is in it",
            CODE in text or "kart-wrong-code-0000" in text or "ABCD-EFGH-JKMN" in text, False)
        saved = dict(CHANNEL_ANSWER)
        for k in ("keys", "issuer", "unlocked", "dev", "code_ok", "code_why"):
            CHANNEL_ANSWER.pop(k)
        code, ch = owner.call("api/deploy/channel")
        chk("a deployer from before per-key switches: keys null, the page says to update the core",
            (code, ch.get("available"), ch.get("keys"), ch.get("unlocked"), ch.get("issuer")),
            (200, True, None, False, None))
        CHANNEL_ANSWER.update(saved)
        STATE["busy"] = True
        code, out = owner.call("api/deploy/channel", {"channel": "stable"})
        chk("a busy deployer: 409 with a sentence", (code, "still running" in (out.get("error") or "")), (409, True))
        STATE["busy"] = False
        CHANNEL["on"] = False
        code, ch = owner.call("api/deploy/channel")
        chk("a deployer without channels reads as such", (code, ch.get("available"), "update the core" in
                                                          (ch.get("why") or "")), (200, False, True))
        CHANNEL["on"] = True
    finally:
        stop(proc)
    logs = []
    for name in os.listdir(tmp):
        if name.startswith("admin-") and name.endswith(".log"):
            with open(os.path.join(tmp, name), encoding="utf-8", errors="replace") as fh:
                logs.append(fh.read())
    chk("the access code is not in the panel's own output", any(CODE in x for x in logs), False)
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
