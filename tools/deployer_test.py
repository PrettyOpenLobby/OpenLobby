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

The update check (GET /api/updates) runs against a stand-in GitHub API on
127.0.0.1 and a stand-in docker CLI: a checkout game behind its remote
(merges dropped, at most 50 changes), current and offline; the core's
published image behind, current, without a revision, pinned, rate limited
and offline; the one-hour cache surviving a restart, and the limit on
forced checks. Nothing reaches the network.

The start-up check that the Docker daemon sees the core checkout where the
deployer does (Docker Desktop's VM sees a Windows folder elsewhere) runs
against a stand-in docker CLI that mounts the folder, reads another one, or
refuses the mount, and a deployer whose check failed must refuse every
compose action before it changes anything. Who clones a game on Docker
Desktop (nobody, so its imports can write) is checked with a stand-in
os.stat.

The updater of a paired game (POST /api/updater, docs/deployer.md "Updaters of
paired games") runs against a stand-in docker CLI that runs the command in
this process: refused bodies start nothing, Windows and Linux folders become
the daemon's path, the folder is checked in a throwaway container first, the
updater runs in the deployer's own image with the folder as its working
directory, the socket, the command as approved and a minimal environment;
exit 0, exit 1 (its last line the summary), a silent exit, a missing folder,
the timeout, no output for 15 minutes (minutes shortened with --minute), one
job at a time, and a deployer outside a container.

The dev channel (GET and POST /api/channel, docs/deployer.md "Dev channel")
runs against a stand-in registry on 127.0.0.1 (the registry API's token
handshake) and a stand-in docker CLI: bad requests and a wrong code are
refused before any job or docker command; a good code unlocks the dev
builds (signs in once with the secret on stdin into the deployer's own
DOCKER_CONFIG, is kept mode 600, writes .channel) and changes nothing else;
each key then switches on its own, pulling and restarting only its own
services; the update check compares the dev image's digest with the
registry's; a revoked code fails the next update of a key on Dev with the
sentence the panel shows and marks those keys, while a key on Stable goes
on and switching back to Stable needs no code; forgetting the code signs
out; a .channel from before reads as every key on Dev; --channel does it
all at once for the setup scripts; a short code that redeems to an olx2.
code is offered to the patch store; the issuer of short codes is the
environment's, then the owner's, then the project's own. The code is in no
argument, job file, log or answer.

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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
with open(os.path.join(d, "envs.txt"), "a") as f:
    f.write(json.dumps({"args": args, "docker_config": os.environ.get("DOCKER_CONFIG")}) + "\n")
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
    keys = sorted(set(state()) | {args[1]})       # read before "w" empties the file
    with open(os.path.join(core, ".games"), "w") as f:
        f.write("\n".join(keys) + "\n")
elif cmd == "disable":
    keys = [k for k in state() if k != args[1]]
    with open(os.path.join(core, ".games"), "w") as f:
        f.write("\n".join(keys) + "\n")
elif cmd == "channel":
    # the real .channel writer (services/deployer/devchannel.py), as tools/stack.py uses it
    sys.path.insert(0, os.environ["FAKE_DEVCHANNEL_DIR"])
    import devchannel
    reg = args[args.index("--registry") + 1] if "--registry" in args else None
    src = args[args.index("--source") + 1] if "--source" in args else None
    if args[1] == "dev":
        devchannel.write_channel(core, "dev", reg, src, catalog=cat)
    elif args[1] == "unlock":
        devchannel.unlock(core, reg, src, catalog=cat)
    elif args[1] == "set":
        devchannel.set_key(core, args[2], args[3], catalog=cat)
    elif args[1] == "lock":
        devchannel.lock(core, catalog=cat)
    else:
        devchannel.write_channel(core, "stable")
elif cmd == "volumes":
    keys = args[1].split(",")
    report = args[args.index("--report") + 1]
    fail = os.path.join(d, "volumes_fail")
    why = open(fail).read() if os.path.exists(fail) else ""
    res = []
    for k in keys:
        for v in cat[k].get("volumes", []):
            if not (v.get("required") and v.get("fill")):
                continue
            if why:
                print(v["name"] + ": not set up. " + why)
                res.append({"game": k, "volume": v["name"], "state": "failed", "files": 0, "why": why})
            else:
                print(v["name"] + ": 304 files, ready.")
                res.append({"game": k, "volume": v["name"], "state": "filled", "files": 304, "why": ""})
    if not os.path.exists(os.path.join(d, "volumes_noreport")):
        json.dump({"ok": not why, "volumes": res}, open(report, "w"))
    sys.exit(1 if why else 0)
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
                    "POL_DEPLOY_GHCR": "", "POL_DEPLOY_GITHUB_API": self.t.no_github,
                    "FAKE_DEVCHANNEL_DIR": os.path.join(ROOT, "services", "deployer"),
                    # short codes reach no network unless a test names an issuer
                    "POL_DEV_ISSUER": "off"})
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
        self.no_github = "http://127.0.0.1:%d" % free_port()     # nothing listens there
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
    ok(g["core"]["running"] is True and g["options"] == {"ghcr": False, "channel": "stable", "problem": None},
       "core running, options")
    t.flag("ps.json", on=False)
    t.flag("check.json", on=False)


def test_volumes(c, t):
    """A game with volumes the catalog says how to fill: install and enable
    run stack.py volumes first; a failure stops the enable with a plain
    sentence and leaves a button-free note on the games list; a success
    clears it."""
    os.makedirs(os.path.join(t.root, "hippaulbridge"), exist_ok=True)
    t.clear_calls()
    j = c.run("enable", "ffxi")
    calls = t.calls()
    vi = next((i for i, a in enumerate(calls) if a[0] == "volumes"), None)
    ok(j["state"] == "ok" and vi is not None and calls[vi][1] == "ffxi" and "--report" in calls[vi],
       "enable ffxi: stack.py volumes ffxi --report runs (%s)" % calls)
    ok(vi is not None and vi < calls.index(["enable", "ffxi"]) < max(
        i for i, a in enumerate(calls) if a[0] == "compose"),
       "  before stack.py enable and compose up")
    ok("lsb_navmeshes: 304 files, ready." in j["log"] and "lsb_ximeshes: 304 files, ready." in j["log"],
       "  its lines are in the job log")
    ok(not [n for n in os.listdir(t.state) if n.startswith("volumes-")], "  its report file is removed")
    t.clear_calls()
    c.run("disable", "ffxi")
    j = c.run("enable", "tm")
    ok(j["state"] == "ok" and not any(a[0] == "volumes" for a in t.calls()),
       "enable tm (no volumes to fill): stack.py volumes is not run")

    why = ("Downloading the map data for Final Fantasy XI failed. This computer could not reach "
           "ghcr.io. Check its internet connection, then try again.")
    t.flag("volumes_fail", text=why)
    t.clear_calls()
    j = c.run("enable", "ffxi")
    ok(j["state"] == "failed" and why in j["summary"] and "not turned on" in j["summary"],
       "fill failing: enable fails with the plain reason (%s)" % j["summary"])
    ok(not any(a[0] in ("enable", "compose") for a in t.calls()), "  and nothing was enabled or started")
    ok([m["path"] for m in j["missing"]] == ["volume lsb_navmeshes", "volume lsb_ximeshes"]
       and all(why in m["hint"] and "import" not in m for m in j["missing"]),
       "  the job lists both volumes with the reason and no import: %s" % j["missing"])
    ok("lsb_navmeshes: not set up. " + why in j["log"], "  the log has the routine's line")
    ffxi = json.load(open(os.path.join(t.core, "games", "ffxi.json")))
    setup = " && ".join(ffxi["volumes"][0]["setup"])
    missing = {"ok": False, "warnings": [], "games": [{
        "key": "ffxi", "name": ffxi["name"], "installed": True, "enabled": False, "data_ok": False,
        "rows": [["fail", "ffxi: volume lsb_navmeshes missing"]],
        "missing": [{"id": "lsb_navmeshes", "path": "volume lsb_navmeshes", "hint": "sets itself up",
                     "required": True, "source": "docker volume", "tool": setup, "fill": True}]}]}
    t.flag("check.json", text=json.dumps(missing))
    time.sleep(5.2)  # past the /api/games cache
    by = {x["key"]: x for x in c.get("/api/games")[1]["games"]}
    m = by["ffxi"]["missing"]
    ok(len(m) == 1 and m[0]["hint"].startswith("Setting it up by itself failed: " + why)
       and setup in m[0]["hint"] and "import" not in m[0],
       "games list: the failed volume's note says why, with the commands by hand (%s)" % m)
    t.flag("volumes_fail", on=False)
    t.flag("check.json", on=False)
    t.clear_calls()
    j = c.run("install", "ffxi")
    ok(j["state"] == "ok" and any(a[0] == "volumes" for a in t.calls()),
       "install ffxi (already cloned): fills its volumes (%s)" % j["summary"])
    t.flag("check.json", text=json.dumps(missing))
    time.sleep(5.2)
    by = {x["key"]: x for x in c.get("/api/games")[1]["games"]}
    ok(by["ffxi"]["missing"][0]["hint"] == "sets itself up",
       "  after a fill that worked the note is stack.py's own again")

    t.flag("volumes_fail", text="x")
    t.flag("volumes_noreport")
    j = c.run("enable", "ffxi")
    ok(j["state"] == "failed" and "stopped (exit 1)" in j["summary"],
       "stack.py volumes with no report: failed, said so (%s)" % j["summary"])
    t.flag("volumes_noreport", on=False)
    t.flag("volumes_fail", on=False)
    t.flag("check.json", on=False)
    if t.git:
        # update: a game whose new version needs a volume gets it before the restart
        bridge = os.path.join(t.root, "hippaulbridge")
        shutil.rmtree(bridge)
        git("clone", "-q", os.path.join(t.tmp, "origin.git"), bridge)
        c.run("enable", "ffxi")
        t.flag("volumes_fail", text=why)
        t.clear_calls()
        j = c.run("update", "ffxi")
        calls = t.calls()
        ok(j["state"] == "failed" and "updated but not restarted" in j["summary"] and why in j["summary"]
           and any(a[0] == "volumes" for a in calls) and not any(a[0] == "compose" for a in calls),
           "update ffxi with the fill failing: not restarted, says why (%s)" % j["summary"])
        t.flag("volumes_fail", on=False)
        t.clear_calls()
        j = c.run("update", "ffxi")
        calls = [a[0] for a in t.calls()]
        ok(j["state"] == "ok" and "volumes" in calls and calls.index("volumes") < calls.index("compose"),
           "update ffxi: volumes, then compose up (%s)" % calls)
    else:
        SKIPS.append("update ffxi fills its volumes (no git)")
        print("SKIP update ffxi fills its volumes (no git)")
    c.run("disable", "ffxi")
    shutil.rmtree(os.path.join(t.root, "hippaulbridge"), onerror=lambda f, p, e: (os.chmod(p, 0o700), f(p)))


# ------------------------------------------------------------ update checks

# The stand-in docker CLI: `docker image inspect <ref>` answers from
# FAKE_DOCKER_DIR/inspect.json ({ref: image}), exit 1 for an unknown ref.
FAKE_DOCKER = r"""
import json, os, sys
args = sys.argv[1:]
if args[:2] != ["image", "inspect"]:
    sys.exit(2)
try:
    images = json.load(open(os.path.join(os.environ["FAKE_DOCKER_DIR"], "inspect.json")))
except (OSError, ValueError):
    images = {}
if args[2] not in images:
    sys.stderr.write("Error: No such image: " + args[2] + "\n")
    sys.exit(1)
print(json.dumps([images[args[2]]]))
"""


def sha(n, salt="kart"):
    return hashlib.sha1(("%s-%d" % (salt, n)).encode()).hexdigest()


# ------------------------------------------------- where Docker sees the checkouts

# The stand-in docker CLI for the start-up check (check_paths): `inspect`
# names an image, and `run` runs the probe code itself with /probe standing
# for the --mount source, the way Docker Engine on Linux mounts it. The file
# FAKE_PROBE_DIR/mode makes it act out the other cases: "other" reads another
# folder (a daemon that sees a different filesystem), "missing" is Docker
# Desktop refusing a source its VM does not have, "noinspect" and "noimage"
# are Docker not answering the way the check needs.
FAKE_PROBE_DOCKER = r"""
import csv, io, json, os, subprocess, sys
args = sys.argv[1:]
d = os.environ["FAKE_PROBE_DIR"]
mode = open(os.path.join(d, "mode")).read().strip()
with open(os.path.join(d, "calls.txt"), "a") as f:
    f.write(json.dumps(args) + "\n")
if args[0] == "inspect":
    if mode == "noinspect":
        sys.stderr.write("Error: No such object: " + args[-1] + "\n")
        sys.exit(1)
    print("sha256:" + "ab" * 32)
    sys.exit(0)
if args[0] == "run":
    if mode == "missing":
        sys.stderr.write('docker: Error response from daemon: invalid mount config for type "bind": '
                         'bind source path does not exist: /srv/pol/openlobby\n'
                         "Run 'docker run --help' for more information\n")
        sys.exit(125)
    if mode == "noimage":
        sys.stderr.write("docker: Error response from daemon: No such image: sha256:abab\n")
        sys.exit(125)
    fields = next(csv.reader(io.StringIO(args[args.index("--mount") + 1])))
    src = [x[len("source="):] for x in fields if x.startswith("source=")][0]
    if mode == "other":
        src = os.path.join(d, "elsewhere")
    code = args[args.index("-c") + 1].replace("'/probe'", repr(src))
    sys.exit(subprocess.run([sys.executable, "-c", code]).returncode)
sys.exit(2)
"""


def probe_docker(t):
    """(stand-in docker path, its folder); the folder's mode file says how it acts."""
    d = os.path.join(t.tmp, "probedocker")
    os.makedirs(os.path.join(d, "elsewhere"), exist_ok=True)
    with open(os.path.join(d, "elsewhere", "docker-compose.yml"), "w") as f:
        f.write("services: {}  # another install\n")
    p = os.path.join(d, "docker.py")
    with open(p, "w") as f:
        f.write(FAKE_PROBE_DOCKER)
    return p, d


def set_mode(d, mode):
    with open(os.path.join(d, "mode"), "w") as f:
        f.write(mode)


def test_check_paths(t):
    """check_paths itself: every outcome, and the probe container it asks for."""
    import types
    sys.path.insert(0, os.path.join(ROOT, "services", "deployer"))
    import deployer as dep
    dock, d = probe_docker(t)
    core = os.path.join(t.tmp, "kart,club", "openlobby")      # a comma, which --mount must quote
    os.makedirs(core)
    with open(os.path.join(core, "docker-compose.yml"), "w") as f:
        f.write("services: {}  # Kart Club\n")
    cfg = types.SimpleNamespace(docker=[sys.executable, dock], core=core, token=TOKEN)
    os.environ["FAKE_PROBE_DIR"] = d        # check_paths hands the stand-in this environment
    set_mode(d, "same")
    state, note = dep.check_paths(cfg, "c0ffee")
    ok(state == "ok" and core in note, "paths: the daemon reads the same compose file: ok (%s)" % note)
    with open(os.path.join(d, "calls.txt")) as f:
        calls = [json.loads(l) for l in f]
    run = calls[-1]
    ok(calls[0][:3] == ["inspect", "--format", "{{.Image}}"] and calls[0][-1] == "c0ffee",
       "paths: asks Docker for this container's image")
    ok(run[:2] == ["run", "--rm"] and run[run.index("--pull") + 1] == "never"
       and run[run.index("--network") + 1] == "none" and "sha256:" + "ab" * 32 in run,
       "paths: a throwaway container of its own image, no pull, no network (%s)" % run[:9])
    spec = run[run.index("--mount") + 1]
    ok(spec.startswith("type=bind,") and spec.endswith(",target=/probe,readonly")
       and '"source=%s"' % core in spec, "paths: the core mounted read-only, the source quoted (%s)" % spec)
    set_mode(d, "other")
    state, note = dep.check_paths(cfg, "c0ffee")
    ok(state == "wrong" and "POL_DEPLOY_ROOT" in note and "/run/desktop/mnt/host/c/" in note,
       "paths: the daemon reads another folder: wrong, with the Docker Desktop path to use")
    set_mode(d, "missing")
    state, note = dep.check_paths(cfg, "c0ffee")
    ok(state == "wrong" and "bind source path does not exist" in note and "docker run --help" not in note,
       "paths: Docker cannot mount the folder: wrong, quoting Docker's line (%s)" % note[:160])
    for mode in ("noinspect", "noimage"):
        set_mode(d, mode)
        state, note = dep.check_paths(cfg, "c0ffee")
        ok(state == "unchecked", "paths: %s: unchecked, jobs run as before (%s)" % (mode, note))
    state, note = dep.check_paths(cfg, "")
    ok(state == "unchecked", "paths: outside a container: unchecked")
    os.remove(os.path.join(core, "docker-compose.yml"))
    set_mode(d, "same")
    state, note = dep.check_paths(cfg, "c0ffee")
    ok(state == "wrong" and "cannot read docker-compose.yml" in note,
       "paths: no compose file in the core here: wrong")


def test_paths_server(t, servers):
    """A deployer whose check found the paths wrong: it says so everywhere and
    runs no compose action, nor the git pull or stack.py enable before one."""
    dock, d = probe_docker(t)
    with open(os.path.join(t.core, "docker-compose.yml"), "w") as f:
        f.write("services: {}  # Kart Club\n")
    set_mode(d, "missing")
    s = Server(t, extra_env={"FAKE_PROBE_DIR": d}, args=["--container", "c0ffee", "--docker", dock])
    servers.append(s)
    c = s.start()
    code, h = c.get("/api/health")
    ok(code == 200 and h["paths"] == "wrong" and "POL_DEPLOY_ROOT" in (h["problem"] or ""),
       "paths wrong: /api/health says so with the sentence")
    code, g = c.get("/api/games")
    ok(g["options"]["problem"] == h["problem"], "paths wrong: /api/games carries it for the panel")
    hc = subprocess.run([sys.executable, DEPLOYER, "--healthcheck", "--bind", "127.0.0.1:%d" % s.port],
                        env=s.env(), capture_output=True, text=True, timeout=30)
    ok(hc.returncode == 1 and "POL_DEPLOY_ROOT" in hc.stdout,
       "paths wrong: --healthcheck exits 1 and prints why (%d)" % hc.returncode)
    t.clear_calls()
    for action, game in (("enable", "jan"), ("disable", "tm"), ("restart", "core"), ("update", "core")):
        j = c.run(action, game)
        ok(j["state"] == "failed" and j["summary"] == h["problem"],
           "paths wrong: %s %s refused with the sentence" % (action, game))
        ok("$ git" not in j.get("log", ""), "  before git ran")
    ok(not [x for x in t.calls() if x[0] in ("enable", "disable", "compose")],
       "paths wrong: stack.py enable, disable and compose never ran (%s)" % t.calls())
    j = c.run("check", "tm")
    ok(j["state"] == "ok", "paths wrong: check still runs (it starts no container)")
    s.stop()
    set_mode(d, "same")
    s = Server(t, extra_env={"FAKE_PROBE_DIR": d}, args=["--container", "c0ffee", "--docker", dock])
    servers.append(s)
    c = s.start()
    code, h = c.get("/api/health")
    ok(h["paths"] == "ok" and h["problem"] is None, "paths right: /api/health paths ok")
    hc = subprocess.run([sys.executable, DEPLOYER, "--healthcheck", "--bind", "127.0.0.1:%d" % s.port],
                        env=s.env(), capture_output=True, text=True, timeout=30)
    ok(hc.returncode == 0 and hc.stdout.strip() == "paths ok", "paths right: --healthcheck prints paths ok")
    j = c.run("restart", "core")
    ok(j["state"] == "ok", "paths right: compose actions run (%s)" % j["summary"])
    s.stop()
    os.remove(os.path.join(t.core, "docker-compose.yml"))


# ------------------------------------------------- the updater of a paired game
#
# A stand-in docker CLI for POST /api/updater. "inspect" names the image; a "run" with
# --network none is a probe (the start-up check, or the folder check), run here against the
# folder its --mount names, translated back from Docker Desktop's /run/desktop/mnt/host/<drive>/
# the way the daemon would read it; any other "run" is the updater itself, run IN this process
# (so killing it kills it) with the -w folder as its working directory and only the -e
# variables as its environment. "ps" and "kill" find and stop it by its job label. Every call
# is kept with the environment the CLI was started with.
FAKE_UPD_DOCKER = r"""
import csv, io, json, os, runpy, signal, subprocess, sys
args = sys.argv[1:]
d = os.environ["FAKE_UPD_DIR"]
MNT = "/run/desktop/mnt/host/"
with open(os.path.join(d, "calls.txt"), "a") as f:
    f.write(json.dumps({"args": args, "token": os.environ.get("POL_DEPLOYER_TOKEN"),
                        "pol": sorted(k for k in os.environ if k.startswith("POL_"))}) + "\n")


def host(p):
    if p.startswith(MNT):
        rest = p[len(MNT):]
        drive, _, tail = rest.partition("/")
        return drive + ":/" + tail
    return p


def mounts():
    out = []
    for i, a in enumerate(args):
        if a == "--mount":
            fields = next(csv.reader(io.StringIO(args[i + 1])))
            kv = dict(x.split("=", 1) for x in fields if "=" in x)
            out.append((kv["source"], kv["target"]))
    return out


if args[0] == "inspect":
    print("sha256:" + "cd" * 32)
    sys.exit(0)
if args[0] == "ps":
    label = args[args.index("--filter") + 1].split("=", 2)
    want = label[2] if len(label) > 2 else None
    for n in os.listdir(os.path.join(d, "running")):
        if want is None or open(os.path.join(d, "running", n)).read().split()[0] == want:
            print(n)
    sys.exit(0)
if args[0] == "kill":
    for n in args[1:]:
        p = os.path.join(d, "running", n)
        try:
            pid = int(open(p).read().split()[1])
            os.kill(pid, signal.SIGTERM)
            os.remove(p)            # --rm: a killed container is gone
        except (OSError, ValueError, IndexError):
            pass
    sys.exit(0)
if args[0] == "run" and "--network" in args and args[args.index("--network") + 1] == "none":
    src = host(mounts()[0][0])
    if not os.path.exists(src):
        sys.stderr.write('docker: Error response from daemon: invalid mount config for type "bind": '
                         "bind source path does not exist: " + mounts()[0][0] + "\n")
        sys.exit(125)
    code = args[args.index("-c") + 1].replace("'/probe'", repr(src))
    sys.exit(subprocess.run([sys.executable, "-c", code]).returncode)
if args[0] == "run":
    name = args[args.index("--name") + 1]
    job = [a.split("=", 1)[1] for a in args if a.startswith("org.openlobby.updater.job=")][0]
    with open(os.path.join(d, "running", name), "w") as f:
        f.write("%s %d" % (job, os.getpid()))
    i = args.index("--entrypoint")
    rest = args[i + 3:]
    env = {}
    for j, a in enumerate(args):
        if a == "-e":
            k, _, v = args[j + 1].partition("=")
            env[k] = v
        if a == "--env-file":
            p = args[j + 1]
            st = os.stat(p)
            with open(os.path.join(d, "envfile.json"), "w") as f:
                json.dump({"path": p, "mode": st.st_mode & 0o777, "text": open(p).read()}, f)
            for line in open(p).read().splitlines():
                k, _, v = line.partition("=")
                env[k] = v
    for k in ("SYSTEMROOT", "PATH"):
        if os.environ.get(k):
            env.setdefault(k, os.environ[k])
    os.environ.clear()
    os.environ.update(env)
    os.chdir(host(args[args.index("-w") + 1]))
    sys.argv = rest
    try:
        if rest[0] == "-c":
            exec(compile(rest[1], "<updater>", "exec"), {"__name__": "__main__"})
        else:
            runpy.run_path(rest[0], run_name="__main__")
        rc = 0
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    finally:
        sys.stdout.flush()
        try:
            os.remove(os.path.join(d, "running", name))
        except OSError:
            pass
    os._exit(rc)
sys.exit(2)
"""

UPD_SCRIPTS = {
    "ok.py": "import json, os, sys\n"
             "print('Kart Club: checking for a new version')\n"
             "print('Kart Club: fetched 9f8e7d6c')\n"
             "json.dump({'cwd': os.getcwd(), 'argv': sys.argv[1:], 'env': dict(os.environ)},"
             " open('seen.json', 'w'))\n"
             "print('The update is done.')\n",
    "fail.py": "print('Checking free space...')\nprint('Update failed: the disk is full.')\nraise SystemExit(1)\n",
    "silent.py": "raise SystemExit(2)\n",
    "slow.py": "import time\nwhile True:\n    print('still building', flush=True)\n    time.sleep(0.1)\n",
    "quiet.py": "import time\nprint('started', flush=True)\ntime.sleep(60)\n",
}


def updater_tree(t):
    """(stand-in docker, its folder, the game's folder with the scripts)."""
    d = os.path.join(t.tmp, "upddocker")
    os.makedirs(os.path.join(d, "running"), exist_ok=True)
    dock = os.path.join(d, "docker.py")
    with open(dock, "w") as f:
        f.write(FAKE_UPD_DOCKER)
    game = os.path.join(t.tmp, "Kart Club, the game")       # a space and a comma in the path
    os.makedirs(game, exist_ok=True)
    for n, code in UPD_SCRIPTS.items():
        with open(os.path.join(game, n), "w") as f:
            f.write(code)
    return dock, d, game


def upd_calls(d):
    try:
        with open(os.path.join(d, "calls.txt")) as f:
            return [json.loads(l) for l in f if l.strip()]
    except OSError:
        return []


def test_updater_unit():
    sys.path.insert(0, os.path.join(ROOT, "services", "deployer"))
    import gameupdater as gu
    for folder, seen in (("C:\\Users\\x\\Game", "/run/desktop/mnt/host/c/Users/x/Game"),
                         ("D:/Games//Kart Club\\", "/run/desktop/mnt/host/d/Games/Kart Club"),
                         ("/srv/kart/", "/srv/kart"), ("/srv//kart", "/srv/kart"),
                         ("/run/desktop/mnt/host/c/Games/Kart", "/run/desktop/mnt/host/c/Games/Kart")):
        ok(gu.daemon_path(folder) == seen, "updater path: %s -> %s (%s)" % (folder, seen, gu.daemon_path(folder)))
    rec, bad = gu.check({"folder": "/srv/kart", "command": ["python3", "u.py"]})
    ok(bad is None and rec["timeout_minutes"] == 60, "updater check: the timeout defaults to 60")
    for what, block in (("argv", {"folder": "/srv/kart", "command": ["python"], "argv": ["sh"]}),
                        ("sh", {"folder": "/srv/kart", "command": ["sh", "-c", "x"]}),
                        ("a system folder", {"folder": "/usr/lib/kart", "command": ["python"]}),
                        ("..", {"folder": "C:\\Games\\..\\Windows", "command": ["python"]})):
        ok(gu.check(block)[1] is not None, "updater check: %s refused" % what)
    ok(gu.shown(["python", "setup.py", "--name", "Kart Club", "it's"]) ==
       "python setup.py --name 'Kart Club' 'it'\"'\"'s'", "updater: the command shell-quoted for showing")
    rec, bad = gu.check({"folder": "/srv/kart", "command": ["python3", "u.py"], "channels": ["stable", "dev"]})
    ok(bad is None and rec["channels"] == ["stable", "dev"], "updater check: channels kept")
    for what, chans in (("no stable", ["dev"]), ("a list of 5", ["stable", "a", "b", "c", "d"]),
                        ("an upper-case name", ["stable", "Dev"]), ("a name twice", ["stable", "stable"]),
                        ("not a list", "stable")):
        ok(gu.check({"folder": "/srv/kart", "command": ["python"], "channels": chans})[1] is not None,
           "updater check: channels with %s refused" % what)
    ok(gu.with_channel(rec, "dev") == ["python3", "u.py", "--channel", "dev"] and gu.with_channel(rec, None) ==
       ["python3", "u.py"] and gu.with_channel(rec, "beta") is None
       and gu.with_channel({"command": ["python"]}, "dev") is None, "updater: with_channel")


class FakeIssuer:
    """POST /api/dev-code/redeem as a panel answers it: self.codes maps a short code (12
    characters) to the access code it redeems to; the first install id binds it."""

    def __init__(self):
        self.codes = {}
        self.bound = {}
        self.asked = []
        iss = self

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

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                iss.asked.append((self.path, body))
                if self.path != "/api/dev-code/redeem":
                    return self.send(404, {"error": "not found"})
                code, inst = body.get("code"), body.get("install_id")
                if code not in iss.codes:
                    return self.send(404, {"error": "That dev code is not known here. Check it, or ask for a new one."})
                if iss.bound.setdefault(code, inst) != inst:
                    return self.send(409, {"error": "That dev code is already used by another server."})
                return self.send(200, {"ok": True, "access_code": iss.codes[code]})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        if self.srv is not None:
            self.srv.shutdown()
            self.srv.server_close()
            self.srv = None


UPD_SCRIPTS["chan.py"] = ("import json, os, sys\n"
                          "print('Kart Club: switching channel')\n"
                          "json.dump({'argv': sys.argv[1:], 'code': os.environ.get('OPENLOBBY_DEV_CODE')},"
                          " open('chan.json', 'w'))\n"
                          "print('Kart Club: the code is ' + str(os.environ.get('OPENLOBBY_DEV_CODE')))\n"
                          "if '--refuse' in sys.argv: print('Kart Club cannot switch: no database backup ' + "
                          "str(os.environ.get('OPENLOBBY_DEV_CODE'))); raise SystemExit(4)\n")


def test_updater_channels(t, servers, dock, d, game):
    """A paired game's channel switch: --channel appended, the access code in the container's
    environment through a 600 --env-file that is gone after the run, never in an argument, the
    log or the job file; a short code redeemed at POL_DEV_ISSUER with the install id first."""
    sys.path.insert(0, os.path.join(ROOT, "services", "deployer"))
    import devchannel
    with open(os.path.join(game, "chan.py"), "w") as f:
        f.write(UPD_SCRIPTS["chan.py"])
    iss = FakeIssuer()
    full = devchannel.make_code("kart.example.org/dev", "kart-dev-token-71Xq", "https://code.example.org/kart")
    iss.codes["KARTZZZZDEV2"] = full
    state = os.path.join(t.tmp, "state-updchan")
    s = Server(t, extra_env={"FAKE_UPD_DIR": d, "POL_DEV_ISSUER": iss.url},
               args=["--container", "c0ffee", "--docker", dock, "--minute", "0.2", "--state", state])
    servers.append(s)
    try:
        c = s.start()
        inst = c.get("/api/channel")[1].get("install_id")
        ok(isinstance(inst, str) and devchannel.INSTALL_ID.match(inst) and inst.startswith("ol-"),
           "install id: made, shown to the panel (%s)" % inst)
        p = os.path.join(state, "install_id")
        ok(os.path.isfile(p) and (os.name == "nt" or os.stat(p).st_mode & 0o777 == 0o600),
           "install id: kept in the state, mode 600")
        ok(c.get("/api/channel")[1].get("short_codes") is True, "short codes: taken with POL_DEV_ISSUER set")
        rec = {"folder": game, "command": ["python", "chan.py"], "timeout_minutes": 5, "channels": ["stable", "dev"]}

        def run(body):
            code, r = c.call("POST", "/api/updater", dict({"game": "kart", "title": "Kart Club", "updater": rec}, **body))
            if code != 202:
                return code, r
            return code, c.wait(r["job"], timeout=90)

        for what, body in (("a channel it does not declare", {"channel": "beta"}),
                           ("switch without a channel", {"switch": True}),
                           ("a code on a regular update", {"channel": "dev", "code": full}),
                           ("a code for stable", {"channel": "stable", "switch": True, "code": full}),
                           ("a code with a space", {"channel": "dev", "switch": True, "code": "a b c d e f g h"})):
            code, r = run(body)
            ok(code == 400 and r.get("error"), "updater channel: %s: 400 (%s)" % (what, r.get("error")))
        code, r = run({"channel": "dev", "switch": True, "code": "ZZZZ-ZZZZ-ZZZZ"})
        ok(code == 400 and "not known" in r.get("error", ""), "updater channel: an unknown short code: the issuer's "
                                                               "sentence, nothing started (%s)" % r)

        before = len(upd_calls(d))
        code, j = run({"channel": "dev", "switch": True, "code": "kart-zzzz-dev2"})
        ok(code == 202 and j["state"] == "ok" and j["summary"] == "The updater of Kart Club switched it to the dev channel.",
           "updater channel: a short code switches to dev (%s)" % (j.get("summary") if code == 202 else j))
        ok(iss.asked[-1] == ("/api/dev-code/redeem", {"code": "KARTZZZZDEV2", "install_id": inst}),
           "updater channel: the short code redeemed at the issuer with the install id (%s)" % (iss.asked[-1:],))
        seen = json.load(open(os.path.join(game, "chan.json")))
        ok(seen == {"argv": ["--channel", "dev"], "code": full},
           "updater channel: --channel dev, and the redeemed access code in OPENLOBBY_DEV_CODE (%s)" % seen["argv"])
        runs = [x["args"] for x in upd_calls(d)[before:] if x["args"][0] == "run" and "--network" not in x["args"]]
        a = runs[-1] if runs else []
        ok(not any(full in x or "KARTZZZZDEV2" in x or "kart-dev-token-71Xq" in x for x in a),
           "updater channel: the code is in no docker argument")
        ok("--env-file" in a and "-i" not in a, "updater channel: an --env-file, stdin still closed")
        ef = json.load(open(os.path.join(d, "envfile.json")))
        ok(ef["text"] == "OPENLOBBY_DEV_CODE=%s\n" % full and (os.name == "nt" or ef["mode"] == 0o600),
           "updater channel: the env file holds the code alone, mode 600 (%o)" % ef["mode"])
        ok(not os.path.exists(ef["path"]) and not os.listdir(os.path.join(state, "updater-env")),
           "updater channel: the env file is deleted after the run")
        ok(full not in j["log"] and "kart-dev-token-71Xq" not in j["log"] and "kart-zzzz-dev2" not in j["log"]
           and "the code is [access code]" in j["log"], "updater channel: the job log blanks the code")
        jf = open(os.path.join(state, "jobs", j["id"] + ".json")).read()
        ok(full not in jf and "KARTZZZZDEV2" not in jf and "kart-zzzz-dev2" not in jf and '"channel": "dev"' in jf,
           "updater channel: the job file has the channel and no code")
        code, pub = c.get("/api/jobs/" + j["id"])
        ok(pub.get("channel") == "dev", "updater channel: the job says its channel")

        iss.codes["KARTZZZZDEV3"] = full
        code, j = run({"channel": "dev", "switch": True, "code": full})
        ok(code == 202 and j["state"] == "ok", "updater channel: a full access code works as it is")
        rec["command"] = ["python", "chan.py", "--refuse"]
        code, j = run({"channel": "dev", "switch": True, "code": full})
        ok(code == 202 and j["state"] == "failed" and j["summary"] == "Kart Club cannot switch: no database backup [access code]"
           and not os.listdir(os.path.join(state, "updater-env")),
           "updater channel: a refused switch fails with its sentence, the code blanked, the env file gone (%s)"
           % (j.get("summary") if code == 202 else j))
        rec["command"] = ["python", "chan.py"]
        code, j = run({"channel": "dev"})
        seen = json.load(open(os.path.join(game, "chan.json")))
        ok(code == 202 and j["state"] == "ok" and j["summary"] == "The updater of Kart Club finished."
           and seen == {"argv": ["--channel", "dev"], "code": None},
           "updater channel: a regular update on dev: --channel dev, no code, no env file (%s)" % seen)
        code, j = run({"channel": "stable", "switch": True})
        seen = json.load(open(os.path.join(game, "chan.json")))
        ok(code == 202 and seen == {"argv": ["--channel", "stable"], "code": None}, "updater channel: back to stable")
        # with the dev builds unlocked, a switch without a code of its own gets the unlock code
        devchannel.unlock(t.core, "kart.example.org/dev", catalog={})
        devchannel.Store(state).save_code(full)
        try:
            code, j = run({"channel": "dev", "switch": True})
            seen = json.load(open(os.path.join(game, "chan.json")))
            ok(code == 202 and j["state"] == "ok" and seen == {"argv": ["--channel", "dev"], "code": full}
               and full not in j["log"],
               "updater channel: unlocked, a switch to dev without a code gets the stored one (%s)" % seen["argv"])
            code, j = run({"channel": "dev"})
            seen = json.load(open(os.path.join(game, "chan.json")))
            ok(code == 202 and seen["code"] is None, "updater channel: unlocked, a regular update still carries no code")
            code, j = run({"channel": "stable", "switch": True})
            seen = json.load(open(os.path.join(game, "chan.json")))
            ok(code == 202 and seen["code"] is None, "updater channel: unlocked, back to stable carries no code")
        finally:
            devchannel.lock(t.core, catalog={})
            devchannel.Store(state).forget()
        ok(not [h for h in scan_for_token(t, [s]) if h], "updater channel: the token is in no file")
        s.stop()
        s2 = Server(t, extra_env={"FAKE_UPD_DIR": d},
                    args=["--container", "c0ffee", "--docker", dock, "--minute", "0.2", "--state", state])
        servers.append(s2)
        c2 = s2.start()
        ch = c2.get("/api/channel")[1]
        ok(ch.get("install_id") == inst and ch.get("short_codes") is False,
           "install id: the same after a restart; short codes off with POL_DEV_ISSUER=off")
        code, r = c2.call("POST", "/api/updater", {"game": "kart", "title": "Kart Club", "updater": rec,
                                                   "channel": "dev", "switch": True, "code": "KART-ZZZZ-DEV3"})
        ok(code == 400 and "the issuer is off" in r.get("error", ""),
           "short codes: refused with the issuer off, with a sentence (%s)" % r)
        s2.stop()
    finally:
        iss.stop()


def test_updater(t, servers):
    """POST /api/updater: the job that runs a paired game's updater in a container of the
    deployer's own image, against the stand-in docker."""
    test_updater_unit()
    sys.path.insert(0, os.path.join(ROOT, "services", "deployer"))
    import gameupdater as gu
    dock, d, game = updater_tree(t)
    with open(os.path.join(t.core, "docker-compose.yml"), "w") as f:
        f.write("services: {}  # Kart Club\n")
    s = Server(t, extra_env={"FAKE_UPD_DIR": d},
               args=["--container", "c0ffee", "--docker", dock, "--minute", "0.2"])
    servers.append(s)
    c = s.start()
    seen = gu.daemon_path(game)

    def run(command, timeout=60, folder=game, title="Kart Club"):
        code, r = c.call("POST", "/api/updater", {"game": "kart", "title": title, "updater": {
            "folder": folder, "command": command, "timeout_minutes": timeout}})
        if code != 202:
            return {"state": "http %d" % code, "summary": str(r), "log": "", "exit": None}
        return c.wait(r["job"], timeout=90)

    for what, body in (("an unknown field", {"game": "kart", "title": "Kart Club", "argv": ["sh"],
                                             "updater": {"folder": game, "command": ["python", "ok.py"]}}),
                       ("a command that is not python", {"game": "kart", "updater": {"folder": game,
                                                                                    "command": ["sh", "ok.py"]}}),
                       ("an updater field the rules do not know", {"game": "kart", "updater": {
                           "folder": game, "command": ["python", "ok.py"], "image": "alpine"}}),
                       ("a relative folder", {"game": "kart", "updater": {"folder": "kart", "command": ["python"]}}),
                       ("a key that does not fit", {"game": "Kart!", "updater": {"folder": game,
                                                                                "command": ["python"]}}),
                       ("a timeout of 300", {"game": "kart", "updater": {"folder": game, "command": ["python"],
                                                                        "timeout_minutes": 300}})):
        code, r = c.call("POST", "/api/updater", body)
        ok(code == 400 and r.get("error"), "updater: %s: 400 (%s)" % (what, r.get("error")))
    ok(c.call("POST", "/api/updater", {"game": "kart"}, token="wrong")[0] == 401, "updater: the token is needed")
    ok(not [x for x in upd_calls(d) if x["args"][0] == "run" and "--network" not in x["args"]],
       "updater: nothing refused reached docker run")

    before = len(upd_calls(d))
    j = run(["python", "ok.py", "--yes"], timeout=60)
    ok(j["state"] == "ok" and j["summary"] == "The updater of Kart Club finished." and j["exit"] == 0,
       "updater: exit 0: done (%s, %s)" % (j["state"], j["summary"]))
    ok("Kart Club: fetched 9f8e7d6c" in j["log"] and "The update is done." in j["log"],
       "updater: its output is in the job log")
    code, pub = c.get("/api/jobs/" + j["id"])
    ok(pub["action"] == "updater" and pub["game"] == "kart" and pub["title"] == "Kart Club",
       "updater: the job says updater, kart, Kart Club")
    calls = upd_calls(d)[before:]
    probe = [x["args"] for x in calls if x["args"][0] == "run" and "--network" in x["args"]]
    runs = [x["args"] for x in calls if x["args"][0] == "run" and "--network" not in x["args"]]
    ok(calls[0]["args"][:3] == ["inspect", "--format", "{{.Image}}"] and calls[0]["args"][-1] == "c0ffee",
       "updater: asks Docker for the deployer's own image")
    ok(len(probe) == 1 and probe[0][probe[0].index("--network") + 1] == "none" and "--rm" in probe[0]
       and probe[0][probe[0].index("--pull") + 1] == "never"
       and probe[0][probe[0].index("--mount") + 1] == 'type=bind,"source=%s","target=/probe",readonly' % seen,
       "updater: the folder checked first in a throwaway container, read-only, as the daemon sees it (%s)"
       % (probe[0][probe[0].index("--mount") + 1] if probe else None))
    ok(len(runs) == 1, "updater: one container run")
    a = runs[0] if runs else ["run"]
    mounts = [a[i + 1] for i, x in enumerate(a) if x == "--mount"]
    image_at = a.index("--entrypoint") + 2 if "--entrypoint" in a else 0
    ok("--rm" in a and a[a.index("--pull") + 1] == "never" and a[a.index("--name") + 1] == "openlobby-updater-" + j["id"]
       and "org.openlobby.updater.job=" + j["id"] in a and "org.openlobby.updater.game=kart" in a,
       "updater: --rm, its own name and labels")
    ok(mounts == ['type=bind,"source=%s","target=%s"' % (seen, seen),
                  'type=bind,"source=/var/run/docker.sock","target=/var/run/docker.sock"']
       and a[a.index("-w") + 1] == seen,
       "updater: the folder mounted at the path the daemon sees, the working directory, and the socket (%s)" % mounts)
    ok(a[a.index("--entrypoint") + 1] == "python" and a[image_at] == "sha256:" + "cd" * 32
       and a[image_at + 1:] == ["ok.py", "--yes"],
       "updater: its own image, the approved command as it is (%s)" % a[a.index("--entrypoint"):])
    envs = [a[i + 1] for i, x in enumerate(a) if x == "-e"]
    ok(envs == ["HOME=/tmp", "LANG=C.UTF-8", "PYTHONUNBUFFERED=1", "PYTHONIOENCODING=utf-8"],
       "updater: a minimal environment, nothing of the deployer's (%s)" % envs)
    ok("-i" not in a and "-t" not in a and "--interactive" not in a, "updater: stdin closed (no -i)")
    ok(all(x["token"] is None for x in calls), "updater: the token is in no docker CLI's environment")
    rec = json.load(open(os.path.join(game, "seen.json")))
    ok(os.path.normcase(os.path.abspath(rec["cwd"])) == os.path.normcase(os.path.abspath(game)) and rec["argv"] == ["--yes"],
       "updater: it ran in the game's folder (%s)" % rec["cwd"])
    ok(not any(k.startswith("POL_") or TOKEN in v for k, v in rec["env"].items()),
       "updater: no POL_ variable or token reached it")
    ok(TOKEN not in j["log"], "updater: the token is not in the log")

    j = run(["python", "fail.py"])
    ok(j["state"] == "failed" and j["summary"] == "Update failed: the disk is full." and j["exit"] == 1,
       "updater: exit 1: failed, the last line as the summary (%s, %s)" % (j["summary"], j["exit"]))
    j = run(["python", "silent.py"])
    ok(j["state"] == "failed" and j["exit"] == 2 and "exited with code 2 and printed nothing" in j["summary"],
       "updater: exit 2 and no output: said so (%s)" % j["summary"])
    j = run(["python", "ok.py"], folder=os.path.join(t.tmp, "not there"))
    ok(j["state"] == "failed" and "Docker does not see the folder" in j["summary"]
       and "bind source path does not exist" in j["summary"],
       "updater: a folder Docker does not see: failed before it runs (%s)" % j["summary"])

    code, r = c.call("POST", "/api/updater", {"game": "kart", "title": "Kart Club", "updater": {
        "folder": game, "command": ["python", "slow.py"], "timeout_minutes": 5}})
    time.sleep(0.5)
    busy = c.call("POST", "/api/updater", {"game": "kart", "updater": {"folder": game, "command": ["python", "ok.py"]}})
    ok(busy[0] == 409 and busy[1].get("job") == r.get("job"), "updater: one job at a time (409)")
    j = c.wait(r["job"], timeout=60)
    ok(j["state"] == "failed" and "stopped after 5 minutes" in j["summary"] and "still building" in j["log"],
       "updater: past its timeout: stopped (%s)" % j["summary"])
    kills = [x["args"] for x in upd_calls(d) if x["args"][0] == "kill"]
    ok(kills and kills[-1] == ["kill", "openlobby-updater-" + r["job"]], "updater: its container killed (%s)" % kills[-1:])
    j = run(["python", "quiet.py"], timeout=240)
    ok(j["state"] == "failed" and "no output for 15 minutes" in j["summary"],
       "updater: no output for 15 minutes: stopped (%s)" % j["summary"])
    ok(not os.listdir(os.path.join(d, "running")), "updater: no container left running")
    s.stop()
    test_updater_channels(t, servers, dock, d, game)

    s = Server(t, extra_env={"FAKE_UPD_DIR": d}, args=["--docker", dock, "--minute", "0.2"])
    servers.append(s)
    c = s.start()
    j = run(["python", "ok.py"])
    ok(j["state"] == "failed" and "not running in a container" in j["summary"],
       "updater: a deployer outside a container says it cannot (%s)" % j["summary"])
    s.stop()
    os.remove(os.path.join(t.core, "docker-compose.yml"))


class FakeGitHub:
    """The parts of the GitHub API the check asks for, over one repository
    with a straight history (MAIN, oldest first) whose commit 30 is a merge,
    and tag v1.2.3 on commit 20."""
    REPO = "PrettyOpenLobby/OpenLobby"

    def __init__(self):
        self.main = [{"sha": sha(i), "subject": ("Merge branch side" if i == 30 else "Change %d" % i),
                      "date": "2026-10-%02dT%02d:00:00Z" % (1 + i // 24, i % 24),
                      "merge": i == 30} for i in range(70)]
        self.tags = {"v1.2.3": 20}
        self.calls = []
        self.mode = "ok"
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, obj, headers=()):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                fake.calls.append(self.path)
                if fake.mode == "limit":
                    return self.reply(403, {"message": "API rate limit exceeded"},
                                      [("X-RateLimit-Remaining", "0"), ("X-RateLimit-Reset", "1793404800")])
                u = urllib.parse.urlsplit(self.path)
                q = urllib.parse.parse_qs(u.query)
                pre = "/repos/%s/" % fake.REPO
                if not u.path.startswith(pre):
                    return self.reply(404, {"message": "Not Found"})
                rest = u.path[len(pre):]
                if rest == "commits":
                    i = fake.index((q.get("sha") or ["main"])[0])
                    if i is None:
                        return self.reply(404, {"message": "No commit found"})
                    n = int((q.get("per_page") or ["30"])[0])
                    return self.reply(200, [fake.api(c) for c in reversed(fake.main[max(0, i + 1 - n):i + 1])])
                if rest == "tags":
                    return self.reply(200, [{"name": k} for k in fake.tags] + [{"name": "v1.1.0"}])
                if rest.startswith("compare/"):
                    base, _, head = rest[len("compare/"):].partition("...")
                    i, j = fake.index(base), fake.index(head)
                    if i is None or j is None:
                        return self.reply(404, {"message": "Not Found"})
                    return self.reply(200, {"status": "ahead" if j > i else "identical",
                                            "ahead_by": max(0, j - i), "behind_by": max(0, i - j),
                                            "commits": [fake.api(c) for c in fake.main[i + 1:j + 1]][:250]})
                return self.reply(404, {"message": "Not Found"})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def index(self, ref):
        if ref == "main":
            return len(self.main) - 1
        if ref in self.tags:
            return self.tags[ref]
        for i, c in enumerate(self.main):
            if len(ref) >= 7 and c["sha"].startswith(ref):
                return i
        return None

    def api(self, c):
        parents = [{"sha": "0" * 40}, {"sha": "1" * 40}] if c["merge"] else [{"sha": "0" * 40}]
        return {"sha": c["sha"], "parents": parents,
                "commit": {"message": c["subject"] + "\n\nbody text", "committer": {"date": c["date"]}}}

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


def updates(c, refresh=False, timeout=60):
    """GET /api/updates, then again until no check is running."""
    code, r = c.get("/api/updates" + ("?refresh=1" if refresh else ""))
    first = r
    t0 = time.time()
    while code == 200 and r.get("checking") and time.time() - t0 < timeout:
        time.sleep(0.1)
        code, r = c.get("/api/updates")
    return code, r, first


def by_key(r):
    return {i["key"]: i for i in r.get("items", [])}


ITEM_KEYS = {"key", "name", "running", "latest", "behind", "changes", "update", "why", "how"}


def shape_ok(it):
    return (ITEM_KEYS <= set(it) and set(it["running"]) == {"commit", "built_at"}
            and set(it["latest"]) == {"commit", "date"} and it["update"] in ("available", "current", "unknown")
            and all(set(ch) == {"commit", "date", "subject"} for ch in it["changes"])
            and len(it["changes"]) <= 50)


def test_updates_checkout(c, t):
    """The checkout deployer (no published images): tm is a clone of a local
    bare repository, the core and fe are plain folders."""
    if not t.git:
        SKIPS.append("update check of a checkout (no git)")
        print("SKIP update check of a checkout (no git on PATH)")
        return
    ok(c.get("/api/updates", token=None)[0] == 401, "updates without the token: 401")
    code, r, _ = updates(c)
    items = by_key(r)
    ok(code == 200 and r["items"] and r["items"][0]["key"] == "core" and r["checked_at"],
       "updates: 200, the core first, a checked_at (%s)" % [i["key"] for i in r.get("items", [])])
    ok(all(shape_ok(i) for i in r["items"]), "  every item has the agreed shape")
    ok(set(items) == {"core", "tm", "fe"}, "  the core and the installed games only (%s)" % sorted(items))
    ok(items["core"]["update"] == "unknown" and "not a git checkout" in (items["core"]["why"] or ""),
       "  a core folder that is not a checkout: unknown, and why (%s)" % items["core"]["why"])
    tm = items["tm"]
    head = git("rev-parse", "HEAD", cwd=t.tm).stdout.strip()
    ok(tm["update"] == "current" and tm["behind"] == 0 and tm["changes"] == []
       and tm["running"]["commit"] == head and tm["latest"]["commit"] == head and tm["how"] is None,
       "  tm, just updated: current at its HEAD (%s)" % tm)
    # 55 commits, then a merge of a side branch, on the remote
    for i in range(1, 56):
        with open(os.path.join(t.seed, "README.md"), "a") as f:
            f.write("c%02d\n" % i)
        git("commit", "-q", "-am", "c%02d lap times" % i, cwd=t.seed)
    git("checkout", "-q", "-b", "side", cwd=t.seed)
    with open(os.path.join(t.seed, "side.txt"), "w") as f:
        f.write("side\n")
    git("add", "side.txt", cwd=t.seed)
    git("commit", "-q", "-m", "side work", cwd=t.seed)
    git("checkout", "-q", "-", cwd=t.seed)
    git("merge", "-q", "--no-ff", "side", "-m", "Merge branch side", cwd=t.seed)
    git("push", "-q", "origin", "HEAD", cwd=t.seed)
    newest = git("rev-parse", "HEAD", cwd=t.seed).stdout.strip()
    code, r, first = updates(c, refresh=True)
    ok(first.get("checking") is True, "refresh=1: answers at once, checking")
    tm = by_key(r)["tm"]
    subjects = [ch["subject"] for ch in tm["changes"]]
    ok(tm["update"] == "available" and tm["behind"] == 57 and tm["latest"]["commit"] == newest,
       "  tm behind its remote: available, behind 57 (%s, %s)" % (tm["update"], tm["behind"]))
    ok(len(subjects) == 50 and not any(x.startswith("Merge") for x in subjects),
       "  50 changes at most, merges dropped (%d)" % len(subjects))
    ok("c55 lap times" in subjects and subjects.index("c55 lap times") < subjects.index("c10 lap times"),
       "  newest first")
    ok(all(re.match(r"^[0-9a-f]{40}$", ch["commit"]) and re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", ch["date"])
           for ch in tm["changes"]), "  hex commits and UTC times")
    ok(tm["how"] and "git pull" in tm["how"], "  and how to update (%s)" % tm["how"])
    code, r2 = c.get("/api/updates?refresh=1")
    ok(code == 200 and r2.get("limited") is True and r2.get("retry_after", 0) >= 1 and not r2["checking"],
       "a second forced check within the gap: refused, the last answer kept (%s)"
       % {k: r2.get(k) for k in ("limited", "retry_after", "checking")})
    # the remote is gone
    origin = os.path.join(t.tmp, "origin.git")
    os.rename(origin, origin + ".away")
    time.sleep(2.1)
    code, r, _ = updates(c, refresh=True)
    tm = by_key(r)["tm"]
    ok(tm["update"] == "unknown" and "git fetch failed" in (tm["why"] or "") and tm["behind"] is None,
       "remote unreachable: unknown, and why (%s)" % tm["why"])
    os.rename(origin + ".away", origin)
    j = c.run("update", "tm")
    ok(j["state"] == "ok", "update tm: ok (%s)" % j["summary"])
    t0 = time.time()
    tm = {}
    while time.time() - t0 < 20:
        code, r, _ = updates(c)
        tm = by_key(r)["tm"]
        if tm["update"] == "current":
            break
        time.sleep(0.1)
    ok(tm["update"] == "current" and tm["running"]["commit"] == newest,
       "after a successful update job the item is checked again: current (%s)" % tm["update"])


def test_updates_images(t, servers):
    """The deployer with published images, against the stand-in GitHub."""
    gh = FakeGitHub()
    ddir = os.path.join(t.tmp, "fakedocker")
    os.makedirs(ddir, exist_ok=True)
    dock = os.path.join(ddir, "docker.py")
    with open(dock, "w") as f:
        f.write(FAKE_DOCKER)
    core_ref = "ghcr.io/prettyopenlobby/openlobby:latest"

    def images(core_rev=None, core_env=None, tm_env=None, extra=None):
        m = {}
        labels = {}
        if core_rev:
            labels = {"org.opencontainers.image.revision": core_rev,
                      "org.opencontainers.image.created": "2026-10-20T08:30:00Z"}
        m[core_ref] = {"Created": "2026-10-20T08:31:00.123456789Z",
                       "Config": {"Labels": labels, "Env": ["PATH=/usr/bin"] + (core_env or [])}}
        # a game image built on the core inherits OPENLOBBY_REVISION, which says nothing of the game
        m["ghcr.io/prettyopenlobby/hippaulmaster:latest"] = {
            "Created": "2026-10-20T08:00:00Z", "Config": {"Labels": {}, "Env": tm_env or []}}
        m.update(extra or {})
        with open(os.path.join(ddir, "inspect.json"), "w") as f:
            json.dump(m, f)

    images(core_rev=sha(5), tm_env=["OPENLOBBY_REVISION=" + sha(5)])
    state = os.path.join(t.tmp, "state-ghcr")
    env = {"POL_DEPLOY_GHCR": "1", "POL_DEPLOY_GITHUB_API": gh.url, "FAKE_DOCKER_DIR": ddir}
    args = ["--state", state, "--docker", dock, "--refresh-gap", "2"]
    s = Server(t, extra_env=env, args=args)
    servers.append(s)
    try:
        c = s.start()
        code, r, _ = updates(c)
        items = by_key(r)
        core = items.get("core") or {}
        subjects = [ch["subject"] for ch in core.get("changes", [])]
        ok(code == 200 and all(shape_ok(i) for i in r["items"]), "published images: every item has the agreed shape")
        ok(core.get("update") == "available" and core["behind"] == 64 and core["running"]["commit"] == sha(5)
           and core["latest"] == {"commit": sha(69), "date": gh.main[69]["date"]},
           "core image 64 commits behind main: available (%s)" % {k: core.get(k) for k in ("update", "behind", "why")})
        ok(core.get("running", {}).get("built_at") == "2026-10-20T08:30:00Z",
           "  built_at from the image's created label")
        ok(len(subjects) == 50 and "Merge branch side" not in subjects and subjects[0] == "Change 69"
           and core["changes"][0]["commit"] == sha(69),
           "  50 changes, newest first, the merge dropped (%s...)" % subjects[:2])
        ok(core["how"] and "published images" in core["how"], "  how: %s" % core.get("how"))
        tm = items.get("tm") or {}
        ok(tm.get("update") == "unknown" and "does not say which commit" in (tm.get("why") or ""),
           "a game image whose only revision is the core's inherited env: unknown (%s)" % tm.get("why"))
        fe = items.get("fe") or {}
        ok(fe.get("update") == "unknown" and "not on this host" in (fe.get("why") or ""),
           "a game image that was never pulled: unknown (%s)" % fe.get("why"))
        n = len(gh.calls)
        ok(any("/compare/%s...%s" % (sha(5), sha(69)) in x for x in gh.calls), "  compared running...latest")
        s.stop()

        # a restart within the hour asks GitHub nothing: the answers are kept on the volume
        s = Server(t, extra_env=env, args=args)
        servers.append(s)
        c = s.start()
        code, r, _ = updates(c)
        ok(len(gh.calls) == n and by_key(r)["core"]["behind"] == 64,
           "restart within the hour: the cached answers, no new GitHub calls (%d -> %d)" % (n, len(gh.calls)))

        # a forced check asks again; a second one within the gap does not
        code, r, _ = updates(c, refresh=True)
        m = len(gh.calls)
        ok(m > n, "a forced check asks GitHub again (%d -> %d)" % (n, m))
        code, r2 = c.get("/api/updates?refresh=1")
        ok(r2.get("limited") is True and len(gh.calls) == m, "  a second forced check within the gap: refused")

        # current
        images(core_rev=sha(69))
        time.sleep(2.1)
        code, r, _ = updates(c, refresh=True)
        core = by_key(r)["core"]
        ok(core["update"] == "current" and core["behind"] == 0 and core["changes"] == [] and core["why"] is None
           and core["how"] is None, "core image at main's head: current (%s)" % core)

        # the revision from the image's environment when there is no label (the core only)
        images(core_env=["OPENLOBBY_REVISION=" + sha(68), "OPENLOBBY_BUILT_AT=2026-10-19T01:02:03Z"])
        time.sleep(2.1)
        core = by_key(updates(c, refresh=True)[1])["core"]
        ok(core["update"] == "available" and core["behind"] == 1 and core["running"] == {
            "commit": sha(68), "built_at": "2026-10-19T01:02:03Z"},
           "core image with OPENLOBBY_REVISION and no label: behind 1 (%s)" % core["running"])

        # no revision at all
        images()
        time.sleep(2.1)
        core = by_key(updates(c, refresh=True)[1])["core"]
        ok(core["update"] == "unknown" and core["latest"]["commit"] == sha(69) and core["behind"] is None
           and "does not say which commit" in (core["why"] or ""),
           "core image without a revision: unknown, latest still shown (%s)" % core["why"])

        # a commit the repository does not have (a private build)
        images(core_rev="abcdef0123456789abcdef0123456789abcdef01")
        time.sleep(2.1)
        core = by_key(updates(c, refresh=True)[1])["core"]
        ok(core["update"] == "unknown" and "not in PrettyOpenLobby/OpenLobby" in (core["why"] or ""),
           "a running commit GitHub does not know: unknown (%s)" % core["why"])

        # pinned: OPENLOBBY_TAG=1.2.3 in the core's .env follows tag v1.2.3 only
        with open(os.path.join(t.core, ".env"), "w") as f:
            f.write("OPENLOBBY_TAG=1.2.3\n")
        images(extra={"ghcr.io/prettyopenlobby/openlobby:1.2.3": {
            "Config": {"Labels": {"org.opencontainers.image.revision": sha(20)}}}})
        time.sleep(2.1)
        core = by_key(updates(c, refresh=True)[1])["core"]
        ok(core["update"] == "current" and core["latest"]["commit"] == sha(20)
           and "pinned to 1.2.3" in (core["why"] or "") and "OPENLOBBY_TAG" in core["why"],
           "pinned to 1.2.3: current on the pin, and why says main has more (%s)" % core["why"])
        os.remove(os.path.join(t.core, ".env"))

        # rate limited
        images(core_rev=sha(5))
        gh.mode = "limit"
        time.sleep(2.1)
        core = by_key(updates(c, refresh=True)[1])["core"]
        ok(core["update"] == "unknown" and "limit" in (core["why"] or "") and core["changes"] == [],
           "GitHub rate limited: unknown, and why (%s)" % core["why"])
        gh.mode = "ok"

        # offline
        gh.stop()
        time.sleep(2.1)
        core = by_key(updates(c, refresh=True)[1])["core"]
        ok(core["update"] == "unknown" and "could not be reached" in (core["why"] or ""),
           "GitHub unreachable: unknown, and why (%s)" % core["why"])
        ok(os.path.isfile(os.path.join(state, "updates.json"))
           and os.path.isfile(os.path.join(state, "github-cache.json")),
           "results and GitHub's answers are kept in the state folder")
        s.stop()
    finally:
        try:
            gh.stop()
        except Exception:
            pass


def test_update_helpers():
    sys.path.insert(0, os.path.join(ROOT, "services", "deployer"))
    import updatecheck as u
    ok(u.to_iso("2026-10-21T14:00:00+02:00") == "2026-10-21T12:00:00Z"
       and u.to_iso("2026-10-21T12:00:00.5Z") == "2026-10-21T12:00:00Z"
       and u.to_iso("yesterday") is None and u.to_iso(5) is None, "to_iso: zones to UTC, junk to None")
    ok(u.commit_id(" ABCDEF0 ") == "abcdef0" and u.commit_id("xyz") is None and u.commit_id("abc") is None,
       "commit_id: hex only")
    ok(u.repo_of({"repo": "https://github.com/PrettyOpenLobby/KartClub"}) == ("PrettyOpenLobby/KartClub", "main")
       and u.repo_of({"source": {"repo": "a/b", "branch": "stable"}}) == ("a/b", "stable")
       and u.repo_of({}, "x/y") == ("x/y", "main"), "repo_of: source, then the repo URL, then the default")


def test_logs_and_token(c, t):
    t.flag("big")
    j = c.run("restart", "tm")
    t.flag("big", on=False)
    ok(j["state"] == "ok", "a job with 1.3 MB of output: ok (%s)" % j["summary"])
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
        # Docker Desktop: the socket as a container sees it there (root:root 660), and
        # a game checkout the deployer cloned as root before, which nobody cannot write
        os.stat = lambda p, *a, **k: St(0, 0, 0o140660)
        ok(not dep.socket_open_to(dep.NOBODY, {dep.NOBODY}, "/s"), "socket root:root 660: closed to nobody")
        os.stat = lambda p, *a, **k: St(0, 0, 0o40755)
        ok(not dep.may_write(dep.NOBODY, {dep.NOBODY}, "/c"), "a root-owned 755 checkout: nobody cannot write")
        os.stat = lambda p, *a, **k: St(dep.NOBODY, dep.NOBODY, 0o40755)
        ok(dep.may_write(dep.NOBODY, {dep.NOBODY}, "/c"), "  one nobody owns: it can")
        # who clones a game: the folder's owner; on Docker Desktop, where it reads as root's, nobody
        desk = "/run/desktop/mnt/host/c/Users/you/openlobby-server"
        os.stat = lambda p, *a, **k: St(0, 0, 0o40777)
        ok(dep.install_identity(desk) == {"user": dep.NOBODY, "group": dep.NOBODY, "extra_groups": []},
           "Docker Desktop: a game is cloned as nobody, so its imports can write")
        ok(dep.install_identity("/srv/pol") == {}, "a root-owned folder elsewhere: cloned as root, as before")
        os.stat = lambda p, *a, **k: St(1000, 1000)
        ok(dep.install_identity(desk)["user"] == 1000 and dep.install_identity("/srv/pol")["user"] == 1000,
           "a folder a user owns: cloned as that user")
        ok(dep.on_desktop(desk + "/openlobby") and not dep.on_desktop("/srv/pol")
           and not dep.on_desktop("/run/desktop/other"), "on_desktop: only the VM's host-drive folders")
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


def docker_open_to_me():
    """Whether this user may connect to a Docker socket, as the deployer's import guard sees it."""
    if not hasattr(os, "getuid") or os.getuid() == 0:
        return False
    import deployer as dep
    gids = set(os.getgroups()) | {os.getgid()}
    return any(dep.socket_open_to(os.getuid(), gids, s) for s in dep.docker_sockets())


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
    if docker_open_to_me():
        # A machine whose user may use the Docker socket (a CI runner): the deployer must refuse
        # to start the tool, and nothing after this can run here.
        ok(j["state"] != "ok" and "Docker socket" in j["summary"],
           "import as a user who may use the Docker socket: refused (%s)" % j["summary"])
        return
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


# ------------------------------------------------------------- the dev channel

class FakeRegistry:
    """The registry API as docker uses it: /v2/ asks for a bearer token, /v2/token hands
    one out for a known password (any user name, as Forgejo does for a token), and a
    manifest answers with its digest while the password that got the token is still
    valid. With `public` set, a token request without a password gets an anonymous token
    that reads every image, as a registry of public images does. Records each request's
    path and whether it carried a password."""

    def __init__(self):
        self.public = False
        self.anon = []
        self.valid = set()
        self.tokens = {}
        self.denied = set()
        self.images = {"kart/lsb-server:stock": "sha256:" + "1" * 64}
        self.paths = []
        reg = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def answer(self, code, body=b"", headers=()):
                self.send_response(code)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def do_GET(self):
                reg.paths.append(self.path)
                u = urllib.parse.urlsplit(self.path)
                auth = self.headers.get("Authorization") or ""
                if u.path == "/v2/":
                    return self.answer(401, b"{}", [("WWW-Authenticate",
                        'Bearer realm="http://127.0.0.1:%d/v2/token",service="container_registry",scope="*"'
                        % reg.port)])
                if u.path == "/v2/token":
                    pw = None
                    if auth.startswith("Basic "):
                        import base64
                        pw = base64.b64decode(auth[6:]).decode().split(":", 1)[1]
                    if pw is None and reg.public:
                        reg.anon.append(self.path)
                        return self.answer(200, json.dumps({"token": "anon"}).encode())
                    if pw in reg.valid:
                        tok = "tok-%d" % len(reg.tokens)
                        reg.tokens[tok] = pw
                        return self.answer(200, json.dumps({"token": tok}).encode())
                    return self.answer(401, b'{"errors":[{"code":"UNAUTHORIZED"}]}')
                m = re.match(r"^/v2/(.+)/manifests/([^/]+)$", u.path)
                if m:
                    tok = auth[7:] if auth.startswith("Bearer ") else ""
                    if not (tok == "anon" and reg.public) and reg.tokens.get(tok) not in reg.valid:
                        return self.answer(401)
                    if m.group(1) in reg.denied:
                        return self.answer(403)
                    dig = reg.images.get("%s:%s" % m.groups())
                    if not dig:
                        return self.answer(404)
                    return self.answer(200, b"{}", [("Docker-Content-Digest", dig)])
                self.answer(404)

            do_HEAD = do_GET

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


# The stand-in docker CLI for the dev channel: login checks the password it reads from
# stdin against FAKE_CH_DIR/valid and (as no real docker does) prints it, so the job log
# must blank it; logout; image inspect from inspect.json. Each call is recorded with its
# DOCKER_CONFIG and the sha256 of what came on stdin.
FAKE_CH_DOCKER = r"""
import base64, hashlib, json, os, sys
d = os.environ["FAKE_CH_DIR"]
args = sys.argv[1:]
rec = {"args": args, "docker_config": os.environ.get("DOCKER_CONFIG")}
pw = ""
if "--password-stdin" in args:
    pw = sys.stdin.read().strip()
    rec["stdin_sha"] = hashlib.sha256(pw.encode()).hexdigest()
with open(os.path.join(d, "calls.txt"), "a") as f:
    f.write(json.dumps(rec) + "\n")
if args[0] == "login":
    valid = open(os.path.join(d, "valid")).read().split()
    if pw in valid:
        cfg = os.path.join(os.environ["DOCKER_CONFIG"], "config.json")
        user = args[args.index("--username") + 1]
        json.dump({"auths": {args[-1]: {"auth": base64.b64encode((user + ":" + pw).encode()).decode()}}},
                  open(cfg, "w"))
        print("debug: signed in with " + pw)
        print("Login Succeeded")
        sys.exit(0)
    sys.stderr.write('Error response from daemon: Get "http://%s/v2/": unauthorized\n' % args[-1])
    sys.exit(1)
if args[0] == "logout":
    print("Removing login credentials for " + args[-1])
    sys.exit(0)
if args[:2] == ["image", "inspect"]:
    p = os.path.join(d, "inspect.json")
    images = json.load(open(p)) if os.path.exists(p) else {}
    if args[2] not in images:
        sys.stderr.write("Error: No such image: " + args[2] + "\n")
        sys.exit(1)
    print(json.dumps([images[args[2]]]))
    sys.exit(0)
sys.exit(0)
"""

CH_CODE = "kart-dev-7Qm2x9Lp4Vb8"
CH_CODE2 = "kartclub:kart-dev-second-55Zr"


def test_channel_unit(reg):
    """devchannel.check_access against the stand-in registry, each outcome."""
    sys.path.insert(0, os.path.join(ROOT, "services", "deployer"))
    import devchannel
    registry = "127.0.0.1:%d/kart" % reg.port
    reg.valid = {CH_CODE}
    probe = [("lsb-server", "stock")]
    r = devchannel.check_access(registry, CH_CODE, probe, scheme="http")
    ok(r["state"] == "ok" and r["digests"] == {"lsb-server:stock": "sha256:" + "1" * 64},
       "check_access: a good code reads the manifest and its digest (%s)" % r)
    r = devchannel.check_access(registry, "someone:" + CH_CODE, probe, scheme="http")
    ok(r["state"] == "ok", "check_access: name:code signs in under that name")
    r = devchannel.check_access(registry, "kart-wrong-code-0000", probe, scheme="http")
    ok(r["state"] == "wrong" and "did not accept" in r["why"], "check_access: a wrong code: wrong (%s)" % r["why"])
    reg.denied = {"kart/lsb-server"}
    r = devchannel.check_access(registry, CH_CODE, probe, scheme="http")
    ok(r["state"] == "denied" and "may not read" in r["why"], "check_access: no read access: denied")
    reg.denied = set()
    r = devchannel.check_access(registry, CH_CODE, [("nothing", "stock")], scheme="http")
    ok(r["state"] == "missing" and "no nothing (dev)" in r["why"], "check_access: no such image: missing (%s)"
       % r["why"])
    gone = "127.0.0.1:%d/kart" % free_port()
    r = devchannel.check_access(gone, CH_CODE, probe, scheme="http", timeout=3)
    ok(r["state"] == "unreachable" and "did not answer" in r["why"] and "127.0.0.1" not in r["why"],
       "check_access: nobody there: unreachable, the address not named (%s)" % r["why"])
    r = devchannel.check_access(registry, None, probe, scheme="http")
    ok(r["state"] == "wrong" and "sign-in" in r["why"], "check_access: no token, private images: wrong")
    reg.public = True
    r = devchannel.check_access(registry, None, probe, scheme="http")
    ok(r["state"] == "ok" and r["digests"] and reg.anon, "check_access: no token, public images: ok, anonymous")
    reg.public = False
    reg.anon[:] = []
    whys = []
    reg.denied = {"kart/lsb-server"}
    whys.append(devchannel.check_access(registry, CH_CODE, probe, scheme="http")["why"])
    reg.denied = set()
    whys.append(devchannel.check_access(registry, "kart-wrong-code-0000", probe, scheme="http")["why"])
    whys.append(devchannel.check_access(registry, CH_CODE, [("nothing", "stock")], scheme="http")["why"])
    ok(not any(("127.0.0.1:%d" % reg.port) in w for w in whys), "check_access: no sentence names the registry")
    ok(not any(CH_CODE in x for x in reg.paths), "check_access: the code is in no request path")
    ok(devchannel.split_code("a:b") == ("a", "b") and devchannel.split_code("abc", "dev") == ("dev", "abc")
       and devchannel.split_code(":abc", "dev") == ("dev", ":abc"), "split_code: name:secret and bare codes")


def test_channel(t, servers):
    reg = FakeRegistry()
    try:
        test_channel_unit(reg)
        _test_channel(t, servers, reg)
    finally:
        reg.stop()


def _test_channel(t, servers, reg):
    import hashlib as _h
    sys.path.insert(0, os.path.join(ROOT, "services", "deployer"))
    import devchannel
    chdir = os.path.join(t.tmp, "chdocker")
    os.makedirs(chdir, exist_ok=True)
    dock = os.path.join(chdir, "docker.py")
    with open(dock, "w") as f:
        f.write(FAKE_CH_DOCKER)

    def set_valid(*secrets):
        reg.valid = set(secrets)
        with open(os.path.join(chdir, "valid"), "w") as f:
            f.write("\n".join(secrets) + "\n")

    def dcalls():
        try:
            return [json.loads(l) for l in open(os.path.join(chdir, "calls.txt")) if l.strip()]
        except OSError:
            return []

    def senvs():
        try:
            return [json.loads(l) for l in open(os.path.join(t.fakedir, "envs.txt")) if l.strip()]
        except OSError:
            return []

    def clear():
        for p in (os.path.join(chdir, "calls.txt"), os.path.join(t.fakedir, "envs.txt")):
            if os.path.exists(p):
                os.remove(p)
        t.clear_calls()

    def compose():
        return [e for e in senvs() if e["args"][0] == "compose"]

    def switch(c, key, to, timeout=60):
        st, r = c.call("POST", "/api/channel", {"key": key, "to": to})
        if st != 202:
            return {"state": "http %d" % st, "summary": (r or {}).get("error", str(r)), "log": ""}
        return c.wait(r["job"], timeout)

    def krow(ch, key):
        return next((k for k in ch.get("keys") or [] if k["key"] == key), {})

    registry = "127.0.0.1:%d/kart" % reg.port
    host = "127.0.0.1:%d" % reg.port
    ref = registry + "/lsb-server:stock"
    lsb_services = ["db-update", "connect", "search", "world", "map"]
    state = os.path.join(t.tmp, "state-channel")
    chstate = os.path.join(state, "channel")
    bridge = os.path.join(t.root, "hippaulbridge")
    os.makedirs(bridge, exist_ok=True)
    games = os.path.join(t.core, ".games")
    saved = open(games).read() if os.path.exists(games) else None
    with open(games, "w") as f:
        f.write("ffxi\ntm\n")
    chfile = os.path.join(t.core, ".channel")
    catalog = {}
    for n in os.listdir(os.path.join(t.core, "games")):
        e = json.load(open(os.path.join(t.core, "games", n)))
        catalog[e["key"]] = e
    all_dev = devchannel.dev_keys(catalog)
    set_valid(CH_CODE)
    clear()
    env = {"POL_DEV_REGISTRY": registry, "POL_DEV_REGISTRY_HTTP": "1", "FAKE_CH_DIR": chdir}
    s = Server(t, extra_env=env, args=["--state", state, "--docker", dock, "--refresh-gap", "0"])
    servers.append(s)
    try:
        c = s.start()
        code, ch = c.get("/api/channel")
        ok(code == 200 and ch["unlocked"] is False and ch["channel"] == "stable" and ch["dev"] == []
           and ch["code_stored"] is False and ch["code_ok"] is None
           and "registry" not in ch and "registry_default" not in ch and host not in json.dumps(ch),
           "channel: locked at first, and the answer names no registry")
        have = [k for k in all_dev if k == "core" or os.path.isdir(os.path.join(t.root, catalog[k]["checkout"]))]
        ok([k["key"] for k in ch["keys"]] == have and {"core", "ffxi", "tm"} <= set(have)
           and all(k["on"] == "stable" and k["problem"] is None for k in ch["keys"]),
           "channel: one row per installed thing with a dev build, the core first, all on Stable (%s)"
           % [k["key"] for k in ch["keys"]])
        ok(krow(ch, "ffxi")["services"] == lsb_services + ["bridge"] and krow(ch, "ffxi")["back"]
           and "restore the backup" in krow(ch, "ffxi")["back"] and krow(ch, "tm")["builds_on_core"] is True
           and krow(ch, "core")["builds_on_core"] is False,
           "channel: each row says what it restarts and what Stable does not undo")
        ok(any("may expect" in w for w in krow(ch, "ffxi")["would"]) and len(krow(ch, "core")["would"]) == len(have) - 1,
           "channel: the warning a switch would bring (ffxi to Dev alone; core to Dev with the games on Stable)")
        ff = [r for r in ch["images"] if r["key"] == "ffxi" and r["stable"] == "ghcr.io/landsandboat/server"]
        ok(ff and ff[0]["dev_name"] == "lsb-server (dev)" and ff[0]["applies"] and ff[0]["enabled"]
           and ff[0]["on"] is False and ff[0]["license"] and ff[0]["has_source"] is False
           and "source" not in ff[0] and "dev" not in ff[0],
           "channel: FFXI's world server has a dev image, by short name (%s)" % (ff[:1],))
        ok(any(r["key"] == "core" and r["dev_name"] == "openlobby (dev)" and not r["applies"]
               and "POL_DEPLOY_GHCR" in r["note"] for r in ch["images"]),
           "channel: the core's dev builds are used with published images only")
        ok(c.get("/api/channel/source?stable=ghcr.io/landsandboat/server")[0] == 404,
           "channel source: none while nothing is on Dev")
        absent = [k for k in ("fe", "fmo", "doc", "jan")
                  if not os.path.isdir(os.path.join(t.root, catalog[k]["checkout"]))]
        ok(absent and not any(r["key"] in absent for r in ch["images"] + ch["keys"]),
           "channel: games not installed are left out (%s)" % absent)
        ok(c.get("/api/games")[1]["options"]["channel"] == "stable", "games: options.channel stable")

        for body, want in (({"channel": "beta"}, "stable or dev"),
                           ({"unlock": "short"}, "8 to 2048"),
                           ({"unlock": "has a space in it"}, "8 to 2048"),
                           ({"unlock": "olx1.bm90IGpzb24"}, "damaged"),
                           ({"channel": "dev", "code": "olx7.eyJyIjoieCJ9"}, "newer version"),
                           ({"unlock": CH_CODE, "extra": 1}, "unknown fields"),
                           ({"key": "ffxi", "to": "dev", "unlock": CH_CODE}, "unknown fields"),
                           ({"registry": registry}, "unknown fields"),
                           ({}, "Say what to do"),
                           ({"unlock": CH_CODE, "registry": "Not A Registry"}, "registry.example.org"),
                           ({"key": "ffxi", "to": "dev"}, devchannel.LOCKED),
                           ({"key": "ffxi", "to": "beta"}, "stable or dev"),
                           ({"key": "ffxi", "to": "stable"}, "already on Stable"),
                           ({"key": "jan", "to": "dev"}, "not installed"),
                           ({"key": "nothing", "to": "dev"}, "no dev build"),
                           ({"lock": True}, "not unlocked"),
                           ({"lock": "yes"}, "lock is true")):
            st, r = c.call("POST", "/api/channel", body)
            ok(st == 400 and want in (r or {}).get("error", ""), "channel: %s refused (%s)" % (body, r))
        st, r = c.call("POST", "/api/channel", {"unlock": "kart-wrong-code-0000"})
        ok(st == 400 and "did not accept" in r.get("error", ""), "unlock: a wrong code refused at once (%s)" % r)
        st, r = c.call("POST", "/api/channel", {"unlock": CH_CODE, "registry": "127.0.0.1:%d/kart" % free_port()})
        ok(st == 502 and "did not answer" in r.get("error", "") and "127.0.0.1" not in r.get("error", ""),
           "unlock: a registry nobody answers: 502, not named (%s)" % r)
        ok(not dcalls() and not os.path.exists(chfile) and not c.get("/api/jobs")[1]["jobs"],
           "channel: refused requests ran no docker command, wrote no .channel, started no job")

        # unlock: sign in and keep the code, nothing else changes
        st, r = c.call("POST", "/api/channel", {"unlock": CH_CODE})
        ok(st == 202 and r.get("job"), "unlock: a good code starts the job (%s)" % r)
        j = c.wait(r["job"], 60)
        ok(j["state"] == "ok" and "unlocked" in j["summary"] and j["action"] == "channel" and j.get("op") == "unlock",
           "unlock job: ok (%s)" % j["summary"])
        logins = [x for x in dcalls() if x["args"][0] == "login"]
        ok(len(logins) == 1 and logins[0]["args"] == ["login", "--username", "dev", "--password-stdin", host]
           and logins[0].get("stdin_sha") == _h.sha256(CH_CODE.encode()).hexdigest()
           and logins[0]["docker_config"] == os.path.join(chstate, "docker"),
           "unlock: one docker login, the code on stdin, into the deployer's own DOCKER_CONFIG")
        ok(not compose(), "unlock: nothing was pulled or restarted (%s)" % compose())
        doc = json.load(open(chfile))
        ok(doc.get("version") == 2 and doc["unlocked"] is True and doc["dev"] == [] and doc["registry"] == registry
           and CH_CODE not in json.dumps(doc), "unlock: .channel is unlocked with nothing on Dev, no code in it")
        cp = os.path.join(chstate, "code")
        ok(open(cp).read() == CH_CODE, "unlock: the code is kept in the deployer's state")
        if os.name != "nt":
            ok(stat.S_IMODE(os.stat(cp).st_mode) == 0o600 and stat.S_IMODE(os.stat(chstate).st_mode) == 0o700,
               "unlock: code file 600, its folder 700")
        ok(host not in j["log"] and "[access code]" in j["log"] and CH_CODE not in j["log"],
           "unlock job: the log blanks the code and names no registry")
        code, ch = c.get("/api/channel")
        ok(ch["unlocked"] and ch["dev"] == [] and ch["code_stored"] and ch["code_ok"] is True and ch["since"]
           and all(k["on"] == "stable" for k in ch["keys"]) and CH_CODE not in json.dumps(ch),
           "unlock: GET says unlocked, the code works, everything still on Stable")

        # one key to Dev: its images only, its services only
        clear()
        j = switch(c, "ffxi", "dev")
        ok(j["state"] == "ok" and "lsb-server (dev)" in j["summary"] and j.get("key") == "ffxi" and j.get("to") == "dev",
           "ffxi to Dev: ok (%s)" % j["summary"])
        comp = compose()
        want_svcs = lsb_services + ["bridge"]
        ok(len(comp) == 2 and comp[0]["args"][1:5] == ["--no-prebuild", "pull", "--ignore-buildable",
                                                      "--ignore-pull-failures"]
           and comp[0]["args"][5:] == want_svcs and comp[1]["args"][1:3] == ["up", "-d"]
           and comp[1]["args"][-len(want_svcs):] == want_svcs
           and all(e["docker_config"] == os.path.join(chstate, "docker") for e in comp),
           "ffxi to Dev: pulls and restarts FFXI's services only, signed in (%s)" % [e["args"] for e in comp])
        ok(any(e["args"] == ["channel", "set", "ffxi", "dev"] for e in senvs()), "ffxi to Dev: stack.py channel set ffxi dev")
        ok(host not in j["log"] and "still reads the dev builds" in j["log"],
           "ffxi to Dev: the code is asked again first; the log names no registry")
        ok(json.load(open(chfile))["dev"] == ["ffxi"], "ffxi to Dev: .channel has ffxi on Dev only")
        code, ch = c.get("/api/channel")
        ok(ch["dev"] == ["ffxi"] and ch["channel"] == "dev" and krow(ch, "ffxi")["on"] == "dev"
           and krow(ch, "ffxi")["since"] and krow(ch, "core")["on"] == "stable"
           and "may expect" in (krow(ch, "ffxi")["note"] or "") and krow(ch, "tm")["note"] is None,
           "GET: ffxi on Dev with a warning (the core is on Stable), the rest on Stable")
        ok(host not in json.dumps(ch), "GET: names no registry")
        ff = [r for r in ch["images"] if r["stable"] == "ghcr.io/landsandboat/server"]
        ok(ff and ff[0]["on"] and ff[0]["has_source"] is True, "GET: on Dev the world server's source is offered")
        st, src = c.get("/api/channel/source?stable=ghcr.io/landsandboat/server")
        ok(st == 200 and src.get("url") == "https://%s/lsb-server" % registry and src.get("license"),
           "channel source: the address behind the link (%s)" % src)
        ok(c.get("/api/channel/source?stable=ghcr.io/prettyopenlobby/openlobby")[0] == 404,
           "channel source: none for the core, which is on Stable")
        ok(c.get("/api/games")[1]["options"]["channel"] == "dev", "games: options.channel dev")
        st, r = c.call("POST", "/api/channel", {"lock": True})
        ok(st == 409 and "Stable first" in r.get("error", ""), "lock: refused while ffxi is on Dev (%s)" % r)

        # a key whose dev build does not apply here (built from its checkout): recorded, nothing restarted
        clear()
        j = switch(c, "tm", "dev")
        ok(j["state"] == "ok" and "nothing was restarted" in j["summary"] and not compose(),
           "tm to Dev, built here: on Dev, nothing pulled or restarted (%s)" % j["summary"])
        j = switch(c, "tm", "stable")
        ok(j["state"] == "ok" and json.load(open(chfile))["dev"] == ["ffxi"], "tm back to Stable")

        # the update check: the dev image's digest against the registry's
        inspect = os.path.join(chdir, "inspect.json")

        def local(digest):
            with open(inspect, "w") as f:
                json.dump({ref: {"RepoDigests": [registry + "/lsb-server@" + digest],
                                 "Config": {"Labels": {}, "Env": []}}}, f)
        local("sha256:" + "0" * 64)
        code, u, _ = updates(c, refresh=True)
        it = by_key(u).get("ffxi") or {}
        ok(u.get("channel") == "dev" and it.get("channel") == "dev" and it.get("update") == "available"
           and "newer dev build" in (it.get("why") or "") and shape_ok(it),
           "updates: an older dev image here: available for the key on Dev (%s)" % it.get("why"))
        ok("dev images" in (it.get("how") or ""), "  how: %s" % it.get("how"))
        local(reg.images["kart/lsb-server:stock"])
        time.sleep(0.05)
        code, u, _ = updates(c, refresh=True)
        it = by_key(u).get("ffxi") or {}
        ok(it.get("update") != "available", "updates: the registry's digest here: no dev update (%s)" % it.get("why"))
        ok(by_key(u).get("tm", {}).get("channel") is None and by_key(u).get("core", {}).get("channel") is None,
           "updates: what is on Stable is not marked dev")
        ok(host not in json.dumps(u), "updates: the answer names no registry")

        # the code is revoked: the keys on Dev say so and refuse updates; Stable keys go on
        set_valid()
        clear()
        j = c.run("update", "ffxi")
        ok(j["state"] == "failed" and j["summary"] == devchannel.REVOKED,
           "revoked: an update of ffxi (on Dev) fails with the sentence (%s)" % j["summary"])
        ok(not compose() and "$ git" not in j.get("log", ""), "revoked: nothing was pulled or fetched")
        j = c.run("enable", "tm")
        ok(j["summary"] != devchannel.REVOKED and j["state"] in ("ok", "needs_data"),
           "revoked: enabling tm (on Stable) is not refused (%s)" % j["summary"])
        code, ch = c.get("/api/channel")
        ok(krow(ch, "ffxi")["problem"] == devchannel.REVOKED and krow(ch, "tm")["problem"] is None
           and ch["code_ok"] is False and ch["code_why"] == devchannel.REVOKED,
           "revoked: GET says the access code no longer works on the key on Dev only (%s)" % krow(ch, "ffxi"))
        st, r = c.call("POST", "/api/channel", {"key": "tm", "to": "dev"})
        ok(st == 202 or (st == 400 and r.get("error") == devchannel.REVOKED),
           "revoked: tm to Dev (nothing to pull here) is not asked of the registry, or refused with the sentence")
        if st == 202:
            c.wait(r["job"], 60)
            switch(c, "tm", "stable")
        code, u, _ = updates(c, refresh=True)
        it = by_key(u).get("ffxi") or {}
        ok(it.get("update") == "unknown" and it.get("why") == devchannel.REVOKED,
           "revoked: the update list says so for the dev item")

        # back to Stable without a working code
        clear()
        j = switch(c, "ffxi", "stable")
        ok(j["state"] == "ok" and "Stable" in j["summary"] and "restore the backup" in j["summary"],
           "revoked: ffxi back to Stable still works, with FFXI's database note (%s)" % j["summary"])
        comp = compose()
        ok(len(comp) == 2 and comp[0]["args"][5:] == want_svcs and comp[1]["args"][-len(want_svcs):] == want_svcs,
           "ffxi to Stable: pulls and restarts FFXI's services only")
        ok(not any(x["args"][0] == "logout" for x in dcalls()) and os.path.exists(cp)
           and json.load(open(chfile))["unlocked"] is True and json.load(open(chfile))["dev"] == [],
           "ffxi to Stable: still unlocked, the code kept")
        ok(not any(host in (x.get("log") or "") for x in [c.get("/api/jobs/" + y["id"])[1]
                                                          for y in c.get("/api/jobs?limit=50")[1]["jobs"]]),
           "no job log names the registry")

        # lock: sign out and forget
        clear()
        st, r = c.call("POST", "/api/channel", {"lock": True})
        j = c.wait(r["job"], 60) if st == 202 else {"state": "http %d" % st, "summary": str(r), "log": ""}
        ok(j["state"] == "ok" and "forgotten" in j["summary"], "lock: ok (%s)" % j["summary"])
        ok(any(x["args"][0] == "logout" for x in dcalls()) and not os.path.exists(chstate)
           and not os.path.exists(chfile) and not compose(),
           "lock: docker logout, code and .channel removed, nothing restarted")
        ok(host not in j.get("log", ""), "lock: the job log names no registry (docker logout's included)")
        ok(c.get("/api/channel")[1]["unlocked"] is False, "lock: GET says locked")

        # the file from before: {"channel": "dev"} is every key with a dev build on Dev
        set_valid(CH_CODE)
        with open(chfile, "w") as f:
            json.dump({"channel": "dev", "registry": registry, "source": "https://" + registry,
                       "since": "2026-10-01T10:00:00Z"}, f)
        code, ch = c.get("/api/channel")
        ok(ch["unlocked"] and ch["dev"] == all_dev and all(k["on"] == "dev" for k in ch["keys"])
           and ch["code_stored"] is False and all(k["problem"] == devchannel.REVOKED for k in ch["keys"]),
           "old .channel: every key on Dev; with no code stored each says the code no longer works (%s)" % ch["dev"])
        j = c.run("update", "ffxi")
        ok(j["state"] == "failed" and j["summary"] == devchannel.NO_CODE,
           "old .channel, no code: an update refuses with the sentence (%s)" % j["summary"])
        clear()
        j = switch(c, "ffxi", "stable")
        doc = json.load(open(chfile))
        ok(j["state"] == "ok" and doc.get("version") == 2 and doc["dev"] == [k for k in all_dev if k != "ffxi"]
           and doc["since"] == "2026-10-01T10:00:00Z",
           "old .channel: ffxi to Stable without a code; the file is version 2 now, the rest still on Dev (%s)"
           % doc.get("dev"))
        clear()
        st, r = c.call("POST", "/api/channel", {"channel": "stable"})
        j = c.wait(r["job"], 60) if st == 202 else {"state": "http %d" % st, "summary": str(r)}
        ok(j["state"] == "ok" and "Everything is on Stable" in j["summary"] and not os.path.exists(chfile),
           "{channel: stable} (as before): everything back on Stable and locked (%s)" % j["summary"])
        comp = compose()
        ok(comp and all(e["docker_config"] is None for e in comp) and comp[0]["args"][-1] == "--ignore-pull-failures",
           "{channel: stable}: the whole stack pulled again, without DOCKER_CONFIG")

        # --channel for the setup scripts: dev is everything with a dev build on Dev
        set_valid(CH_CODE2.split(":", 1)[1])
        clear()
        argv = [sys.executable, DEPLOYER, "--channel", "dev", "--bind", "127.0.0.1:%d" % s.port]
        cli = subprocess.run(argv, input="kart-wrong-code-0000\n", env=s.env(), capture_output=True,
                             text=True, timeout=120)
        ok(cli.returncode == 1 and "did not accept" in cli.stdout, "--channel dev, a wrong code: exit 1 (%s)" % cli.stdout.strip())
        cli = subprocess.run(argv, input=CH_CODE2 + "\n", env=s.env(), capture_output=True, text=True, timeout=120)
        ok(cli.returncode == 0 and "dev channel" in cli.stdout and "lsb-server (dev)" in cli.stdout,
           "--channel dev: exit 0 (%s)" % cli.stdout.strip()[-200:])
        logins = [x for x in dcalls() if x["args"][0] == "login"]
        ok(logins and logins[-1]["args"][2] == "kartclub", "--channel dev: name:code signs in under that name")
        doc = json.load(open(chfile))
        ok(doc["unlocked"] and doc["dev"] == all_dev, "--channel dev: unlocked, every key with a dev build on Dev")
        comp = compose()
        ok(len(comp) == 2 and comp[0]["args"][-1] == "--ignore-pull-failures" and comp[1]["args"][1:3] == ["up", "-d"],
           "--channel dev: the whole stack pulled and restarted, as before")
        cli = subprocess.run([sys.executable, DEPLOYER, "--channel", "stable", "--bind", "127.0.0.1:%d" % s.port],
                             env=s.env(), capture_output=True, text=True, timeout=120)
        ok(cli.returncode == 0 and "Stable" in cli.stdout and not os.path.exists(chfile), "--channel stable: exit 0, locked")

        # the issuer of short codes: the environment says off here
        ch = c.get("/api/channel")[1]
        ok(ch["issuer"] == {"value": "off", "from": "env", "default": "https://openlobby.fyi"}
           and ch["short_codes"] is False, "issuer: POL_DEV_ISSUER=off (%s)" % ch["issuer"])
        st, r = c.call("POST", "/api/channel", {"issuer": "https://codes.example.org"})
        ok(st == 409 and "POL_DEV_ISSUER" in r.get("error", ""), "issuer: not set here while the environment sets it")

        # the code is nowhere it should not be
        secrets = [CH_CODE, CH_CODE2, CH_CODE2.split(":", 1)[1]]
        hits = []
        files = [os.path.join(dp, f) for dp, _, fs in os.walk(state) for f in fs]
        files += [os.path.join(t.fakedir, n) for n in ("calls.txt", "envs.txt")]
        files += [os.path.join(chdir, "calls.txt"), s.err.name]
        for path in files:
            try:
                data = open(path, "rb").read()
            except OSError:
                continue
            for sec in secrets:
                if sec.encode() in data:
                    hits.append("%s in %s" % (sec[:9], os.path.relpath(path, t.tmp)))
        ok(not hits, "the access code is in no job file, log, argument or answer (%s)" % hits)
        ok(not any(sec in x for x in reg.paths for sec in secrets), "the access code is in no URL")
    finally:
        s.stop()

    # the issuer: the default, the owner's setting, validation
    istate = os.path.join(t.tmp, "state-issuer")
    s = Server(t, extra_env=dict(env, POL_DEV_ISSUER=""), args=["--state", istate, "--docker", dock])
    servers.append(s)
    try:
        c = s.start()
        ch = c.get("/api/channel")[1]
        ok(ch["issuer"] == {"value": "https://openlobby.fyi", "from": "default", "default": "https://openlobby.fyi"}
           and ch["short_codes"] is True, "issuer: unset, the project's own (%s)" % ch["issuer"])
        for bad in ("http://codes.example.org", "codes.example.org", "https://a b", 7, ["x"]):
            st, r = c.call("POST", "/api/channel", {"issuer": bad})
            ok(st == 400 and "https" in r.get("error", ""), "issuer: %r refused (%s)" % (bad, r))
        st, r = c.call("POST", "/api/channel", {"issuer": "https://codes.example.org/"})
        ok(st == 200 and r["issuer"] == {"value": "https://codes.example.org", "from": "state",
                                         "default": "https://openlobby.fyi"},
           "issuer: the owner's https address is kept (%s)" % r)
        ok(json.load(open(os.path.join(istate, "dev-issuer.json"))) == {"issuer": "https://codes.example.org"},
           "issuer: kept in the deployer's state")
        st, r = c.call("POST", "/api/channel", {"issuer": "off"})
        ch = c.get("/api/channel")[1]
        ok(st == 200 and ch["issuer"]["value"] == "off" and ch["issuer"]["from"] == "state" and ch["short_codes"] is False,
           "issuer: off by the owner")
        st, r = c.call("POST", "/api/channel", {"unlock": "KART-ZZZZ-DEV9"})
        ok(st == 400 and "the issuer is off" in r.get("error", ""), "issuer off: a short code is refused (%s)" % r)
        st, r = c.call("POST", "/api/channel", {"issuer": ""})
        ok(st == 200 and r["issuer"]["from"] == "default" and not os.path.exists(os.path.join(istate, "dev-issuer.json")),
           "issuer: empty goes back to the default")
    finally:
        s.stop()
    _test_channel_codes(t, servers, reg, chdir, dock, set_valid, dcalls, clear, registry, host, state,
                        games, saved, bridge, chfile)


def _test_channel_codes(t, servers, reg, chdir, dock, set_valid, dcalls, clear, registry, host, state,
                        games, saved, bridge, chfile):
    """olx1. access codes: no POL_DEV_REGISTRY, the code carries the registry, the token and
    the source; a code without a token for public images signs in as nobody."""
    import hashlib
    sys.path.insert(0, os.path.join(ROOT, "services", "deployer"))
    import devchannel
    token = "kart-dev-token-3Hq8"
    code = devchannel.make_code(registry, token, "https://code.example.org/kart")
    os.makedirs(bridge, exist_ok=True)
    with open(games, "w") as f:
        f.write("ffxi\ntm\n")
    set_valid(token)
    clear()
    env = {"POL_DEV_REGISTRY_HTTP": "1", "FAKE_CH_DIR": chdir}
    s = Server(t, extra_env=env, args=["--state", state, "--docker", dock, "--refresh-gap", "0"])
    servers.append(s)
    try:
        c = s.start()
        st, r = c.call("POST", "/api/channel", {"channel": "dev", "code": "kart-plain-token-01"})
        ok(st == 400 and "start with olx" in r.get("error", ""), "code: a plain token with no registry set: refused (%s)" % r)
        bad = devchannel.make_code(registry, "kart-wrong-token", None)
        st, r = c.call("POST", "/api/channel", {"channel": "dev", "code": bad})
        ok(st == 400 and "did not accept" in r.get("error", "") and host not in json.dumps(r),
           "code: a code whose token is refused: 400, the registry not named (%s)" % r)
        st, r = c.call("POST", "/api/channel", {"channel": "dev", "code": code})
        j = c.wait(r["job"], 60) if st == 202 else {"state": "http %d" % st, "summary": str(r), "log": ""}
        ok(j["state"] == "ok", "code: an olx1. code switches to Dev (%s)" % j["summary"])
        logins = [x for x in dcalls() if x["args"][0] == "login"]
        ok(len(logins) == 1 and logins[0]["args"][-1] == host
           and logins[0].get("stdin_sha") == hashlib.sha256(token.encode()).hexdigest(),
           "code: docker login to the code's registry with its token on stdin")
        doc = json.load(open(chfile))
        ok(doc["registry"] == registry and doc["source"] == "https://code.example.org/kart",
           "code: .channel has the code's registry and source")
        ok(host not in j["log"] and token not in j["log"] and code not in j["log"],
           "code: the job log has neither the registry, the token nor the code")
        st, src = c.get("/api/channel/source?stable=ghcr.io/landsandboat/server")
        ok(st == 200 and src.get("url") == "https://code.example.org/kart/lsb-server",
           "code: the source is under the code's source address (%s)" % src)
        ch = c.get("/api/channel")[1]
        ok(host not in json.dumps(ch) and "code.example.org" not in json.dumps(ch),
           "code: GET /api/channel names neither the registry nor the source host")
        j = c.run("enable", "ffxi")
        ok(j["state"] in ("ok", "needs_data") and host not in j.get("log", "") and "still reads the dev builds" in j.get("log", ""),
           "code: enabling a game on Dev asks again with the code's token (%s)" % j["summary"])
        st, r = c.call("POST", "/api/channel", {"channel": "stable"})
        j = c.wait(r["job"], 60) if st == 202 else {"state": "http %d" % st}
        ok(j["state"] == "ok", "code: back to stable")

        # public images: a code without a token, no docker login at all
        reg.public = True
        set_valid()
        clear()
        pub = devchannel.make_code(registry)
        st, r = c.call("POST", "/api/channel", {"channel": "dev", "code": pub})
        j = c.wait(r["job"], 60) if st == 202 else {"state": "http %d" % st, "summary": str(r), "log": ""}
        ok(j["state"] == "ok" and not [x for x in dcalls() if x["args"][0] == "login"]
           and "no sign-in" in j["log"], "code: public images: on Dev with no docker login (%s)" % j["summary"])
        j = c.run("enable", "ffxi")
        ok(j["state"] in ("ok", "needs_data") and "still reads the dev builds" in j.get("log", ""), "code: public images: enable asks the registry as nobody (%s)" % j["summary"])
        st, r = c.call("POST", "/api/channel", {"channel": "stable"})
        if st == 202:
            c.wait(r["job"], 60)
        # a POL_DEV_REGISTRY for a plain token does not override a code's own
        reg.public = False
        st, r = c.call("POST", "/api/channel", {"channel": "dev", "code": "KART-ZZZZ-DEV9"})
        ok(st == 400 and "the issuer is off" in r.get("error", ""), "short code: the issuer off: refused (%s)" % r)
        s.stop()
        iss = FakeIssuer()
        iss.codes["KARTZZZZDEV9"] = code
        set_valid(token)
        clear()
        s = Server(t, extra_env=dict(env, POL_DEV_ISSUER=iss.url),
                   args=["--state", state, "--docker", dock, "--refresh-gap", "0"])
        servers.append(s)
        try:
            c = s.start()
            inst = c.get("/api/channel")[1]["install_id"]
            st, r = c.call("POST", "/api/channel", {"channel": "dev", "code": "kart-zzzz-dev9"})
            j = c.wait(r["job"], 60) if st == 202 else {"state": "http %d" % st, "summary": str(r), "log": ""}
            ok(j["state"] == "ok" and iss.asked[-1][1] == {"code": "KARTZZZZDEV9", "install_id": inst},
               "short code: redeemed with the install id, then on Dev (%s)" % j["summary"])
            logins = [x for x in dcalls() if x["args"][0] == "login"]
            ok(len(logins) == 1 and logins[0].get("stdin_sha") == hashlib.sha256(token.encode()).hexdigest(),
               "short code: docker login with the token of the access code it redeemed to")
            ok(open(os.path.join(state, "channel", "code")).read() == code,
               "short code: the deployer keeps the access code, not the short one")
            ok("kart-zzzz-dev9" not in j["log"] and "KARTZZZZDEV9" not in j["log"] and code not in j["log"],
               "short code: in no job log")
            st, r = c.call("POST", "/api/channel", {"channel": "stable"})
            j = c.wait(r["job"], 60) if st == 202 else {"state": "http %d" % st}
            ok(j["state"] == "ok" and c.get("/api/channel")[1]["install_id"] == inst,
               "short code: back to stable keeps the install id")
            sys.path.insert(0, os.path.join(ROOT, "services", "deployer"))
            import patchfeed as _pf
            ok(_pf.Store(state).code() is None, "short code to an olx1. code: nothing offered to the patch store")
            fcode = devchannel.make_code(registry, token, "https://code.example.org/kart",
                                         feed="code.example.org/kart-patches")
            iss.codes["KARTZZZZDEV8"] = fcode
            st, r = c.call("POST", "/api/channel", {"unlock": "KARTZZZZDEV8"})
            j = c.wait(r["job"], 60) if st == 202 else {"state": "http %d" % st, "summary": str(r), "log": ""}
            ok(j["state"] == "ok" and open(os.path.join(state, "channel", "code")).read() == fcode,
               "short code to an olx2. code: unlocks with it (%s)" % j["summary"])
            ok(_pf.Store(state).code() == fcode and not _pf.Store(state).enabled(),
               "short code to an olx2. code: offered to the patch store, patches stay off")
            ok(fcode not in j["log"] and "KARTZZZZDEV8" not in j["log"], "  and in no job log")
            st, r = c.call("POST", "/api/channel", {"lock": True})
            j = c.wait(r["job"], 60) if st == 202 else {"state": "http %d" % st}
            ok(j["state"] == "ok" and _pf.Store(state).code() == fcode,
               "lock forgets the channel's code, the patch store keeps its own")
            iss.bound["KARTZZZZDEV9"] = "ol-" + "f" * 32
            st, r = c.call("POST", "/api/channel", {"channel": "dev", "code": "KARTZZZZDEV9"})
            ok(st == 400 and "another server" in r.get("error", ""), "short code: bound to another install: the "
                                                                    "issuer's sentence (%s)" % r)
            iss.stop()
            st, r = c.call("POST", "/api/channel", {"channel": "dev", "code": "KARTZZZZDEV9"})
            ok(st == 502 and "did not answer" in r.get("error", ""), "short code: the issuer away: 502 (%s)" % r)
        finally:
            iss.stop()
    finally:
        reg.public = False
        s.stop()
        if saved is None:
            os.remove(games)
        else:
            with open(games, "w") as f:
                f.write(saved)
        shutil.rmtree(bridge, ignore_errors=True)
        if os.path.exists(chfile):
            os.remove(chfile)
        if saved is None:
            os.remove(games)
        else:
            with open(games, "w") as f:
                f.write(saved)
        shutil.rmtree(bridge, ignore_errors=True)
        if os.path.exists(chfile):
            os.remove(chfile)
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
                "POL_DEV_REGISTRY_HTTP": "1",
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


class FakeValkey:
    """Answers GET and SELECT over RESP on loopback with whatever `value` holds."""

    def __init__(self):
        self.value = None
        self.keys = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                f = conn.makefile("rb")
                try:
                    while True:
                        head = f.readline()
                        if not head.startswith(b"*"):
                            break
                        args = []
                        for _ in range(int(head[1:])):
                            n = int(f.readline()[1:])
                            args.append(f.read(n + 2)[:n].decode())
                        if args[0].upper() == "SELECT":
                            conn.sendall(b"+OK\r\n")
                        else:
                            self.keys.append(args[1])
                            v = self.value() if callable(self.value) else self.value
                            if v is None:
                                conn.sendall(b"$-1\r\n")
                            else:
                                v = v.encode()
                                conn.sendall(b"$%d\r\n%s\r\n" % (len(v), v))
                except (OSError, ValueError):
                    pass

    def close(self):
        self.sock.close()


def test_patch_drain():
    """restart_patch waits while the patch service reports downloads (its
    live:patch marker in Valkey), restarts once they finish, gives up after
    the limit with a sentence, and does not wait on a stale or missing marker."""
    import types
    sys.path.insert(0, os.path.join(ROOT, "services", "deployer"))
    import deployer as dep
    v = FakeValkey()

    class Log:
        def __init__(self):
            self.lines = []

        def line(self, s):
            self.lines.append(s)

    def runner(url, drain=2.0):
        r = types.SimpleNamespace(log=Log(), composed=[])
        r.cfg = types.SimpleNamespace(valkey=dep.patches.valkey_address(url), kv_prefix="pol:",
                                      patch_drain=drain, drain_poll=0.05)
        r.compose = lambda args, prebuild=True: r.composed.append(args)
        return r

    def marker(count, age=0.0):
        return json.dumps({"count": count, "stamp": time.time() - age})

    url = "valkey://127.0.0.1:%d/0" % v.port
    try:
        ok(dep.patches.valkey_address("valkey://valkey:6379/0") == ("valkey", 6379, 0)
           and dep.patches.valkey_address("redis://:pw@h:7000/2") == ("h", 7000, 2)
           and dep.patches.valkey_address("http://x") is None
           and dep.patches.valkey_address("") is None, "valkey_address reads the URL forms")
        v.value = marker(0)
        r = runner(url)
        note = dep.restart_patch(r)
        ok(note == "" and r.composed == [["restart", "patch"]] and v.keys[-1] == "pol:live:patch"
           and not any("waiting" in x for x in r.log.lines),
           "no downloads: restarts at once (%s)" % r.log.lines)
        until = time.time() + 0.4
        v.value = lambda: marker(2 if time.time() < until else 0)
        r = runner(url)
        t0 = time.time()
        note = dep.restart_patch(r)
        ok(note == "" and r.composed == [["restart", "patch"]] and time.time() - t0 >= 0.35
           and any("2 download(s) in progress; waiting" in x for x in r.log.lines)
           and "0 download(s) in progress" in r.log.lines,
           "downloads that finish: waits for them, then restarts (%s)" % r.log.lines)
        v.value = lambda: marker(1)
        r = runner(url, drain=0.3)
        note = dep.restart_patch(r)
        ok("1 download(s) were still running" in note and r.composed == [["restart", "patch"]],
           "downloads past the limit: restarts and says so (%s)" % note.strip())
        v.value = marker(3, age=600)
        r = runner(url)
        note = dep.restart_patch(r)
        ok(note == "" and r.composed == [["restart", "patch"]]
           and any("not reported" in x for x in r.log.lines),
           "a stale marker: no wait, and the log says why")
        r = runner("")
        note = dep.restart_patch(r)
        ok(note == "" and r.composed == [["restart", "patch"]] and not r.log.lines[:-1],
           "no POL_VALKEY_URL: restarts as before")
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        port = dead.getsockname()[1]
        dead.close()
        r = runner("valkey://127.0.0.1:%d/0" % port)
        t0 = time.time()
        note = dep.restart_patch(r)
        ok(note == "" and r.composed == [["restart", "patch"]] and time.time() - t0 < 5,
           "Valkey not answering: restarts without waiting")
    finally:
        v.close()


class FakeFeed:
    """A Forgejo generic package registry on loopback, enough of it for the
    patch feed: the package list, a version's files and the downloads, all
    behind Basic auth as dev:<token>. `packages` is {(name, version): {file: bytes}}."""

    def __init__(self, token):
        import base64 as _b64
        self.want = "Basic " + _b64.b64encode(("dev:%s" % token).encode()).decode()
        self.packages = {}
        self.tamper = set()
        self.hits = []
        feed = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                u = urllib.parse.urlsplit(self.path)
                q = urllib.parse.parse_qs(u.query)
                parts = [urllib.parse.unquote(p) for p in u.path.split("/") if p]
                feed.hits.append(u.path)
                if self.headers.get("Authorization") != feed.want:
                    return self.send(401, {"message": "unauthorized"})
                if parts[:3] == ["api", "v1", "packages"] and len(parts) == 4:
                    page, limit = int(q["page"][0]), int(q["limit"][0])
                    rows = [{"name": n, "version": v, "type": "generic"}
                            for n, v in sorted(feed.packages)]
                    return self.send(200, rows[(page - 1) * limit:page * limit])
                if parts[:3] == ["api", "v1", "packages"] and len(parts) == 8 and parts[7] == "files":
                    files = feed.packages.get((parts[5], parts[6]))
                    if files is None:
                        return self.send(404, {"message": "not found"})
                    return self.send(200, [{"name": n, "Size": len(b),
                                            "sha256": hashlib.sha256(b).hexdigest()}
                                           for n, b in sorted(files.items())])
                if parts[:2] == ["api", "packages"] and len(parts) == 7:
                    b = (feed.packages.get((parts[4], parts[5])) or {}).get(parts[6])
                    if b is None:
                        return self.send(404, {"message": "not found"})
                    if (parts[5], parts[6]) in feed.tamper:
                        b = b[:-1] + bytes([b[-1] ^ 1])
                    return self.send(200, b, "application/octet-stream")
                self.send(404, {"message": "not found"})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


def test_patch_feed_plan():
    """patchfeed.plan: every status, and the cheapest route it picks."""
    sys.path.insert(0, os.path.join(ROOT, "services", "deployer"))
    import patchfeed as pf
    f = {"size": 10, "sha256": "0" * 64}
    full = {"size": 100, "sha256": "1" * 64}
    vs = {PV1: {"full.polpatch": full},
          PV2: {"update-from-%s.polpatch" % PV1: f},
          PV3: {"update-from-%s.polpatch" % PV2: f, "full.polpatch": full}}
    p = pf.plan(vs, [])
    ok(p["status"] == "new" and [s[1] for s in p["steps"]] == ["full.polpatch"] and p["steps"][0][0] == PV3,
       "plan: nothing here -> the newest full file (%s)" % p["steps"])
    p = pf.plan(vs, [PV1])
    ok(p["status"] == "behind" and [s[0] for s in p["steps"]] == [PV2, PV3] and p["size"] == 20,
       "plan: behind -> the chain of updates (%s)" % p["steps"])
    p = pf.plan(vs, [PV1, PV2, PV3])
    ok(p["status"] == "current" and not p["steps"], "plan: current")
    p = pf.plan({PV1: vs[PV1]}, [PV1, PV2])
    ok(p["status"] == "ahead", "plan: this server is ahead")
    p = pf.plan(vs, [PV1, "20041111_0"])
    ok(p["status"] == "custom" and "20041111_0" in p["why"], "plan: a version the feed lacks -> custom (%s)" % p["why"])
    p = pf.plan({PV1: vs[PV1], PV2: {}, PV3: {"update-from-%s.polpatch" % PV2: f}}, [PV1])
    ok(p["status"] == "unavailable" and p["why"], "plan: a broken chain and no full file -> unavailable")
    del vs[PV2]["update-from-%s.polpatch" % PV1]
    vs[PV2]["x"] = f
    p = pf.plan(vs, [PV1])
    ok(p["status"] == "behind" and [s[1] for s in p["steps"]] == ["full.polpatch"],
       "plan: a broken chain falls back to the newest full file")
    jan = {"20261009_9": {"full.polpatch": full}, "20261009_14": {"update-from-20261009_9.polpatch": f}}
    p = pf.plan(jan, ["20261009_9"])
    ok(p["status"] == "behind" and p["latest"] == "20261009_14" and [s[0] for s in p["steps"]] == ["20261009_14"],
       "plan: a same-day _14 is newer than _9 (%s)" % p["steps"])
    ok(pf.plan(jan, ["20261009_9", "20261009_14"])["status"] == "current", "plan: and _14 here is current")
    ok(pf.PACKAGE.match("polpatch-W2U-0002") and not pf.PACKAGE.match("polpatch-w2u-0002")
       and not pf.PACKAGE.match("polpatch-W2U-0002/../x") and pf.UPDATE.match("update-from-%s.polpatch" % PV1)
       and not pf.UPDATE.match("update-from-x.polpatch"), "names: only the shapes the feed uses")


def test_patch_feed(c, k, t):
    sys.path.insert(0, os.path.join(ROOT, "services", "deployer"))
    import devchannel as dc
    lib = polpatch_lib()
    import patchfeed as pfd
    chfile = os.path.join(t.core, ".channel")
    store = dc.Store(k.state)

    def feed_post(body):
        return pseen(c.call("POST", "/api/patches/feed", body))
    code, r = pseen(c.get("/api/patches/feed"))
    ok(code == 409 and r.get("feed") is False and r.get("enabled") is False and r.get("can_enable") is False,
       "patches are off by default: 409, nothing to turn them on with (%s)" % r)
    code, r = pseen(c.post({"action": "patch_fetch", "region": "W2U", "product": "0002"}))
    ok(code == 409, "patch_fetch while patches are off: 409")
    dc.write_channel(t.core, "dev", registry="registry.example.org/kart")
    store.save_code(dc.make_code("registry.example.org/kart", "kart-token-0123"))
    code, r = pseen(c.get("/api/patches/feed"))
    ok(code == 409 and r.get("can_enable") is False, "a dev channel olx1. code does not make patches possible")
    code, r = feed_post({"enabled": True})
    ok(code == 400 and "Type an access code" in r.get("error", ""), "turning on with no code: 400 (%s)" % r)
    code, r = feed_post({"enabled": True, "code": dc.make_code("registry.example.org/kart", "kart-token-0123")})
    ok(code == 400 and "does not include patches" in r.get("error", ""), "turning on with an olx1. code: 400")
    ok(feed_post({"enabled": "yes"})[0] == 400, "enabled must be true or false")
    store.forget()
    os.remove(chfile)

    feed = FakeFeed("feed-token-4567")
    try:
        good = dc.make_code("registry.example.org/kart", "feed-token-4567",
                            feed="127.0.0.1:%d/kart-patches" % feed.port)
        state2 = os.path.join(t.tmp, "offer-state")
        ok(pfd.offer_code(state2, good) and pfd.Store(state2).code() == good and not pfd.Store(state2).enabled()
           and not pfd.offer_code(state2, good + "x"),
           "offer_code keeps a feed code without turning patches on, and never replaces one")
        ok(not pfd.offer_code(os.path.join(t.tmp, "offer-2"), dc.make_code("registry.example.org/kart", "x1")),
           "offer_code refuses a code with no feed")
        code, r = feed_post({"enabled": True, "code": good})
        ok(code == 200 and r.get("enabled") is True and not os.path.exists(chfile),
           "patches turned on with an olx2. code on the STABLE channel (%s)" % r)
        kept = os.path.join(k.state, "patchfeed", "code")
        ok(open(kept).read() == good and (os.name == "nt" or (os.stat(kept).st_mode & 0o777) == 0o600),
           "  the code is kept apart from the channel's, mode 600")
        name = "polpatch-W2U-0002"
        feed.packages[(name, PV1)] = {"full.polpatch": open(k.full, "rb").read()}
        feed.packages[(name, PV2)] = {"update-from-%s.polpatch" % PV1: open(k.up2, "rb").read()}
        feed.packages[(name, PV3)] = {"update-from-%s.polpatch" % PV2: open(k.up3, "rb").read(),
                                      "evil.sh": b"echo"}
        feed.packages[("polpatch-w2u-9", "1")] = {"full.polpatch": b"x"}
        # start from no bundle
        if lib.find_bundle(k.mirrors, "W2U", "0002"):
            code, j = patch_job(c, {"action": "patch_remove", "region": "W2U", "product": "0002"})
            ok(j["state"] == "ok", "feed: the old W2U/0002 bundle is removed first")
        code, r = pseen(c.get("/api/patches/feed?refresh=1"))
        rows = {"%s/%s" % (b["region"], b["product"]): b for b in r.get("bundles", [])}
        b = rows.get("W2U/0002", {})
        ok(code == 200 and r.get("feed") is True and list(rows) == ["W2U/0002"]
           and b.get("status") == "new" and b.get("latest") == PV3 and b.get("have") is None
           and b.get("files") == 3 and b.get("title", "").startswith("Tetra Master"),
           "GET /api/patches/feed: the bundle, new here, three files (%s)" % b)
        ok(set(b) == {"region", "product", "title", "latest", "have", "status", "files", "size", "why"},
           "  a row has exactly the contract's fields (%s)" % sorted(b))
        code, j = patch_job(c, {"action": "patch_fetch", "region": "W2U", "product": "0002"})
        ok(j["state"] == "ok" and "from the patch feed" in j["summary"],
           "patch_fetch: ok (%s: %s)" % (j["state"], j["summary"]))
        log = j.get("log", "")
        ok(str(feed.port) not in log and "feed-token" not in log and "127.0.0.1:%d" % feed.port not in log,
           "  the log names neither the feed's address nor the token")
        code, rows2, _ = patch_rows(c)
        b2 = rows2.get("W2U/0002", {})
        ok(b2.get("versions") == [PV1, PV2, PV3] and b2.get("served") is True and b2.get("selfcheck") == "ok",
           "  the bundle has all three versions, is served and checked (%s)" % b2)
        ok(not any(h.endswith("evil.sh") for h in feed.hits), "  a file of another shape is never fetched")
        ok(not os.listdir(os.path.join(k.state, "feed")), "  the downloads are cleaned up")
        code, r = pseen(c.get("/api/patches/feed?refresh=1"))
        ok(r["bundles"][0]["status"] == "current" and r["bundles"][0]["files"] == 0, "feed: now current")

        code, j = patch_job(c, {"action": "patch_rollback", "region": "W2U", "product": "0002", "to": PV1})
        code, r = pseen(c.get("/api/patches/feed?refresh=1"))
        ok(r["bundles"][0]["status"] == "behind" and r["bundles"][0]["files"] == 2,
           "feed after a roll back to %s: behind, two updates (%s)" % (PV1, r["bundles"][0]))
        feed.tamper.add((PV3, "update-from-%s.polpatch" % PV2))
        code, j = patch_job(c, {"action": "patch_fetch", "region": "W2U", "product": "0002"})
        ok(j["state"] == "failed" and "damaged" in j["summary"],
           "a file that comes down altered fails the job (%s)" % j["summary"])
        code, rows2, _ = patch_rows(c)
        ok(rows2["W2U/0002"]["versions"] == [PV1], "  and nothing was applied")
        feed.tamper.clear()
        code, j = patch_job(c, {"action": "patch_fetch", "region": "W2U", "product": "0002"})
        code, rows2, _ = patch_rows(c)
        ok(j["state"] == "ok" and rows2["W2U/0002"]["versions"] == [PV1, PV2, PV3],
           "the chain of updates brings it to %s (%s)" % (PV3, j["summary"]))

        hits = len(feed.hits)
        code, r = feed_post({"enabled": False})
        ok(code == 200 and r.get("enabled") is False and r.get("can_enable") is True, "turning patches off")
        code, r = pseen(c.get("/api/patches/feed?refresh=1"))
        ok(code == 409 and r.get("can_enable") is True and len(feed.hits) == hits,
           "  off: 409, and the feed is never asked (%d new requests)" % (len(feed.hits) - hits))
        code, r = feed_post({"enabled": True})
        ok(code == 200 and r.get("enabled") is True, "turning them on again uses the kept code")
        dc.Store(k.state).forget()
        ok(open(kept).read() == good, "  forgetting the channel's code leaves the patch code")
        bad = dc.make_code("registry.example.org/kart", "wrong-token", feed="127.0.0.1:%d/kart-patches" % feed.port)
        code, r = feed_post({"enabled": True, "code": bad})
        ok(code == 502 and "no longer opens the patch feed" in r.get("error", "") and open(kept).read() == good,
           "a code the feed refuses: 502 with a sentence, and the good code is still kept (%s)" % r.get("error"))
        with open(kept, "w") as f:
            f.write(bad)
        code, r = pseen(c.get("/api/patches/feed?refresh=1"))
        ok(code == 502 and "no longer opens the patch feed" in r.get("error", ""),
           "a kept code the feed stops taking: 502 with a sentence (%s)" % r.get("error"))
        code, r = feed_post({"enabled": False, "forget": True})
        ok(code == 200 and not os.path.exists(kept) and r.get("can_enable") is False, "off with forget removes the code")
    finally:
        feed.close()
        store.forget()
        if os.path.exists(chfile):
            os.remove(chfile)


def run_patch_tests(t, servers):
    test_patch_drain()
    test_patch_feed_plan()
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
        test_patch_feed(c, k, t)
        test_patch_leaks(k, t)
    finally:
        s.stop()
        k.stop_patch_service()


def main():
    t = Tree()
    servers = []
    try:
        test_refuses_without_token(t)
        test_update_helpers()
        s = Server(t, extra_env={"FAKE_ECHO": "echo before %s after" % TOKEN},
                   args=["--refresh-gap", "2"])
        servers.append(s)
        c = s.start()
        test_auth(c)
        test_validation(c)
        test_lifecycle(c, t)
        test_needs_data(c, t)
        test_busy_and_order(c, t)
        test_actions(c, t)
        test_update(c, t)
        test_updates_checkout(c, t)
        test_games(c, t)
        test_volumes(c, t)
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
        test_updates_images(t, servers)
        test_check_paths(t)
        test_paths_server(t, servers)
        test_updater(t, servers)
        test_channel(t, servers)
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
