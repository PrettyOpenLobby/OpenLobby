#!/usr/bin/env python3
"""Offline test for services/deployer/deployer.py.

    python tools/deployer_test.py

Starts the deployer on 127.0.0.1 with a stand-in for tools/stack.py (so no
docker and no network are used) against a scratch directory of checkouts,
and checks the HTTP API: the token, action and game validation, the one-job
lock, job states and logs, client-data detection, the dirty-checkout refusal
on update (with real git, against a local bare repository), and that the
token never reaches a log. Needs git on PATH for the update part.

The client data import runs against a second deployer with a clients
folder: browsing and its escapes (.., absolute paths, symlinks out), the
500-entry cap, uploads of good and crafted archives (absolute paths, ..,
symlinks, hard links, devices, FIFOs, zip and tar.gz bombs, too many files,
a zip that understates its sizes), 411 and 413 on the request itself,
upload listing, deletion and expiry, the import action's validation, and
import jobs run against a stand-in tool, checking its arguments, working
directory and environment, and that no answer carries an import's argv.

The patch actions run against a third deployer with a mirrors folder and a
real polserver2 that the stand-in stack.py restarts on loopback ports: a
tiny bundle in three versions built with tools/patch/polpatch_lib.py,
uploads kept whole with their manifest fields, tampered .polpatch files
refused on upload, the browser's polpatch fields, imports from an upload
and from the clients folder, a build from a client folder, roll backs (from
the kept list files and rebuilt) that remove exactly the newer versions,
refused roll back targets, remove into the trash folder, GET /api/patches
with its order, latest, served and self-check fields (a corrupted blob
included), the format checks, and that no answer spells out a path or a
command line.
"""
import http.client
import io
import json
import os
import re
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEPLOYER = os.path.join(ROOT, "services", "deployer", "deployer.py")
TOKEN = "test-token-6f1d2c9a8b7e4d3c"

FAILS = []
SKIPS = []


def ok(cond, what):
    line = ("ok   " if cond else "FAIL ") + what
    enc = getattr(sys.stdout, "encoding", None) or "ascii"
    print(line.encode(enc, "backslashreplace").decode(enc, "replace"))
    if not cond:
        FAILS.append(what)
    return cond


# The stand-in for tools/stack.py. It records its arguments, prints what the
# deployer should never see (the token in its environment) and behaves as
# the files in FAKE_STACK_DIR say: check.json and ps.json are its answers,
# hold makes compose wait for release, big makes compose print 1.3 MB.
FAKE_STACK = r'''
import json, os, sys, time
d = os.environ["FAKE_STACK_DIR"]
args = sys.argv[1:]
with open(os.path.join(d, "calls.txt"), "a") as f:
    f.write(json.dumps(args) + "\n")
err = sys.stderr
err.write("fake stack.py " + " ".join(args) + "\n")
err.write("token in env: " + os.environ.get("POL_DEPLOYER_TOKEN", "<absent>") + "\n")
if os.environ.get("FAKE_ECHO"):
    err.write(os.environ["FAKE_ECHO"] + "\n")
err.flush()
core = os.getcwd()
cat = {}
for n in sorted(os.listdir(os.path.join(core, "games"))):
    e = json.load(open(os.path.join(core, "games", n)))
    cat[e["key"]] = e
def state():
    try:
        return [l.strip() for l in open(os.path.join(core, ".games")) if l.strip() and not l.startswith("#")]
    except OSError:
        return []
cmd = args[0]
if cmd == "check" and "--json" in args:
    p = os.path.join(d, "check.json")
    if os.path.exists(p):
        print(open(p).read())
        sys.exit(0)
    named = [a for a in args[1:] if not a.startswith("--")]
    keys = [k for k in cat if cat[k]["kind"] == "game"] if "--all" in args else \
        (named[0].split(",") if named else state())
    games = [{"key": k, "name": cat[k]["name"], "installed": True, "enabled": k in state(),
              "data_ok": True, "missing": [], "rows": [["ok", k + ": all present"]]} for k in keys]
    print(json.dumps({"ok": True, "games": games, "warnings": []}))
elif cmd == "ps":
    p = os.path.join(d, "ps.json")
    print(open(p).read() if os.path.exists(p) else json.dumps({"ok": False, "error": "no docker here"}))
elif cmd == "add":
    os.makedirs(os.path.join(os.path.dirname(core), cat[args[1]]["checkout"]), exist_ok=True)
elif cmd == "enable":
    with open(os.path.join(core, ".games"), "w") as f:
        f.write("\n".join(sorted(set(state()) | {args[1]})) + "\n")
elif cmd == "disable":
    with open(os.path.join(core, ".games"), "w") as f:
        f.write("\n".join(k for k in state() if k != args[1]) + "\n")
elif cmd == "compose":
    if os.path.exists(os.path.join(d, "hold")):
        t = time.time()
        while not os.path.exists(os.path.join(d, "release")) and time.time() - t < 30:
            time.sleep(0.05)
    if os.path.exists(os.path.join(d, "big")):
        line = "x" * 99 + "\n"
        for i in range(13000):
            sys.stdout.write(line)
        print("last line of the big output")
    if os.path.exists(os.path.join(d, "compose_fail")):
        sys.exit(3)
    if os.environ.get("FAKE_PATCH_MIRRORS") and args[-2:] == ["restart", "patch"]:
        # the patch service: a real polserver2 on loopback, started again
        import signal, subprocess
        pidf = os.path.join(d, "patch.pid")
        if os.path.exists(pidf):
            try:
                os.kill(int(open(pidf).read()), signal.SIGTERM)
            except (OSError, ValueError):
                pass
            time.sleep(0.5)
        log = open(os.path.join(d, "patch.log"), "ab")
        p = subprocess.Popen([sys.executable, os.environ["FAKE_PATCH_SERVER"],
                              os.environ["FAKE_PATCH_MIRRORS"], os.environ["FAKE_PATCH_OFFSET"]],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        open(pidf, "w").write(str(p.pid))
'''

# The patch service the stand-in stack.py restarts: polserver2's own Server,
# listening on 127.0.0.1 only, each port moved up by an offset.
FAKE_PATCH_SERVER = r'''
import os, socket, sys, threading, time
sys.path.insert(0, os.environ["FAKE_PATCH_SERVICES"])
import polserver2
mirrors, off = sys.argv[1], int(sys.argv[2])
bundles = polserver2.load_bundles(mirrors)
srv = polserver2.Server(bundles, "127.0.0.1", False, (), True)
ports = {b.port for b in bundles.values()} | {54000}
def serve(port):
    for attempt in range(100):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", port + off))
            break
        except OSError:
            s.close()
            time.sleep(0.1)
    else:
        print("cannot bind", port + off, flush=True)
        return
    s.listen(16)
    while True:
        conn, addr = s.accept()
        threading.Thread(target=srv.handle, args=(conn, addr), daemon=True).start()
for p in sorted(ports):
    threading.Thread(target=serve, args=(p,), daemon=True).start()
print("serving", sorted((r, pr, b.latest) for (r, pr), b in bundles.items()), flush=True)
threading.Event().wait()
'''


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Client:
    def __init__(self, port):
        self.base = "http://127.0.0.1:%d" % port

    def call(self, method, path, body=None, token=TOKEN, raw=False):
        data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if token is not None:
            req.add_header("Authorization", token if token.startswith(("Bearer", "Basic")) or token == ""
                           else "Bearer " + token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                code, hdrs, payload = r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            code, hdrs, payload = e.code, e.headers, e.read()
        if raw:
            return code, hdrs, payload
        try:
            return code, json.loads(payload or b"null")
        except ValueError:
            return code, payload

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, body, **kw):
        return self.call("POST", "/api/actions", body, **kw)

    def wait(self, jid, timeout=30):
        t = time.time()
        while time.time() - t < timeout:
            code, j = self.get("/api/jobs/" + jid)
            if code == 200 and j["state"] not in ("queued", "running"):
                return j
            time.sleep(0.1)
        return {"state": "timeout", "summary": "", "missing": [], "log": "", "exit": None}

    def run(self, action, game):
        code, r = self.post({"action": action, "game": game})
        if code != 202:
            return {"state": "http %d" % code, "summary": str(r), "missing": [], "log": "", "exit": None}
        return self.wait(r["job"])


class Server:
    def __init__(self, t, token=TOKEN, extra_env=None, args=()):
        self.t, self.token, self.extra, self.args = t, token, extra_env or {}, list(args)
        self.proc = None

    def argv(self, port):
        return [sys.executable, DEPLOYER, "--bind", "127.0.0.1:%d" % port, "--root", self.t.root,
                "--stack", self.t.fake, "--state", self.t.state] + self.args

    def env(self):
        env = dict(os.environ)
        env.update({"POL_DEPLOYER_TOKEN": self.token, "FAKE_STACK_DIR": self.t.fakedir,
                    "POL_DEPLOY_GHCR": ""})
        env.pop("COMPOSE_PROFILES", None)
        env.pop("POL_DEPLOY_CLIENTS", None)
        env.pop("POL_DEPLOY_UPLOAD_MAX", None)
        env.update(self.extra)
        return env

    def start(self):
        self.port = free_port()
        self.err = open(os.path.join(self.t.tmp, "server-%d.err" % self.port), "w")
        self.proc = subprocess.Popen(self.argv(self.port), env=self.env(),
                                     stdout=self.err, stderr=subprocess.STDOUT)
        c = Client(self.port)
        t = time.time()
        while time.time() - t < 15:
            try:
                if c.get("/api/health")[0] == 200:
                    return c
            except OSError:
                pass
            if self.proc.poll() is not None:
                break
            time.sleep(0.1)
        raise RuntimeError("the deployer did not start")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.err.close()


def git(*args, cwd=None):
    return subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True, text=True,
                          env=dict(os.environ, GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.com",
                                   GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.com"))


class Tree:
    """tmp/root/openlobby (catalog copy), tmp/root/hippaulmaster (a clone of
    tmp/origin.git), the stand-in stack.py and the job state dir."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="deployertest-")
        self.root = os.path.join(self.tmp, "root")
        self.core = os.path.join(self.root, "openlobby")
        os.makedirs(os.path.join(self.core, "games"))
        for n in os.listdir(os.path.join(ROOT, "games")):
            shutil.copy(os.path.join(ROOT, "games", n), os.path.join(self.core, "games", n))
        # one game without an admin module, to see admin_env empty
        p = os.path.join(self.core, "games", "jan.json")
        e = json.load(open(p))
        e["admin_module"] = None
        json.dump(e, open(p, "w"))
        self.fakedir = os.path.join(self.tmp, "fake")
        os.makedirs(self.fakedir)
        self.fake = os.path.join(self.fakedir, "stack.py")
        with open(self.fake, "w") as f:
            f.write(FAKE_STACK)
        self.state = os.path.join(self.tmp, "state")
        self.git = shutil.which("git") is not None
        self.tm = os.path.join(self.root, "hippaulmaster")
        if self.git:
            origin = os.path.join(self.tmp, "origin.git")
            git("init", "--bare", "-q", origin)
            seed = os.path.join(self.tmp, "seed")
            git("clone", "-q", origin, seed)
            with open(os.path.join(seed, "README.md"), "w") as f:
                f.write("one\n")
            git("add", "README.md", cwd=seed)
            git("commit", "-q", "-m", "one", cwd=seed)
            git("push", "-q", "origin", "HEAD", cwd=seed)
            git("clone", "-q", origin, self.tm)
            self.seed = seed
        else:
            os.makedirs(self.tm)

    def calls(self):
        try:
            with open(os.path.join(self.fakedir, "calls.txt")) as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    def clear_calls(self):
        try:
            os.remove(os.path.join(self.fakedir, "calls.txt"))
        except OSError:
            pass

    def flag(self, name, on=True, text=""):
        p = os.path.join(self.fakedir, name)
        if on:
            with open(p, "w") as f:
                f.write(text)
        elif os.path.exists(p):
            os.remove(p)

    def put_job(self, jid, state, t):
        meta = {"id": jid, "action": "check", "game": "tm", "state": state, "created": "x",
                "started": "x", "ended": None, "exit": None, "summary": "", "missing": [],
                "log_dropped": 0, "_t": t}
        d = os.path.join(self.state, "jobs")
        os.makedirs(d, exist_ok=True)
        json.dump(meta, open(os.path.join(d, jid + ".json"), "w"))
        open(os.path.join(d, jid + ".log"), "w").close()


FE_MISSING = {"ok": False, "games": [{
    "key": "fe", "name": "Fantasy Earth", "installed": True, "enabled": False, "data_ok": False,
    "rows": [["ok", "fe: checkout"], ["fail", "fe: fedata missing"]],
    "missing": [{"id": "fedata", "path": "../hippaulring/services/fedata/fe-fet-area.tsv",
                 "hint": "Spawn tables. From your Fantasy Earth install: python tools/fedata_build.py "
                         "--client <install> (run in ../hippaulring)",
                 "required": True, "source": "your Fantasy Earth install",
                 "tool": "python tools/fedata_build.py --client <install>"},
                {"id": "boardart", "path": "../hippaulring/services/boardart/x.png",
                 "hint": "optional art", "required": False, "source": "your install", "tool": "t"}]}],
    "warnings": []}


def test_refuses_without_token(t):
    for tok in ("", "   "):
        s = Server(t, token=tok)
        r = subprocess.run(s.argv(free_port()), env=s.env(), capture_output=True, text=True, timeout=20)
        ok(r.returncode != 0 and "POL_DEPLOYER_TOKEN" in r.stderr,
           "empty token %r: refuses to start (exit %d)" % (tok, r.returncode))
    s = Server(t)
    argv = s.argv(free_port())
    argv[argv.index("--root") + 1] = "relative/dir"
    r = subprocess.run(argv, env=s.env(), capture_output=True, text=True, timeout=20)
    ok(r.returncode != 0 and "POL_DEPLOY_ROOT" in r.stderr, "a relative POL_DEPLOY_ROOT is refused")


def test_auth(c):
    ok(c.get("/api/health", token=None)[0] == 401, "no token: 401")
    ok(c.get("/api/health", token="wrong")[0] == 401, "wrong token: 401")
    ok(c.get("/api/health", token=TOKEN + "x")[0] == 401, "token with a suffix: 401")
    ok(c.get("/api/health", token=TOKEN[:-1])[0] == 401, "token prefix: 401")
    ok(c.get("/api/health", token="Basic " + TOKEN)[0] == 401, "not a bearer: 401")
    ok(c.post({"action": "check", "game": "tm"}, token=None)[0] == 401, "POST without token: 401")
    code, h = c.get("/api/health")
    ok(code == 200 and h["ok"] is True and isinstance(h["version"], str) and h["busy"] is False,
       "right token: health %s" % h)


def test_validation(c):
    for body, what in (({"action": "rm", "game": "tm"}, "unknown action"),
                       ({"action": "check", "game": "nope"}, "unknown game"),
                       ({"action": "check"}, "no game"),
                       ({"action": "install", "game": "core"}, "install core"),
                       ({"action": "enable", "game": "core"}, "enable core"),
                       ({"action": "disable", "game": "core"}, "disable core"),
                       ({"action": "check", "game": "tm; rm -rf /"}, "game with shell text"),
                       (["check", "tm"], "a list body")):
        code, r = c.post(body)
        ok(code == 400 and r.get("error"), "%s: 400 (%s)" % (what, r))
    ok(c.post(b"{not json")[0] == 400, "bad JSON: 400")
    ok(c.get("/api/jobs/../../etc/passwd")[0] == 404 and c.get("/api/jobs/nope")[0] == 404,
       "bad job id: 404")


def test_lifecycle(c, t):
    j = c.run("check", "core")
    ok(j["state"] == "ok", "check core: ok (%s)" % j.get("summary"))
    for f in ("id", "action", "game", "state", "started", "ended", "exit", "summary", "missing",
              "log", "log_size"):
        ok(f in j, "job has %s" % f)
    ok(j["missing"] == [] and j["exit"] == 0, "ok job: missing [] and exit 0")
    ok("$ python tools/stack.py check --json" in j["log"], "log names the command")
    ok(t.calls()[-1] == ["check", "--json"], "check core runs check --json for the enabled set")


def test_needs_data(c, t):
    t.flag("check.json", text=json.dumps(FE_MISSING))
    os.makedirs(os.path.join(t.root, "hippaulring"), exist_ok=True)
    j = c.run("check", "fe")
    ok(j["state"] == "needs_data", "check fe without its data: needs_data (%s)" % j["state"])
    ok("fe-fet-area.tsv" in j["summary"] and "../hippaulring/services/fedata" in j["summary"],
       "summary names the file and where: %s" % j["summary"])
    m0 = FE_MISSING["games"][0]["missing"][0]
    ok(len(j["missing"]) == 1 and j["missing"][0]["path"] == m0["path"]
       and j["missing"][0]["hint"] == m0["hint"] and j["missing"][0]["item"] == "fedata",
       "job missing lists the required item as {path, hint, item}: %s" % j["missing"])
    imp = j["missing"][0].get("import") or {}
    ok(set(imp) == {"hint", "expects"} and imp["expects"] == ["DATA/dat.pak"],
       "  with its import's hint and expects, and no argv: %s" % imp)
    t.clear_calls()
    j = c.run("enable", "fe")
    ok(j["state"] == "needs_data" and "not enabled" in j["summary"],
       "enable fe without data: needs_data, not enabled (%s)" % j["summary"])
    ok(not any(a[0] in ("enable", "compose") for a in t.calls()), "  and nothing was enabled or started")
    j = c.run("install", "fe")
    ok(j["state"] == "needs_data", "install fe: cloned, then needs_data")
    t.flag("check.json", on=False)
    shutil.rmtree(os.path.join(t.root, "hippaulring"))
    t.clear_calls()
    j = c.run("install", "fe")
    ok(j["state"] == "ok" and os.path.isdir(os.path.join(t.root, "hippaulring")),
       "install fe: ok (%s)" % j["summary"])
    ok(t.calls()[0] == ["add", "fe", "--yes"], "install runs stack.py add fe --yes")


def test_busy_and_order(c, t):
    t.flag("hold")
    t.clear_calls()
    code, r = c.post({"action": "enable", "game": "tm"})
    ok(code == 202, "enable tm: 202")
    jid = r["job"]
    deadline = time.time() + 15
    while time.time() < deadline and not any(a[0] == "compose" for a in t.calls()):
        time.sleep(0.05)
    code, r2 = c.post({"action": "check", "game": "fe"})
    ok(code == 409 and r2.get("job") == jid, "second action while one runs: 409 with its id")
    ok(c.get("/api/health")[1]["busy"] is True, "health: busy while a job runs")
    t.put_job("29991231T235959-abcdef", "ok", time.time() + 1000)
    jobs = c.get("/api/jobs?limit=50")[1]["jobs"]
    ok(jobs[0]["id"] == jid and jobs[0]["state"] == "running",
       "jobs list: the running job first")
    ok(jobs[1]["id"] == "29991231T235959-abcdef", "  then the rest, newest first")
    ok(all("missing" in j for j in jobs), "  every listed job has missing")
    ok(len(c.get("/api/jobs?limit=2")[1]["jobs"]) == 2, "jobs limit")
    t.flag("release")
    j = c.wait(jid)
    t.flag("hold", on=False)
    t.flag("release", on=False)
    for ext in (".json", ".log"):
        os.remove(os.path.join(t.state, "jobs", "29991231T235959-abcdef" + ext))
    ok(j["state"] == "ok", "enable tm: ok (%s)" % j["summary"])
    calls = t.calls()
    ok(["enable", "tm"] in calls and ["compose", "up", "-d", "--build"] in calls,
       "enable tm: stack.py enable tm, then compose up -d --build (%s)" % calls)


def test_actions(c, t):
    t.clear_calls()
    j = c.run("restart", "tm")
    ok(j["state"] == "ok" and t.calls()[-1] == ["compose", "--no-prebuild", "up", "-d",
                                                "--force-recreate", "tmrank"],
       "restart tm: force-recreate its services (%s)" % t.calls())
    j = c.run("restart", "core")
    core = json.load(open(os.path.join(t.core, "games", "core.json")))["services"]
    ok(j["state"] == "ok" and t.calls()[-1][-len(core):] == core and "--force-recreate" in t.calls()[-1],
       "restart core: the core's services")
    j = c.run("restart", "jan")
    ok(j["state"] == "failed" and "not enabled" in j["summary"], "restart a disabled game: failed")
    t.flag("compose_fail")
    j = c.run("restart", "tm")
    ok(j["state"] == "failed" and j["exit"] == 3 and "docker compose up failed" in j["summary"],
       "compose failing: failed with its exit code (%s, %s)" % (j["exit"], j["summary"]))
    t.flag("compose_fail", on=False)
    with open(os.path.join(t.core, ".env"), "w") as f:
        f.write("COMPOSE_PROFILES=gm,deployer\n")
    j = c.run("restart", "tm")
    ok(j["state"] == "failed" and "COMPOSE_PROFILES" in j["summary"],
       "COMPOSE_PROFILES=deployer in .env: refused")
    os.remove(os.path.join(t.core, ".env"))


def test_update(c, t):
    if not t.git:
        SKIPS.append("update (no git)")
        print("SKIP update (no git on PATH)")
        return
    with open(os.path.join(t.tm, "README.md"), "a") as f:
        f.write("local edit\n")
    t.clear_calls()
    j = c.run("update", "tm")
    ok(j["state"] == "failed" and "local changes" in j["summary"],
       "update a dirty checkout: refused (%s)" % j["summary"])
    ok(not any(a[0] == "compose" for a in t.calls()), "  and nothing was started")
    git("checkout", "--", "README.md", cwd=t.tm)
    with open(os.path.join(t.tm, "untracked.txt"), "w") as f:
        f.write("data files are untracked\n")
    with open(os.path.join(t.seed, "README.md"), "a") as f:
        f.write("two\n")
    git("commit", "-q", "-am", "two", cwd=t.seed)
    git("push", "-q", "origin", "HEAD", cwd=t.seed)
    j = c.run("update", "tm")
    ok(j["state"] == "ok", "update a clean checkout (untracked files allowed): ok (%s)" % j["summary"])
    ok("two" in open(os.path.join(t.tm, "README.md")).read(), "  the commit was pulled")
    ok(["compose", "up", "-d", "--build"] in t.calls(), "  and the stack brought up")


def test_games(c, t):
    ps = {"ok": True, "services": {"tmrank": "running", "login": "running"},
          "core": {"running": True, "services": {"login": "running"}},
          "games": {"tm": {"running": True, "services": {"tmrank": "running"}},
                    "fe": {"running": False, "services": {}}}}
    t.flag("ps.json", text=json.dumps(ps))
    t.flag("check.json", text=json.dumps(FE_MISSING))
    time.sleep(5.2)  # past the /api/games cache
    code, g = c.get("/api/games")
    ok(code == 200, "GET /api/games: 200")
    by = {x["key"]: x for x in g["games"]}
    ok("core" not in by and set(by) >= {"tm", "fe", "jan"}, "games: every game, no core")
    cat = {k: json.load(open(os.path.join(t.core, "games", k + ".json"))) for k in by}
    for key, x in by.items():
        for f in ("key", "name", "admin_module", "installed", "enabled", "running", "data_ok",
                  "missing", "admin_env", "ports", "notes"):
            ok(f in x, "games %s: field %s" % (key, f)) if f not in x else None
        if x["ports"] != cat[key]["ports"]:
            ok(False, "games %s: ports copied from the catalog" % key)
    ok(all(x["ports"] == cat[x["key"]]["ports"] for x in by.values()), "games: ports from the catalog")
    ok(by["tm"]["admin_env"] == ["POL_ADMIN_GAME_TM_URL", "POL_ADMIN_GAME_TM_TOKEN"],
       "admin_env for tm: %s" % by["tm"]["admin_env"])
    ok(by["jan"]["admin_module"] is None and by["jan"]["admin_env"] == [],
       "admin_env empty without an admin module")
    ok(by["tm"]["installed"] and by["tm"]["enabled"] and by["tm"]["running"] is True,
       "tm: installed, enabled, running")
    ok(by["fe"]["data_ok"] is False and by["fe"]["missing"][0]["path"].endswith("fe-fet-area.tsv")
       and set(by["fe"]["missing"][0]) >= {"path", "hint"}, "fe: data_ok false with missing")
    ok(not by["fmo"]["installed"] and by["fmo"]["data_ok"] is None, "fmo: not installed, data_ok None")
    ok(g["core"]["running"] is True and g["options"] == {"ghcr": False}, "core running, options")
    t.flag("ps.json", on=False)
    t.flag("check.json", on=False)


def test_logs_and_token(c, t):
    t.flag("big")
    j = c.run("restart", "tm")
    t.flag("big", on=False)
    ok(j["state"] == "ok", "a job with 1.3 MB of output: ok")
    size = j["log_size"]
    path = os.path.join(t.state, "jobs", j["id"] + ".log")
    ok(size > 1300000 and os.path.getsize(path) <= 1 << 20,
       "log capped on disk (%d bytes) while log_size counts it all (%d)" % (os.path.getsize(path), size))
    ok(len(j["log"].encode()) <= 64 << 10 and "last line of the big output" in j["log"],
       "job log is the tail, at most 64 KB")
    code, h, body = c.get("/api/jobs/%s/log?offset=0" % j["id"], raw=True)
    ok(code == 200 and h["X-Job-State"] == "ok" and int(h["X-Log-Offset"]) > 0
       and int(h["X-Log-Offset"]) + len(body) == size,
       "log from 0 starts at the kept tail (X-Log-Offset %s)" % h["X-Log-Offset"])
    code, h, body = c.get("/api/jobs/%s/log?offset=%d" % (j["id"], size - 50), raw=True)
    ok(len(body) == 50 and body.endswith(b"\n"), "log from an offset: the last 50 bytes")
    code, h, body = c.get("/api/jobs/%s/log?offset=%d" % (j["id"], size), raw=True)
    ok(code == 200 and body == b"" and h["Content-Type"].startswith("text/plain"),
       "log at its end: empty text/plain")
    return j["id"]


def scan_for_token(t, servers):
    hits = []
    for dp, _, fs in os.walk(t.state):
        for f in fs:
            if TOKEN.encode() in open(os.path.join(dp, f), "rb").read():
                hits.append(f)
    for s in servers:
        if TOKEN in open(s.err.name, errors="replace").read():
            hits.append(os.path.basename(s.err.name))
    return hits


# --------------------------------------------------------- client data import
#
# A second deployer runs with a clients folder (POL_DEPLOY_CLIENTS), a 1 MiB
# upload cap and a 50-file cap. A stand-in for Tetra Master's
# tools/tmdata_build.py records how it was started.

UPLOAD_CAP = 1 << 20
FILE_CAP = 50
ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

STAND_IN_TOOL = r'''
import json, os, sys, time
d = %r
rec = {"argv": sys.argv[1:], "cwd": os.getcwd(), "env": dict(os.environ)}
with open(os.path.join(d, "tool.json"), "w") as f:
    json.dump(rec, f)
print("stand-in tmdata_build " + " ".join(sys.argv[1:]))
t = time.time()
while os.path.exists(os.path.join(d, "tool_hold")) and time.time() - t < 30:
    time.sleep(0.05)
rc = 0
if os.path.exists(os.path.join(d, "tool_rc")):
    rc = int(open(os.path.join(d, "tool_rc")).read())
sys.exit(rc)
'''

TM_MISSING = {"ok": False, "games": [{
    "key": "tm", "name": "Tetra Master", "installed": True, "enabled": False, "data_ok": False,
    "rows": [["fail", "tm: tmdata missing"]],
    "missing": [{"id": "tmdata", "path": "../hippaulmaster/services/tmdata/CardPri.BIN",
                 "hint": "The four parameter tables.", "required": True,
                 "source": "your Tetra Master install",
                 "tool": "python tools/tmdata_build.py --client <install>"}]}],
    "warnings": []}


def raw_request(port, method, path, body=b"", headers=None, send_length=True):
    """(status, headers, parsed body) over http.client, with full control of
    the headers (no Content-Length, a Content-Length larger than the body)."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    c.putrequest(method, path, skip_accept_encoding=True)
    c.putheader("Authorization", "Bearer " + TOKEN)
    for k, v in (headers or {}).items():
        c.putheader(k, v)
    if send_length:
        c.putheader("Content-Length", str(len(body)))
    c.endheaders()
    try:
        if body:
            c.send(body)
        r = c.getresponse()
        data = r.read()
    except (ConnectionError, http.client.HTTPException) as exc:
        return 0, {}, {"error": "connection: %s" % exc}
    finally:
        c.close()
    try:
        obj = json.loads(data or b"null")
    except ValueError:
        obj = data
    return r.status, dict(r.getheaders()), obj


def upload(port, data, name="client.zip", headers=None):
    h = {"X-Filename": urllib.parse.quote(name)}
    h.update(headers or {})
    return raw_request(port, "POST", "/api/uploads", data, h)


def make_zip(entries):
    """entries: (name or ZipInfo, bytes)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries:
            z.writestr(name, data)
    return buf.getvalue()


def zinfo(name, mode):
    zi = zipfile.ZipInfo(name)
    zi.create_system = 3
    zi.external_attr = mode << 16
    return zi


def make_tar(entries, gz=False):
    """entries: (TarInfo, bytes or None)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz" if gz else "w", format=tarfile.GNU_FORMAT) as tf:
        for ti, data in entries:
            if data is not None:
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
            else:
                tf.addfile(ti)
    return buf.getvalue()


def tinfo(name, kind=tarfile.REGTYPE, linkname=""):
    ti = tarfile.TarInfo(name)
    ti.type = kind
    ti.linkname = linkname
    return ti


TM_FILES = [("TetraMaster/data/CardPrm.BIN", b"c" * 64), ("TetraMaster/data/PlPrm.BIN", b"p" * 64),
            ("TetraMaster/readme.txt", b"hello")]


def import_tree(t):
    """clients/ with a Tetra Master install, an unrelated folder, a big one."""
    t.clients = os.path.join(t.tmp, "clients")
    for rel, data in TM_FILES:
        p = os.path.join(t.clients, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
    os.makedirs(os.path.join(t.clients, "Empty"))
    big = os.path.join(t.clients, "big")
    os.makedirs(os.path.join(big, "sub"))
    for i in range(510):
        open(os.path.join(big, "f%03d.txt" % i), "w").close()
    t.outside = os.path.join(t.tmp, "outside")
    os.makedirs(t.outside)
    with open(os.path.join(t.outside, "secret.txt"), "w") as f:
        f.write("not for the panel\n")
    t.symlinks = True
    try:
        os.symlink(t.outside, os.path.join(t.clients, "out"), target_is_directory=True)
        os.symlink(os.path.join(t.outside, "secret.txt"), os.path.join(t.clients, "TetraMaster", "s.txt"))
    except (OSError, NotImplementedError, AttributeError):
        t.symlinks = False
    tools = os.path.join(t.tm, "tools")
    os.makedirs(tools, exist_ok=True)
    with open(os.path.join(tools, "tmdata_build.py"), "w") as f:
        f.write(STAND_IN_TOOL % t.fakedir)


def walk_keys(obj, found):
    if isinstance(obj, dict):
        for k, v in obj.items():
            found.add(k)
            walk_keys(v, found)
    elif isinstance(obj, list):
        for v in obj:
            walk_keys(v, found)


SEEN = []   # every JSON answer the import tests got, for the argv check


def seen(obj):
    SEEN.append(obj)
    return obj


def test_unit_helpers():
    sys.path.insert(0, os.path.join(ROOT, "services", "deployer"))
    import clientdata
    import deployer as dep
    for bad in ("/etc", "..", "a/../../b", "C:/x", "c:x", "\\\\server\\x", "\\x", "a\x00b"):
        try:
            clientdata.rel_parts(bad)
            ok(False, "rel_parts refuses %r" % bad)
        except clientdata.PathError:
            pass
    ok(clientdata.rel_parts("./a//b/") == ["a", "b"], "rel_parts keeps a plain relative path")
    # the writer counts bytes whatever the archive declared
    tmp = tempfile.mkdtemp(prefix="deployertest-w-")
    try:
        w = clientdata._Writer(tmp, 100)
        try:
            w.write("a.bin", io.BytesIO(b"x" * 50), 10)
            ok(False, "writer: more bytes than declared is refused")
        except clientdata.ArchiveError as exc:
            ok(exc.code == 400, "writer: more bytes than declared: 400")
        w = clientdata._Writer(tmp, 100)
        try:
            w.write("b.bin", io.BytesIO(b"x" * 60), 60)
            w.write("c.bin", io.BytesIO(b"x" * 60), 60)
            ok(False, "writer: past the total cap is refused")
        except clientdata.ArchiveError as exc:
            ok(exc.code == 413, "writer: past the total cap while writing: 413")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    # who an import runs as, and the socket guard, with a stand-in os.stat
    class St:
        def __init__(self, uid, gid, mode=0o40755):
            self.st_uid, self.st_gid, self.st_mode = uid, gid, mode
    saved = {k: getattr(os, k, None) for k in ("getuid", "stat")}
    try:
        os.getuid = lambda: 0
        os.stat = lambda p, *a, **k: St(0, 0)
        kw, uid, gids = dep.import_identity("/x")
        ok(uid == dep.NOBODY and kw == {"user": dep.NOBODY, "group": dep.NOBODY, "extra_groups": []},
           "a root-owned checkout: the import runs as nobody, never root (%s)" % kw)
        os.stat = lambda p, *a, **k: St(1000, 1000)
        kw, uid, gids = dep.import_identity("/x")
        ok(uid == 1000 and kw["group"] == 1000 and kw["extra_groups"] == [],
           "the import runs as the checkout's owner with no extra groups")
        os.stat = lambda p, *a, **k: St(0, 998, 0o140660)
        ok(not dep.socket_open_to(1000, {1000}, "/s"), "socket root:docker 660: closed to uid 1000")
        ok(dep.socket_open_to(1000, {998}, "/s"), "  open to a member of its group")
        ok(dep.socket_open_to(0, set(), "/s"), "  open to root")
        os.stat = lambda p, *a, **k: St(0, 0, 0o140666)
        ok(dep.socket_open_to(1000, {1000}, "/s"), "socket 666: open to anyone")
    finally:
        for k, v in saved.items():
            if v is None:
                delattr(os, k)
            else:
                setattr(os, k, v)
    env = dep.import_env("/h")
    ok(not any(k.startswith(("DOCKER", "POL_")) for k in env) and env["HOME"] == "/h",
       "the import environment has no DOCKER_* or POL_* variable")


def test_browse(c, port, t):
    code, b = c.get("/api/browse")
    seen(b)
    ok(code == 200 and b["path"] == "" and b["parent"] is None and b["root"] == os.path.realpath(t.clients),
       "browse the top: path '', parent null, root (%s)" % (b if code != 200 else b["root"]))
    ok({"TetraMaster", "Empty", "big"} <= set(b["dirs"]) and "files" in b and b["truncated"] is False,
       "  dirs listed, files present, not truncated")
    ok(t.symlinks is False or "out" not in b["dirs"], "  a symlink out of the root is not listed")
    code, b = c.get("/api/browse?path=TetraMaster")
    seen(b)
    ok(code == 200 and b["parent"] == "" and b["dirs"] == ["data"]
       and [f["name"] for f in b["files"]] == ["readme.txt"] and b["files"][0]["size"] == 5,
       "browse TetraMaster: parent '', its dir and file with size (%s)" % b)
    m = [x for x in b["matches"] if (x["game"], x["item"]) == ("tm", "tmdata")]
    ok(m and m[0]["label"] == "Tetra Master: card tables" and set(m[0]) == {"game", "item", "label"},
       "  matches tm/tmdata with its label: %s" % b["matches"])
    code, b = c.get("/api/browse?path=TetraMaster/data")
    ok(code == 200 and b["parent"] == "TetraMaster" and b["matches"] == [],
       "browse TetraMaster/data: parent TetraMaster, no match there")
    code, b = c.get("/api/browse?path=big")
    seen(b)
    ok(code == 200 and b["truncated"] is True and len(b["dirs"]) + len(b["files"]) == 500
       and isinstance(b.get("files"), list) and len(b["files"]) > 0,
       "a folder of 511 entries: 500 listed, truncated, files still there (%d)"
       % (len(b.get("files", [])) if code == 200 else -1))
    for bad in ("..", "../outside", "TetraMaster/../../outside", "/etc", "%2Fetc", "C:/Windows",
                "..%5Coutside", "a%00b"):
        code, b = c.get("/api/browse?path=" + bad)
        ok(code == 400 and b.get("error"), "browse %s: 400" % bad)
    if t.symlinks:
        code, b = c.get("/api/browse?path=out")
        ok(code == 400, "browse a symlink that leads out of the root: 400")
        code, b = c.get("/api/browse?path=TetraMaster")
        ok("s.txt" not in [f["name"] for f in b["files"]], "  a file symlink out is not listed")
    else:
        SKIPS.append("browse symlink escapes (no symlinks here)")
        print("SKIP browse symlink escapes (this system will not make symlinks)")
    ok(c.get("/api/browse?path=nope")[0] == 404, "browse a missing folder: 404")


def test_uploads(c, port, t):
    up_dir = os.path.join(t.state2, "uploads")
    code, h, r = upload(port, make_zip(TM_FILES), name="TM install \u30c6.zip")
    seen(r)
    ok(code == 201 and ID_PATTERN.match(r.get("upload", "")) and r["files"] == 3
       and r["size"] == 133, "upload a zip: 201 with id, files and size (%s %s)" % (code, r))
    uid = r.get("upload", "")
    m = [x for x in r.get("matches", []) if (x["game"], x["item"]) == ("tm", "tmdata")]
    ok(m and m[0]["subdir"] == "TetraMaster" and m[0]["label"] == "Tetra Master: card tables",
       "  matches tm/tmdata in its TetraMaster folder: %s" % r.get("matches"))
    code, lst = c.get("/api/uploads")
    seen(lst)
    mine = [u for u in lst["uploads"] if u["id"] == uid]
    ok(mine and mine[0]["name"] == "TM install \u30c6.zip" and mine[0]["last_import"] is None
       and mine[0]["created"] and mine[0]["size"] == 133,
       "  listed at once, X-Filename decoded from UTF-8: %s" % mine)
    for name, data in (("plain.tar", make_tar([(tinfo(n), d) for n, d in TM_FILES])),
                       ("gz.tar.gz", make_tar([(tinfo(n), d) for n, d in TM_FILES], gz=True))):
        code, h, r = upload(port, data, name=name)
        ok(code == 201 and r["files"] == 3 and any(x["item"] == "tmdata" for x in r["matches"]),
           "upload a %s: 201 with its match" % name)
    code, h, r = upload(port, make_zip([("a.txt", b"x")]), name="../../../etc/passwd")
    ok(code == 201 and r["name"] == "passwd" and not os.path.exists(os.path.join(t.tmp, "etc")),
       "X-Filename with a path: shown as its last part, never used as a path")

    before = sorted(os.listdir(up_dir))
    bombs = [
        ("absolute path", make_zip([("/evil-abs.txt", b"x")]), 400),
        ("..", make_zip([("../evil-up.txt", b"x")]), 400),
        ("nested ..", make_zip([("a/../../evil-up2.txt", b"x")]), 400),
        ("drive letter", make_zip([("C:/evil-drive.txt", b"x")]), 400),
        ("backslash ..", make_zip([("a\\..\\..\\evil-bs.txt", b"x")]), 400),
        ("zip symlink", make_zip([(zinfo("evil-link", stat.S_IFLNK | 0o777), b"/etc/passwd")]), 400),
        ("zip device", make_zip([(zinfo("evil-dev", stat.S_IFCHR | 0o666), b"")]), 400),
        ("zip FIFO", make_zip([(zinfo("evil-fifo", stat.S_IFIFO | 0o666), b"")]), 400),
        ("the same name twice", make_zip([("dup.txt", b"1"), ("dup.txt", b"2")]), 400),
        ("a file where a folder goes", make_zip([("f", b"1"), ("f/g.txt", b"2")]), 400),
        ("zip bomb (declared size over the cap)",
         make_zip([("evil-bomb.bin", b"\0" * (UPLOAD_CAP + 1))]), 413),
        ("zip over the cap in all", make_zip([("evil-%d.bin" % i, b"\0" * (300 << 10)) for i in range(4)]), 413),
        ("zip over the file count", make_zip([("evil-n%03d.txt" % i, b"") for i in range(FILE_CAP + 1)]), 413),
        ("tar absolute path", make_tar([(tinfo("/evil-tabs.txt"), b"x")]), 400),
        ("tar ..", make_tar([(tinfo("../evil-tup.txt"), b"x")]), 400),
        ("tar symlink", make_tar([(tinfo("evil-tsym", tarfile.SYMTYPE, "/etc/passwd"), None)]), 400),
        ("tar hard link", make_tar([(tinfo("evil-tlnk", tarfile.LNKTYPE, "/etc/passwd"), None)]), 400),
        ("tar device", make_tar([(tinfo("evil-tdev", tarfile.CHRTYPE), None)]), 400),
        ("tar FIFO", make_tar([(tinfo("evil-tfifo", tarfile.FIFOTYPE), None)]), 400),
        ("tar.gz bomb", make_tar([(tinfo("evil-tbomb.bin"), b"\0" * (UPLOAD_CAP + 1))], gz=True), 413),
        ("tar over the file count", make_tar([(tinfo("evil-t%03d" % i), b"") for i in range(FILE_CAP + 1)]), 413),
        ("not an archive", b"just some text, not an archive" * 20, 400),
        ("a truncated zip", make_zip(TM_FILES)[:-30], 400),
    ]
    # a zip whose local and central headers both claim 10 bytes for 2000
    good = make_zip([("evil-lie.bin", b"y" * 2000)])
    lie = bytearray(good)
    for sig, off in ((b"PK\x03\x04", 22), (b"PK\x01\x02", 24)):
        i = lie.find(sig)
        struct.pack_into("<I", lie, i + off, 10)
    bombs.append(("a zip that understates its size", bytes(lie), 400))
    for what, data, want in bombs:
        code, h, r = upload(port, data, name="evil.zip")
        ok(code == want and r.get("error"), "%s: %d (%s %s)" % (what, want, code, r.get("error")))
        if want == 413:
            ok(r.get("max") == UPLOAD_CAP, "  413 carries max")
    hits = [os.path.join(dp, f) for dp, _, fs in os.walk(t.tmp) for f in fs if f.startswith("evil")]
    ok(not hits, "no refused archive wrote a file anywhere (%s)" % hits[:3])
    ok(sorted(os.listdir(up_dir)) == before, "  and none left anything in the uploads folder")
    ok(len(c.get("/api/uploads")[1]["uploads"]) == 4, "  and none is listed")

    code, h, r = raw_request(port, "POST", "/api/uploads", b"PK", {"X-Filename": "x.zip"},
                             send_length=False)
    ok(code == 411 and r.get("error"), "no Content-Length: 411 (%s)" % code)
    code, h, r = raw_request(port, "POST", "/api/uploads", b"", {"Content-Length": str(UPLOAD_CAP + 1)},
                             send_length=False)
    ok(code == 413 and r.get("max") == UPLOAD_CAP, "Content-Length over the cap: 413 with max (%s %s)"
       % (code, r))
    code, h, r = raw_request(port, "POST", "/api/uploads", b"", {})
    ok(code == 400, "an empty upload: 400")
    sk = socket.create_connection(("127.0.0.1", port), timeout=30)
    sk.sendall(b"POST /api/uploads HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + TOKEN.encode()
               + b"\r\nContent-Length: 100\r\n\r\nPK\x03\x04")
    sk.shutdown(socket.SHUT_WR)
    answer = b""
    while True:
        chunk = sk.recv(4096)
        if not chunk:
            break
        answer += chunk
    sk.close()
    ok(answer.startswith(b"HTTP/1.0 400") or answer.startswith(b"HTTP/1.1 400"),
       "a body shorter than its Content-Length: 400 (%r)" % answer[:40])
    ok(sorted(os.listdir(up_dir)) == before, "  and nothing is kept of it")

    code, h, r = upload(port, make_zip([("x.txt", b"x")]))
    gone = r["upload"]
    code, h, body = raw_request(port, "DELETE", "/api/uploads/" + gone, send_length=False)
    ok(code == 204 and not os.path.exists(os.path.join(up_dir, gone)), "DELETE an upload: 204, removed")
    ok(gone not in [u["id"] for u in c.get("/api/uploads")[1]["uploads"]], "  and no longer listed")
    ok(raw_request(port, "DELETE", "/api/uploads/" + gone, send_length=False)[0] == 404,
       "DELETE it again: 404")
    ok(raw_request(port, "DELETE", "/api/uploads/..%2F..%2Fjobs", send_length=False)[0] == 404,
       "DELETE a bad id: 404")
    return uid


def test_import_validation(c, port, t, uid):
    def post(body):
        code, r = c.post(body)
        seen(r)
        return code, r
    code, r = post({"action": "import", "game": "tm", "item": "nope", "source": {"path": "TetraMaster"}})
    ok(code == 400 and "nope" in r["error"] and "tmdata" in r["error"], "import an unknown item: 400 (%s)" % r)
    code, r = post({"action": "import", "game": "fe", "item": "blowfish", "source": {"path": "TetraMaster"}})
    ok(code == 400 and "cannot be imported" in r["error"], "import an item without import: 400")
    code, r = post({"action": "import", "game": "core", "item": "portal", "source": {"path": "x"}})
    ok(code == 400, "import for core: 400")
    for src in (None, {}, {"path": "a", "upload": uid}, {"path": "a", "subdir": "b"}, {"upload": 3},
                {"upload": uid, "extra": 1}, "TetraMaster"):
        code, r = post({"action": "import", "game": "tm", "item": "tmdata", "source": src})
        ok(code == 400, "import with source %r: 400" % (src,))
    for bad in ("..", "../outside", "/etc", "TetraMaster/../../outside"):
        code, r = post({"action": "import", "game": "tm", "item": "tmdata", "source": {"path": bad}})
        ok(code == 400, "import from %s: 400" % bad)
    code, r = post({"action": "import", "game": "tm", "item": "tmdata",
                    "source": {"upload": uid, "subdir": "../../jobs"}})
    ok(code == 400, "import from an upload subdir that climbs out: 400")
    if t.symlinks:
        code, r = post({"action": "import", "game": "tm", "item": "tmdata", "source": {"path": "out"}})
        ok(code == 400, "import through a symlink out of the root: 400")
    code, r = post({"action": "import", "game": "tm", "item": "tmdata", "source": {"upload": "20000101T000000-000000"}})
    ok(code == 400 and "upload" in r["error"], "import from an unknown upload: 400")
    code, r = post({"action": "import", "game": "tm", "item": "tmdata", "source": {"path": "Empty"}})
    ok(code == 400 and "data/CardPrm.BIN" in r["error"]
       and [m["path"] for m in r.get("missing", [])] == ["data/CardPrm.BIN", "data/PlPrm.BIN"]
       and all(m["hint"] for m in r["missing"]),
       "import from a folder without the expected files: 400 naming them (%s)" % r)
    code, r = post({"action": "import", "game": "tm", "item": "tmdata", "source": {"upload": uid}})
    ok(code == 400 and r.get("missing"), "import from an upload's top when the install is a folder down: 400")


def tool_record(t):
    with open(os.path.join(t.fakedir, "tool.json")) as f:
        return json.load(f)


def test_import_jobs(c, port, t, uid):
    def run(src):
        code, r = c.post({"action": "import", "game": "tm", "item": "tmdata", "source": src})
        seen(r)
        if code != 202:
            return {"state": "http %d" % code, "summary": str(r), "missing": [], "exit": None}
        j = c.wait(r["job"])
        seen({k: v for k, v in j.items() if k != "log"})
        return j
    t.clear_calls()
    j = run({"path": "TetraMaster"})
    ok(j["state"] == "ok" and "Imported" in j["summary"], "import tm card tables from a folder: ok (%s)"
       % j["summary"])
    ok(j.get("item") == "tmdata" and j.get("source") == {"path": "TetraMaster"},
       "  the job names its item and source")
    rec = tool_record(t)
    want = os.path.realpath(os.path.join(t.clients, "TetraMaster"))
    ok(rec["argv"] == ["--client", want], "  the tool got --client <the folder> (%s)" % rec["argv"])
    ok(os.path.normcase(os.path.realpath(rec["cwd"])) == os.path.normcase(os.path.realpath(t.tm)),
       "  and ran in the game's checkout")
    env = rec["env"]
    leaked = [k for k in env if k.startswith(("DOCKER", "POL_", "FAKE_")) or TOKEN in env[k]]
    ok(not leaked, "  with no DOCKER_*, POL_* or token in its environment (%s)" % leaked)
    ok(["check", "--json", "tm"] in t.calls(), "  then stack.py check --json tm")

    t.flag("check.json", text=json.dumps(TM_MISSING))
    j = run({"upload": uid, "subdir": "TetraMaster"})
    ok(j["state"] == "needs_data" and "still missing" in j["summary"],
       "import that leaves the item missing: needs_data (%s)" % j["summary"])
    ok(j["missing"] and j["missing"][0]["item"] == "tmdata"
       and set(j["missing"][0]["import"]) == {"hint", "expects"},
       "  missing carries item and import {hint, expects}: %s" % j["missing"])
    ok(j.get("source") == {"upload": uid, "subdir": "TetraMaster"}, "  from the upload's subfolder")
    rec = tool_record(t)
    ok(rec["argv"][1].replace("\\", "/").endswith("/uploads/%s/TetraMaster" % uid),
       "  the tool read the unpacked upload (%s)" % rec["argv"])
    lst = c.get("/api/uploads")[1]["uploads"]
    ok([u["last_import"] for u in lst if u["id"] == uid][0], "  and the upload's last_import is set")

    t.flag("tool_rc", text="3")
    j = run({"path": "TetraMaster"})
    ok(j["state"] == "failed" and j["exit"] == 3 and "exit 3" in j["summary"],
       "a failing tool with the item still missing: failed, exit 3 (%s)" % j["summary"])
    t.flag("tool_rc", on=False)

    code, g = c.get("/api/games")
    seen(g)
    tm = [x for x in g["games"] if x["key"] == "tm"][0]
    ok(tm["missing"] and tm["missing"][0]["item"] == "tmdata" and tm["missing"][0]["required"] is True
       and tm["missing"][0]["import"]["expects"] == ["data/CardPrm.BIN", "data/PlPrm.BIN"],
       "/api/games: missing carries item, required and import: %s" % tm["missing"])
    t.flag("check.json", on=False)

    # an upload an import is reading cannot be deleted
    t.flag("tool_hold")
    os.remove(os.path.join(t.fakedir, "tool.json"))
    code, r = c.post({"action": "import", "game": "tm", "item": "tmdata",
                      "source": {"upload": uid, "subdir": "TetraMaster"}})
    deadline = time.time() + 15
    while time.time() < deadline and not os.path.exists(os.path.join(t.fakedir, "tool.json")):
        time.sleep(0.05)
    time.sleep(0.2)
    code2, h, body = raw_request(port, "DELETE", "/api/uploads/" + uid, send_length=False)
    ok(code2 == 409, "DELETE the upload an import is reading: 409 (%s)" % code2)
    code3, r3 = c.post({"action": "import", "game": "tm", "item": "tmdata", "source": {"path": "TetraMaster"}})
    ok(code3 == 409 and r3.get("job") == r.get("job"), "a second import while one runs: 409 with its id")
    t.flag("tool_hold", on=False)
    c.wait(r["job"])

    # the argv of an import never leaves the deployer
    keys = set()
    for obj in SEEN:
        walk_keys(obj, keys)
    text = json.dumps(SEEN)
    ok("argv" not in keys and "{input}" not in text and '"python", "tools/' not in text,
       "no API answer carries an import's argv (%d answers looked at)" % len(SEEN))


def test_expiry(t):
    """Uploads past 24 hours after creation and after their last import go
    when the deployer starts; the others stay."""
    up = os.path.join(t.state2, "uploads")
    now = time.time()
    for uid, created, last in (("20000101T000000-0000aa", now - 2 * 86400, None),
                               ("20000101T000000-0000bb", now - 2 * 86400, now - 3600),
                               ("20000101T000000-0000cc", now - 3600, None),
                               ("20000101T000000-0000dd", now - 3 * 86400, now - 25 * 3600)):
        os.makedirs(os.path.join(up, uid))
        with open(os.path.join(up, uid + ".json"), "w") as f:
            json.dump({"id": uid, "name": "x.zip", "created": "x", "last_import": "x" if last else None,
                       "size": 1, "files": 1, "matches": [], "_t": created, "_last": last}, f)
    os.makedirs(os.path.join(up, "20000101T000000-0000ee.tmp"))
    os.makedirs(os.path.join(up, "20000101T000000-0000ff"))   # no record: half written
    return {"20000101T000000-0000bb", "20000101T000000-0000cc"}, \
        {"20000101T000000-0000aa", "20000101T000000-0000dd"}


def run_import_tests(t, servers):
    import_tree(t)
    test_unit_helpers()
    t.state2 = os.path.join(t.tmp, "state2")
    args = ["--clients", t.clients, "--upload-max", "1M", "--max-files", str(FILE_CAP),
            "--state", t.state2]
    env = {"DOCKER_HOST": "tcp://198.51.100.7:2375", "POL_DEPLOY_SOMETHING": "x",
           "POL_DEPLOY_CLIENTS": ""}
    s = Server(t, extra_env=env, args=args)
    servers.append(s)
    c = s.start()
    test_browse(c, s.port, t)
    uid = test_uploads(c, s.port, t)
    test_import_validation(c, s.port, t, uid)
    test_import_jobs(c, s.port, t, uid)
    s.stop()
    keep, drop = test_expiry(t)
    s2 = Server(t, extra_env=env, args=args)
    servers.append(s2)
    c = s2.start()
    ids = {u["id"] for u in c.get("/api/uploads")[1]["uploads"]}
    ok(keep <= ids and not (drop & ids) and uid in ids,
       "on start, uploads past 24 h since creation and last import are removed (%s)" % sorted(ids))
    names = os.listdir(os.path.join(t.state2, "uploads"))
    ok(not any(n.endswith(".tmp") or n == "20000101T000000-0000ff" for n in names),
       "  and half-written ones too")
    s2.stop()
    s3 = Server(t, args=["--state", os.path.join(t.tmp, "state3")])
    servers.append(s3)
    c = s3.start()
    code, r = c.get("/api/browse")
    ok(code == 409 and r.get("error"), "browse with no POL_DEPLOY_CLIENTS: 409 (%s)" % r)
    code, r = c.post({"action": "import", "game": "tm", "item": "tmdata", "source": {"path": "TetraMaster"}})
    ok(code == 409, "import from a path with no POL_DEPLOY_CLIENTS: 409")
    s3.stop()


# ------------------------------------------------------------ patch bundles

PV1, PV2, PV3 = "20030429_0", "20040714_0", "20050101_0"
PSEEN = []   # every JSON answer the patch tests got, for the path and argv check


def pseen(code_obj):
    PSEEN.append(code_obj[1])
    return code_obj


def polpatch_lib():
    d = os.path.join(ROOT, "tools", "patch")
    if d not in sys.path:
        sys.path.insert(0, d)
    import polpatch_lib as lib
    return lib


def write_file(root, rel, data):
    p = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)


def retamper(src, dst, change=None, extra=()):
    """Copy a .polpatch; change(name, data) -> data rewrites entries, extra
    adds (name, data) entries as written (a ZipInfo keeps a ../ name)."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for info in zin.infolist():
            data = zin.read(info)
            if change:
                data = change(info.filename, data)
            zout.writestr(info, data)
        for name, data in extra:
            zi = zipfile.ZipInfo("x")
            zi.filename = zi.orig_filename = name
            zout.writestr(zi, data)


class PatchKit:
    """Three versions of a tiny Tetra Master bundle as .polpatch files, a
    clients folder, an empty mirrors folder and the toolkit in the core."""

    def __init__(self, t):
        lib = polpatch_lib()
        self.t = t
        for rel in ("tools/patch", "services"):
            os.makedirs(os.path.join(t.core, *rel.split("/")), exist_ok=True)
        for n in os.listdir(os.path.join(ROOT, "tools", "patch")):
            if n.endswith(".py"):
                shutil.copy(os.path.join(ROOT, "tools", "patch", n), os.path.join(t.core, "tools", "patch", n))
        shutil.copy(os.path.join(ROOT, "tools", "patchver.py"), os.path.join(t.core, "tools"))
        for n in ("slc.py", "polbundle.py", "polserver2.py"):
            shutil.copy(os.path.join(ROOT, "services", n), os.path.join(t.core, "services", n))
        p = os.path.join(t.tmp, "patch")
        self.dir = p
        self.install = os.path.join(p, "install")
        write_file(self.install, "data/a.bin", bytes(range(256)) * 8)
        write_file(self.install, "data/b.txt", b"the base build\n" * 40)
        write_file(self.install, "readme.txt", b"base\n")
        out = os.path.join(p, "out")
        self.full = os.path.join(out, "full.polpatch")
        self.up2 = os.path.join(out, "up2.polpatch")
        self.up3 = os.path.join(out, "up3.polpatch")
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):     # the toolkit's progress
            lib.build_full(self.install, "W2U", "0002", self.full, version=PV1, walk=True)
            scratchm = os.path.join(p, "scratch-mirrors")
            lib.apply(self.full, scratchm)
            sb = os.path.join(scratchm, "W2U-0002")
            self.cfg_v1 = open(os.path.join(sb, "patch.cfg"), "rb").read()
            write_file(os.path.join(p, "ov2"), "data/b.txt", b"the second build\n" * 40)
            write_file(os.path.join(p, "ov2"), "extra/new.txt", b"a file the base did not have\n")
            lib.build_update(self.full, os.path.join(p, "ov2"), PV2, self.up2)
            lib.apply(self.up2, scratchm)
            write_file(os.path.join(p, "ov3"), "readme.txt", b"third\n")
            lib.build_update(sb, os.path.join(p, "ov3"), PV3, self.up3)
        self.clients = os.path.join(p, "clients")
        write_file(self.clients, "updates/W2U-0002-%s.polpatch" % PV2, open(self.up2, "rb").read())
        write_file(self.clients, "broken.polpatch", b"PK\x03\x04 this is not a zip at all")
        shutil.copytree(self.install, os.path.join(self.clients, "TetraInstall"))
        self.mirrors = os.path.join(p, "mirrors")
        os.makedirs(self.mirrors)
        self.state = os.path.join(t.tmp, "state-patch")
        self.server = os.path.join(t.fakedir, "patchserver.py")
        with open(self.server, "w") as f:
            f.write(FAKE_PATCH_SERVER)
        self.offset = self.pick_offset()

    def pick_offset(self):
        import random
        for _ in range(50):
            off = random.randint(3000, 11000)
            try:
                for port in (54000 + off, 53002 + off, 53003 + off):
                    s = socket.socket()
                    s.bind(("127.0.0.1", port))
                    s.close()
                return off
            except OSError:
                continue
        return 7000

    def env(self):
        return {"FAKE_PATCH_MIRRORS": self.mirrors, "FAKE_PATCH_SERVER": self.server,
                "FAKE_PATCH_OFFSET": str(self.offset),
                "FAKE_PATCH_SERVICES": os.path.join(self.t.core, "services")}

    def args(self):
        return ["--clients", self.clients, "--mirrors", self.mirrors, "--state", self.state,
                "--patch-host", "127.0.0.1", "--patch-port-offset", str(self.offset)]

    def stop_patch_service(self):
        pidf = os.path.join(self.t.fakedir, "patch.pid")
        if os.path.exists(pidf):
            try:
                import signal
                os.kill(int(open(pidf).read()), signal.SIGTERM)
            except (OSError, ValueError):
                pass
            os.remove(pidf)

    def bundle(self, name="W2U-0002"):
        return os.path.join(self.mirrors, name)


def patch_rows(c):
    code, r = pseen(c.get("/api/patches"))
    return code, {"%s/%s" % (b["region"], b["product"]): b for b in (r or {}).get("bundles", [])}, r


def patch_job(c, body, timeout=120):
    code, r = pseen(c.post(body))
    if code != 202:
        return code, r
    j = c.wait(r["job"], timeout)
    PSEEN.append(j)
    return code, j


def test_patch_formats(c, k):
    def post(body):
        return pseen(c.post(body))
    for body, what in (
            ({"action": "patch_build", "region": "w2u", "product": "0002", "source": {"path": "TetraInstall"}},
             "a lower-case region"),
            ({"action": "patch_build", "region": "W2UU", "product": "0002", "source": {"path": "TetraInstall"}},
             "a four-letter region"),
            ({"action": "patch_build", "region": "W2U", "product": "12", "source": {"path": "TetraInstall"}},
             "a two-digit product"),
            ({"action": "patch_build", "region": "W2U", "product": "9999", "source": {"path": "TetraInstall"}},
             "a product the patch service does not know"),
            ({"action": "patch_build", "region": "W2U", "product": "0003", "version": "2004-07-14",
              "source": {"path": "TetraInstall"}}, "a badly shaped version"),
            ({"action": "patch_build", "region": "W2U", "product": "0003", "source": {"path": "../x"}},
             "a source path with .."),
            ({"action": "patch_build", "region": "W2U", "product": "0003", "source": {"path": "broken.polpatch"}},
             "a source path that is a file"),
            ({"action": "patch_rollback", "region": "W2U", "product": "0002\n", "to": PV1},
             "a product with a newline"),
            ({"action": "patch_remove", "region": "", "product": "0002"}, "an empty region"),
            ({"action": "patch_import", "source": {}}, "an empty source"),
            ({"action": "patch_import", "source": {"path": "x.polpatch", "upload": "y"}}, "two sources"),
            ({"action": "patch_import", "source": {"path": "../../etc/passwd.polpatch"}}, "an import path with .."),
            ({"action": "patch_import", "source": {"path": "TetraInstall"}}, "an import path that is a folder"),
            ({"action": "patch_import", "source": {"path": "broken.polpatch"}}, "a broken .polpatch"),
            ({"action": "patch_import", "source": {"upload": "20000101T000000-abcdef"}}, "an unknown upload")):
        code, r = post(body)
        ok(code == 400 and isinstance(r, dict) and r.get("error"), "%s: 400 (%s)" % (what, r))
    code, r = post({"action": "patch_remove", "region": "W2U", "product": "0002"})
    ok(code == 404, "patch_remove with no such bundle: 404 (%s)" % r)
    code, r = post({"action": "patch_rollback", "region": "W2U", "product": "0002", "to": PV1})
    ok(code == 404, "patch_rollback with no such bundle: 404")
    code, rows, r = patch_rows(c)
    ok(code == 200 and r == {"bundles": []}, "GET /api/patches with an empty mirrors folder (%s)" % r)


def test_patch_uploads(c, port, k):
    full = open(k.full, "rb").read()
    code, h, r = upload(port, full, name="TetraMaster full.polpatch")
    PSEEN.append(r)
    ok(code == 201 and r.get("kind") == "polpatch" and r.get("size") == len(full),
       "upload a .polpatch: 201, kind polpatch, size the file's (%s %s)" % (code, r))
    want = {"region": "W2U", "product": "0002", "kind": "full", "versions": [PV1],
            "title": "Tetra Master (Windows, North America and Europe)"}
    ok(r.get("polpatch") == want, "  with region, product, kind, versions and title: %s" % r.get("polpatch"))
    uid = r.get("upload")
    tree = os.path.join(k.state, "uploads", uid or "x")
    ok(os.listdir(tree) == ["upload.polpatch"] and open(os.path.join(tree, "upload.polpatch"), "rb").read() == full,
       "  kept whole, not unpacked")
    code, lst = pseen(c.get("/api/uploads"))
    mine = [u for u in lst["uploads"] if u["id"] == uid]
    ok(mine and mine[0]["kind"] == "polpatch" and mine[0]["polpatch"] == want,
       "  listed with kind and polpatch")
    code, h, r2 = upload(port, full, name="renamed.zip")
    PSEEN.append(r2)
    ok(code == 201 and r2.get("kind") == "polpatch",
       "a zip named .zip with polpatch.json at its root is a .polpatch too")
    ok(raw_request(port, "DELETE", "/api/uploads/" + r2["upload"])[0] == 204, "  and can be deleted")
    code, h, r3 = upload(port, make_zip(TM_FILES), name="plain.zip")
    ok(code == 201 and r3.get("kind") == "archive" and r3.get("polpatch") is None,
       "a client archive is kind archive with polpatch null (%s)" % r3.get("kind"))
    archive = r3.get("upload")

    def flip(name, data):
        if name.endswith("readme.txt.slc"):
            return data[:-1] + bytes([data[-1] ^ 1])
        return data
    bad = os.path.join(k.dir, "tampered.polpatch")
    retamper(k.full, bad, change=flip)
    code, h, r = upload(port, open(bad, "rb").read(), name="tampered.polpatch")
    PSEEN.append(r)
    ok(code == 400 and "sha256" in str(r.get("error")), "a .polpatch with a changed byte: 400 (%s)" % r)
    for name, extra in (("dotdot.polpatch", [("bundle/../../evil.txt", b"x")]),
                        ("unlisted.polpatch", [("bundle/blobs/%s/Direct/extra.slc" % PV1, b"\x01x")]),
                        ("absolute.polpatch", [("/etc/evil", b"x")])):
        p = os.path.join(k.dir, name)
        retamper(k.full, p, extra=extra)
        code, h, r = upload(port, open(p, "rb").read(), name=name)
        PSEEN.append(r)
        ok(code == 400 and r.get("error"), "%s: refused with 400 (%s)" % (name, r.get("error")))
    code, h, r = upload(port, b"this is not a zip", name="text.polpatch")
    ok(code == 400 and r.get("error"), "a .polpatch that is not a zip: 400 (%s)" % r.get("error"))
    code, lst = c.get("/api/uploads")
    ids = {u["id"] for u in lst["uploads"]}
    left = sorted(n for n in os.listdir(os.path.join(k.state, "uploads"))
                  if n not in ids and n + "" not in {i + ".json" for i in ids} and n != ".incoming")
    ok(ids == {uid, archive} and left == [], "the refused uploads left nothing behind (%s)" % left)
    code, r = pseen(c.post({"action": "import", "game": "tm", "item": "tmdata", "source": {"upload": uid}}))
    ok(code == 400 and "polpatch" in r.get("error", ""), "a client data import from a .polpatch upload: 400")
    code, r = pseen(c.post({"action": "patch_import", "source": {"upload": archive}}))
    ok(code == 400, "patch_import of an archive upload: 400 (%s)" % r)
    code, r = pseen(c.post({"action": "patch_build", "region": "W2U", "product": "0003",
                            "source": {"upload": uid}}))
    ok(code == 400, "patch_build from a .polpatch upload: 400")
    return uid


def test_patch_browse(c, k):
    code, b = pseen(c.get("/api/browse"))
    files = {f["name"]: f for f in b.get("files", [])}
    ok(code == 200 and isinstance(files.get("broken.polpatch", {}).get("polpatch", {}).get("error"), str),
       "browse: an unreadable .polpatch carries polpatch.error (%s)" % files.get("broken.polpatch"))
    code, b = pseen(c.get("/api/browse?path=updates"))
    f = (b.get("files") or [{}])[0]
    ok(code == 200 and f.get("polpatch") == {"region": "W2U", "product": "0002", "kind": "update",
                                            "versions": [PV2],
                                            "title": "Tetra Master (Windows, North America and Europe)"},
       "browse: a .polpatch carries its manifest's fields (%s)" % f.get("polpatch"))
    code, b = c.get("/api/browse?path=TetraInstall")
    ok(all("polpatch" not in x for x in b.get("files", [])), "  other files carry no polpatch field")


def test_patch_jobs(c, k, t, uid):
    t.clear_calls()
    code, j = patch_job(c, {"action": "patch_import", "source": {"upload": uid}})
    ok(code == 202 and j["state"] == "ok" and j.get("region") == "W2U" and j.get("product") == "0002"
       and j.get("source") == {"upload": uid},
       "patch_import of the full upload: ok (%s: %s)" % (j["state"], j["summary"]))
    ok(["compose", "--no-prebuild", "restart", "patch"] in t.calls(),
       "  restarted the patch service through stack.py compose (%s)" % t.calls())
    ok(os.path.isfile(os.path.join(k.bundle(), "patchlist.raw")), "  the bundle is in the mirrors folder")
    ok("[PASS]" in j.get("log", "") and "polselfcheck.py" in j.get("log", ""),
       "  the log has the full check and the self-check")
    code, rows, r = patch_rows(c)
    b = rows.get("W2U/0002", {})
    ok(code == 200 and b.get("versions") == [PV1] and b.get("latest") == PV1 and b.get("served") is True
       and b.get("selfcheck") == "ok" and b.get("selfcheck_why") is None
       and re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", b.get("selfcheck_at") or "")
       and b.get("size", 0) > 0 and b.get("aliases") == []
       and b.get("title") == "Tetra Master (Windows, North America and Europe)",
       "GET /api/patches after the import: %s" % b)
    ok(set(b) == {"region", "product", "title", "aliases", "versions", "latest", "served",
                  "selfcheck", "selfcheck_at", "selfcheck_why", "size"},
       "  a bundle has exactly the contract's fields (%s)" % sorted(b))

    code, j = patch_job(c, {"action": "patch_import",
                            "source": {"path": "updates/W2U-0002-%s.polpatch" % PV2}})
    ok(j["state"] == "ok" and j.get("source") == {"path": "updates/W2U-0002-%s.polpatch" % PV2},
       "patch_import of an update from the clients folder: ok (%s)" % j["summary"])
    code, h, r = upload(k.port, open(k.up3, "rb").read(), name="up3.polpatch")
    code, j = patch_job(c, {"action": "patch_import", "source": {"upload": r["upload"]}})
    ok(j["state"] == "ok", "patch_import of a second update: ok (%s)" % j["summary"])
    code, rows, r = patch_rows(c)
    b = rows.get("W2U/0002", {})
    ok(b.get("versions") == [PV1, PV2, PV3] and b.get("latest") == PV3 and b.get("selfcheck") == "ok",
       "  versions oldest to newest, latest the last (%s)" % b.get("versions"))
    code, j = patch_job(c, {"action": "patch_import", "source": {"upload": uid}})
    ok(j["state"] == "failed" and "drop" in j["summary"],
       "the full file again, which would drop versions, fails (%s)" % j["summary"])

    for to, what in ((PV3, "the latest version"), ("20990101_0", "a version it does not serve"),
                     ("20040714", "a badly shaped version"), (None, "no version")):
        code, r = pseen(c.post({"action": "patch_rollback", "region": "W2U", "product": "0002", "to": to}))
        ok(code == 400 and r.get("error"), "roll back to %s: 400 (%s)" % (what, r.get("error")))
    code, j = patch_job(c, {"action": "patch_rollback", "region": "W2U", "product": "0002", "to": PV1})
    ok(j["state"] == "ok" and j.get("removes") == [PV2, PV3] and j.get("to") == PV1,
       "roll back to %s: ok, removes %s (%s)" % (PV1, j.get("removes"), j["summary"]))
    ok("oldest to newest: %s, %s" % (PV2, PV3) in j.get("log", ""), "  the log names them in that order")
    ok("copy kept before %s" % PV2 in j.get("log", ""), "  using the list files apply kept")
    ok(open(os.path.join(k.bundle(), "patch.cfg"), "rb").read() == k.cfg_v1,
       "  patch.cfg is the first version's again")
    blobs = sorted(os.listdir(os.path.join(k.bundle(), "blobs")))
    ok(blobs == [PV1], "  only the kept version's blobs stay (%s)" % blobs)
    trash = os.path.join(k.state, "patch-trash")
    moved = sorted(v for d in os.listdir(trash) for v in os.listdir(os.path.join(trash, d, "blobs")))
    ok(moved == [PV2, PV3], "  the removed blobs are in the trash folder (%s)" % moved)
    code, rows, r = patch_rows(c)
    b = rows.get("W2U/0002", {})
    ok(b.get("versions") == [PV1] and b.get("latest") == PV1 and b.get("served") and b.get("selfcheck") == "ok",
       "  served at %s and the self-check passes (%s)" % (PV1, b))

    # again, with no kept copies, so the lists are rebuilt
    code, j = patch_job(c, {"action": "patch_import",
                            "source": {"path": "updates/W2U-0002-%s.polpatch" % PV2}})
    ok(j["state"] == "ok", "the update imports again on top of the rolled back bundle")
    shutil.rmtree(os.path.join(k.mirrors, ".polpatch-backup"))
    code, j = patch_job(c, {"action": "patch_rollback", "region": "W2U", "product": "0002", "to": PV1})
    ok(j["state"] == "ok" and "rebuilt" in j.get("log", ""),
       "roll back with no kept copy: the lists are rebuilt (%s)" % j["summary"])
    cfg = open(os.path.join(k.bundle(), "patch.cfg"), "rb").read()
    ok(cfg == k.cfg_v1, "  and patch.cfg comes out exactly as the first version's")
    sys.path.insert(0, os.path.join(ROOT, "services"))
    import slc
    raw = open(os.path.join(k.bundle(), "patchlist.raw"), "rb").read()
    meta = json.load(open(os.path.join(k.bundle(), "meta.json")))
    ok(slc.slc_decompress(raw) == cfg and meta["latest_version"] == PV1
       and all(p.get("version") != PV2 for p in meta.get("published", [])),
       "  patchlist.raw and meta.json agree with it")
    code, rows, r = patch_rows(c)
    ok(rows.get("W2U/0002", {}).get("selfcheck") == "ok", "  and the self-check passes")

    code, j = patch_job(c, {"action": "patch_build", "region": "W2U", "product": "0003", "version": PV1,
                            "walk": True, "source": {"path": "TetraInstall"}})
    ok(j["state"] == "ok" and j.get("source") == {"path": "TetraInstall"},
       "patch_build from a folder under the clients folder: ok (%s)" % j["summary"])
    code, rows, r = patch_rows(c)
    ok([x for x in rows] == ["W2U/0002", "W2U/0003"] and rows["W2U/0003"]["versions"] == [PV1]
       and rows["W2U/0003"]["selfcheck"] == "ok" and rows["W2U/0003"]["served"],
       "  the new bundle is listed, served and checked (%s)" % rows.get("W2U/0003"))
    code, j = patch_job(c, {"action": "patch_build", "region": "W2U", "product": "0004",
                            "source": {"path": "TetraInstall"}})
    ok(j["state"] == "failed" and "file.txt" in j["summary"],
       "patch_build of a folder without file.txt and no walk fails with a sentence (%s)" % j["summary"])

    blob = os.path.join(k.bundle("W2U-0003"), "blobs", PV1, "Direct", "readme.txt.slc")
    data = open(blob, "rb").read()
    open(blob, "wb").write(data[:-1] + bytes([data[-1] ^ 1]))
    code, r = pseen(c.get("/api/patches?recheck=1"))
    ok(code == 202 and r.get("job") and len(r.get("bundles", [])) == 2,
       "GET /api/patches?recheck=1: 202 with the job and the bundles")
    j = c.wait(r["job"], 120)
    PSEEN.append(j)
    ok(j["state"] == "failed" and "W2U/0003" in j["summary"], "  the check job fails on the bad blob (%s)" % j["summary"])
    code, rows, r = patch_rows(c)
    b = rows["W2U/0003"]
    ok(b["selfcheck"] == "failed" and isinstance(b["selfcheck_why"], str) and b["selfcheck_at"]
       and rows["W2U/0002"]["selfcheck"] == "ok",
       "  selfcheck failed with a sentence for that bundle only (%s)" % b["selfcheck_why"])

    code, j = patch_job(c, {"action": "patch_remove", "region": "W2U", "product": "0003"})
    ok(j["state"] == "ok" and not os.path.exists(k.bundle("W2U-0003")),
       "patch_remove: ok, the bundle is out of the mirrors folder (%s)" % j["summary"])
    gone = [d for d in os.listdir(trash) if d.endswith("-W2U-0003")]
    ok(gone and os.path.isfile(os.path.join(trash, gone[0], "patch.cfg"))
       and os.path.join(trash, gone[0]) in j.get("log", ""),
       "  it was moved to the trash folder and the log says where")
    code, rows, r = patch_rows(c)
    ok(list(rows) == ["W2U/0002"], "  and is no longer listed")
    code, r = pseen(c.post({"action": "patch_rollback", "region": "W2U", "product": "0002", "to": PV1}))
    ok(code == 400, "roll back a bundle that holds one version: 400")


def test_patch_leaks(k, t):
    bad = []
    keys = set()
    for obj in PSEEN:
        walk_keys(obj, keys)

        def strings(o, path=""):
            if isinstance(o, dict):
                for kk, v in o.items():
                    if kk in ("log",):
                        continue
                    if kk == "root" and path == "":
                        continue      # browse names its root, as the contract says
                    strings(v, kk)
            elif isinstance(o, list):
                for v in o:
                    strings(v, path)
            elif isinstance(o, str):
                for needle in (t.tmp, os.path.realpath(t.tmp), "patchtool", "polpatch.py ",
                               "polselfcheck", "--mirrors"):
                    if needle in o:
                        bad.append(o)
        strings(obj)
    ok(not bad, "no answer spells out a path or a command line (%s)" % bad[:3])
    ok("argv" not in keys and "_file" not in keys and "_input" not in keys,
       "  and none has argv or the job's own paths (%s)" % sorted(x for x in keys if x.startswith("_")))


def run_patch_tests(t, servers):
    k = PatchKit(t)
    s0 = Server(t, args=["--state", os.path.join(t.tmp, "state-nopatch")])
    servers.append(s0)
    c = s0.start()
    code, r = c.get("/api/patches")
    ok(code == 409 and "POL_DEPLOY_MIRRORS" in r.get("error", ""), "GET /api/patches with no mirrors: 409")
    code, r = c.post({"action": "patch_check"})
    ok(code == 409, "a patch action with no mirrors: 409")
    s0.stop()
    s = Server(t, extra_env=k.env(), args=k.args())
    servers.append(s)
    c = s.start()
    k.port = s.port
    try:
        test_patch_formats(c, k)
        uid = test_patch_uploads(c, s.port, k)
        test_patch_browse(c, k)
        test_patch_jobs(c, k, t, uid)
        test_patch_leaks(k, t)
    finally:
        s.stop()
        k.stop_patch_service()


def main():
    t = Tree()
    servers = []
    try:
        test_refuses_without_token(t)
        s = Server(t, extra_env={"FAKE_ECHO": "echo before %s after" % TOKEN})
        servers.append(s)
        c = s.start()
        test_auth(c)
        test_validation(c)
        test_lifecycle(c, t)
        test_needs_data(c, t)
        test_busy_and_order(c, t)
        test_actions(c, t)
        test_update(c, t)
        test_games(c, t)
        big = test_logs_and_token(c, t)
        code, j = c.get("/api/jobs/" + c.get("/api/jobs?limit=50")[1]["jobs"][-1]["id"])
        ok("echo before [token] after" in j["log"] and "token in env: <absent>" in j["log"],
           "the token is blanked in output and absent from the children's environment")
        s.stop()

        # a restart keeps the history, fails what was cut off, and prunes to 50
        t.put_job("20000101T000000-aaaaaa", "running", time.time() + 5000)
        for i in range(60):
            t.put_job("20000102T0000%02d-bbbbbb" % (i % 60), "ok", 2.0 + i)
        s2 = Server(t)
        servers.append(s2)
        c = s2.start()
        code, j = c.get("/api/jobs/" + big)
        ok(code == 200 and j["state"] == "ok", "after a restart: the jobs are still there")
        names = os.listdir(os.path.join(t.state, "jobs"))
        n = len(names)
        ok(n == 100, "after a restart: 50 jobs kept (%d files)" % n)
        cut = c.get("/api/jobs/20000101T000000-aaaaaa")
        ok(cut[0] == 200 and cut[1]["state"] == "failed" and "stopped" in cut[1]["summary"],
           "a job left running by the last run is failed")
        ok(c.get("/api/health")[1]["busy"] is False, "  and does not hold the lock")
        s2.stop()

        run_import_tests(t, servers)
        run_patch_tests(t, servers)
        hits = scan_for_token(t, servers)
        ok(not hits, "the token is in no job file or server log (%s)" % hits)
    finally:
        for s in servers:
            s.stop() if s.proc and s.proc.poll() is None else None
        # git marks its objects read-only, which Windows will not delete
        shutil.rmtree(t.tmp, onerror=lambda f, p, e: (os.chmod(p, 0o700), f(p)))
    print("deployer_test: %d failed, %d skipped" % (len(FAILS), len(SKIPS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
