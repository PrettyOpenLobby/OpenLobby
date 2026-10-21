#!/usr/bin/env python3
"""Offline test for services/deployer/deployer.py.

    python tools/deployer_test.py

Starts the deployer on 127.0.0.1 with a stand-in for tools/stack.py (so no
docker and no network are used) against a scratch directory of checkouts,
and checks the HTTP API: the token, action and game validation, the one-job
lock, job states and logs, client-data detection, the dirty-checkout refusal
on update (with real git, against a local bare repository), and that the
token never reaches a log. Needs git on PATH for the update part.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEPLOYER = os.path.join(ROOT, "services", "deployer", "deployer.py")
TOKEN = "test-token-6f1d2c9a8b7e4d3c"

FAILS = []
SKIPS = []


def ok(cond, what):
    print(("ok   " if cond else "FAIL ") + what)
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
    def __init__(self, t, token=TOKEN, extra_env=None):
        self.t, self.token, self.extra = t, token, extra_env or {}
        self.proc = None

    def argv(self, port):
        return [sys.executable, DEPLOYER, "--bind", "127.0.0.1:%d" % port, "--root", self.t.root,
                "--stack", self.t.fake, "--state", self.t.state]

    def env(self):
        env = dict(os.environ)
        env.update({"POL_DEPLOYER_TOKEN": self.token, "FAKE_STACK_DIR": self.t.fakedir,
                    "POL_DEPLOY_GHCR": ""})
        env.pop("COMPOSE_PROFILES", None)
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
    ok(j["missing"] == [{"path": FE_MISSING["games"][0]["missing"][0]["path"],
                         "hint": FE_MISSING["games"][0]["missing"][0]["hint"]}],
       "job missing lists the required item as {path, hint}: %s" % j["missing"])
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
