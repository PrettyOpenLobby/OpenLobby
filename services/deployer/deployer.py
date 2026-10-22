#!/usr/bin/env python3
"""The deployer: installs, enables, updates and restarts games for the admin
panel, by running tools/stack.py, and builds a game's client data from a
client folder or an uploaded archive. docs/deployer.md describes the HTTP
API and how to run it.

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
    POL_DEPLOYER_STATE   where jobs, logs and uploads are kept (default /deployer)
    POL_DEPLOY_CLIENTS   absolute path of a folder holding client installs, for
                         the folder browser (unset: browsing is off)
    POL_DEPLOY_UPLOAD_MAX  the most an upload may hold, unpacked (default 8G)
    POL_DEPLOY_GITHUB_API  the GitHub API the update check asks (default
                         https://api.github.com; tests point it at a stand-in)

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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clientdata  # noqa: E402
import updatecheck  # noqa: E402

VERSION = "2.0"

ACTIONS = ("install", "enable", "disable", "update", "restart", "check", "import")
CORE_ACTIONS = ("update", "restart", "check")

KEEP_JOBS = 50
LOG_CAP = 1 << 20          # a job log never grows past this on disk
LOG_KEEP = 768 << 10       # what is left of it after a trim (the tail)
TAIL = 64 << 10            # the log tail GET /api/jobs/<id> returns
MAX_BODY = 4096
GAMES_TTL = 5.0            # seconds GET /api/games reuses its last answer
UPLOAD_MAX = 8 << 30       # default POL_DEPLOY_UPLOAD_MAX
IMPORT_TIMEOUT = 6 * 3600  # an import tool is stopped after this long
NOBODY = 65534             # the uid and gid an import runs as when the checkout is root's
UPDATE_PERIOD = 6 * 3600   # the update check runs at start and this often
REFRESH_GAP = 60.0         # at most one forced update check this often
GIT_TIMEOUT = 60           # a git fetch for the update check is stopped after this long

JOB_ID = re.compile(r"^[0-9]{8}T[0-9]{6}-[0-9a-f]{6}$")
ACTIVE = ("queued", "running")


def say(msg):
    print("deployer: " + msg, file=sys.stderr, flush=True)


def now_iso(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if t is None else t))


def truthy(v):
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


# ------------------------------------------------------------- settings

def parse_size(text):
    """'8G', '512M', '1048576' -> bytes; ValueError otherwise."""
    t = str(text).strip().upper()
    for suffix in ("IB", "B"):
        if t.endswith(suffix) and len(t) > len(suffix):
            t = t[:-len(suffix)]
            break
    mult = 1
    if t and t[-1] in "KMGT":
        mult = 1 << (10 * ("KMGT".index(t[-1]) + 1))
        t = t[:-1]
    n = int(float(t) * mult)
    if n <= 0:
        raise ValueError(text)
    return n


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
        self.clients = None          # realpath of POL_DEPLOY_CLIENTS; None: browsing is off
        self.clients_note = "POL_DEPLOY_CLIENTS is not set, so browsing is off"
        self.upload_max = UPLOAD_MAX
        self.max_files = clientdata.MAX_FILES
        self.docker = ["docker"]     # the docker CLI, as an argv prefix
        self.github_api = updatecheck.API
        self.refresh_gap = REFRESH_GAP


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
    ap.add_argument("--clients", default=env.get("POL_DEPLOY_CLIENTS", ""),
                    help="folder of client installs the panel may browse (POL_DEPLOY_CLIENTS)")
    ap.add_argument("--upload-max", default=env.get("POL_DEPLOY_UPLOAD_MAX", ""),
                    help="the most an upload may hold unpacked, e.g. 8G (POL_DEPLOY_UPLOAD_MAX)")
    ap.add_argument("--max-files", type=int, default=clientdata.MAX_FILES,
                    help="the most files an upload may hold (tests lower it)")
    ap.add_argument("--stack", default="",
                    help="the stack.py to run (default CORE/tools/stack.py; tests point "
                         "this at a stand-in)")
    ap.add_argument("--refresh-gap", type=float, default=REFRESH_GAP,
                    help="seconds between two forced update checks (tests lower it)")
    ap.add_argument("--docker", default="",
                    help="the docker CLI the update check runs (default docker; tests point "
                         "this at a stand-in .py)")
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
    if args.upload_max.strip():
        try:
            cfg.upload_max = parse_size(args.upload_max)
        except ValueError:
            say("POL_DEPLOY_UPLOAD_MAX must be a size such as 8G or 500M (got %r)" % args.upload_max)
            sys.exit(2)
    cfg.max_files = max(1, args.max_files)
    cfg.refresh_gap = max(0.0, args.refresh_gap)
    if args.docker:
        cfg.docker = ([cfg.python] if args.docker.endswith(".py") else []) + [args.docker]
    api = os.environ.get("POL_DEPLOY_GITHUB_API", "").strip()
    if api:
        if not api.startswith(("http://", "https://")):
            say("POL_DEPLOY_GITHUB_API must be an http(s) URL (got %r)" % api)
            sys.exit(2)
        cfg.github_api = api.rstrip("/")
    clients = args.clients.strip()
    if clients:
        if not os.path.isabs(clients):
            cfg.clients_note = ("POL_DEPLOY_CLIENTS must be an absolute path (got %r), so "
                                "browsing is off" % clients)
        elif not os.path.isdir(clients):
            cfg.clients_note = ("POL_DEPLOY_CLIENTS (%s) is not a folder inside the container; "
                                "it must be mounted at the same path. Browsing is off" % clients)
        else:
            cfg.clients = os.path.realpath(clients)
            cfg.clients_note = ""
    if cfg.clients_note:
        say(cfg.clients_note)
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


def docker_sockets():
    """Unix sockets a Docker client could use from here."""
    out = ["/var/run/docker.sock", "/run/docker.sock"]
    host = os.environ.get("DOCKER_HOST", "")
    if host.startswith("unix://"):
        out.append(host[len("unix://"):])
    return [p for i, p in enumerate(out) if p not in out[:i]]


def socket_open_to(uid, gids, path):
    """Whether uid (with the groups gids) may connect to the socket at path.
    Connecting to a unix socket needs write permission on it."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    if uid == 0:
        return True
    mode = st.st_mode
    if st.st_uid == uid:
        return bool(mode & 0o200)
    if st.st_gid in gids:
        return bool(mode & 0o020)
    return bool(mode & 0o002)


def import_identity(checkout):
    """(subprocess kwargs, uid, gids) for an import tool: the checkout's
    owner, or nobody when the checkout belongs to root; never root itself.
    uid is None where there are no uids (Windows)."""
    if not hasattr(os, "getuid"):
        return {}, None, set()
    if os.getuid() != 0:
        return {}, os.getuid(), set(os.getgroups()) | {os.getgid()}
    st = os.stat(checkout)
    uid, gid = st.st_uid, st.st_gid
    if uid == 0:
        uid = NOBODY
    if gid == 0:
        gid = NOBODY
    return {"user": uid, "group": gid, "extra_groups": []}, uid, {gid}


def import_env(home):
    """The whole environment an import tool gets. Nothing of the deployer's
    own is passed on: no token, no DOCKER_*, no POL_*."""
    env = {"HOME": home, "TMPDIR": home, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
           "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"}
    if os.name == "nt":
        for k in ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PATHEXT", "COMSPEC"):
            if os.environ.get(k):
                env[k] = os.environ[k]
        env["TEMP"] = env["TMP"] = home
    else:
        env["PATH"] = "/usr/local/bin:/usr/bin:/bin"
    return env


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

    def create(self, action, game, extra=None):
        t = time.time()
        jid = time.strftime("%Y%m%dT%H%M%S", time.gmtime(t)) + "-" + secrets.token_hex(3)
        job = {"id": jid, "action": action, "game": game, "state": "queued",
               "created": now_iso(t), "started": None, "ended": None, "exit": None,
               "summary": "", "missing": [], "log_dropped": 0, "_t": t}
        job.update(extra or {})
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
    if job.get("action") == "import":
        out["item"] = job.get("item")
        out["source"] = job.get("source")
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

    def run(self, argv, owner=None, cwd=None, env=None, kw=None, timeout=None):
        """Run argv with its output streamed into the log; the exit code.
        kw and env, when given, replace the owner's identity and the usual
        environment (the import tools get both from the caller)."""
        if kw is None:
            kw = owner_kwargs(owner) if owner else {}
        if env is None:
            env = self.child_env(bool(kw))
        self.log.line("$ " + self.show(argv))
        if timeout and os.name != "nt":
            kw = dict(kw, start_new_session=True)
        try:
            p = subprocess.Popen(argv, cwd=cwd or self.cfg.core, env=env,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, **kw)
        except FileNotFoundError:
            raise JobFailed("%s is not installed in the deployer image" % os.path.basename(argv[0]))
        timer = None
        if timeout:
            def stop():
                self.log.line("(stopped after %d seconds)" % timeout)
                try:
                    if os.name != "nt":
                        os.killpg(p.pid, signal.SIGKILL)
                    else:
                        p.kill()
                except OSError:
                    pass
            timer = threading.Timer(timeout, stop)
            timer.daemon = True
            timer.start()
        for chunk in iter(p.stdout.readline, b""):
            self.log.write(chunk)
        p.stdout.close()
        rc = p.wait()
        if timer:
            timer.cancel()
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


def missing_entry(catalog, key, m, required=False):
    """One "missing" row as the API shows it: the path, the hint, the
    catalog data item it belongs to, and, when that item can be imported,
    what the import wants (its hint and expects; never its argv)."""
    out = {"path": m["path"], "hint": m["hint"], "item": m.get("id")}
    if required:
        out["required"] = bool(m.get("required"))
    _, _, imp = clientdata.find_import(catalog, key, m.get("id"))
    if imp:
        out["import"] = clientdata.public_import(imp)
    return out


def evaluate(catalog, report, done):
    """(state, summary, missing) from a check report. `done` is the summary
    when nothing is wrong."""
    for g in report.get("games", []):
        req = [m for m in g.get("missing", []) if m.get("required")]
        if req:
            return "needs_data", needs_sentence(g["name"], req), \
                [missing_entry(catalog, g.get("key"), m) for m in req]
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
    return evaluate(r.catalog, r.check_report([key]), "%s is installed; enable it to start it." % r.name(key))


def act_enable(r, key):
    if not os.path.isdir(r.checkout(key)):
        raise JobFailed("%s is not installed; install it first." % r.name(key))
    state, summary, missing = evaluate(r.catalog, r.check_report([key]), "")
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
            return evaluate(r.catalog, r.check_report([key]),
                            "%s is updated; it is not enabled, so nothing was restarted." % r.name(key))
        state, summary, missing = evaluate(r.catalog, r.check_report([key]), "")
        if state != "ok":
            return state, summary + (". It was updated but not restarted." if state == "needs_data" else ""), missing
    if r.cfg.ghcr:
        r.compose(["pull", "--ignore-buildable", "--ignore-pull-failures"], prebuild=False)
    r.up()
    if key == "core":
        return evaluate(r.catalog, r.check_report([]), "%s is updated and restarted." % r.name(key))
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
    return evaluate(r.catalog, report, "Everything %s needs is in place." % what)


def act_import(r, key, job):
    """Run the item's import tool on the folder the request named, as the
    checkout's owner with a bare environment, then check the game."""
    e, item, imp = clientdata.find_import(r.catalog, key, job["item"])
    if not imp:
        raise JobFailed("%s's %s cannot be imported from the panel." % (r.name(key), job["item"]))
    what = imp["label"]
    folder = job["_input"]
    if not os.path.isdir(folder):
        raise JobFailed("The folder to import from is gone; upload or choose it again.")
    gone = clientdata.missing_expects(folder, job["_input_root"], imp)
    if gone:
        raise JobFailed("The folder to import from no longer holds %s." % ", ".join(gone))
    d = r.checkout(key)
    if not os.path.isdir(d):
        raise JobFailed("%s is not installed; install it first." % r.name(key))
    tool = os.path.join(d, *imp["argv"][1].split("/"))
    if not os.path.isfile(tool):
        raise JobFailed("%s has no %s; update it, then import again." % (r.name(key), imp["argv"][1]))
    kw, uid, gids = import_identity(d)
    if uid is not None:
        if uid == 0:
            raise JobFailed("The import would run as root; the deployer refuses that.")
        for sock in docker_sockets():
            if socket_open_to(uid, gids, sock):
                raise JobFailed("The import tool would run as uid %d, which may use the Docker "
                                "socket %s, so it was not started. Give the checkout to a user "
                                "outside the docker group (docs/deployer.md)." % (uid, sock))
    home = os.path.join(r.cfg.state, "imports", job["id"])
    os.makedirs(home, exist_ok=True)
    if kw:
        os.chown(home, kw["user"], kw["group"])
    argv = [r.cfg.python, tool] + [a.replace("{input}", folder) for a in imp["argv"][2:]]
    try:
        rc = r.run(argv, cwd=d, env=import_env(home), kw=kw, timeout=IMPORT_TIMEOUT)
    finally:
        shutil.rmtree(home, ignore_errors=True)
    tool_name = imp["argv"][1]
    report = r.check_report([key])
    g = next((x for x in report.get("games", []) if x.get("key") == key), {})
    still = [m for m in g.get("missing", []) if m.get("id") == item["id"]]
    if still:
        if rc != 0:
            raise JobFailed("%s failed (exit %d) and %s is still missing; the log has its "
                            "output." % (tool_name, rc, still[0]["path"]), rc)
        return "needs_data", "%s ran, but %s is still missing; check that the folder is %s." % (
            tool_name, still[0]["path"], imp["hint"]), \
            [missing_entry(r.catalog, key, m) for m in still]
    if rc != 0 and not item.get("paths"):
        raise JobFailed("%s failed (exit %d); the log has its output." % (tool_name, rc), rc)
    state, summary, missing = evaluate(r.catalog, report, "")
    if state == "needs_data":
        return state, "Imported the %s. %s" % (what, summary), missing
    note = "" if rc == 0 else " %s exited %d, so some optional files may be missing; the log " \
                              "has its messages." % (tool_name, rc)
    if key in r.enabled():
        then = "Run enable again to rebuild and restart it with the new data."
    else:
        then = "Enable it to start it."
    return "ok", "Imported %s's %s. %s%s" % (r.name(key), what, then, note), []


ACT = {"install": act_install, "enable": act_enable, "disable": act_disable,
       "update": act_update, "restart": act_restart, "check": act_check}


# --------------------------------------------------------------- updates

class Updates:
    """GET /api/updates: whether the core and each installed game have a
    newer version than the one this host runs (docs/deployer.md, "Updates").
    Checked at start, every UPDATE_PERIOD, after an install or update job,
    and on a forced refresh (at most one every REFRESH_GAP seconds, or
    --refresh-gap). The results and GitHub's answers are kept in the state
    volume. A check never waits for a job and a job never waits for a
    check; a checkout a job is working in is not fetched."""

    def __init__(self, dep):
        self.dep, self.cfg = dep, dep.cfg
        self.path = os.path.join(self.cfg.state, "updates.json")
        self.gh = updatecheck.GitHub(api=self.cfg.github_api,
                                     cache_path=os.path.join(self.cfg.state, "github-cache.json"))
        self.lock = threading.Lock()        # the results below
        self.run_lock = threading.Lock()    # one check at a time
        self.items, self.checked_at, self.running = {}, None, 0
        self.last_forced = 0.0
        try:
            with open(self.path, encoding="utf-8") as f:
                got = json.load(f)
            if isinstance(got, dict) and isinstance(got.get("items"), dict):
                self.items, self.checked_at = got["items"], got.get("checked_at")
        except (OSError, ValueError):
            pass

    def save(self):
        with self.lock:
            snap = {"checked_at": self.checked_at, "items": dict(self.items)}
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snap, f)
            os.replace(tmp, self.path)
        except OSError as exc:
            say("cannot keep the update check's results: %s" % exc)

    def start(self):
        with self.lock:
            self.running += 1
        threading.Thread(target=self._loop, daemon=True, name="updates").start()

    def _loop(self):
        counted = True
        while True:
            self.check(counted=counted)
            counted = False
            time.sleep(UPDATE_PERIOD)

    def kick(self, keys=None, force=False, fetch=True):
        """Check in the background; the answer says "checking" until done."""
        with self.lock:
            self.running += 1
        threading.Thread(target=self.check, args=(keys, force, fetch, True), daemon=True,
                         name="updates-now").start()

    def installed(self, catalog):
        keys = ["core"] if "core" in catalog else []
        for key, e in sorted(catalog.items(), key=lambda kv: (kv[1].get("order", 0), kv[0])):
            if e["kind"] == "game" and os.path.isdir(os.path.join(self.cfg.root, e["checkout"])):
                keys.append(key)
        return keys

    def env_values(self, catalog, keys):
        """What compose sees for the image tags: the core's .env, then each
        installed game's, then the deployer's own environment."""
        vals = read_dotenv(os.path.join(self.cfg.core, ".env"))
        for key in keys:
            if key != "core":
                vals.update(read_dotenv(os.path.join(self.cfg.root, catalog[key]["checkout"], ".env")))
        vals.update(os.environ)
        return vals

    def check(self, keys=None, force=False, fetch=True, counted=False):
        if not counted:
            with self.lock:
                self.running += 1
        try:
            with self.run_lock:
                catalog = load_catalog(self.cfg.core)
                have = self.installed(catalog)
                vals = self.env_values(catalog, have)
                for key in have if keys is None else [k for k in have if k in keys]:
                    try:
                        it = self.check_one(key, catalog[key], vals, force, fetch)
                    except Exception as exc:  # one item's fault is its own
                        say("update check of %s: %s" % (key, traceback.format_exc()))
                        it = updatecheck.unknown(updatecheck.item(key, catalog[key]["name"]),
                                                 "The check failed: %s." % exc)
                    with self.lock:
                        self.items[key] = it
                with self.lock:
                    self.checked_at = now_iso()
                    for gone in set(self.items) - set(have):
                        del self.items[gone]
                self.save()
                self.gh.save()
        finally:
            with self.lock:
                self.running -= 1

    def answer(self, refresh=False):
        limited = False
        if refresh:
            now = time.time()
            with self.lock:
                limited = now - self.last_forced < self.cfg.refresh_gap
                if not limited:
                    self.last_forced = now
            if not limited:
                self.kick(force=True)
        catalog = load_catalog(self.cfg.core)
        with self.lock:
            items, checked, busy = dict(self.items), self.checked_at, self.running > 0
            wait = max(0, int(self.last_forced + self.cfg.refresh_gap - time.time() + 0.999))
        out = []
        for key in self.installed(catalog):
            it = items.get(key)
            if it is None:
                it = updatecheck.unknown(updatecheck.item(key, catalog[key]["name"]),
                                         "It has not been checked yet." if not busy
                                         else "The check is still running.")
                if key != "core":
                    it["module"] = catalog[key].get("admin_module")
            out.append(it)
        ans = {"checked_at": checked, "checking": busy, "items": out}
        if limited:
            ans["limited"] = True
            ans["retry_after"] = wait
        return ans

    # ---- one item

    def job_on(self, key):
        return self.dep.current is not None and self.dep.current_game == key

    def check_one(self, key, e, vals, force, fetch):
        it = updatecheck.item(key, e["name"])
        if e["kind"] == "game":
            it["module"] = e.get("admin_module")
        repo, branch = updatecheck.repo_of(e, "PrettyOpenLobby/OpenLobby" if key == "core" else None)
        img = (e.get("images") or [{}])[0]
        if self.cfg.ghcr and img.get("ghcr"):
            tag = (vals.get(img.get("tag_var") or "") or "latest").strip()
            ref = "%s:%s" % (img["ghcr"], tag)
            rev, built, why = self.image_revision(ref, key == "core")
            it["running"] = {"commit": rev, "built_at": built}
            updatecheck.check_github(it, self.gh, repo, branch, force=force,
                                     pin=None if tag == "latest" else tag,
                                     pin_name=img.get("tag_var") or "", no_revision=why)
            how = "Update it from Settings > Install games; the deployer pulls the published images and restarts it."
        else:
            d = self.cfg.core if e["kind"] == "core" else os.path.join(self.cfg.root, e["checkout"])
            self.check_checkout(it, d, branch, fetch and not self.job_on(key))
            how = "Update it from Settings > Install games; the deployer pulls its checkout (git pull --ff-only) and rebuilds."
        if it["update"] == "available":
            it["how"] = how
        return it

    def image_revision(self, ref, core):
        """(commit, built_at, why it is unknown) of the image `ref` names on
        this host: its org.opencontainers.image.revision label, or for the
        core the OPENLOBBY_REVISION its build left in the environment."""
        env = {k: v for k, v in os.environ.items()
               if k != "POL_DEPLOYER_TOKEN" and v != self.cfg.token}
        try:
            r = subprocess.run(self.cfg.docker + ["image", "inspect", ref], env=env,
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
            doc = json.loads(r.stdout.decode("utf-8", "replace")) if r.returncode == 0 else None
        except (OSError, ValueError, subprocess.TimeoutExpired):
            doc = None
        if not isinstance(doc, list) or not doc or not isinstance(doc[0], dict):
            return None, None, ("The image %s is not on this host, so the version that runs "
                                "is unknown." % ref)
        conf = doc[0].get("Config") if isinstance(doc[0].get("Config"), dict) else {}
        labels = conf.get("Labels") if isinstance(conf.get("Labels"), dict) else {}
        envs = dict(x.split("=", 1) for x in conf.get("Env") or [] if isinstance(x, str) and "=" in x)
        rev = labels.get("org.opencontainers.image.revision") or \
            (envs.get("OPENLOBBY_REVISION") if core else "") or ""
        built = labels.get("org.opencontainers.image.created") or \
            (envs.get("OPENLOBBY_BUILT_AT") if core else "") or doc[0].get("Created")
        why = None
        if not updatecheck.commit_id(rev):
            why = ("The image %s does not say which commit it was built from, so it cannot be "
                   "compared." % ref)
        return updatecheck.commit_id(rev), updatecheck.to_iso(built), why

    def git(self, d, args, timeout=30):
        """(exit code, stdout, last line of stderr) of git in checkout d, as
        its owner, never asking for a password."""
        kw = owner_kwargs(d)
        env = {k: v for k, v in os.environ.items()
               if k != "POL_DEPLOYER_TOKEN" and v != self.cfg.token}
        env.update(GIT_TERMINAL_PROMPT="0", LC_ALL="C")
        if kw:
            env["HOME"] = "/tmp"
        try:
            r = subprocess.run(["git", "-C", d] + list(args), env=env, stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=timeout, **kw)
        except FileNotFoundError:
            return 127, "", "git is not installed in the deployer image"
        except subprocess.TimeoutExpired:
            return 124, "", "it did not finish within %d seconds" % timeout
        err = [l for l in r.stderr.decode("utf-8", "replace").splitlines() if l.strip()]
        return r.returncode, r.stdout.decode("utf-8", "replace").strip(), (err[-1].strip() if err else "")

    def check_checkout(self, it, d, branch, fetch):
        rel = os.path.relpath(d, self.cfg.core).replace(os.sep, "/")
        rc, head, _ = self.git(d, ["rev-parse", "HEAD"])
        if rc != 0 or not updatecheck.commit_id(head):
            return updatecheck.unknown(it, "%s is not a git checkout, so its version is unknown." % rel)
        it["running"]["commit"] = head.lower()
        if fetch:
            rc, _, err = self.git(d, ["fetch", "--quiet"], timeout=GIT_TIMEOUT)
            if rc != 0:
                return updatecheck.unknown(it, "git fetch failed in %s (%s), so newer commits are "
                                               "unknown." % (rel, err[:160] or "exit %d" % rc))
        rc, up, _ = self.git(d, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
        if rc != 0 or not up:
            up = "origin/" + branch
            if self.git(d, ["rev-parse", "--verify", "--quiet", up + "^{commit}"])[0] != 0:
                return updatecheck.unknown(it, "%s is not on a branch that follows a remote one, so "
                                               "there is nothing to compare it with." % rel)
        rc, out, _ = self.git(d, ["log", "-1", "--format=%H%x1f%ct", up])
        parts = out.split("\x1f")
        if rc != 0 or len(parts) != 2 or not parts[1].isdigit():
            return updatecheck.unknown(it, "git could not read %s in %s." % (up, rel))
        it["latest"] = {"commit": parts[0].lower(), "date": now_iso(int(parts[1]))}
        rc, counts, _ = self.git(d, ["rev-list", "--left-right", "--count", "HEAD..." + up])
        try:
            mine, behind = (int(x) for x in counts.split())
        except ValueError:
            return updatecheck.unknown(it, "git could not compare %s with %s." % (rel, up))
        rc, log, _ = self.git(d, ["log", "--no-merges", "-n", str(updatecheck.MAX_CHANGES),
                                  "--format=%H%x1f%ct%x1f%s", "HEAD.." + up])
        changes = []
        for line in log.splitlines() if rc == 0 else []:
            p = line.split("\x1f", 2)
            if len(p) == 3 and p[1].isdigit() and updatecheck.commit_id(p[0]):
                changes.append({"commit": p[0].lower(), "date": now_iso(int(p[1])),
                                "subject": updatecheck.subject(p[2])})
        it.update(behind=behind, changes=changes, update="available" if behind else "current",
                  why=None)
        if mine:
            it["why"] = ("The checkout has %d commit%s of its own that %s does not, so update "
                         "(git pull --ff-only) %s." % (mine, "" if mine == 1 else "s", up,
                                                       "will refuse" if behind else "has nothing to do"))
        return it


# ---------------------------------------------------------------- server

class Deployer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.store = Store(cfg.state)
        self.store.recover()
        self.store.prune()
        self.lock = threading.Lock()
        self.current = None
        self.current_game = None
        self.current_upload = None    # the upload the running import reads
        self.games_cache = (0.0, None)
        self.uploads = clientdata.Uploads(cfg.state)
        shutil.rmtree(os.path.join(cfg.state, "imports"), ignore_errors=True)
        for uid in self.uploads.expire():
            say("upload %s expired and was removed" % uid)
        self.updates = Updates(self)

    def busy(self):
        return self.current

    def expire_uploads(self):
        self.uploads.expire(keep=[self.current_upload] if self.current_upload else [])

    def start(self, action, game, extra=None):
        """(202, {"job"}) or (409, {"job": running id})."""
        upload = ((extra or {}).get("source") or {}).get("upload")
        with self.lock:
            if self.current:
                return 409, {"job": self.current, "error": "another job is running"}
            job = self.store.create(action, game, extra)
            self.current = job["id"]
            self.current_game = game
            self.current_upload = upload
        if upload:
            self.uploads.touch(upload, now_iso())
        self.store.prune()
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return 202, {"job": job["id"]}

    def import_request(self, body, catalog):
        """(code, answer) for POST /api/actions with action import: 202 with
        the job, or the 400/409 the request earns."""
        game, item_id, src = body.get("game"), body.get("item"), body.get("source")
        e, item, imp = clientdata.find_import(catalog, game, item_id)
        if item is None:
            known = ", ".join(i["id"] for i in e.get("data", [])) or "none"
            return 400, {"error": "%s has no data item %r (its items: %s)" % (e["name"], item_id, known)}
        if imp is None:
            return 400, {"error": "%s's %s cannot be imported from the panel; %s"
                         % (e["name"], item_id, item.get("tool", "see its README"))}
        if not isinstance(src, dict) or len(set(src) & {"upload", "path"}) != 1 \
                or set(src) - {"upload", "path", "subdir"} \
                or ("subdir" in src and "upload" not in src):
            return 400, {"error": "source must be {\"upload\": id, \"subdir\": folder} or "
                                  "{\"path\": folder under POL_DEPLOY_CLIENTS}"}
        try:
            if "upload" in src:
                meta = self.uploads.load(src["upload"]) if isinstance(src["upload"], str) else None
                if not meta:
                    return 400, {"error": "there is no upload %r; it may have expired"
                                 % (src["upload"],)}
                root = os.path.realpath(self.uploads.tree(meta["id"]))
                folder, rel = clientdata.resolve_under(root, src.get("subdir") or "")
                shown = {"upload": meta["id"], "subdir": rel}
            else:
                if not self.cfg.clients:
                    return 409, {"error": self.cfg.clients_note}
                root = self.cfg.clients
                folder, rel = clientdata.resolve_under(root, src["path"])
                shown = {"path": rel}
        except clientdata.PathError as exc:
            return 400, {"error": "the folder to import from is refused: %s" % exc}
        if not os.path.isdir(folder):
            return 400, {"error": "%s is not a folder" % (rel or "the top")}
        gone = clientdata.missing_expects(folder, root, imp)
        if gone:
            return 400, {"error": "This folder is not %s: it has no %s."
                                  % (imp["hint"], " or ".join(gone)),
                         "missing": [{"path": p, "hint": imp["hint"]} for p in gone]}
        return self.start("import", game, {"item": item["id"], "source": shown,
                                           "_input": folder, "_input_root": root})

    def _run(self, job):
        log = JobLog(self.store, job, self.cfg.token)
        r = Runner(self.cfg, load_catalog(self.cfg.core), log)
        try:
            job["state"] = "running"
            job["started"] = now_iso()
            self.store.save(job)
            log.line("deployer %s: %s %s%s" % (VERSION, job["action"], job["game"],
                                                " (published images)" if self.cfg.ghcr else ""))
            if job["action"] == "import":
                log.line("importing %s from %s" % (job["item"], json.dumps(job["source"])))
                state, summary, missing = act_import(r, job["game"], job)
            else:
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
        upload = (job.get("source") or {}).get("upload")
        if upload:
            self.uploads.touch(upload, job["ended"])
        # what runs now changed: check that item again (its checkout was just fetched),
        # counted as running before anyone can see the job finished
        if job["action"] in ("install", "update") and state in ("ok", "needs_data"):
            self.updates.kick(keys=[job["game"]], fetch=False)
        # one step, so whoever sees the job finished can start the next one
        with self.lock:
            try:
                self.store.save(job)
            finally:
                self.current = None
                self.current_game = None
                self.current_upload = None
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
                "missing": [missing_entry(catalog, key, m, required=True)
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
        timeout = 300      # seconds a request (an upload included) may sit silent

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
            if parts == ["api", "updates"]:
                return self.send_json(200, dep.updates.answer(
                    truthy((q.get("refresh") or [""])[0])))
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
            if parts == ["api", "browse"]:
                if not cfg.clients:
                    return self.send_json(409, {"error": cfg.clients_note})
                rel = (q.get("path") or [""])[0]
                try:
                    out = clientdata.browse(cfg.clients, rel, load_catalog(cfg.core))
                except clientdata.PathError as exc:
                    return self.send_json(400, {"error": "%s: %s" % (rel, exc)})
                except (FileNotFoundError, NotADirectoryError):
                    return self.send_json(404, {"error": "no folder %s under the clients folder"
                                                % rel})
                except PermissionError:
                    return self.send_json(403, {"error": "the deployer may not read %s" % rel})
                return self.send_json(200, out)
            if parts == ["api", "uploads"]:
                dep.expire_uploads()
                return self.send_json(200, {"uploads": [clientdata.public_upload(m)
                                                        for m in dep.uploads.all()]})
            self.send_json(404, {"error": "not found"})

        def refuse_upload(self, code, obj):
            # the body was not read, so this connection cannot carry another request
            self.close_connection = True
            self.send_json(code, obj, [("Connection", "close")])

        def upload(self):
            if self.headers.get("Transfer-Encoding"):
                return self.refuse_upload(411, {"error": "send the archive with a Content-Length; "
                                                         "chunked uploads are not accepted"})
            raw = self.headers.get("Content-Length")
            if raw is None:
                return self.refuse_upload(411, {"error": "Content-Length is required"})
            try:
                n = int(raw)
            except ValueError:
                return self.refuse_upload(400, {"error": "Content-Length is not a number"})
            if n <= 0:
                return self.refuse_upload(400, {"error": "the upload is empty"})
            if n > cfg.upload_max:
                return self.refuse_upload(413, {"error": "The archive is larger than the %d bytes "
                                                         "POL_DEPLOY_UPLOAD_MAX allows."
                                                         % cfg.upload_max,
                                                "max": cfg.upload_max})
            dep.expire_uploads()
            ups = dep.uploads
            try:
                free = shutil.disk_usage(ups.dir).free
            except OSError:
                free = None
            if free is not None and free < 2 * n:
                return self.refuse_upload(507, {"error": "The deployer's volume has %d bytes free, "
                                                         "too little for this archive." % free})
            uid = ups.new_id()
            part = os.path.join(ups.incoming, uid + ".part")
            tmp = ups.tree(uid) + ".tmp"
            try:
                got = 0
                with open(part, "wb") as f:
                    while got < n:
                        try:
                            chunk = self.rfile.read(min(clientdata.CHUNK, n - got))
                        except OSError:       # the timeout below, or the client went away
                            chunk = b""
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                if got < n:
                    self.close_connection = True
                    return self.send_json(400, {"error": "the upload ended after %d of %d bytes"
                                                % (got, n)})
                try:
                    size, files = clientdata.extract(part, tmp, cfg.upload_max, cfg.max_files)
                except clientdata.ArchiveError as exc:
                    obj = {"error": exc.message}
                    if exc.code == 413:
                        obj["max"] = cfg.upload_max
                    return self.send_json(exc.code, obj)
                os.replace(tmp, ups.tree(uid))
                os.chmod(ups.tree(uid), 0o755)
                matches = clientdata.find_matches(os.path.realpath(ups.tree(uid)),
                                                  load_catalog(cfg.core))
                t = time.time()
                meta = {"id": uid, "name": clientdata.clean_name(self.headers.get("X-Filename")),
                        "created": now_iso(t), "last_import": None, "size": size,
                        "files": files, "archive_size": n, "matches": matches,
                        "_t": t, "_last": None}
                ups.save(meta)
                out = clientdata.public_upload(meta)
                out["upload"] = uid
                say("upload %s: %d files, %d bytes" % (uid, files, size))
                return self.send_json(201, out)
            finally:
                try:
                    os.remove(part)
                except OSError:
                    pass
                shutil.rmtree(tmp, ignore_errors=True)

        def do_POST(self):
            if not self.authed():
                self.close_connection = True
                return
            path = urllib.parse.urlsplit(self.path).path.rstrip("/")
            if path == "/api/uploads":
                return self.upload()
            if path != "/api/actions":
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
            if action == "import":
                code, obj = dep.import_request(body, catalog)
            else:
                code, obj = dep.start(action, game)
            self.send_json(code, obj)

        def do_DELETE(self):
            if not self.authed():
                return
            parts = [p for p in urllib.parse.urlsplit(self.path).path.split("/") if p]
            if len(parts) != 3 or parts[:2] != ["api", "uploads"]:
                return self.send_json(404, {"error": "not found"})
            uid = parts[2]
            if not dep.uploads.load(uid):
                return self.send_json(404, {"error": "no such upload"})
            with dep.lock:
                if dep.current_upload == uid:
                    return self.send_json(409, {"error": "an import is reading this upload",
                                                "job": dep.current})
                dep.uploads.remove(uid)
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_PUT(self):
            self.send_json(405, {"error": "method not allowed"})

        do_PATCH = do_PUT

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
    dep.updates.start()

    def stop(*_):
        threading.Thread(target=srv.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    say("version %s listening on %s:%d; checkouts in %s, core %s%s%s"
        % (VERSION, cfg.host, srv.server_address[1], cfg.root, cfg.core,
           ", published images" if cfg.ghcr else "",
           ", client folders in %s" % cfg.clients if cfg.clients else ""))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
