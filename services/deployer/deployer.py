#!/usr/bin/env python3
"""The deployer: installs, enables, updates and restarts games for the admin
panel, by running tools/stack.py. docs/deployer.md describes the HTTP API
and how to run it.

    python deployer.py                 serve (settings from the environment)
    python deployer.py --healthcheck   exit 0 when the local server answers

It is the only container that holds the Docker socket, so it accepts
nothing but a fixed set of actions on game keys from the catalog, each run
as a fixed argument list (never a shell), and only with the bearer token.

Settings (environment, or the options of the same meaning):

    POL_DEPLOYER_TOKEN   required; the service refuses to start without it
    POL_DEPLOYER_BIND    host:port to listen on (default 0.0.0.0:8099)
    POL_DEPLOY_ROOT      absolute path of the directory holding the openlobby
                         checkout and the game checkouts beside it
    POL_DEPLOY_CORE      the openlobby checkout (default POL_DEPLOY_ROOT/openlobby)
    POL_DEPLOY_GHCR      1: run the stack from the published images
    POL_DEPLOYER_STATE   where jobs and logs are kept (default /deployer)

Standard library only.
"""
import argparse
import glob
import hmac
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0"

ACTIONS = ("install", "enable", "disable", "update", "restart", "check")
CORE_ACTIONS = ("update", "restart", "check")

KEEP_JOBS = 50
LOG_CAP = 1 << 20          # a job log never grows past this on disk
LOG_KEEP = 768 << 10       # what is left of it after a trim (the tail)
TAIL = 64 << 10            # the log tail GET /api/jobs/<id> returns
MAX_BODY = 4096
GAMES_TTL = 5.0            # seconds GET /api/games reuses its last answer

JOB_ID = re.compile(r"^[0-9]{8}T[0-9]{6}-[0-9a-f]{6}$")
ACTIVE = ("queued", "running")


def say(msg):
    print("deployer: " + msg, file=sys.stderr, flush=True)


def now_iso(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if t is None else t))


def truthy(v):
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


# ------------------------------------------------------------- settings

class Config:
    def __init__(self, token, bind, root, core, stack, state, ghcr):
        self.token = token
        host, _, port = bind.rpartition(":")
        self.host, self.port = host or "0.0.0.0", int(port)
        self.root = root
        self.core = core
        self.stack = stack
        self.state = state
        self.ghcr = ghcr
        self.python = sys.executable or "python3"


def parse_args(argv):
    env = os.environ
    ap = argparse.ArgumentParser(
        prog="deployer.py",
        description="Install, enable, update and restart OpenLobby games for the "
                    "admin panel (docs/deployer.md).")
    ap.add_argument("--bind", default=env.get("POL_DEPLOYER_BIND") or "0.0.0.0:8099",
                    help="host:port to listen on (POL_DEPLOYER_BIND)")
    ap.add_argument("--root", default=env.get("POL_DEPLOY_ROOT", ""),
                    help="directory holding the openlobby checkout and the games beside it "
                         "(POL_DEPLOY_ROOT)")
    ap.add_argument("--core", default=env.get("POL_DEPLOY_CORE", ""),
                    help="the openlobby checkout (POL_DEPLOY_CORE, default ROOT/openlobby)")
    ap.add_argument("--state", default=env.get("POL_DEPLOYER_STATE") or "/deployer",
                    help="where jobs and their logs are kept (POL_DEPLOYER_STATE)")
    ap.add_argument("--stack", default="",
                    help="the stack.py to run (default CORE/tools/stack.py; tests point "
                         "this at a stand-in)")
    ap.add_argument("--healthcheck", action="store_true",
                    help="ask the local server for /api/health and exit 0 if it answers")
    return ap.parse_args(argv)


def make_config(args):
    """Config, or exit 2 with the reason. The token is checked first, so a
    container started without one says so and stops."""
    token = os.environ.get("POL_DEPLOYER_TOKEN", "").strip()
    if not token:
        say("POL_DEPLOYER_TOKEN is empty. The deployer controls Docker on this host "
            "and refuses to start without a token; set one in .env (docs/deployer.md).")
        sys.exit(2)
    if len(token) < 16:
        say("warning: POL_DEPLOYER_TOKEN is shorter than 16 characters")
    root = args.root.strip()
    if not root or not os.path.isabs(root):
        say("POL_DEPLOY_ROOT must be the absolute path of the directory that holds the "
            "openlobby checkout (got %r)." % root)
        sys.exit(2)
    root = os.path.normpath(root)
    core = os.path.normpath(args.core.strip() or os.path.join(root, "openlobby"))
    stack = args.stack or os.path.join(core, "tools", "stack.py")
    if not os.path.isfile(stack):
        say("%s not found. POL_DEPLOY_ROOT (%s) must be mounted at the same path inside "
            "the container and hold the openlobby checkout." % (stack, root))
        sys.exit(2)
    if not glob.glob(os.path.join(core, "games", "*.json")):
        say("no game catalog in %s/games" % core)
        sys.exit(2)
    try:
        cfg = Config(token, args.bind, root, core, stack, args.state,
                     truthy(os.environ.get("POL_DEPLOY_GHCR")))
    except ValueError:
        say("POL_DEPLOYER_BIND must be host:port (got %r)" % args.bind)
        sys.exit(2)
    os.makedirs(os.path.join(cfg.state, "jobs"), exist_ok=True)
    return cfg


def load_catalog(core):
    """{key: entry} from the core checkout's games/*.json."""
    out = {}
    for path in sorted(glob.glob(os.path.join(core, "games", "*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                e = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(e, dict) and e.get("key") and e.get("kind") in ("core", "game"):
            out[e["key"]] = e
    return out


def read_dotenv(path):
    out = {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:].lstrip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return out


def owner_kwargs(path):
    """subprocess arguments that run a command as the owner of `path`, so
    files git and stack.py write on the host belong to the person who owns
    the checkouts. Empty when we are not root or the owner is root."""
    if not hasattr(os, "getuid") or os.getuid() != 0:
        return {}
    try:
        st = os.stat(path)
    except OSError:
        return {}
    if st.st_uid == 0:
        return {}
    return {"user": st.st_uid, "group": st.st_gid, "extra_groups": []}


# ----------------------------------------------------------------- jobs

class Store:
    """Jobs as <id>.json beside <id>.log under STATE/jobs."""

    def __init__(self, state):
        self.dir = os.path.join(state, "jobs")
        os.makedirs(self.dir, exist_ok=True)
        self.lock = threading.Lock()

    def meta_path(self, jid):
        return os.path.join(self.dir, jid + ".json")

    def log_path(self, jid):
        return os.path.join(self.dir, jid + ".log")

    def save(self, job):
        with self.lock:
            tmp = self.meta_path(job["id"]) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(job, f)
            for attempt in range(50):
                try:
                    os.replace(tmp, self.meta_path(job["id"]))
                    return
                except PermissionError:
                    # Windows refuses while a reader has the file open
                    if attempt == 49:
                        raise
                    time.sleep(0.02)

    def load(self, jid):
        if not JOB_ID.fullmatch(jid or ""):
            return None
        try:
            with open(self.meta_path(jid), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def all(self):
        jobs = []
        for path in glob.glob(os.path.join(self.dir, "*.json")):
            j = self.load(os.path.basename(path)[:-5])
            if j:
                jobs.append(j)
        return jobs

    def create(self, action, game):
        t = time.time()
        jid = time.strftime("%Y%m%dT%H%M%S", time.gmtime(t)) + "-" + secrets.token_hex(3)
        job = {"id": jid, "action": action, "game": game, "state": "queued",
               "created": now_iso(t), "started": None, "ended": None, "exit": None,
               "summary": "", "missing": [], "log_dropped": 0, "_t": t}
        open(self.log_path(jid), "wb").close()
        self.save(job)
        return job

    def recover(self):
        """Jobs a previous run left queued or running did not finish."""
        for j in self.all():
            if j["state"] in ACTIVE:
                j["state"] = "failed"
                j["ended"] = j["ended"] or now_iso()
                j["summary"] = "The deployer stopped while this job ran; check the log, then run it again."
                self.save(j)

    def prune(self, keep=KEEP_JOBS):
        jobs = sorted(self.all(), key=lambda j: j.get("_t", 0), reverse=True)
        for j in jobs[keep:]:
            if j["state"] in ACTIVE:
                continue
            for p in (self.meta_path(j["id"]), self.log_path(j["id"])):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def log_size(self, job):
        try:
            return job.get("log_dropped", 0) + os.path.getsize(self.log_path(job["id"]))
        except OSError:
            return job.get("log_dropped", 0)

    def read_log(self, job, offset):
        """(bytes from logical offset, the logical offset they start at)."""
        dropped = job.get("log_dropped", 0)
        start = max(offset - dropped, 0)
        try:
            with open(self.log_path(job["id"]), "rb") as f:
                f.seek(start)
                data = f.read()
        except OSError:
            data = b""
        return data, dropped + start

    def tail(self, job, n=TAIL):
        size = self.log_size(job)
        data, _ = self.read_log(job, max(size - n, 0))
        if size > n:
            cut = data.find(b"\n")
            if 0 <= cut < len(data) - 1:
                data = data[cut + 1:]
        return data.decode("utf-8", "replace")


def public(job):
    out = {k: job.get(k) for k in ("id", "action", "game", "state", "created", "started",
                                   "ended", "exit", "summary", "missing")}
    out["missing"] = out["missing"] or []
    return out


class JobLog:
    """Appends to a job's log, with the token blanked out and the file kept
    under LOG_CAP by dropping its head."""

    def __init__(self, store, job, token):
        self.store, self.job, self.token = store, job, token.encode("utf-8")
        self.path = store.log_path(job["id"])

    def write(self, text):
        data = text.encode("utf-8", "replace") if isinstance(text, str) else text
        if self.token:
            data = data.replace(self.token, b"[token]")
        with open(self.path, "ab") as f:
            f.write(data)
            size = f.tell()
        if size > LOG_CAP:
            self._trim()

    def line(self, text):
        self.write(text.rstrip("\n") + "\n")

    def _trim(self):
        with open(self.path, "rb") as f:
            data = f.read()
        keep = data[-LOG_KEEP:]
        nl = keep.find(b"\n")
        if 0 <= nl < len(keep) - 1:
            keep = keep[nl + 1:]
        dropped = len(data) - len(keep)
        tmp = self.path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(keep)
        os.replace(tmp, self.path)
        self.job["log_dropped"] = self.job.get("log_dropped", 0) + dropped
        self.store.save(self.job)


class JobFailed(Exception):
    def __init__(self, summary, rc=None):
        super().__init__(summary)
        self.summary, self.rc = summary, rc


# --------------------------------------------------------------- runner

class Runner:
    """One job's commands. Every command is an argument list built here."""

    def __init__(self, cfg, catalog, log):
        self.cfg, self.catalog, self.log = cfg, catalog, log
        self.last_rc = None

    def child_env(self, as_owner=False):
        env = {k: v for k, v in os.environ.items()
               if k != "POL_DEPLOYER_TOKEN" and v != self.cfg.token}
        if as_owner:
            env["HOME"] = "/tmp"
        return env

    def show(self, argv):
        shown = []
        for a in argv:
            if a == self.cfg.stack:
                a = "tools/stack.py"
            elif a == self.cfg.python:
                a = "python"
            shown.append(shlex.quote(a))
        return " ".join(shown)

    def run(self, argv, owner=None):
        """Run argv with its output streamed into the log; the exit code."""
        kw = owner_kwargs(owner) if owner else {}
        self.log.line("$ " + self.show(argv))
        try:
            p = subprocess.Popen(argv, cwd=self.cfg.core, env=self.child_env(bool(kw)),
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, **kw)
        except FileNotFoundError:
            raise JobFailed("%s is not installed in the deployer image" % os.path.basename(argv[0]))
        for chunk in iter(p.stdout.readline, b""):
            self.log.write(chunk)
        p.stdout.close()
        rc = p.wait()
        self.last_rc = rc
        self.log.line("(exit %d)" % rc)
        return rc

    def capture(self, argv, owner=None, timeout=300):
        """(exit code, stdout) of argv; stderr goes to the log."""
        kw = owner_kwargs(owner) if owner else {}
        self.log.line("$ " + self.show(argv))
        try:
            r = subprocess.run(argv, cwd=self.cfg.core, env=self.child_env(bool(kw)),
                               stdin=subprocess.DEVNULL, capture_output=True,
                               timeout=timeout, **kw)
        except FileNotFoundError:
            raise JobFailed("%s is not installed in the deployer image" % os.path.basename(argv[0]))
        except subprocess.TimeoutExpired:
            raise JobFailed("%s did not finish within %d seconds" % (self.show(argv), timeout))
        if r.stderr:
            self.log.write(r.stderr)
        return r.returncode, r.stdout.decode("utf-8", "replace")

    # ---- the commands

    def stack(self, args, owner=None):
        return self.run([self.cfg.python, self.cfg.stack] + args, owner=owner)

    def compose(self, args, prebuild=True):
        self.profile_guard()
        head = ["compose"] + (["--ghcr"] if self.cfg.ghcr else []) + ([] if prebuild else ["--no-prebuild"])
        rc = self.stack(head + args)
        if rc != 0:
            raise JobFailed("docker compose %s failed (exit %d); the log has the details."
                            % (args[0], rc), rc)

    def up(self, extra=()):
        self.compose(["up", "-d"] + ([] if self.cfg.ghcr else ["--build"]) + list(extra))

    def check_report(self, keys):
        """stack.py check --json for `keys` (all enabled games when empty),
        with its rows copied into the log."""
        argv = [self.cfg.python, self.cfg.stack, "check", "--json"] + \
            (["--ghcr"] if self.cfg.ghcr else []) + ([",".join(keys)] if keys else [])
        rc, out = self.capture(argv)
        try:
            report = json.loads(out)
        except ValueError:
            self.log.write(out)
            raise JobFailed("stack.py check did not answer (exit %d); the log has its output." % rc, rc)
        for g in report.get("games", []):
            for level, text in g.get("rows", []):
                self.log.line("%-4s %s" % (level.upper(), text))
        for w in report.get("warnings", []):
            self.log.line("WARN " + w)
        return report

    def profile_guard(self):
        for src, val in (("the environment", os.environ.get("COMPOSE_PROFILES", "")),
                         (".env", read_dotenv(os.path.join(self.cfg.core, ".env"))
                          .get("COMPOSE_PROFILES", ""))):
            if "deployer" in [p.strip() for p in val.split(",")]:
                raise JobFailed("COMPOSE_PROFILES in %s includes deployer, so docker compose "
                                "would recreate the deployer mid-job; remove it from there."
                                % src)

    def need_git(self):
        if not shutil.which("git"):
            raise JobFailed("git is not installed in the deployer image")

    def enabled(self):
        path = os.path.join(self.cfg.core, ".games")
        keys = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.split("#", 1)[0].strip()
                    if line:
                        keys.append(line)
        except OSError:
            pass
        return keys

    def checkout(self, key):
        e = self.catalog[key]
        return self.cfg.core if e["kind"] == "core" else os.path.join(self.cfg.root, e["checkout"])

    def name(self, key):
        return self.catalog[key]["name"]


def evaluate(report, done):
    """(state, summary, missing) from a check report. `done` is the summary
    when nothing is wrong."""
    for g in report.get("games", []):
        req = [m for m in g.get("missing", []) if m.get("required")]
        if req:
            return "needs_data", needs_sentence(g["name"], req), \
                [{"path": m["path"], "hint": m["hint"]} for m in req]
    for g in report.get("games", []):
        if g.get("enabled") and not g.get("installed"):
            return "failed", "%s is enabled but has no checkout; install it." % g["name"], []
        for level, text in g.get("rows", []):
            if level == "fail":
                return "failed", "The check found a problem: %s" % text, []
    return "ok", done, []


def needs_sentence(name, req):
    m = req[0]
    path = m["path"]
    if m.get("source") == "docker volume":
        s = "%s needs the docker volume %s: %s" % (name, path[len("volume "):], m.get("tool", ""))
    elif str(m.get("source", "")).startswith("none"):
        s = "%s needs generated files: %s (make them with %s)" % (name, path, m.get("tool", ""))
    else:
        d, f = path.rsplit("/", 1) if "/" in path else ("", path)
        s = "%s needs the client's data: put %s in %s (%s)" % (
            name, f, d or ".", m.get("tool", "see its README"))
    if len(req) > 1:
        s += ", and %d more" % (len(req) - 1)
    return s


# --------------------------------------------------------------- actions

def act_install(r, key):
    r.need_git()
    if r.stack(["add", key, "--yes"], owner=r.cfg.root) != 0:
        raise JobFailed("Cloning %s failed; the log has git's message." % r.name(key), r.last_rc)
    if not os.path.isdir(r.checkout(key)):
        raise JobFailed("%s was not cloned to %s." % (r.name(key), r.checkout(key)))
    return evaluate(r.check_report([key]), "%s is installed; enable it to start it." % r.name(key))


def act_enable(r, key):
    if not os.path.isdir(r.checkout(key)):
        raise JobFailed("%s is not installed; install it first." % r.name(key))
    state, summary, missing = evaluate(r.check_report([key]), "")
    if state != "ok":
        return state, summary + (". It was not enabled." if state == "needs_data" else ""), missing
    if r.stack(["enable", key], owner=r.cfg.core) != 0:
        raise JobFailed("stack.py enable %s failed; the log has its message." % key, r.last_rc)
    r.up()
    return "ok", "%s is enabled and started." % r.name(key), []


def act_disable(r, key):
    if r.stack(["disable", key], owner=r.cfg.core) != 0:
        raise JobFailed("stack.py disable %s failed; the log has its message." % key, r.last_rc)
    r.up(["--remove-orphans"])
    return "ok", "%s is disabled and its containers are removed." % r.name(key), []


def act_update(r, key):
    d = r.checkout(key)
    rel = os.path.relpath(d, r.cfg.core).replace(os.sep, "/")
    if not os.path.isdir(d):
        raise JobFailed("%s is not installed; install it first." % r.name(key))
    r.need_git()
    rc, out = r.capture(["git", "-C", d, "status", "--porcelain", "--untracked-files=no"], owner=d)
    if rc != 0:
        raise JobFailed("%s is not a git checkout, so it cannot be updated from here." % rel, rc)
    if out.strip():
        r.log.write(out)
        raise JobFailed("%s has local changes (%s); commit or discard them, then update again."
                        % (r.name(key), rel), 1)
    if r.run(["git", "-C", d, "pull", "--ff-only"], owner=d) != 0:
        raise JobFailed("git pull --ff-only failed in %s; the log has git's message." % rel, r.last_rc)
    if key != "core":
        if key not in r.enabled():
            return evaluate(r.check_report([key]),
                            "%s is updated; it is not enabled, so nothing was restarted." % r.name(key))
        state, summary, missing = evaluate(r.check_report([key]), "")
        if state != "ok":
            return state, summary + (". It was updated but not restarted." if state == "needs_data" else ""), missing
    if r.cfg.ghcr:
        r.compose(["pull", "--ignore-buildable", "--ignore-pull-failures"], prebuild=False)
    r.up()
    if key == "core":
        return evaluate(r.check_report([]), "%s is updated and restarted." % r.name(key))
    return "ok", "%s is updated and restarted." % r.name(key), []


def act_restart(r, key):
    if key != "core" and key not in r.enabled():
        raise JobFailed("%s is not enabled; enable it first." % r.name(key))
    services = list(r.catalog[key].get("services", []))
    if not services:
        raise JobFailed("%s has no services of its own to restart." % r.name(key))
    r.compose(["up", "-d", "--force-recreate"] + services, prebuild=False)
    return "ok", "%s is restarted." % r.name(key), []


def act_check(r, key):
    report = r.check_report([] if key == "core" else [key])
    what = "the enabled games" if key == "core" else r.name(key)
    return evaluate(report, "Everything %s needs is in place." % what)


ACT = {"install": act_install, "enable": act_enable, "disable": act_disable,
       "update": act_update, "restart": act_restart, "check": act_check}


# ---------------------------------------------------------------- server

class Deployer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.store = Store(cfg.state)
        self.store.recover()
        self.store.prune()
        self.lock = threading.Lock()
        self.current = None
        self.games_cache = (0.0, None)

    def busy(self):
        return self.current

    def start(self, action, game):
        """(202, {"job"}) or (409, {"job": running id})."""
        with self.lock:
            if self.current:
                return 409, {"job": self.current, "error": "another job is running"}
            job = self.store.create(action, game)
            self.current = job["id"]
        self.store.prune()
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return 202, {"job": job["id"]}

    def _run(self, job):
        log = JobLog(self.store, job, self.cfg.token)
        r = Runner(self.cfg, load_catalog(self.cfg.core), log)
        try:
            job["state"] = "running"
            job["started"] = now_iso()
            self.store.save(job)
            log.line("deployer %s: %s %s%s" % (VERSION, job["action"], job["game"],
                                                " (published images)" if self.cfg.ghcr else ""))
            state, summary, missing = ACT[job["action"]](r, job["game"])
            job["exit"] = 0 if r.last_rc is None else r.last_rc
        except JobFailed as exc:
            state, summary, missing = "failed", exc.summary, []
            job["exit"] = exc.rc if exc.rc is not None else 1
        except Exception as exc:  # report it on the job rather than lose it
            log.write(traceback.format_exc())
            state, summary, missing = "failed", "The deployer hit an internal error: %s" % exc, []
            job["exit"] = 1
        log.line("%s: %s" % (state, summary))
        job.update(state=state, summary=summary, missing=missing, ended=now_iso())
        # one step, so whoever sees the job finished can start the next one
        with self.lock:
            try:
                self.store.save(job)
            finally:
                self.current = None
                self.games_cache = (0.0, None)

    def _stack_json(self, args, timeout):
        env = {k: v for k, v in os.environ.items()
               if k != "POL_DEPLOYER_TOKEN" and v != self.cfg.token}
        try:
            r = subprocess.run([self.cfg.python, self.cfg.stack] + args, cwd=self.cfg.core,
                               env=env, stdin=subprocess.DEVNULL, capture_output=True,
                               timeout=timeout)
            return json.loads(r.stdout.decode("utf-8", "replace"))
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None

    def games(self):
        t, cached = self.games_cache
        if cached and time.time() - t < GAMES_TTL:
            return cached
        catalog = load_catalog(self.cfg.core)
        g = ["--ghcr"] if self.cfg.ghcr else []
        report = self._stack_json(["check", "--json", "--all"] + g, 300) or {}
        ps = self._stack_json(["ps", "--json"] + g, 60) or {}
        by_key = {x["key"]: x for x in report.get("games", [])}
        ps_ok = ps.get("ok") is True
        on = Runner(self.cfg, catalog, None).enabled()
        games = []
        for key, e in sorted(catalog.items(), key=lambda kv: (kv[1].get("order", 0), kv[0])):
            if e["kind"] != "game":
                continue
            c = by_key.get(key)
            installed = os.path.isdir(os.path.join(self.cfg.root, e["checkout"]))
            mod = e.get("admin_module")
            psg = ps.get("games", {}).get(key, {}) if ps_ok else {}
            games.append({
                "key": key, "name": e["name"], "admin_module": mod,
                "installed": installed, "enabled": key in on,
                "running": psg.get("running") if ps_ok else None,
                "services": psg.get("services", {}) if ps_ok else {},
                "data_ok": c["data_ok"] if c and installed else None,
                "missing": [{"path": m["path"], "hint": m["hint"],
                             "required": bool(m.get("required"))}
                            for m in (c or {}).get("missing", [])],
                "admin_env": (["POL_ADMIN_GAME_%s_URL" % mod.upper(),
                               "POL_ADMIN_GAME_%s_TOKEN" % mod.upper()] if mod else []),
                "ports": e.get("ports", []),
                "notes": e.get("notes", ""),
            })
        out = {"games": games,
               "core": {"running": ps["core"]["running"] if ps_ok else None,
                        "services": ps["core"].get("services", {}) if ps_ok else {}},
               "options": {"ghcr": self.cfg.ghcr}}
        if not report:
            out["error"] = "stack.py check did not answer; data_ok is unknown"
        if not ps_ok:
            out["running_error"] = ps.get("error") or "docker compose ps did not answer"
        with self.lock:
            self.games_cache = (time.time(), out)
        return out

    def jobs(self, limit):
        jobs = self.store.all()
        jobs.sort(key=lambda j: j.get("_t", 0), reverse=True)
        jobs.sort(key=lambda j: 0 if j["state"] in ACTIVE else 1)
        return [public(j) for j in jobs[:limit]]


def make_handler(dep):
    cfg = dep.cfg
    want = ("Bearer " + cfg.token).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        server_version = "openlobby-deployer/" + VERSION
        sys_version = ""

        def log_message(self, fmt, *args):
            say("%s %s" % (self.address_string(), fmt % args))

        def send_json(self, code, obj, headers=()):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def authed(self):
            got = (self.headers.get("Authorization") or "").strip().encode("utf-8", "replace")
            if hmac.compare_digest(got, want):
                return True
            self.send_json(401, {"error": "missing or wrong token"},
                           [("WWW-Authenticate", "Bearer")])
            return False

        def do_GET(self):
            if not self.authed():
                return
            u = urllib.parse.urlsplit(self.path)
            q = urllib.parse.parse_qs(u.query)
            parts = [p for p in u.path.split("/") if p]
            if parts == ["api", "health"]:
                return self.send_json(200, {"ok": True, "version": VERSION,
                                            "busy": bool(dep.busy())})
            if parts == ["api", "games"]:
                return self.send_json(200, dep.games())
            if parts == ["api", "jobs"]:
                try:
                    limit = int((q.get("limit") or ["20"])[0])
                except ValueError:
                    limit = 20
                return self.send_json(200, {"jobs": dep.jobs(max(1, min(limit, KEEP_JOBS)))})
            if len(parts) in (3, 4) and parts[:2] == ["api", "jobs"]:
                job = dep.store.load(parts[2])
                if not job:
                    return self.send_json(404, {"error": "no such job"})
                if len(parts) == 3:
                    out = public(job)
                    out["log"] = dep.store.tail(job)
                    out["log_size"] = dep.store.log_size(job)
                    return self.send_json(200, out)
                if parts[3] == "log":
                    try:
                        offset = max(int((q.get("offset") or ["0"])[0]), 0)
                    except ValueError:
                        return self.send_json(400, {"error": "offset must be a number"})
                    data, start = dep.store.read_log(job, offset)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Job-State", job["state"])
                    self.send_header("X-Log-Offset", str(start))
                    self.send_header("X-Log-Size", str(start + len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
            self.send_json(404, {"error": "not found"})

        def do_POST(self):
            if not self.authed():
                return
            if urllib.parse.urlsplit(self.path).path.rstrip("/") != "/api/actions":
                return self.send_json(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:
                return self.send_json(400, {"error": "the body must be a small JSON object"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self.send_json(400, {"error": "the body is not JSON"})
            if not isinstance(body, dict):
                return self.send_json(400, {"error": "the body must be a JSON object"})
            action, game = body.get("action"), body.get("game")
            if action not in ACTIONS:
                return self.send_json(400, {"error": "unknown action %r; one of %s"
                                            % (action, ", ".join(ACTIONS))})
            catalog = load_catalog(cfg.core)
            if not isinstance(game, str) or game not in catalog:
                known = ", ".join(k for k, e in catalog.items() if e["kind"] == "game")
                return self.send_json(400, {"error": "unknown game %r (known: core, %s)"
                                            % (game, known)})
            if catalog[game]["kind"] == "core" and action not in CORE_ACTIONS:
                return self.send_json(400, {"error": "%s is for games; the core takes %s"
                                            % (action, ", ".join(CORE_ACTIONS))})
            code, obj = dep.start(action, game)
            self.send_json(code, obj)

        def do_PUT(self):
            self.send_json(405, {"error": "method not allowed"})

        do_DELETE = do_PATCH = do_PUT

    return Handler


def healthcheck(args):
    token = os.environ.get("POL_DEPLOYER_TOKEN", "").strip()
    port = (args.bind.rpartition(":")[2] or "8099")
    req = urllib.request.Request("http://127.0.0.1:%s/api/health" % port,
                                 headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return 0 if json.load(r).get("ok") else 1
    except Exception:
        return 1


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.healthcheck:
        return healthcheck(args)
    cfg = make_config(args)
    dep = Deployer(cfg)
    srv = ThreadingHTTPServer((cfg.host, cfg.port), make_handler(dep))
    srv.daemon_threads = True

    def stop(*_):
        threading.Thread(target=srv.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    say("version %s listening on %s:%d; checkouts in %s, core %s%s"
        % (VERSION, cfg.host, srv.server_address[1], cfg.root, cfg.core,
           ", published images" if cfg.ghcr else ""))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
