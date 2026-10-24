#!/usr/bin/env python3
"""The deployer: installs, enables, updates and restarts games for the admin
panel, by running tools/stack.py, builds a game's client data from a client
folder or an uploaded archive, and installs, rolls back and removes the
patch service's bundles from .polpatch files. docs/deployer.md describes
the HTTP API and how to run it.

    python deployer.py                 serve (settings from the environment)
    python deployer.py --healthcheck   exit 0 when the local server answers (and
                                       Docker sees the checkouts where it does)
    python deployer.py --channel dev   switch the running deployer to the dev channel,
                                       the access code read from stdin (setup scripts);
                                       --registry HOST/PATH only for a plain token
    python deployer.py --channel stable

It is the only container that holds the Docker socket, so it accepts
nothing but a fixed set of actions on game keys from the catalog, each run
as a fixed argument list (never a shell), and only with the bearer token.
The one exception is the updater of a game paired with the panel (POST
/api/updater): a python command the panel's owner approved, run in a
container of this image with the game's folder and the socket (see
docs/deployer.md, "Updaters of paired games").

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
    POL_DEV_REGISTRY     advanced: the registry a plain token (not an olx1. access code,
                         which carries its own) is for, host/path
    POL_DEV_SOURCE       where the dev images' source is when the code does not say
                         (default https://<registry>)
    POL_DEV_USER         the user name a bare access code signs in under (default dev)
    POL_DEV_REGISTRY_HTTP  1: the registry speaks plain http (tests, a registry on the LAN)
    POL_DEV_ISSUER       the panel that redeems short dev codes (ABCD-EFGH-JKMN) for this
                         install, its base URL; empty: only full access codes are taken
    POL_DEPLOY_MIRRORS   the patch service's mirrors folder as mounted here
                         (unset: the patch actions are off)
    POL_DEPLOY_PATCH_HOST  where the patch service answers (default patch)
    POL_VALKEY_URL       where the patch service reports downloads in progress; a patch
                         action waits for them before it restarts the service (unset: no wait)
    POL_DEPLOY_PATCH_DRAIN  the most seconds that wait lasts (default 1800)

Standard library only.
"""
import argparse
import getpass
import glob
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import tempfile
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clientdata  # noqa: E402
import devchannel  # noqa: E402
import gameupdater  # noqa: E402
import updatecheck  # noqa: E402
import patches  # noqa: E402
import patchfeed  # noqa: E402

VERSION = "3.0"

PATCH_ACTIONS = ("patch_import", "patch_build", "patch_remove", "patch_rollback", "patch_check",
                 "patch_fetch")
ACTIONS = ("install", "enable", "disable", "update", "restart", "check", "import") + PATCH_ACTIONS
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
COMPOSING = ("enable", "disable", "update", "restart")   # the actions that run docker compose
PROBE_TIMEOUT = 120        # the start-up check that Docker sees the checkout where we do
UPDATER_IDLE = 15          # minutes a paired game's updater may print nothing before it is stopped
UPDATER_BODY = 16384       # the most a POST /api/updater may hold
UPDATER_LABEL = "org.openlobby.updater.job"   # on an updater's container: its job id
VERIFY_TIMEOUT = 3600      # the quick check of an uploaded .polpatch is stopped after this long
SERVE_WAIT = 90            # seconds the patch service gets to answer again after a restart
FEED_TTL = 300             # seconds GET /api/patches/feed reuses its last listing
PATCH_DRAIN = 1800         # the most seconds a patch action waits for downloads to finish
DRAIN_POLL = 10.0          # how often it asks again while it waits
PATCHTOOL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "patchtool.py")
POLPATCH_UPLOAD = "upload.polpatch"   # the one file a .polpatch upload's folder holds

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
        # whether the Docker daemon sees the core checkout at the path we see it
        # (check_paths): "ok", "wrong" or "unchecked", and the sentence for it
        self.paths = "unchecked"
        self.paths_note = "not checked"
        # the container this deployer runs in (its image runs the updaters of paired games),
        # "" outside a container; and how many seconds an updater's minute is (tests shorten it)
        self.container = ""
        self.minute = 60.0
        # the dev channel (docs/deployer.md, "Dev channel"); the code itself is in
        # STATE/channel, never in the environment
        self.dev_registry = None
        self.dev_source = None
        self.dev_user = devchannel.DEFAULT_USER
        self.dev_scheme = "https"
        self.dev_issuer = None       # POL_DEV_ISSUER as given: where short dev codes are redeemed
                                     # (None: not set; the owner's setting or the default)
        self.mirrors = None          # realpath of POL_DEPLOY_MIRRORS; None: patch actions are off
        self.mirrors_note = "POL_DEPLOY_MIRRORS is not set, so the patch actions are off"
        self.patch_host = "patch"
        self.patch_offset = 0
        self.valkey = None           # (host, port, db) of POL_VALKEY_URL; None: no download wait
        self.kv_prefix = "pol:"
        self.patch_drain = PATCH_DRAIN
        self.drain_poll = DRAIN_POLL


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
    ap.add_argument("--mirrors", default=env.get("POL_DEPLOY_MIRRORS", ""),
                    help="the patch service's mirrors folder as mounted here (POL_DEPLOY_MIRRORS)")
    ap.add_argument("--patch-host", default=env.get("POL_DEPLOY_PATCH_HOST") or "patch",
                    help="host name of the patch service (POL_DEPLOY_PATCH_HOST)")
    ap.add_argument("--valkey", default=env.get("POL_VALKEY_URL", ""),
                    help="Valkey URL the patch service reports downloads to (POL_VALKEY_URL)")
    ap.add_argument("--patch-drain", type=float,
                    default=float(env.get("POL_DEPLOY_PATCH_DRAIN") or PATCH_DRAIN),
                    help="the most seconds to wait for downloads before restarting the "
                         "patch service (POL_DEPLOY_PATCH_DRAIN)")
    ap.add_argument("--drain-poll", type=float, default=DRAIN_POLL,
                    help="seconds between two download counts while waiting (tests lower it)")
    ap.add_argument("--patch-port-offset", type=int, default=0,
                    help="added to the patch service's ports (tests run it with --bind-offset)")
    ap.add_argument("--stack", default="",
                    help="the stack.py to run (default CORE/tools/stack.py; tests point "
                         "this at a stand-in)")
    ap.add_argument("--refresh-gap", type=float, default=REFRESH_GAP,
                    help="seconds between two forced update checks (tests lower it)")
    ap.add_argument("--docker", default="",
                    help="the docker CLI the update check runs (default docker; tests point "
                         "this at a stand-in .py)")
    ap.add_argument("--container", default="",
                    help="the container to check the checkout paths from (default: this one, "
                         "when running in a container; tests name one their docker stand-in knows)")
    ap.add_argument("--minute", type=float, default=60.0,
                    help="seconds in a minute of a game updater's timeouts (tests lower it)")
    ap.add_argument("--healthcheck", action="store_true",
                    help="ask the local server for /api/health and exit 0 if it answers and "
                         "Docker sees the checkouts where the deployer does")
    ap.add_argument("--channel", choices=devchannel.CHANNELS,
                    help="ask the running deployer to switch channel; for dev the access code "
                         "is read from stdin (a hidden prompt on a terminal)")
    ap.add_argument("--registry", default="",
                    help="with --channel dev and a plain token (advanced): its registry "
                         "(default POL_DEV_REGISTRY); an olx1. code carries its own")
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
    cfg.minute = max(0.01, args.minute)
    if args.docker:
        cfg.docker = ([cfg.python] if args.docker.endswith(".py") else []) + [args.docker]
    api = os.environ.get("POL_DEPLOY_GITHUB_API", "").strip()
    if api:
        if not api.startswith(("http://", "https://")):
            say("POL_DEPLOY_GITHUB_API must be an http(s) URL (got %r)" % api)
            sys.exit(2)
        cfg.github_api = api.rstrip("/")
    reg = os.environ.get("POL_DEV_REGISTRY", "").strip()
    if reg:
        if devchannel.REGISTRY.match(reg):
            cfg.dev_registry = reg
        else:
            say("POL_DEV_REGISTRY must look like registry.example.org/team (got %r); ignored, "
                "so only olx1. access codes work" % reg)
    src = os.environ.get("POL_DEV_SOURCE", "").strip()
    if src:
        if devchannel.SOURCE.match(src):
            cfg.dev_source = src.rstrip("/")
        else:
            say("POL_DEV_SOURCE must be an http(s) URL (got %r); ignored" % src)
    user = os.environ.get("POL_DEV_USER", "").strip()
    if user and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", user):
        cfg.dev_user = user
    if truthy(os.environ.get("POL_DEV_REGISTRY_HTTP")):
        cfg.dev_scheme = "http"
    issuer = os.environ.get("POL_DEV_ISSUER", "").strip().rstrip("/")
    if issuer:
        if issuer.lower() == devchannel.ISSUER_OFF or devchannel.ISSUER.match(issuer):
            cfg.dev_issuer = issuer
        else:
            say("POL_DEV_ISSUER must be an http(s) address or off (got %r), so short dev codes "
                "are not taken" % issuer)
            cfg.dev_issuer = devchannel.ISSUER_OFF
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
    cfg.patch_host = args.patch_host.strip() or "patch"
    cfg.patch_offset = args.patch_port_offset
    cfg.valkey = patches.valkey_address(args.valkey.strip())
    if args.valkey.strip() and cfg.valkey is None:
        say("POL_VALKEY_URL (%s) is not a valkey:// URL, so patch actions do not wait for "
            "downloads" % args.valkey)
    cfg.kv_prefix = os.environ.get("POL_KV_PREFIX", "pol:")
    cfg.patch_drain = max(0.0, args.patch_drain)
    cfg.drain_poll = max(0.05, args.drain_poll)
    mirrors = args.mirrors.strip()
    if mirrors:
        if not os.path.isabs(mirrors):
            cfg.mirrors_note = ("POL_DEPLOY_MIRRORS must be an absolute path (got %r), so the "
                                "patch actions are off" % mirrors)
        elif not os.path.isdir(mirrors):
            cfg.mirrors_note = ("POL_DEPLOY_MIRRORS (%s) is not a folder inside the container; "
                                "mount the patch service's mirrors folder there. The patch "
                                "actions are off" % mirrors)
        else:
            cfg.mirrors = os.path.realpath(mirrors)
            cfg.mirrors_note = ""
    if cfg.mirrors_note:
        say(cfg.mirrors_note)
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
    return may_write(uid, gids, path)


def may_write(uid, gids, path):
    """Whether uid (with the groups gids) has write permission on path, by its
    owner, group and mode. False when path is not there."""
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


DESKTOP_MNT = "/run/desktop/mnt/host/"


def on_desktop(path):
    """Whether path is a host folder as Docker Desktop's VM sees it. Every file
    there reads as root's unless a Linux program made it; the Windows owner is
    kept apart."""
    return path.replace("\\", "/").startswith(DESKTOP_MNT)


def install_identity(root):
    """subprocess arguments for `stack.py add`, which clones a game beside the
    core: as the owner of the checkouts folder, as owner_kwargs. On Docker
    Desktop that folder reads as root's, so the clone is made as nobody
    instead: it then belongs to nobody, and an import tool, which runs as the
    checkout's owner and never as root, can write its data into it."""
    kw = owner_kwargs(root)
    if not kw and on_desktop(root) and hasattr(os, "getuid") and os.getuid() == 0:
        kw = {"user": NOBODY, "group": NOBODY, "extra_groups": []}
    return kw


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


# ------------------------------------------------- where Docker sees the checkouts

# docker compose runs here, but the Docker daemon resolves the paths it is
# handed. With Docker Engine on Linux the checkouts are mounted at their host
# path, so both read the same folder. Docker Desktop's daemon runs in a Linux
# VM that sees a Windows folder C:\x as /run/desktop/mnt/host/c/x, so there
# POL_DEPLOY_ROOT is that path. Rather than trust either, the deployer starts
# a throwaway container from its own image with the core checkout mounted the
# way compose will mount it, and compares what that container reads with what
# it reads itself.
PROBE_FILE = "docker-compose.yml"
PROBE_CODE = ("import hashlib, os\n"
              "p = os.path.join('/probe', %r)\n"
              "st = os.stat(p)\n"
              "with open(p, 'rb') as f:\n"
              "    print('%%s %%d %%d' %% (hashlib.sha256(f.read()).hexdigest(), st.st_size, "
              "int(st.st_mtime)))\n" % PROBE_FILE)
MOUNT_MISSING = ("bind source path does not exist", "no such file or directory",
                 "not a directory", "invalid mount config")


def core_mark(core):
    """What the probe container must print for `core`: the compose file's
    sha256, size and whole-second mtime."""
    p = os.path.join(core, PROBE_FILE)
    st = os.stat(p)
    with open(p, "rb") as f:
        return "%s %d %d" % (hashlib.sha256(f.read()).hexdigest(), st.st_size, int(st.st_mtime))


def own_container():
    """This container's id (Docker makes it the hostname), or "" outside a container."""
    if not os.path.exists("/.dockerenv"):
        return ""
    return socket.gethostname()


def paths_sentence(core, why):
    return ("The Docker daemon does not see the OpenLobby checkout at %s, where the deployer "
            "sees it (%s), so docker compose would mount other folders than the ones it reads. "
            "The deployer runs no docker compose command until POL_DEPLOY_ROOT is the folder "
            "as the daemon sees it, mounted at that same path. On Docker Desktop for Windows "
            "that is the path under /run/desktop/mnt/host/, for example "
            "/run/desktop/mnt/host/c/Users/you/openlobby-server for "
            "C:\\Users\\you\\openlobby-server (docs/deployer.md)." % (core, why))


def check_paths(cfg, container):
    """("ok" | "wrong" | "unchecked", sentence) for the core checkout: whether
    a container the daemon starts with cfg.core mounted reads the same
    docker-compose.yml there as this one does. "unchecked" when Docker could
    not be asked at all; jobs then run as before."""
    env = {k: v for k, v in os.environ.items()
           if k != "POL_DEPLOYER_TOKEN" and v != cfg.token}

    def ask(argv):
        try:
            r = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                               timeout=PROBE_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, "", str(exc)
        return (r.returncode, r.stdout.decode("utf-8", "replace").strip(),
                r.stderr.decode("utf-8", "replace").strip())

    if not container:
        return "unchecked", "not running in a container"
    rc, image, err = ask(cfg.docker + ["inspect", "--format", "{{.Image}}", container])
    if rc != 0 or not image:
        return "unchecked", "could not ask Docker about this container (%s)" % (
            (err.splitlines() or ["exit %s" % rc])[-1])
    try:
        mine = core_mark(cfg.core)
    except OSError as exc:
        return "wrong", paths_sentence(cfg.core, "this container cannot read %s: %s"
                                       % (PROBE_FILE, exc.strerror or exc))
    # --mount takes CSV: a quoted field keeps a comma in the path
    source = '"source=%s"' % cfg.core.replace('"', '""')
    rc, out, err = ask(cfg.docker + [
        "run", "--rm", "--pull", "never", "--network", "none", "--entrypoint", "python",
        "--mount", "type=bind,%s,target=/probe,readonly" % source, image, "-c", PROBE_CODE])
    if rc == 0 and out == mine:
        return "ok", "Docker sees %s where the deployer does" % cfg.core
    if rc == 0:
        return "wrong", paths_sentence(cfg.core, "a container started there reads another "
                                       "%s" % PROBE_FILE)
    lines = [l.strip() for l in err.splitlines() if l.strip()]
    hit = [l for l in lines if any(m in l.lower() for m in MOUNT_MISSING)]
    if rc is not None and hit:
        return "wrong", paths_sentence(cfg.core, "Docker says: %s" % hit[0])
    return "unchecked", "the check container did not run (%s)" % (
        (lines or ["exit %s" % rc])[0])


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
    if job.get("action") == "updater":
        out["title"] = job.get("title")
        out["channel"] = job.get("channel")
    if job.get("action") == "channel":
        for k in ("op", "key", "to"):
            if job.get(k) is not None:
                out[k] = job[k]
    if job.get("action") in PATCH_ACTIONS:
        for k in ("region", "product", "to", "removes", "source"):
            if job.get(k) is not None:
                out[k] = job[k]
    return out


class JobLog:
    """Appends to a job's log, with the token and any access code blanked out, the
    dev registry's address replaced by "[dev builds]" (`hide`: registries, host/path),
    and the file kept under LOG_CAP by dropping its head."""

    def __init__(self, store, job, token, secrets=(), hide=()):
        self.store, self.job, self.token = store, job, token.encode("utf-8")
        found = []
        for sec in secrets:
            if not sec:
                continue
            found.append(sec)
            tok = devchannel.token_of(sec)
            if tok and tok != sec:
                found.append(tok)        # the token inside an olx1. code
            for one in (sec, tok or ""):
                # a "name:secret" code: its secret part alone as well
                if ":" in one and one.split(":", 1)[1]:
                    found.append(one.split(":", 1)[1])
        # longest first, so a code is blanked before a token inside it
        self.secrets = [x.encode("utf-8") for x in sorted(set(found), key=len, reverse=True)]
        names = set()
        for reg in hide:
            if reg:
                names.update((reg, devchannel.registry_host(reg)))
        self.hide = [x.encode("utf-8") for x in sorted(names, key=len, reverse=True)]
        self.path = store.log_path(job["id"])

    def write(self, text):
        data = text.encode("utf-8", "replace") if isinstance(text, str) else text
        if self.token:
            data = data.replace(self.token, b"[token]")
        for sec in self.secrets:
            data = data.replace(sec, b"[access code]")
        for name in self.hide:
            data = data.replace(name, b"[dev builds]")
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
        self.channel = devchannel.read_channel(cfg.core, catalog)
        self.chstore = devchannel.Store(cfg.state)
        self.on_check = None      # called with each check_access answer (the panel's code state)
        self.last_lines = []      # the last lines the latest run() printed

    def dev(self):
        """Whether anything is on Dev."""
        return bool(self.channel.get("dev"))

    def unlocked(self):
        return bool(self.channel.get("unlocked"))

    def child_env(self, as_owner=False):
        env = {k: v for k, v in os.environ.items()
               if k != "POL_DEPLOYER_TOKEN" and v != self.cfg.token}
        if as_owner:
            env["HOME"] = "/tmp"
        # with the dev builds unlocked every docker command signs in with the deployer's
        # own client config (STATE/channel/docker), never the host's
        if self.unlocked() and os.path.isdir(self.chstore.docker_dir):
            env["DOCKER_CONFIG"] = self.chstore.docker_dir
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
        tail = []
        for chunk in iter(p.stdout.readline, b""):
            self.log.write(chunk)
            tail.append(chunk.decode("utf-8", "replace").rstrip())
            del tail[:-40]
        self.last_lines = tail
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

    def stack(self, args, owner=None, kw=None):
        return self.run([self.cfg.python, self.cfg.stack] + args, owner=owner, kw=kw)

    def paths_guard(self):
        if self.cfg.paths == "wrong":
            raise JobFailed(self.cfg.paths_note)

    def compose(self, args, prebuild=True):
        self.paths_guard()
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

    def fill_volumes(self, keys, catalog=None):
        """Create and fill the data volumes of `keys` that the catalog says how
        to fill (stack.py volumes), streaming its lines into the log, and keep
        what failed for the games list. Games with none are skipped without a
        word. Returns the failed results ({"volume", "why", ...})."""
        catalog = catalog or self.catalog
        keys = [k for k in keys if fillable(catalog, k)]
        if not keys:
            return []
        report = os.path.join(self.cfg.state, "volumes-%s.json" % secrets.token_hex(6))
        try:
            rc = self.stack(["volumes", ",".join(keys), "--report", report])
            try:
                with open(report, encoding="utf-8") as f:
                    results = json.load(f).get("volumes", [])
            except (OSError, ValueError, AttributeError):
                results = None
        finally:
            try:
                os.remove(report)
            except OSError:
                pass
        if results is None:
            why = ("stack.py volumes stopped (exit %d) before it said what it did; the log has "
                   "its output." % rc)
            results = [{"game": k, "volume": v["name"], "state": "failed", "why": why}
                       for k in keys for v in catalog[k].get("volumes", [])
                       if v.get("required") and v.get("fill")]
        record_fills(self.cfg.state, results)
        return [x for x in results if x.get("state") == "failed"]

    def need_git(self):
        if not shutil.which("git"):
            raise JobFailed("git is not installed in the deployer image")

    # ---- the dev channel

    def dev_rows(self, keys=None):
        """The image rows of the core and `keys` (the enabled games by default) as this
        job started."""
        keys = [k for k in self.enabled() if k in self.catalog] if keys is None else keys
        return devchannel.image_rows(self.catalog, self.channel, self.cfg.ghcr, keys=keys)

    def dev_gate(self, strict, keys=None, rows=None):
        """Before anything pulls the dev builds of `keys` (those on Dev; every one on Dev by
        default): the stored access code must still read them. A refused code fails the job
        with the sentence the panel shows; a registry that does not answer fails it only when
        `strict` (an update), else the job goes on with the images it has. `rows`: the image
        rows to ask about instead (a switch to Dev, before .channel says so)."""
        if rows is None:
            rows = [x for x in devchannel.swapped(self.dev_rows())
                    if keys is None or x["key"] in keys]
        if not rows:
            return
        code = self.chstore.code()
        if not code:
            raise JobFailed(devchannel.NO_CODE)
        res = devchannel.check_access(self.channel["registry"], devchannel.token_of(code),
                                      devchannel.probes_of(rows),
                                      scheme=self.cfg.dev_scheme, user=self.cfg.dev_user)
        if self.on_check:
            self.on_check(res)
        if res["state"] == "ok":
            self.log.line("the access code still reads the dev builds")
            return
        self.log.line("dev builds: %s" % res["why"])
        if res["state"] in ("wrong", "denied"):
            raise JobFailed(devchannel.REVOKED)
        if strict:
            raise JobFailed("%s Nothing was updated." % res["why"])
        self.log.line("WARN the job goes on with the images this host already has")

    def docker_login(self, host, user, secret):
        """docker login to the dev registry into the deployer's own client config, the
        secret on stdin: never in an argument, the environment or the log."""
        argv = self.cfg.docker + ["login", "--username", user, "--password-stdin", host]
        self.log.line("$ " + self.show(argv))
        env = self.child_env()
        env["DOCKER_CONFIG"] = self.chstore.docker_config()
        try:
            r = subprocess.run(argv, cwd=self.cfg.core, env=env, input=(secret + "\n").encode("utf-8"),
                               capture_output=True, timeout=120)
        except FileNotFoundError:
            raise JobFailed("docker is not installed in the deployer image")
        except subprocess.TimeoutExpired:
            raise JobFailed("docker login did not finish within 120 seconds")
        self.log.write(r.stdout + r.stderr)
        self.log.line("(exit %d)" % r.returncode)
        self.last_rc = r.returncode
        return r.returncode

    def docker_logout(self, host):
        if not os.path.isdir(self.chstore.docker_dir):
            return 0
        argv = self.cfg.docker + ["logout", host]
        env = self.child_env()
        env["DOCKER_CONFIG"] = self.chstore.docker_dir
        return self.run(argv, env=env, kw={})

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


def fillable(catalog, key):
    """Whether game `key` has a required volume the catalog says how to fill."""
    e = catalog.get(key) or {}
    return any(v.get("required") and v.get("fill") for v in e.get("volumes", []))


FILLS_FILE = "volume-fills.json"


def read_fills(state):
    """{volume: {"why", "when"}}: the volumes whose last automatic fill failed."""
    try:
        with open(os.path.join(state, FILLS_FILE), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record_fills(state, results):
    """Keep the failures of a fill run and forget the volumes it set up."""
    fills = read_fills(state)
    for x in results:
        name = x.get("volume")
        if not isinstance(name, str):
            continue
        if x.get("state") == "failed":
            fills[name] = {"why": str(x.get("why") or "")[:600], "when": now_iso()}
        else:
            fills.pop(name, None)
    path = os.path.join(state, FILLS_FILE)
    try:
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(fills, f)
        os.replace(path + ".tmp", path)
    except OSError:
        pass


def fill_missing(failed):
    """A failed fill as "missing" rows: what the panel shows, no button."""
    return [{"path": "volume %s" % x.get("volume"), "item": x.get("volume"),
             "hint": "Setting it up by itself failed: %s" % x.get("why", "")} for x in failed]


def fill_sentence(name, failed):
    s = "%s's data could not be set up: %s" % (name, failed[0].get("why", ""))
    if len(failed) > 1 and any(x.get("why") != failed[0].get("why") for x in failed[1:]):
        s += " (and %d more; the log has each)" % (len(failed) - 1)
    return s


def fill_note(entry, m, fills):
    """A missing volume the deployer sets up by itself: when its last try
    failed, the row says why (with the commands to do it by hand)."""
    if m.get("source") == "docker volume" and m.get("fill"):
        failed = fills.get(m.get("id"))
        if failed:
            entry["hint"] = "Setting it up by itself failed: %s By hand: %s" % (
                failed.get("why", ""), m.get("tool", ""))
    return entry
def unprivileged(checkout, what, tool):
    """subprocess kwargs for running a tool on files someone supplied: the
    checkout's owner (nobody for root's), never root, never a user that may
    use the Docker socket. Raises JobFailed with the reason."""
    kw, uid, gids = import_identity(checkout)
    if uid is not None:
        if uid == 0:
            raise JobFailed("%s would run as root; the deployer refuses that." % what)
        for sock in docker_sockets():
            if socket_open_to(uid, gids, sock):
                raise JobFailed("%s would run as uid %d, which may use the Docker "
                                "socket %s, so it was not started. Give the checkout to a user "
                                "outside the docker group (docs/deployer.md)." % (tool, uid, sock))
    return kw


def scratch(cfg, name, kw):
    """A fresh folder under STATE/imports for one child process, owned by
    the user kw runs it as."""
    home = os.path.join(cfg.state, "imports", name)
    shutil.rmtree(home, ignore_errors=True)
    os.makedirs(home)
    if kw:
        os.chown(home, kw["user"], kw["group"])
    return home


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
    if m.get("source") == "docker volume" and m.get("fill"):
        s = "%s's data (the volume %s) is not set up yet; turning the game on sets it up" % (
            name, path[len("volume "):])
    elif m.get("source") == "docker volume":
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
    if r.stack(["add", key, "--yes"], kw=install_identity(r.cfg.root)) != 0:
        raise JobFailed("Cloning %s failed; the log has git's message." % r.name(key), r.last_rc)
    if not os.path.isdir(r.checkout(key)):
        raise JobFailed("%s was not cloned to %s." % (r.name(key), r.checkout(key)))
    failed = r.fill_volumes([key])
    if failed:
        return "failed", "%s is installed, but its data could not be set up: %s Turning it on " \
            "tries again." % (r.name(key), failed[0].get("why", "")), fill_missing(failed)
    return evaluate(r.catalog, r.check_report([key]), "%s is installed; enable it to start it." % r.name(key))


def act_enable(r, key):
    if not os.path.isdir(r.checkout(key)):
        raise JobFailed("%s is not installed; install it first." % r.name(key))
    failed = r.fill_volumes([key])
    if failed:
        return "failed", "%s It was not turned on." % fill_sentence(r.name(key), failed), \
            fill_missing(failed)
    state, summary, missing = evaluate(r.catalog, r.check_report([key]), "")
    if state != "ok":
        return state, summary + (". It was not enabled." if state == "needs_data" else ""), missing
    r.dev_gate(strict=False, keys=[key])
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
    r.dev_gate(strict=True, keys=[key])
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
        # the update may have changed the volumes the game needs
        failed = r.fill_volumes([key])
        if failed:
            return "failed", "%s It was updated but not restarted." % fill_sentence(r.name(key), failed), \
                fill_missing(failed)
        state, summary, missing = evaluate(r.catalog, r.check_report([key]), "")
        if state != "ok":
            return state, summary + (". It was updated but not restarted." if state == "needs_data" else ""), missing
        failed = []
    else:
        # the core's update brings the catalog: an enabled game may need a new volume
        catalog = load_catalog(r.cfg.core)
        failed = r.fill_volumes([k for k in r.enabled() if k in catalog], catalog)
    if r.cfg.ghcr or r.dev():
        # a dev image keeps its tag while it moves, so with anything on Dev it is pulled
        # even when the rest is built here
        r.compose(["pull", "--ignore-buildable", "--ignore-pull-failures"], prebuild=False)
    r.up()
    if failed:
        return "failed", "%s is updated and restarted, but some game data could not be set " \
            "up: %s" % (r.name(key), failed[0].get("why", "")), fill_missing(failed)
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
        if not may_write(uid, gids, d):
            raise JobFailed("The import tool would run as uid %d (the checkout's owner, or nobody "
                            "when root owns it), which cannot write to %s, so it was not started. "
                            "Give the checkout to a user outside the docker group "
                            "(docs/deployer.md, \"How an import is kept apart from the socket\")."
                            % (uid, os.path.basename(d)))
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


def active_profiles(cfg):
    """The compose profiles that are on: COMPOSE_PROFILES in the environment, else the core's .env."""
    val = os.environ.get("COMPOSE_PROFILES")
    if val is None:
        val = read_dotenv(os.path.join(cfg.core, ".env")).get("COMPOSE_PROFILES", "")
    return {p.strip() for p in val.split(",") if p.strip()}


def key_services(cfg, catalog, enabled, key):
    """The services a switch of `key` restarts: what its dev builds run (and the title chain
    for a game whose published image is a title link), without those of a profile that is
    off (naming one would start it). `enabled`: the games that are on."""
    on = active_profiles(cfg)
    off = set()
    for k in ["core"] + [x for x in enabled if x in catalog]:
        for prof, svcs in (catalog[k].get("profiles") or {}).items():
            if prof not in on:
                off.update(svcs)
    return [s for s in devchannel.services_of(catalog[key]) if s not in off]


def channel_login(r, reg, code):
    """Sign in to the dev builds with the access code `code` into the deployer's own client
    config, and keep the code."""
    if not code:
        raise JobFailed("The access code did not reach the job; enter it again.")
    host = devchannel.registry_host(reg)
    token = devchannel.token_of(code)
    if token:
        user, secret = devchannel.split_code(token, r.cfg.dev_user)
        r.log.line("unlocking the dev builds: signing in to them as %s" % user)
        if r.docker_login(host, user, secret) != 0:
            raise JobFailed("Docker could not sign in to the dev builds with the access code; "
                            "the log has its message. Nothing was changed.", r.last_rc)
    else:
        r.log.line("unlocking the dev builds: they are public, no sign-in")
        r.chstore.docker_config()     # an empty client config, so no host sign-in is used
    r.chstore.save_code(code)


def channel_forget(r, before):
    """Sign out of the dev builds and forget the access code."""
    if before.get("unlocked") and before.get("registry"):
        r.docker_logout(devchannel.registry_host(before["registry"]))
    r.chstore.forget()
    r.log.line("the access code and the registry sign-in are removed")


def act_channel(r, job, code):
    """What follows the dev builds (docs/deployer.md, "Dev channel"). job["op"] is

        unlock  sign in with the access code, keep it, record the registry (stack.py
                channel unlock); nothing else changes
        switch  put job["key"] on job["to"] (dev or stable), pull its images and restart
                only its services
        lock    sign out and forget the code, once everything is on Stable
        all     the whole install on Dev at once, as before (setup's --channel dev): unlock,
                every key with a dev build on Dev, pull and restart
        stable  the whole install back on Stable and locked, as before"""
    op = job.get("op") or ("all" if job.get("channel") == "dev" else "stable")
    before = r.channel
    if op in ("unlock", "all"):
        reg = job.get("registry")
        channel_login(r, reg, code)
        args = ["channel", "dev" if op == "all" else "unlock", "--registry", reg]
        src = job.get("dev_source") or r.cfg.dev_source
        if src:
            args += ["--source", src]
        if r.stack(args, owner=r.cfg.core) != 0:
            raise JobFailed("stack.py channel %s failed; the log has its message." % args[1], r.last_rc)
        r.channel = devchannel.read_channel(r.cfg.core, r.catalog)
        if not r.unlocked():
            raise JobFailed("stack.py did not record the dev builds in %s." % devchannel.CHANNEL_FILE)
        if op == "unlock":
            on = [r.name(k) for k in r.channel["dev"] if k in r.catalog]
            return "ok", ("The dev builds are unlocked. Nothing was switched: pick Dev for each "
                          "server that should follow them." if not on else
                          "The access code is replaced; still on Dev: %s." % ", ".join(on)), []
    elif op == "lock":
        if before.get("dev"):
            raise JobFailed("Switch everything back to Stable first; on Dev: %s."
                            % ", ".join(r.name(k) if k in r.catalog else k for k in before["dev"]))
        if r.stack(["channel", "lock"], owner=r.cfg.core) != 0:
            raise JobFailed("stack.py channel lock failed; the log has its message.", r.last_rc)
        channel_forget(r, before)
        r.channel = devchannel.read_channel(r.cfg.core, r.catalog)
        return "ok", "The access code is forgotten; the dev builds are locked again.", []
    elif op == "stable":
        r.log.line("everything back on Stable")
        if r.stack(["channel", "stable"], owner=r.cfg.core) != 0:
            raise JobFailed("stack.py channel stable failed; the log has its message.", r.last_rc)
        channel_forget(r, before)
        r.channel = devchannel.read_channel(r.cfg.core, r.catalog)
    elif op == "switch":
        return act_switch(r, job, before)
    else:
        raise JobFailed("Unknown channel job.")
    want = "dev" if op == "all" else "stable"
    rows = devchannel.image_rows(r.catalog, r.channel if want == "dev" else before, r.cfg.ghcr,
                                 keys=[k for k in r.enabled() if k in r.catalog])
    swapped = devchannel.swapped(rows)
    r.compose(["pull", "--ignore-buildable", "--ignore-pull-failures"], prebuild=False)
    r.up()
    if want == "dev":
        if not swapped:
            return "ok", ("Everything with a dev build is on Dev. Nothing this server runs has one "
                          "yet, so the published images stay; a game installed later follows its "
                          "dev build when it has one."), []
        names = sorted(set(x["name"] for x in swapped))
        return "ok", "Everything is on Dev: %s now run%s the dev image%s (%s)." % (
            ", ".join(names), "s" if len(names) == 1 else "",
            "" if len(swapped) == 1 else "s", ", ".join(x["dev_name"] for x in swapped)), []
    backs = [d.get("back") for k in set(x["key"] for x in swapped)
             for d in devchannel.dev_entries(r.catalog.get(k, {})) if isinstance(d.get("back"), str)]
    note = (" " + " ".join(sorted(set(backs)))) if backs else ""
    return "ok", ("Everything is on Stable: the published images run again, and the access code "
                  "is forgotten.%s" % note), []


def act_switch(r, job, before):
    """One key to Dev or Stable: record it, then pull its images and restart its services only
    (nothing when it is not turned on)."""
    key, to = job.get("key"), job.get("to")
    if key not in r.catalog or not devchannel.dev_entries(r.catalog[key]):
        raise JobFailed("%s has no dev build." % key)
    name = r.name(key)
    if to == "dev":
        if not r.unlocked():
            raise JobFailed(devchannel.LOCKED)
        after = dict(before, dev=sorted(set(before.get("dev") or ()) | {key}))
        rows = [x for x in devchannel.swapped(devchannel.image_rows(
            r.catalog, after, r.cfg.ghcr, keys=[key])) if x["key"] == key]
        r.dev_gate(strict=True, rows=rows)
    if r.stack(["channel", "set", key, to], owner=r.cfg.core) != 0:
        raise JobFailed("stack.py channel set %s %s failed; the log has its message." % (key, to),
                        r.last_rc)
    r.channel = devchannel.read_channel(r.cfg.core, r.catalog)
    if (key in r.channel["dev"]) != (to == "dev"):
        raise JobFailed("stack.py did not record %s on %s in %s." % (name, to.capitalize(),
                                                                       devchannel.CHANNEL_FILE))
    word = "Dev" if to == "dev" else "Stable"
    shown = devchannel.image_rows(r.catalog, r.channel if to == "dev" else before, r.cfg.ghcr,
                                  keys=[key])
    mine = [x for x in devchannel.swapped(shown) if x["key"] == key]
    if key != "core" and key not in r.enabled():
        return "ok", "%s is on %s. It is not turned on, so nothing was restarted; it %s when it " \
            "is." % (name, word, "follows its dev build" if to == "dev" else "runs the published "
                     "images"), []
    if not mine:
        why = [x["note"] for x in shown if x["key"] == key and x["dev_name"] and not x["applies"]]
        return "ok", "%s is on %s. Nothing it runs changes here%s, so nothing was restarted." % (
            name, word, (": " + why[0].rstrip(".")) if why else ""), []
    services = key_services(r.cfg, r.catalog, r.enabled(), key)
    r.log.line("%s to %s: pulling and restarting %s" % (name, word, ", ".join(services)))
    r.compose(["pull", "--ignore-buildable", "--ignore-pull-failures"] + services, prebuild=False)
    r.up(services)
    images = ", ".join(x["dev_name"] for x in mine)
    ran = []
    for x in mine:
        ran += [s for s in x["services"] if s in services and s not in ran]
    if to == "dev":
        return "ok", "%s is on Dev: %s now run%s the dev build%s (%s)." % (
            name, ", ".join(ran), "s" if len(ran) == 1 else "",
            "" if len(mine) == 1 else "s", images), []
    backs = sorted(set(d["back"] for d in devchannel.dev_entries(r.catalog[key])
                       if isinstance(d.get("back"), str)))
    return "ok", "%s is on Stable: %s run%s the published image%s again.%s" % (
        name, ", ".join(ran), "s" if len(ran) == 1 else "",
        "" if len(mine) == 1 else "s", (" " + " ".join(backs)) if backs else ""), []


ACT = {"install": act_install, "enable": act_enable, "disable": act_disable,
       "update": act_update, "restart": act_restart, "check": act_check}


# ------------------------------------------------ the updater of a paired game
#
# A game installed by its own setup program and paired with the panel may name an updater
# (docs/installs.md, "Updates"): a python command to run in its install folder. The panel
# sends the record its owner approved (POST /api/updater); everything in it is checked again
# here (gameupdater.check). The deployer's own container mounts only its checkouts, so the
# updater runs in a new container of the deployer's image, the install folder mounted at the
# path the Docker daemon sees it at and made the working directory, with the Docker socket
# (a game's update builds and restarts its containers): see docs/deployer.md, "Updaters of
# paired games", for what approving one means.

FOLDER_CODE = ("import os, sys\n"
               "sys.exit(0 if os.path.isdir('/probe') else 3)\n")


def mount_arg(source, target, readonly=False):
    """A --mount value; quoted CSV fields keep a comma in a path."""
    q = lambda f: '"%s"' % f.replace('"', '""')     # noqa: E731
    return "type=bind,%s,%s%s" % (q("source=" + source), q("target=" + target),
                                  ",readonly" if readonly else "")


def own_image(r):
    """The image of the container the deployer runs in, or JobFailed."""
    if not r.cfg.container:
        raise JobFailed("The deployer is not running in a container here, so it cannot start a "
                        "container of its own image for the updater.")
    rc, out = r.capture(r.cfg.docker + ["inspect", "--format", "{{.Image}}", r.cfg.container],
                        timeout=PROBE_TIMEOUT)
    image = out.strip()
    if rc != 0 or not image:
        raise JobFailed("Docker did not say which image the deployer runs (exit %d); the log has "
                        "its output." % rc, rc)
    return image


def updater_folder_check(r, image, folder, seen):
    """A throwaway container of the deployer's image with the folder mounted where the daemon
    sees it (read-only, no network), the start-up check's way: fails the job with a sentence
    when Docker does not see a folder there."""
    argv = r.cfg.docker + ["run", "--rm", "--pull", "never", "--network", "none",
                           "--entrypoint", "python", "--mount", mount_arg(seen, "/probe", True),
                           image, "-c", FOLDER_CODE]
    r.log.line("checking that Docker sees %s as %s" % (folder, seen))
    r.log.line("$ " + r.show(argv))
    try:
        p = subprocess.run(argv, cwd=r.cfg.core, env=r.child_env(), stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=PROBE_TIMEOUT)
    except FileNotFoundError:
        raise JobFailed("docker is not installed in the deployer image")
    except subprocess.TimeoutExpired:
        raise JobFailed("The folder check did not finish within %d seconds." % PROBE_TIMEOUT)
    err = p.stderr.decode("utf-8", "replace")
    if err.strip():
        r.log.write(err if err.endswith("\n") else err + "\n")
    r.log.line("(exit %d)" % p.returncode)
    if p.returncode == 0:
        return
    lines = [l.strip() for l in err.splitlines() if l.strip()]
    hit = [l for l in lines if any(m in l.lower() for m in MOUNT_MISSING)]
    if p.returncode == 3 or hit:
        raise JobFailed("Docker does not see the folder %s (as %s)%s. Is the game installed there?"
                        % (folder, seen, ": %s" % hit[0] if hit else ""), p.returncode)
    raise JobFailed("The folder check container did not run (%s)." % ((lines or ["exit %d" % p.returncode])[0]),
                    p.returncode)


def stop_updaters(r, job_id=None):
    """docker kill the updater containers (of one job, or every one left over)."""
    label = UPDATER_LABEL + ("=" + job_id if job_id else "")
    try:
        p = subprocess.run(r.cfg.docker + ["ps", "-q", "--filter", "label=" + label], env=r.child_env(),
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
        ids = p.stdout.decode("utf-8", "replace").split() if p.returncode == 0 else []
        if ids:
            subprocess.run(r.cfg.docker + ["kill"] + ids, env=r.child_env(), stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=60)
        return ids
    except (OSError, subprocess.TimeoutExpired):
        return []


def run_watched(r, argv, total, idle, job_id):
    """Run argv with its output streamed into the log. Stopped (its container killed) after
    `total` seconds, or after `idle` seconds without a byte of output. (exit code, "timeout" |
    "idle" | None, the last line it printed)."""
    r.log.line("$ " + r.show(argv))
    kw = {"start_new_session": True} if os.name != "nt" else {}
    try:
        p = subprocess.Popen(argv, cwd=r.cfg.core, env=r.child_env(), stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)
    except FileNotFoundError:
        raise JobFailed("docker is not installed in the deployer image")
    st = {"last": time.time(), "why": None, "line": ""}
    done = threading.Event()

    def dog():
        start = time.time()
        while not done.wait(min(1.0, idle / 10.0)):
            now = time.time()
            if now - start >= total:
                st["why"] = "timeout"
            elif now - st["last"] >= idle:
                st["why"] = "idle"
            else:
                continue
            stop_updaters(r, job_id)
            if done.wait(10):
                return
            try:
                if os.name != "nt":
                    os.killpg(p.pid, signal.SIGKILL)
                else:
                    p.kill()
            except OSError:
                pass
            return
    threading.Thread(target=dog, daemon=True).start()
    for chunk in iter(p.stdout.readline, b""):
        st["last"] = time.time()
        r.log.write(chunk)
        text = chunk.decode("utf-8", "replace").strip()
        if text:
            st["line"] = text
    p.stdout.close()
    rc = p.wait()
    done.set()
    r.last_rc = rc
    r.log.line("(exit %d)" % rc)
    return rc, st["why"], st["line"]


def env_file(r, job_id, values):
    """A docker --env-file for one updater run: STATE/updater-env/<job>.env, mode 600 in a
    folder of mode 700, deleted by the caller once docker run has ended. What it holds goes to
    the one container and to no argument, log line or job file."""
    d = os.path.join(r.cfg.state, "updater-env")
    os.makedirs(d, mode=0o700, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    path = os.path.join(d, job_id + ".env")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        for k, v in values.items():
            f.write("%s=%s\n" % (k, v))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def act_updater(r, job, code=None):
    """Run a paired game's updater. With job["channel"] the command gets "--channel <name>"
    (a switch, or a regular update of a game that is not on stable); `code`, a dev access
    code for a switch, reaches the command as OPENLOBBY_DEV_CODE through an --env-file that is
    deleted when the run ends (docs/deployer.md, "Updaters of paired games")."""
    rec, bad = gameupdater.check(job.get("updater"))
    if bad:
        raise JobFailed("The updater the panel sent does not fit: %s" % bad)
    key = job["game"]
    title = job.get("title") or key
    minutes = rec["timeout_minutes"]
    seen = gameupdater.daemon_path(rec["folder"])
    command = gameupdater.with_channel(rec, job.get("channel"))
    if command is None:
        raise JobFailed("The updater of %s does not offer the %s channel." % (title, job.get("channel")))
    r.log.line("updater of %s: %s in %s, at most %d minutes%s" % (
        title, gameupdater.shown(command), rec["folder"], minutes,
        ", with the dev access code in %s" % gameupdater.DEV_CODE_ENV if code else ""))
    image = own_image(r)
    left = stop_updaters(r)
    if left:
        r.log.line("stopped %d updater container(s) an earlier deployer left running" % len(left))
    updater_folder_check(r, image, rec["folder"], seen)
    envf = env_file(r, job["id"], {gameupdater.DEV_CODE_ENV: code}) if code else None
    argv = r.cfg.docker + [
        "run", "--rm", "--pull", "never", "--name", "openlobby-updater-" + job["id"],
        "--label", UPDATER_LABEL + "=" + job["id"], "--label", "org.openlobby.updater.game=" + key,
        "--mount", mount_arg(seen, seen), "-w", seen,
        "--mount", mount_arg("/var/run/docker.sock", "/var/run/docker.sock"),
        "-e", "HOME=/tmp", "-e", "LANG=C.UTF-8", "-e", "PYTHONUNBUFFERED=1",
        "-e", "PYTHONIOENCODING=utf-8"] + (["--env-file", envf] if envf else []) + [
        "--entrypoint", command[0], image] + command[1:]
    try:
        rc, why, last = run_watched(r, argv, minutes * r.cfg.minute, UPDATER_IDLE * r.cfg.minute, job["id"])
    finally:
        if envf:
            try:
                os.remove(envf)
            except OSError:
                pass
    if why == "timeout":
        raise JobFailed("The updater of %s was stopped after %d minutes." % (title, minutes), rc)
    if why == "idle":
        raise JobFailed("The updater of %s was stopped: no output for %d minutes." % (title, UPDATER_IDLE), rc)
    if rc != 0:
        last = last.replace(r.cfg.token, "[token]") if r.cfg.token else last
        for sec in r.log.secrets:
            last = last.replace(sec.decode("utf-8", "replace"), "[access code]")
        raise JobFailed(last[:300] if last else "The updater of %s exited with code %d and printed "
                                                "nothing." % (title, rc), rc)
    if job.get("switch"):
        return "ok", "The updater of %s switched it to the %s channel." % (title, job["channel"]), []
    return "ok", "The updater of %s finished." % title, []


def updater_request(body):
    """(None, (game, title, record, channel, switch, code)) for a POST /api/updater body that
    fits, else (error, None). Only {"game", "title", "updater"}, the updater checked with the
    panel's own rules, and optionally "channel" (one the updater declares), "switch" (true: a
    channel switch, not a regular update) and "code" (a dev access code or a short dev code,
    for a switch to a channel other than stable)."""
    extra = sorted(set(body) - {"game", "title", "updater", "channel", "switch", "code"})
    if extra:
        return "unknown fields: %s" % ", ".join(str(k)[:40] for k in extra[:5]), None
    game = body.get("game")
    if not isinstance(game, str) or not re.fullmatch(r"[a-z0-9]{1,16}", game):
        return "game must be the paired game's key, 1 to 16 of a-z and 0-9", None
    title = body.get("title")
    title = " ".join(title.split())[:80] if isinstance(title, str) else ""
    rec, bad = gameupdater.check(body.get("updater"))
    if bad:
        return bad, None
    channel = body.get("channel")
    if channel is not None and gameupdater.with_channel(rec, channel) is None:
        return "channel must be one the updater declares (%s)" % ", ".join(rec.get("channels") or ["none"]), None
    switch = body.get("switch", False)
    if not isinstance(switch, bool) or (switch and channel is None):
        return "switch is true only with a channel", None
    code = body.get("code")
    if code is not None:
        code = code.strip() if isinstance(code, str) else ""
        if not switch or channel == "stable":
            return "a code goes only with a switch to a channel other than stable", None
        if not devchannel.CODE.match(code):
            return "The access code is 8 to 2048 characters, with no spaces.", None
    return None, (game, title or game, rec, channel, switch, code or None)


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
                dev = self.dev_state(catalog, have)
                for key in have if keys is None else [k for k in have if k in keys]:
                    try:
                        it = self.check_one(key, catalog[key], vals, force, fetch)
                        self.dev_one(it, key, dev)
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
        ans = {"checked_at": checked, "checking": busy, "items": out,
               "channel": devchannel.read_channel(self.cfg.core, catalog)["channel"]}
        if limited:
            ans["limited"] = True
            ans["retry_after"] = wait
        return ans

    # ---- the dev channel

    def dev_state(self, catalog, have):
        """None with nothing on Dev; else the swapped rows of the installed items and
        what the registry says of them (devchannel.check_access), asked once per check."""
        ch = devchannel.read_channel(self.cfg.core, catalog)
        if not ch["dev"]:
            return None
        rows = devchannel.swapped(devchannel.image_rows(catalog, ch, self.cfg.ghcr, keys=have))
        code = devchannel.Store(self.cfg.state).code()
        if not rows:
            res = {"state": "ok", "why": None, "digests": {}}
        elif not code:
            res = {"state": "nocode", "why": devchannel.NO_CODE, "digests": {}}
        else:
            res = devchannel.check_access(ch["registry"], devchannel.token_of(code),
                                          devchannel.probes_of(rows),
                                          scheme=self.cfg.dev_scheme, user=self.cfg.dev_user)
            self.dep.note_code(res)
            if res["state"] in ("wrong", "denied"):
                res["why"] = devchannel.REVOKED
        return {"rows": rows, "res": res}

    def local_digests(self, ref):
        """The registry digests (sha256:...) the image `ref` has on this host."""
        env = {k: v for k, v in os.environ.items()
               if k != "POL_DEPLOYER_TOKEN" and v != self.cfg.token}
        try:
            r = subprocess.run(self.cfg.docker + ["image", "inspect", ref], env=env,
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
            doc = json.loads(r.stdout.decode("utf-8", "replace")) if r.returncode == 0 else None
        except (OSError, ValueError, subprocess.TimeoutExpired):
            doc = None
        if not isinstance(doc, list) or not doc or not isinstance(doc[0], dict):
            return []
        return [d.split("@", 1)[1] for d in doc[0].get("RepoDigests") or []
                if isinstance(d, str) and "@" in d]

    def dev_one(self, it, key, dev):
        """Fold the dev images of item `key` into its update check: a dev image whose
        registry digest this host does not have is an update."""
        if not dev:
            return
        rows = [r for r in dev["rows"] if r["key"] == key]
        if not rows:
            return
        it["channel"] = "dev"
        res = dev["res"]
        if res["state"] != "ok":
            updatecheck.unknown(it, res["why"])
            return
        newer = [r for r in rows if res["digests"].get(r["dev_image"])
                 and res["digests"][r["dev_image"]] not in self.local_digests(r["dev"])]
        if not newer:
            return
        what = "A newer dev build is published: %s." % ", ".join(r["dev_name"] for r in newer)
        if it["update"] == "available":
            it["why"] = " ".join(x for x in (it.get("why"), what) if x)
        else:
            it.update(update="available", behind=None, changes=[], why=what)
        it["how"] = ("Update it from Settings > Install games; the deployer pulls the dev images "
                     "and restarts it.")

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
# ------------------------------------------------------- patch bundles

def patch_lib(r):
    try:
        return patches.toolkit(r.cfg.core)
    except patches.ToolkitMissing as exc:
        raise JobFailed(str(exc))


def toolkit_dir(cfg):
    return os.path.join(cfg.core, "tools", "patch")


def chown_to(paths, ref):
    """Give paths (and what is under them) to the owner of ref, when we are
    root and ref is not root's: files the deployer writes into the mirrors
    folder belong to whoever owns it."""
    if not hasattr(os, "getuid") or os.getuid() != 0:
        return
    st = os.stat(ref)
    if st.st_uid == 0 and st.st_gid == 0:
        return
    for p in paths:
        if not os.path.lexists(p):
            continue
        os.lchown(p, st.st_uid, st.st_gid)
        if os.path.isdir(p) and not os.path.islink(p):
            for dirpath, dirs, files in os.walk(p):
                for n in dirs + files:
                    os.lchown(os.path.join(dirpath, n), st.st_uid, st.st_gid)


def last_fail(lines):
    """The sentence of the last "[FAIL] ..." line a toolkit command printed,
    with the line after it when it ends in a colon."""
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith("[FAIL]"):
            text = lines[i][len("[FAIL]"):].strip()
            if text.endswith(":") and i + 1 < len(lines):
                text += " " + lines[i + 1].strip()
            return text
    return ""


def patchtool(r, kw, home, args):
    """Run patchtool.py as the unprivileged user kw names; its answer."""
    res = os.path.join(home, "result.json")
    argv = [r.cfg.python, PATCHTOOL, toolkit_dir(r.cfg)] + args + ["--result", res]
    rc = r.run(argv, cwd=r.cfg.core, env=import_env(home), kw=kw, timeout=IMPORT_TIMEOUT)
    try:
        with open(res, encoding="utf-8") as f:
            out = json.loads(f.read(1 << 20))
        if not isinstance(out, dict):
            raise ValueError(out)
    except (OSError, ValueError):
        raise JobFailed("The .polpatch toolkit stopped without an answer (exit %d); the log "
                        "has its output." % rc, rc or 1)
    return out


def problems_sentence(res, default):
    probs = [p for p in res.get("problems") or [] if isinstance(p, str)] or [default]
    return probs[0] + (" (%d more problem(s))" % (len(probs) - 1) if len(probs) > 1 else "")


def restart_patch(r):
    """Restart the patch service once no client is mid-update, since a client
    cannot resume a cut update. A sentence for the job's summary when the wait
    ran out and downloads were cut, else ""."""
    note = ""
    n = patches.live_downloads(r.cfg.valkey, r.cfg.kv_prefix)
    if n is None and r.cfg.valkey:
        r.log.line("the patch service has not reported its downloads recently; not waiting")
    if n:
        t = time.time()
        r.log.line("%d download(s) in progress; waiting up to %d minutes for them to finish"
                   % (n, round(r.cfg.patch_drain / 60)))
        while n and time.time() - t < r.cfg.patch_drain:
            time.sleep(r.cfg.drain_poll)
            m = patches.live_downloads(r.cfg.valkey, r.cfg.kv_prefix) or 0
            if m != n:
                r.log.line("%d download(s) in progress" % m)
            n = m
        if n:
            note = (" %d download(s) were still running after %d minutes and were cut off by "
                    "the restart." % (n, round(r.cfg.patch_drain / 60)))
            r.log.line(note.strip())
    r.log.line("restarting the patch service, which reads its bundles only when it starts")
    r.compose(["restart", "patch"], prebuild=False)
    return note


def wait_served(r, info, wait):
    t = time.time()
    while True:
        if patches.served(r.cfg.patch_host, r.cfg.patch_offset, info):
            return True
        if time.time() - t >= wait:
            return False
        time.sleep(1)


def selfcheck(r, dep, bdir, info, wait=SERVE_WAIT):
    """polselfcheck against the running patch service for one bundle, as
    the mirrors folder's owner. Records and returns (ok, why)."""
    region, product = info["region"], info["product"]
    port = (info.get("port") or 0) + r.cfg.patch_offset
    if not wait_served(r, info, wait):
        why = ("The patch service at %s:%d did not answer for %s/%s with version %s; "
               "docker compose logs patch says why." % (r.cfg.patch_host, port, region, product,
                                                        info.get("meta_latest")))
        r.log.line(why)
        dep.checks.put(region, product, info["versions"], False, why)
        return False, why
    owner = owner_kwargs(r.cfg.mirrors)
    home = scratch(r.cfg, "selfcheck-%s-%s" % (region, product), owner)
    try:
        argv = [r.cfg.python, os.path.join(toolkit_dir(r.cfg), "polselfcheck.py"),
                "--bundle", bdir, "--host", r.cfg.patch_host, "--port", str(port)]
        rc = r.run(argv, cwd=r.cfg.core, env=import_env(home), kw=owner, timeout=IMPORT_TIMEOUT)
    finally:
        shutil.rmtree(home, ignore_errors=True)
    why = None
    if rc != 0:
        text = last_fail(r.last_lines)
        why = ("The self-check of %s/%s failed: %s" % (region, product, text)) if text else \
              ("The self-check of %s/%s failed (exit %d); the log has its output."
               % (region, product, rc))
    dep.checks.put(region, product, info["versions"], rc == 0, why)
    return rc == 0, why


def patch_finish(r, dep, region, product, done, restart=True):
    lib = patch_lib(r)
    bdir = lib.find_bundle(r.cfg.mirrors, region, product)
    if restart:
        done += restart_patch(r)
    if bdir is None:
        raise JobFailed("%s/%s is not in the mirrors folder any more." % (region, product))
    info = dep.bundles.describe(lib, bdir)
    if info["error"]:
        raise JobFailed(info["error"])
    good, why = selfcheck(r, dep, bdir, info)
    if good:
        return "ok", done + " The patch service serves it and the self-check passed.", []
    return "failed", done + " " + why, []


def patch_install(r, job, dep, path, what):
    """The steps of patch_import: patch_apply, a restart of the patch service
    and the self-check."""
    region, product, summary, unchanged = patch_apply(r, job, dep, path, what)
    lib = patch_lib(r)
    bdir = lib.find_bundle(r.cfg.mirrors, region, product)
    if unchanged and bdir and patches.served(r.cfg.patch_host, r.cfg.patch_offset,
                                             dep.bundles.describe(lib, bdir)):
        return patch_finish(r, dep, region, product, summary, restart=False)
    return patch_finish(r, dep, region, product, summary)


def patch_apply(r, job, dep, path, what):
    """The full check of a .polpatch as an unprivileged user, then
    polpatch.py apply as the mirrors folder's owner. (region, product,
    summary, unchanged)."""
    lib = patch_lib(r)
    mirrors = r.cfg.mirrors
    try:
        m = lib.read_manifest(path)
    except lib.PolpatchError as exc:
        raise JobFailed(str(exc))
    region, product = m["region"], m["product"]
    job["region"], job["product"] = region, product
    dep.store.save(job)
    r.log.line("%s %s: %s .polpatch for %s/%s, version(s) %s"
               % (what, m["title"], m["kind"], region, product, ", ".join(m["versions"])))
    target = lib.find_bundle(mirrors, region, product)
    kw = unprivileged(r.cfg.core, "The .polpatch check", "The .polpatch check")
    home = scratch(r.cfg, job["id"] + "-check", kw)
    try:
        res = patchtool(r, kw, home, ["verify", path] + (["--against", target] if target else []))
    finally:
        shutil.rmtree(home, ignore_errors=True)
    if not res.get("ok"):
        raise JobFailed(problems_sentence(res, "The .polpatch did not pass its check."),
                        r.last_rc or 1)
    owner = owner_kwargs(mirrors)
    home = scratch(r.cfg, job["id"] + "-apply", owner)
    try:
        # the full check above unpacked every blob; apply checks every hash again
        argv = [r.cfg.python, os.path.join(toolkit_dir(r.cfg), "polpatch.py"), "apply", path,
                "--mirrors", mirrors, "--quick"]
        rc = r.run(argv, cwd=r.cfg.core, env=import_env(home), kw=owner, timeout=IMPORT_TIMEOUT)
    finally:
        shutil.rmtree(home, ignore_errors=True)
    if rc != 0:
        raise JobFailed(last_fail(r.last_lines) or
                        "polpatch.py apply failed (exit %d); the log has its output." % rc, rc)
    done = next((ln for ln in reversed(r.last_lines) if ln.startswith("[*] ") and ": " in ln), "")
    action = done[4:].split(":", 1)[0] if done else ""
    words = {"installed": "Installed", "replaced": "Replaced", "updated": "Added",
             "unchanged": "Already had"}
    summary = "%s %s/%s %s." % (words.get(action, "Imported"), region, product,
                                ", ".join(m["versions"]))
    return region, product, summary, action == "unchanged"


def act_patch_import(r, job, dep):
    path = job["_file"]
    if not os.path.isfile(path):
        raise JobFailed("The .polpatch to import is gone; upload or choose it again.")
    return patch_install(r, job, dep, path, "importing")


def act_patch_build(r, job, dep):
    folder = job["_input"]
    if not os.path.isdir(folder):
        raise JobFailed("The install folder to build from is gone; upload or choose it again.")
    region, product = job["region"], job["product"]
    kw = unprivileged(r.cfg.core, "The .polpatch build", "The .polpatch builder")
    home = scratch(r.cfg, job["id"] + "-build", kw)
    try:
        out = os.path.join(home, "%s-%s.polpatch" % (region, product))
        args = ["build", "--source", folder, "--region", region, "--product", product,
                "--out", out, "--version", job.get("_version") or "auto"]
        if job.get("_walk"):
            args.append("--walk")
        for a in job.get("_aliases") or []:
            args += ["--alias", a]
        res = patchtool(r, kw, home, args)
        if not res.get("ok") or not os.path.isfile(out):
            raise JobFailed(problems_sentence(res, "Building the .polpatch failed; the log "
                                                   "has the toolkit's output."), r.last_rc or 1)
        return patch_install(r, job, dep, out, "importing the file just built,")
    finally:
        shutil.rmtree(home, ignore_errors=True)


def act_patch_fetch(r, job, dep):
    """Bring one bundle to the patch feed's newest version: plan from a fresh
    listing, download every file with its size and SHA-256 checked, apply
    each as patch_import would, then restart the patch service once."""
    lib = patch_lib(r)
    region, product = job["region"], job["product"]
    feed, refused = dep.feed_client()
    if refused:
        raise JobFailed(refused[1]["error"])
    try:
        listing = feed.bundles()
    except patchfeed.FeedError as exc:
        raise JobFailed(str(exc))
    versions = listing.get((region, product))
    if not versions:
        raise JobFailed("The patch feed has nothing for %s/%s." % (region, product))
    bdir = lib.find_bundle(r.cfg.mirrors, region, product)
    have = dep.bundles.describe(lib, bdir)["versions"] if bdir else []
    p = patchfeed.plan(versions, have)
    if p["status"] in ("current", "ahead"):
        return "ok", "%s/%s already has %s; there was nothing to fetch." % (
            region, product, have[-1]), []
    if not p["steps"]:
        raise JobFailed(p["why"])
    r.log.line("fetching %d file(s) for %s/%s, %d bytes in all: %s"
               % (len(p["steps"]), region, product, p["size"],
                  ", ".join("%s (%s)" % (v, n) for v, n, _ in p["steps"])))
    home = os.path.join(r.cfg.state, "feed", job["id"])
    shutil.rmtree(home, ignore_errors=True)
    os.makedirs(home, mode=0o755)
    os.chmod(home, 0o755)
    try:
        files = []
        for i, (v, name, want) in enumerate(p["steps"]):
            dest = os.path.join(home, "%02d-%s" % (i, name))
            r.log.line("downloading %s %s (%d bytes)" % (v, name, want["size"]))
            try:
                feed.download(region, product, v, name, want, dest, r.cfg.upload_max)
            except patchfeed.FeedError as exc:
                raise JobFailed(str(exc))
            os.chmod(dest, 0o644)
            files.append(dest)
        for path in files:
            _, _, summary, _ = patch_apply(r, job, dep, path, "installing from the patch feed,")
            r.log.line(summary)
    finally:
        shutil.rmtree(home, ignore_errors=True)
    dep.feed_cache = None
    done = "Fetched %s/%s %s from the patch feed (%d file(s))." % (
        region, product, p["latest"], len(p["steps"]))
    return patch_finish(r, dep, region, product, done)


def the_bundle(r, lib, region, product):
    bdir = lib.find_bundle(r.cfg.mirrors, region, product)
    if bdir is None:
        raise JobFailed("There is no %s/%s bundle in the mirrors folder." % (region, product))
    return bdir


def act_patch_remove(r, job, dep):
    lib = patch_lib(r)
    region, product = job["region"], job["product"]
    bdir = the_bundle(r, lib, region, product)
    versions = dep.bundles.describe(lib, bdir)["versions"]
    trash = os.path.join(r.cfg.state, "patch-trash")
    os.makedirs(trash, exist_ok=True)
    dest = os.path.join(trash, "%s-%s" % (patches.stamp(), os.path.basename(bdir)))
    r.log.line("moving %s (version(s) %s) to %s" % (bdir, ", ".join(versions) or "none", dest))
    shutil.move(bdir, dest)
    r.log.line("moved; the bundle's files are in %s" % dest)
    dep.checks.drop(region, product)
    cut = restart_patch(r)
    return "ok", ("Removed the %s/%s bundle (%s). Its files are in patch-trash/%s in the "
                  "deployer's volume; delete them there when you no longer need them.%s"
                  % (region, product, ", ".join(versions) or "no versions",
                     os.path.basename(dest), cut)), []


def act_patch_rollback(r, job, dep):
    lib = patch_lib(r)
    mirrors = r.cfg.mirrors
    region, product, to = job["region"], job["product"], job["to"]
    bdir = the_bundle(r, lib, region, product)
    try:
        versions = lib.bundle_info(bdir)["versions"]
    except lib.PolpatchError as exc:
        raise JobFailed(str(exc))
    if to not in versions:
        raise JobFailed("%s/%s does not serve %s; its versions are %s."
                        % (region, product, to, ", ".join(versions)))
    if to == versions[-1]:
        raise JobFailed("%s is already the latest version of %s/%s; there is nothing to roll back."
                        % (to, region, product))
    drop = lib.versions_after(versions, to)
    r.log.line("rolling %s/%s back to %s removes %d version(s), oldest to newest: %s"
               % (region, product, to, len(drop), ", ".join(drop)))
    with open(os.path.join(bdir, "patch.cfg"), "rb") as f:
        kept = [x for x in lib.polbundle.parse_cfg(f.read()) if x["version"] not in set(drop)]
    stuck = patches.blobs_of(kept, drop)
    if stuck:
        raise JobFailed("%s/%s cannot be rolled back to %s: %s, which stays, is stored in the "
                        "folder of a version that would be removed." % (region, product, to, stuck[0]))
    stage_root = tempfile.mkdtemp(prefix=".polpatch-stage-rollback-", dir=mirrors)
    stamp = patches.stamp()
    try:
        stage = os.path.join(stage_root, "lists")
        os.makedirs(stage)
        try:
            how, src = patches.rollback_lists(lib, mirrors, bdir, to, drop, stage)
        except (lib.PolpatchError, OSError, ValueError) as exc:
            raise JobFailed("The list files for %s could not be made: %s" % (to, exc))
        if how == "backup":
            r.log.line("the list files come from the copy kept before %s was added: %s"
                       % (drop[0], src))
        else:
            r.log.line("no kept copy of the list files matches %s, so they were rebuilt from "
                       "the current ones without the removed rows" % to)
        backup = os.path.join(mirrors, patches.BACKUP_DIR, os.path.basename(bdir),
                              "%s-before-rollback-to-%s" % (stamp, to))
        os.makedirs(backup)
        names = patches.LIST_FILES + ("manifest.json",)
        for n in names:
            if os.path.exists(os.path.join(bdir, n)):
                shutil.copy2(os.path.join(bdir, n), os.path.join(backup, n))
        r.log.line("the current list files are kept in %s" % backup)
        for n in names:
            os.replace(os.path.join(stage, n), os.path.join(bdir, n))
        chown_to([os.path.join(bdir, n) for n in names], bdir)
        chown_to([os.path.join(mirrors, patches.BACKUP_DIR)], mirrors)
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)
    trash = os.path.join(r.cfg.state, "patch-trash",
                         "%s-%s-after-%s" % (stamp, os.path.basename(bdir), to))
    for v in drop:
        src = os.path.join(bdir, "blobs", v)
        if os.path.isdir(src):
            os.makedirs(os.path.join(trash, "blobs"), exist_ok=True)
            shutil.move(src, os.path.join(trash, "blobs", v))
            r.log.line("removed %s: its blobs are in %s" % (v, os.path.join(trash, "blobs", v)))
        else:
            r.log.line("removed %s (it had no blobs folder)" % v)
    done = ("Rolled %s/%s back to %s, removing %s. Their blobs are in patch-trash/%s in the "
            "deployer's volume." % (region, product, to, ", ".join(drop), os.path.basename(trash)))
    return patch_finish(r, dep, region, product, done)


def act_patch_check(r, job, dep):
    lib = patch_lib(r)
    dirs = patches.bundle_dirs(r.cfg.mirrors)
    if not dirs:
        return "ok", "There are no patch bundles to check.", []
    bad = []
    for name, bdir in dirs:
        info = dep.bundles.describe(lib, bdir)
        if info["error"]:
            bad.append(info["error"])
            continue
        r.log.line("checking %s/%s (%s)" % (info["region"], info["product"], info["title"]))
        good, why = selfcheck(r, dep, bdir, info, wait=5)
        if not good:
            bad.append(why)
    if bad:
        return "failed", "%d of %d bundle(s) failed: %s" % (len(bad), len(dirs), bad[0]), []
    return "ok", "All %d bundle(s) are served and pass the self-check." % len(dirs), []


PATCH_ACT = {"patch_import": act_patch_import, "patch_build": act_patch_build,
             "patch_remove": act_patch_remove, "patch_rollback": act_patch_rollback,
             "patch_check": act_patch_check, "patch_fetch": act_patch_fetch}


def hide_paths(text, cfg):
    """A sentence for an answer, with the deployer's own folders named
    instead of spelled out (the job log keeps the full paths)."""
    if not isinstance(text, str):
        return text
    pairs = []
    for path, label in ((cfg.mirrors, "mirrors"), (cfg.clients, "clients"),
                        (cfg.state, "deployer-volume"), (cfg.root, "checkouts")):
        if path:
            for p in {path, os.path.realpath(path), os.path.abspath(path)}:
                pairs.append((p, label))
    for p, label in sorted(pairs, key=lambda x: -len(x[0])):
        text = text.replace(p + os.sep, label + "/").replace(p + "/", label + "/").replace(p, label)
    return text


def id_error(region, product):
    if not patches.ok_region(region):
        return "region must be three capital letters or digits, such as W2U (got %r)" % (region,)
    if not patches.ok_product(product):
        return "product must be four digits, such as 0002 (got %r)" % (product,)
    return None


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
        self.secrets = {}             # job id -> (the access code a job uses, more to blank)
        self.install_id = devchannel.InstallId(cfg.state).get()
        self.issuers = devchannel.IssuerStore(cfg.state)
        self.code_state = None        # {"state", "why", "at"}: the access code's last check
        self.code_checking = False
        self.games_cache = (0.0, None)
        self.uploads = clientdata.Uploads(cfg.state)
        self.checks = patches.Checks(cfg.state)
        self.bundles = patches.BundleCache()
        self.manifests = patches.ManifestCache()
        self.feed_cache = None       # (time, answer) of the last GET /api/patches/feed
        shutil.rmtree(os.path.join(cfg.state, "imports"), ignore_errors=True)
        for uid in self.uploads.expire():
            say("upload %s expired and was removed" % uid)
        self.updates = Updates(self)

    def busy(self):
        return self.current

    def expire_uploads(self):
        self.uploads.expire(keep=[self.current_upload] if self.current_upload else [])

    def start(self, action, game, extra=None, secret=None, also=()):
        """(202, {"job"}) or (409, {"job": running id}). `secret` (an access code) is
        handed to the job in memory; it is never part of the job's file. `also`: more
        strings its log blanks (the short dev code the access code was redeemed with)."""
        upload = ((extra or {}).get("source") or {}).get("upload")
        with self.lock:
            if self.current:
                return 409, {"job": self.current, "error": "another job is running"}
            job = self.store.create(action, game, extra)
            self.current = job["id"]
            self.current_game = game
            self.current_upload = upload
            if secret:
                self.secrets[job["id"]] = (secret, tuple(a for a in also if a))
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
                if meta.get("kind") == "polpatch":
                    return 400, {"error": "upload %s is a .polpatch, not a client archive; "
                                          "install it with patch_import" % meta["id"]}
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

    # ---- patch bundles

    def toolkit(self):
        """(polpatch_lib, None) or (None, (409, answer))."""
        try:
            return patches.toolkit(self.cfg.core), None
        except patches.ToolkitMissing as exc:
            return None, (409, {"error": str(exc)})

    def patch_request(self, body):
        """(code, answer) for POST /api/actions with a patch action."""
        action = body["action"]
        if not self.cfg.mirrors:
            return 409, {"error": self.cfg.mirrors_note}
        lib, refused = self.toolkit()
        if refused:
            return refused
        if action == "patch_check":
            return self.start("patch_check", None)
        if action == "patch_fetch":
            region, product = body.get("region"), body.get("product")
            err = id_error(region, product)
            if err:
                return 400, {"error": err}
            feed, refused = self.feed_client()
            if refused:
                return refused
            return self.start("patch_fetch", None, {"region": region, "product": product})
        if action == "patch_import":
            code, obj = self.patch_import_request(body, lib)
        elif action == "patch_build":
            code, obj = self.patch_build_request(body, lib)
        else:
            code, obj = self.patch_bundle_request(action, body, lib)
        if "error" in obj:
            obj["error"] = hide_paths(obj["error"], self.cfg)
        return code, obj

    def source_upload(self, src):
        meta = self.uploads.load(src["upload"]) if isinstance(src["upload"], str) else None
        if not meta:
            return None, (400, {"error": "there is no upload %r; it may have expired"
                                % (src["upload"],)})
        return meta, None

    def patch_import_request(self, body, lib):
        src = body.get("source")
        if not isinstance(src, dict) or set(src) not in ({"upload"}, {"path"}):
            return 400, {"error": "source must be {\"upload\": id} or {\"path\": a .polpatch "
                                  "file under POL_DEPLOY_CLIENTS}"}
        if "upload" in src:
            meta, refused = self.source_upload(src)
            if refused:
                return refused
            if meta.get("kind") != "polpatch":
                return 400, {"error": "upload %s is a client archive, not a .polpatch; upload "
                                      "the .polpatch file itself" % meta["id"]}
            path = os.path.join(self.uploads.tree(meta["id"]), POLPATCH_UPLOAD)
            shown = {"upload": meta["id"]}
        else:
            if not self.cfg.clients:
                return 409, {"error": self.cfg.clients_note}
            try:
                path, rel = clientdata.resolve_under(self.cfg.clients, src["path"])
            except clientdata.PathError as exc:
                return 400, {"error": "the .polpatch to import is refused: %s" % exc}
            if not rel.lower().endswith(".polpatch") or not os.path.isfile(path):
                return 400, {"error": "%s is not a .polpatch file" % (rel or "the top")}
            shown = {"path": rel}
        try:
            m = lib.read_manifest(path)
        except lib.PolpatchError as exc:
            return 400, {"error": str(exc)}
        return self.start("patch_import", None, {"source": shown, "region": m["region"],
                                                 "product": m["product"], "_file": path})

    def patch_build_request(self, body, lib):
        region, product = body.get("region"), body.get("product")
        err = id_error(region, product)
        if err:
            return 400, {"error": err}
        try:
            lib.check_region(region)
            lib.check_product(product)
        except lib.PolpatchError as exc:
            return 400, {"error": str(exc)}
        version = body.get("version", "auto")
        if version != "auto" and not patches.ok_version(version):
            return 400, {"error": "version must look like 20040714_0 (YYYYMMDD_X) or be auto "
                                  "(got %r)" % (version,)}
        walk = body.get("walk", False)
        if not isinstance(walk, bool):
            return 400, {"error": "walk must be true or false"}
        aliases = body.get("aliases", [])
        if not (isinstance(aliases, list) and len(aliases) <= 8
                and all(patches.ok_region(a) for a in aliases)):
            return 400, {"error": "aliases must be a list of region codes such as W20"}
        src = body.get("source")
        if not isinstance(src, dict) or len(set(src) & {"upload", "path"}) != 1 \
                or set(src) - {"upload", "path", "subdir"} \
                or ("subdir" in src and "upload" not in src):
            return 400, {"error": "source must be {\"upload\": id, \"subdir\": folder} or "
                                  "{\"path\": folder under POL_DEPLOY_CLIENTS}"}
        try:
            if "upload" in src:
                meta, refused = self.source_upload(src)
                if refused:
                    return refused
                if meta.get("kind") == "polpatch":
                    return 400, {"error": "upload %s is a .polpatch; install it with "
                                          "patch_import" % meta["id"]}
                root = os.path.realpath(self.uploads.tree(meta["id"]))
                folder, rel = clientdata.resolve_under(root, src.get("subdir") or "")
                shown = {"upload": meta["id"], "subdir": rel}
            else:
                if not self.cfg.clients:
                    return 409, {"error": self.cfg.clients_note}
                folder, rel = clientdata.resolve_under(self.cfg.clients, src["path"])
                shown = {"path": rel}
        except clientdata.PathError as exc:
            return 400, {"error": "the folder to build from is refused: %s" % exc}
        if not os.path.isdir(folder):
            return 400, {"error": "%s is not a folder" % (rel or "the top")}
        return self.start("patch_build", None, {"region": region, "product": product,
                                                "source": shown, "_input": folder,
                                                "_version": version, "_walk": walk,
                                                "_aliases": aliases})

    def patch_bundle_request(self, action, body, lib):
        region, product = body.get("region"), body.get("product")
        err = id_error(region, product)
        if err:
            return 400, {"error": err}
        bdir = lib.find_bundle(self.cfg.mirrors, region, product)
        if not bdir:
            return 404, {"error": "there is no %s/%s bundle in the mirrors folder"
                                  % (region, product)}
        if action == "patch_remove":
            return self.start("patch_remove", None, {"region": region, "product": product})
        to = body.get("to")
        if not patches.ok_version(to):
            return 400, {"error": "to must be a version such as 20040714_0 (YYYYMMDD_X), "
                                  "got %r" % (to,)}
        info = self.bundles.describe(lib, bdir)
        versions = info["versions"]
        if info["error"]:
            return 409, {"error": info["error"]}
        if to not in versions:
            return 400, {"error": "%s/%s does not serve %s; its versions are %s"
                                  % (region, product, to, ", ".join(versions))}
        if to == versions[-1]:
            return 400, {"error": "%s is already the latest version of %s/%s, so there is "
                                  "nothing to roll back" % (to, region, product)}
        return self.start("patch_rollback", None, {"region": region, "product": product,
                                                   "to": to,
                                                   "removes": lib.versions_after(versions, to)})

    def patch_list(self, recheck=False):
        """(code, answer) for GET /api/patches."""
        if not self.cfg.mirrors:
            return 409, {"error": self.cfg.mirrors_note}
        lib, refused = self.toolkit()
        if refused:
            return refused
        infos = [self.bundles.describe(lib, p) for _, p in patches.bundle_dirs(self.cfg.mirrors)]
        up = [False] * len(infos)

        def ask(i):
            if not infos[i]["error"]:
                up[i] = patches.served(self.cfg.patch_host, self.cfg.patch_offset, infos[i])
        threads = [threading.Thread(target=ask, args=(i,), daemon=True) for i in range(len(infos))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(patches.PROBE_TIMEOUT * 3)
        rows = []
        for i, info in enumerate(infos):
            row = {k: info[k] for k in ("region", "product", "title", "aliases", "versions",
                                        "latest")}
            row["served"] = bool(up[i])
            row.update(self.checks.get(info["region"], info["product"], info["versions"]))
            row["size"] = info["size"]
            if info["error"]:
                row["error"] = hide_paths(info["error"], self.cfg)
            rows.append(row)
        rows.sort(key=lambda x: (x["region"], x["product"]))
        out = {"bundles": rows}
        if recheck:
            code, j = self.start("patch_check", None)
            out["job"] = j["job"]
            if code == 409:
                out["error"] = "another job is running; ask again when it ends"
            return code, out
        return 200, out

    def feed_candidate(self):
        """The code turning patches on would use with none typed: the patch store's
        own, else the dev channel's when it opens a feed. None when there is none."""
        for code in (patchfeed.Store(self.cfg.state).code(),
                     devchannel.Store(self.cfg.state).code()):
            if patchfeed.feed_of(code, self.cfg.dev_registry):
                return code
        return None

    def feed_info(self):
        """The parsed patch code when patches from the developers are on, else
        (None, (code, answer)). Independent of the dev channel: on works on Stable."""
        store = patchfeed.Store(self.cfg.state)
        if not store.enabled():
            return None, (409, {"error": "Patches from the developers are off.", "feed": False,
                                "enabled": False, "can_enable": self.feed_candidate() is not None})
        info = patchfeed.feed_of(store.code(), self.cfg.dev_registry)
        if not info:
            return None, (409, {"error": "The kept access code does not open the patches any "
                                         "more; turn them on again with a code that includes "
                                         "patches.", "feed": False, "enabled": False,
                                "can_enable": False})
        return info, None

    def feed_set(self, body):
        """POST /api/patches/feed: {"enabled": false} turns patches off (the code is
        kept, unless "forget": true); {"enabled": true, "code"?} turns them on with the
        code given, or the kept one, after the feed has answered to it."""
        store = patchfeed.Store(self.cfg.state)
        on = body.get("enabled")
        if not isinstance(on, bool):
            return 400, {"error": "Say whether patches are on: {\"enabled\": true or false}."}
        if not on:
            store.set_enabled(False, now_iso())
            if body.get("forget") is True:
                store.forget()
            self.feed_cache = None
            return 200, {"feed": False, "enabled": False,
                         "can_enable": self.feed_candidate() is not None}
        code = body.get("code")
        if code is not None and (not isinstance(code, str) or len(code) > 2048):
            return 400, {"error": "The access code is up to 2048 characters."}
        code = (code or "").strip() or self.feed_candidate()
        if not code:
            return 400, {"error": "Type an access code that includes patches."}
        info = patchfeed.feed_of(code, self.cfg.dev_registry)
        if not info:
            return 400, {"error": "That access code does not include patches; ask the "
                                  "developers for one that does."}
        try:
            listing = patchfeed.Feed(info["feed"], info["token"], self.cfg.dev_user,
                                     self.cfg.dev_scheme).bundles()
        except patchfeed.FeedError as exc:
            return 502, {"error": str(exc)}
        store.save_code(code)
        store.set_enabled(True, now_iso())
        self.feed_cache = (time.time(), listing)
        say("patches from the developers turned on (%d bundle(s) in the feed)" % len(listing))
        return 200, {"feed": True, "enabled": True, "bundles": len(listing)}

    def feed_host(self):
        info = patchfeed.feed_of(patchfeed.Store(self.cfg.state).code(), self.cfg.dev_registry)
        return info["feed"].split("/", 1)[0] if info else None

    def feed_client(self):
        info, refused = self.feed_info()
        if refused:
            return None, refused
        return patchfeed.Feed(info["feed"], info["token"], self.cfg.dev_user,
                              self.cfg.dev_scheme), None

    def feed_list(self, refresh=False):
        """(code, answer) for GET /api/patches/feed: each bundle the feed has,
        its newest version, the one this server has, and what fetching it takes."""
        if not self.cfg.mirrors:
            return 409, {"error": self.cfg.mirrors_note}
        lib, refused = self.toolkit()
        if refused:
            return refused
        feed, refused = self.feed_client()
        if refused:
            return refused
        hit = self.feed_cache
        if hit and not refresh and time.time() - hit[0] < FEED_TTL:
            listing = hit[1]
        else:
            try:
                listing = feed.bundles()
            except patchfeed.FeedError as exc:
                return 502, {"error": str(exc)}
            self.feed_cache = (time.time(), listing)
        rows = []
        for (region, product), versions in sorted(listing.items()):
            bdir = lib.find_bundle(self.cfg.mirrors, region, product)
            have = self.bundles.describe(lib, bdir)["versions"] if bdir else []
            p = patchfeed.plan(versions, have)
            rows.append({"region": region, "product": product,
                         "title": lib.title_for(region, product),
                         "latest": p["latest"], "have": have[-1] if have else None,
                         "status": p["status"], "files": len(p["steps"]),
                         "size": p["size"], "why": p["why"]})
        return 200, {"feed": True, "enabled": True, "bundles": rows}

    def describe_polpatch(self, path):
        lib, refused = self.toolkit()
        if refused:
            return {"error": refused[1]["error"]}
        out = self.manifests.describe(lib, path)
        if "error" in out:
            out = {"error": hide_paths(out["error"], self.cfg)}
        return out

    def is_polpatch(self, path, name):
        if name.lower().endswith(".polpatch"):
            return True
        if clientdata.sniff(path) != "zip":
            return False
        import zipfile
        try:
            with zipfile.ZipFile(path) as zf:
                return "polpatch.json" in zf.namelist()
        except (zipfile.BadZipFile, OSError, ValueError, RuntimeError, NotImplementedError):
            return False

    def quick_verify(self, uid, path):
        """None when the .polpatch at path passes the quick check, run as
        an unprivileged user; else (code, answer)."""
        try:
            kw = unprivileged(self.cfg.core, "The .polpatch check", "The .polpatch check")
        except JobFailed as exc:
            return 409, {"error": exc.summary}
        home = scratch(self.cfg, "upload-" + uid, kw)
        res = os.path.join(home, "result.json")
        argv = [self.cfg.python, PATCHTOOL, toolkit_dir(self.cfg), "verify", path, "--quick",
                "--result", res]
        try:
            subprocess.run(argv, cwd=self.cfg.core, env=import_env(home),
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=VERIFY_TIMEOUT, **kw)
            with open(res, encoding="utf-8") as f:
                out = json.loads(f.read(1 << 20))
            if not isinstance(out, dict):
                raise ValueError(out)
        except subprocess.TimeoutExpired:
            return 400, {"error": "Checking the .polpatch took longer than %d seconds."
                         % VERIFY_TIMEOUT}
        except (OSError, ValueError):
            return 400, {"error": "The .polpatch check stopped without an answer."}
        finally:
            shutil.rmtree(home, ignore_errors=True)
        if out.get("ok"):
            return None
        probs = [hide_paths(p, self.cfg) for p in out.get("problems") or []
                 if isinstance(p, str)][:10]
        return 400, {"error": problems_sentence({"problems": probs},
                                                "The .polpatch did not pass its check."),
                     "problems": probs}

    def _run(self, job):
        secret, also = self.secrets.pop(job["id"], (None, ()))
        log = JobLog(self.store, job, self.cfg.token,
                     secrets=[secret, devchannel.Store(self.cfg.state).code(),
                              patchfeed.Store(self.cfg.state).code()] + list(also),
                     hide=[job.get("registry"), devchannel.read_channel(self.cfg.core).get("registry"),
                           self.cfg.dev_registry, self.feed_host()])
        r = Runner(self.cfg, load_catalog(self.cfg.core), log)
        r.on_check = self.note_code
        try:
            job["state"] = "running"
            job["started"] = now_iso()
            self.store.save(job)
            target = job["game"] or "/".join(job[k] for k in ("region", "product")
                                             if job.get(k)) or "every bundle"
            log.line("deployer %s: %s %s%s%s" % (VERSION, job["action"], target,
                                                  " (published images)" if self.cfg.ghcr else "",
                                                  ", on Dev: " + ", ".join(r.channel["dev"])
                                                  if r.dev() else ""))
            if job["action"] == "channel":
                state, summary, missing = act_channel(r, job, secret)
            elif job["action"] == "updater":
                state, summary, missing = act_updater(r, job, secret)
            elif job["action"] in PATCH_ACTIONS:
                state, summary, missing = PATCH_ACT[job["action"]](r, job, self)
            elif job["action"] == "import":
                log.line("importing %s from %s" % (job["item"], json.dumps(job["source"])))
                state, summary, missing = act_import(r, job["game"], job)
            else:
                if job["action"] in COMPOSING:
                    r.paths_guard()        # before stack.py enable or git pull change anything
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
        if job["action"] in PATCH_ACTIONS:
            summary = hide_paths(summary, self.cfg)
        job.update(state=state, summary=summary, missing=missing, ended=now_iso())
        upload = (job.get("source") or {}).get("upload")
        if upload:
            self.uploads.touch(upload, job["ended"])
        # what runs now changed: check that item again (its checkout was just fetched),
        # counted as running before anyone can see the job finished
        if job["action"] in ("install", "update") and state in ("ok", "needs_data"):
            self.updates.kick(keys=[job["game"]], fetch=False)
        elif job["action"] == "channel":
            self.updates.kick(fetch=False)
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
        fills = read_fills(self.cfg.state)
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
                "missing": [fill_note(missing_entry(catalog, key, m, required=True), m, fills)
                            for m in (c or {}).get("missing", [])],
                "admin_env": (["POL_ADMIN_GAME_%s_URL" % mod.upper(),
                               "POL_ADMIN_GAME_%s_TOKEN" % mod.upper()] if mod else []),
                "ports": e.get("ports", []),
                "notes": e.get("notes", ""),
            })
        out = {"games": games,
               "core": {"running": ps["core"]["running"] if ps_ok else None,
                        "services": ps["core"].get("services", {}) if ps_ok else {}},
               "options": {"ghcr": self.cfg.ghcr,
                           "channel": devchannel.read_channel(self.cfg.core)["channel"],
                           "problem": self.cfg.paths_note if self.cfg.paths == "wrong" else None}}
        if not report:
            out["error"] = "stack.py check did not answer; data_ok is unknown"
        if not ps_ok:
            out["running_error"] = ps.get("error") or "docker compose ps did not answer"
        with self.lock:
            self.games_cache = (time.time(), out)
        return out

    # ---- the dev builds (docs/deployer.md, "Dev channel")

    #: what GET /api/channel says of each image; never its registry or its source address
    CHANNEL_ROW = ("key", "name", "stable", "dev_name", "applies", "on", "what", "license", "note")
    #: how long a check of the access code holds for the panel's list
    CODE_FRESH = 300

    def note_code(self, res):
        """Keep what the registry last said of the stored access code (check_access)."""
        if isinstance(res, dict) and res.get("state"):
            self.code_state = {"state": res["state"], "why": res.get("why"), "at": time.time()}

    def code_refresh(self, ch, rows, code):
        """Ask the registry about the stored code in the background when the last answer is
        older than CODE_FRESH, so the list says when it stopped working."""
        st = self.code_state
        if not rows or not code or self.code_checking or (st and time.time() - st["at"] < self.CODE_FRESH):
            return
        self.code_checking = True

        def run():
            try:
                self.note_code(devchannel.check_access(ch["registry"], devchannel.token_of(code),
                                                       devchannel.probes_of(rows),
                                                       scheme=self.cfg.dev_scheme, user=self.cfg.dev_user))
            finally:
                self.code_checking = False
        threading.Thread(target=run, daemon=True, name="code-check").start()

    def issuer(self):
        """(where short dev codes are redeemed, or None when they are off; "env", "state" or
        "default")."""
        return devchannel.issuer_of(self.cfg.dev_issuer, self.issuers.get())

    def channel_rows(self):
        catalog = load_catalog(self.cfg.core)
        ch = devchannel.read_channel(self.cfg.core, catalog)
        installed = [k for k, e in catalog.items() if e["kind"] == "game"
                     and os.path.isdir(os.path.join(self.cfg.root, e["checkout"]))]
        return catalog, ch, installed, devchannel.image_rows(catalog, ch, self.cfg.ghcr, keys=installed,
                                                             registry=None)

    def channel_info(self):
        """GET /api/channel: whether the dev builds are unlocked, each thing that has a dev
        build (the core and the installed games, "keys") and whether it is on Dev, and what
        each image of them is, by short name. Never the access code, and never the registry's
        address: the panel says "dev builds". A row on Dev whose source is known says
        has_source; GET /api/channel/source gives the address."""
        catalog, ch, installed, rows = self.channel_rows()
        on = Runner(self.cfg, catalog, None).enabled()
        code = devchannel.Store(self.cfg.state).code() if ch["unlocked"] else None
        shown = []
        for row in rows:
            one = {k: row.get(k) for k in self.CHANNEL_ROW}
            one["enabled"] = row["key"] == "core" or row["key"] in on
            one["has_source"] = bool(row["on"] and row.get("dev_name") and row.get("source"))
            shown.append(one)
        swapped = devchannel.swapped(rows)
        self.code_refresh(ch, swapped, code)
        st = self.code_state
        code_ok = None
        if ch["unlocked"]:
            if not code:
                code_ok = False
            elif st:
                code_ok = False if st["state"] in ("wrong", "denied") else \
                    (True if st["state"] == "ok" else None)
        have = ["core"] + installed
        keys = [k for k in devchannel.dev_keys(catalog) if k in have]
        notes = devchannel.fit_notes(catalog, ch["dev"], keys)
        key_rows = []
        for k in keys:
            e = catalog[k]
            mine = [r for r in rows if r["key"] == k and r["dev_name"]]
            dev = k in ch["dev"]
            key_rows.append({
                "key": k, "name": e.get("name") or k, "on": "dev" if dev else "stable",
                "since": ch["dev_since"].get(k) if dev else None,
                "enabled": k == "core" or k in on,
                "applies": any(r["applies"] for r in mine),
                "builds_on_core": k != "core" and devchannel.builds_on_core(e),
                "images": [r["dev_name"] for r in mine],
                "services": key_services(self.cfg, catalog, on, k),
                "note": notes.get(k),
                "problem": devchannel.REVOKED if dev and code_ok is False else None,
                "would": devchannel.switch_notes(catalog, ch["dev"], keys, k,
                                                 "stable" if dev else "dev"),
                "back": " ".join(sorted(set(d["back"] for d in devchannel.dev_entries(e)
                                            if isinstance(d.get("back"), str)))) or None})
        issuer, where = self.issuer()
        return {"unlocked": ch["unlocked"], "channel": ch["channel"], "dev": list(ch["dev"]),
                "since": ch.get("since") if ch["unlocked"] else None,
                "code_stored": bool(code), "code_ok": code_ok,
                "code_why": devchannel.REVOKED if code_ok is False else None,
                "ghcr": self.cfg.ghcr, "problem": ch.get("problem"), "keys": key_rows,
                "images": shown, "install_id": self.install_id, "short_codes": bool(issuer),
                "issuer": {"value": issuer or devchannel.ISSUER_OFF, "from": where,
                           "default": devchannel.DEFAULT_ISSUER}}

    def resolve_code(self, code):
        """(access code, the short code it came from or None) for what someone typed: a short
        dev code is redeemed at the issuer with this install's id, anything else is as it
        is. ChannelError (IssuerDown when the issuer did not answer) with the sentence. A
        redeemed code with a patch feed is offered to the patch store (kept when none is
        kept yet, never turned on)."""
        short = devchannel.short_code(code)
        if short is None:
            return code, None
        issuer, _where = self.issuer()
        got = devchannel.redeem(issuer, short, self.install_id)
        say("dev builds: short dev code redeemed at the issuer for this install")
        try:
            if patchfeed.offer_code(self.cfg.state, got):
                say("dev builds: the redeemed code opens a patch feed; kept for it (patches stay off)")
        except (OSError, ValueError, patchfeed.FeedError) as exc:
            say("dev builds: the redeemed code could not be kept for the patch feed (%s)" % exc)
        return got, short

    def channel_source(self, stable):
        """GET /api/channel/source?stable=<image>: (status, {"url"}) of the source of the dev
        image that replaces `stable`, while its key is on Dev only (the GPL and AGPL source
        offer)."""
        _catalog, ch, _inst, rows = self.channel_rows()
        if not ch["dev"]:
            return 404, {"error": "Nothing on this server is on Dev."}
        for row in rows:
            if row["stable"] == stable and row["on"] and row.get("dev_name") and row.get("source"):
                return 200, {"url": row["source"], "license": row.get("license")}
        return 404, {"error": "No dev image there has a source address."}

    def check_new_code(self, body, field):
        """(None, (code, info, short)) for the access code in body[field], or (status, answer)
        when it is refused: parsed, then asked of its registry for the dev images the
        installed things would run."""
        code = body.get(field)
        code = code.strip() if isinstance(code, str) else ""
        given = body.get("registry")
        given = given.strip() if isinstance(given, str) else ""
        if given and not devchannel.REGISTRY.match(given):
            return (400, {"error": "The dev registry must look like registry.example.org/team."}), None
        try:
            code, short = self.resolve_code(code)
            info = devchannel.parse_code(code, registry=given or self.cfg.dev_registry)
        except devchannel.IssuerDown as exc:
            return (502, {"error": str(exc)}), None
        except devchannel.ChannelError as exc:
            return (400, {"error": str(exc)}), None
        reg = info["registry"]
        catalog = load_catalog(self.cfg.core)
        rows = devchannel.image_rows(catalog, {"unlocked": True, "registry": reg, "dev": []},
                                     self.cfg.ghcr)
        installed = set(Updates.installed(self.updates, catalog))
        mine = [r for r in rows if r["key"] in installed]
        probes = devchannel.probes_of(mine) or devchannel.probes_of(rows)[:1]
        res = devchannel.check_access(reg, info["token"], probes, scheme=self.cfg.dev_scheme,
                                      user=self.cfg.dev_user)
        if res["state"] != "ok":
            say("dev builds: the access code was refused (%s)" % res["state"])
            return ((502 if res["state"] == "unreachable" else 400), {"error": res["why"]}), None
        self.note_code(res)
        return None, (code, info, short)

    def channel_request(self, body):
        """POST /api/channel: (status, answer). One of

            {"unlock": "<code>"}           check the access code (an olx1. or olx2. code, a short
                                           dev code, or, advanced, a plain token with "registry"
                                           or POL_DEV_REGISTRY) and unlock the dev builds with it
            {"key": "<key>", "to": "dev"|"stable"}   put one thing on Dev or Stable
            {"lock": true}                 forget the code, once everything is on Stable
            {"issuer": "<https address>"|"off"|""}   the issuer of short codes ("": the default)
            {"channel": "dev", "code"}     as before: unlock and put everything on Dev
            {"channel": "stable"}          as before: everything back on Stable, locked

        A refused code answers 400 with the sentence (502 when its registry or the issuer
        does not answer), before any job; a job answers 202."""
        fields = set(body)
        shapes = ({"unlock", "registry"}, {"key", "to"}, {"lock"}, {"issuer"},
                  {"channel", "code", "registry"})
        if not fields or not any(fields <= s for s in shapes) or \
                (fields & {"registry"} and not fields & {"unlock", "channel"}):
            return 400, {"error": "unknown fields: %s" % ", ".join(sorted(fields)) if fields
                         else "Say what to do: unlock, key and to, lock, or channel."}
        if "issuer" in body:
            return self.set_issuer(body["issuer"])
        if self.current:
            return 409, {"job": self.current, "error": "another job is running"}
        catalog = load_catalog(self.cfg.core)
        ch = devchannel.read_channel(self.cfg.core, catalog)
        if "channel" in body:
            want = body.get("channel")
            if want not in devchannel.CHANNELS:
                return 400, {"error": "channel must be stable or dev"}
            if want == "stable":
                return self.start("channel", "core", {"channel": "stable", "op": "stable"})
            bad, got = self.check_new_code(body, "code")
            if bad:
                return bad
            code, info, short = got
            extra = {"channel": "dev", "op": "all", "registry": info["registry"]}
            if info["source"]:
                extra["dev_source"] = info["source"]
            return self.start("channel", "core", extra, secret=code,
                              also=[short, short and devchannel.show_short(short)])
        if "unlock" in body:
            bad, got = self.check_new_code(body, "unlock")
            if bad:
                return bad
            code, info, short = got
            extra = {"op": "unlock", "registry": info["registry"]}
            if info["source"]:
                extra["dev_source"] = info["source"]
            return self.start("channel", "core", extra, secret=code,
                              also=[short, short and devchannel.show_short(short)])
        if "lock" in body:
            if body["lock"] is not True:
                return 400, {"error": "lock is true"}
            if not ch["unlocked"]:
                return 400, {"error": "The dev builds are not unlocked."}
            if ch["dev"]:
                return 409, {"error": "Switch everything back to Stable first; on Dev: %s." % ", ".join(
                    catalog[k]["name"] if k in catalog else k for k in ch["dev"])}
            return self.start("channel", "core", {"op": "lock"})
        key, to = body.get("key"), body.get("to")
        if to not in devchannel.CHANNELS:
            return 400, {"error": "to must be stable or dev"}
        if not isinstance(key, str) or key not in catalog or not devchannel.dev_entries(catalog[key]):
            return 400, {"error": "That has no dev build."}
        if key != "core" and key not in Updates.installed(self.updates, catalog):
            return 400, {"error": "%s is not installed." % catalog[key]["name"]}
        if to == ("dev" if key in ch["dev"] else "stable"):
            return 400, {"error": "%s is already on %s." % (catalog[key]["name"], to.capitalize())}
        if to == "dev":
            if not ch["unlocked"]:
                return 400, {"error": devchannel.LOCKED}
            code = devchannel.Store(self.cfg.state).code()
            if not code:
                return 400, {"error": devchannel.REVOKED}
            after = dict(ch, dev=sorted(set(ch["dev"]) | {key}))
            rows = [r for r in devchannel.swapped(devchannel.image_rows(catalog, after, self.cfg.ghcr,
                                                                       keys=[key])) if r["key"] == key]
            if rows:
                res = devchannel.check_access(ch["registry"], devchannel.token_of(code),
                                              devchannel.probes_of(rows),
                                              scheme=self.cfg.dev_scheme, user=self.cfg.dev_user)
                if res["state"] in ("wrong", "denied", "ok"):
                    self.note_code(res)
                if res["state"] in ("wrong", "denied"):
                    return 400, {"error": devchannel.REVOKED}
                if res["state"] != "ok":
                    return (502 if res["state"] == "unreachable" else 400), {"error": res["why"]}
        return self.start("channel", key, {"op": "switch", "key": key, "to": to})

    def set_issuer(self, value):
        """POST /api/channel {"issuer"}: the owner's issuer of short codes. Refused while
        POL_DEV_ISSUER sets it."""
        if self.cfg.dev_issuer:
            return 409, {"error": "POL_DEV_ISSUER sets the issuer on this server; change it there."}
        try:
            got = self.issuers.set(value)
        except devchannel.ChannelError as exc:
            return 400, {"error": str(exc)}
        except OSError as exc:
            return 500, {"error": "The setting could not be kept (%s)." % exc}
        issuer, where = self.issuer()
        say("dev builds: the issuer of short codes is now %s" % (got or "the default"))
        return 200, {"issuer": {"value": issuer or devchannel.ISSUER_OFF, "from": where,
                                "default": devchannel.DEFAULT_ISSUER}}

    def updater_start(self, body):
        """POST /api/updater: (status, answer). A switch to a channel other than stable
        without a code of its own gets the code the dev builds are unlocked with, if any."""
        bad, got = updater_request(body)
        if bad:
            return 400, {"error": bad}
        game, title, rec, channel, switch, code = got
        if self.current:
            return 409, {"job": self.current, "error": "another job is running"}
        short = None
        if code:
            try:
                code, short = self.resolve_code(code)
            except devchannel.IssuerDown as exc:
                return 502, {"error": str(exc)}
            except devchannel.ChannelError as exc:
                return 400, {"error": str(exc)}
        elif switch and channel not in (None, "stable") and \
                devchannel.read_channel(self.cfg.core)["unlocked"]:
            code = devchannel.Store(self.cfg.state).code()
        extra = {"title": title, "updater": rec}
        if channel is not None:
            extra["channel"] = channel
        if switch:
            extra["switch"] = True
        return self.start("updater", game, extra, secret=code,
                          also=[short, short and devchannel.show_short(short)])

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
                                            "busy": bool(dep.busy()), "paths": dep.cfg.paths,
                                            "problem": dep.cfg.paths_note
                                            if dep.cfg.paths == "wrong" else None})
            if parts == ["api", "games"]:
                return self.send_json(200, dep.games())
            if parts == ["api", "updates"]:
                return self.send_json(200, dep.updates.answer(
                    truthy((q.get("refresh") or [""])[0])))
            if parts == ["api", "channel"]:
                return self.send_json(200, dep.channel_info())
            if parts == ["api", "channel", "source"]:
                return self.send_json(*dep.channel_source((q.get("stable") or [""])[0]))
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
                    out = clientdata.browse(cfg.clients, rel, load_catalog(cfg.core),
                                            describe=dep.describe_polpatch)
                except clientdata.PathError as exc:
                    return self.send_json(400, {"error": "%s: %s" % (rel, exc)})
                except (FileNotFoundError, NotADirectoryError):
                    return self.send_json(404, {"error": "no folder %s under the clients folder"
                                                % rel})
                except PermissionError:
                    return self.send_json(403, {"error": "the deployer may not read %s" % rel})
                return self.send_json(200, out)
            if parts == ["api", "patches"]:
                recheck = (q.get("recheck") or ["0"])[0].strip().lower() in ("1", "true", "yes")
                code, obj = dep.patch_list(recheck)
                return self.send_json(code, obj)
            if parts == ["api", "patches", "feed"]:
                return self.send_json(*dep.feed_list(truthy((q.get("refresh") or [""])[0])))
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
                name = clientdata.clean_name(self.headers.get("X-Filename"))
                if dep.is_polpatch(part, name):
                    return self.store_polpatch(uid, part, tmp, n, name)
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

        def store_polpatch(self, uid, part, tmp, n, name):
            """Keep a .polpatch upload whole, as <id>/upload.polpatch, once
            its manifest reads and it passes the quick check."""
            lib, refused = dep.toolkit()
            if refused:
                return self.send_json(*refused)
            ups = dep.uploads
            os.makedirs(tmp, mode=0o755)
            os.chmod(tmp, 0o755)
            dest = os.path.join(tmp, POLPATCH_UPLOAD)
            os.replace(part, dest)
            os.chmod(dest, 0o644)
            try:
                m = lib.read_manifest(dest)
            except lib.PolpatchError as exc:
                return self.send_json(400, {"error": hide_paths(str(exc), cfg)})
            refused = dep.quick_verify(uid, dest)
            if refused:
                return self.send_json(*refused)
            os.replace(tmp, ups.tree(uid))
            t = time.time()
            meta = {"id": uid, "name": name, "created": now_iso(t), "last_import": None,
                    "size": n, "files": len(m["files"]), "archive_size": n, "matches": [],
                    "kind": "polpatch", "polpatch": patches.manifest_fields(m),
                    "_t": t, "_last": None}
            ups.save(meta)
            out = clientdata.public_upload(meta)
            out["upload"] = uid
            say("upload %s: a .polpatch for %s/%s, %d bytes" % (uid, m["region"], m["product"], n))
            return self.send_json(201, out)

        def do_POST(self):
            if not self.authed():
                self.close_connection = True
                return
            path = urllib.parse.urlsplit(self.path).path.rstrip("/")
            if path == "/api/uploads":
                return self.upload()
            if path not in ("/api/actions", "/api/channel", "/api/updater", "/api/patches/feed"):
                return self.send_json(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                n = -1
            if n < 0 or n > (UPDATER_BODY if path == "/api/updater" else MAX_BODY):
                self.close_connection = True
                return self.send_json(400, {"error": "the body must be a small JSON object"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self.send_json(400, {"error": "the body is not JSON"})
            if not isinstance(body, dict):
                return self.send_json(400, {"error": "the body must be a JSON object"})
            if path == "/api/channel":
                return self.send_json(*dep.channel_request(body))
            if path == "/api/patches/feed":
                return self.send_json(*dep.feed_set(body))
            if path == "/api/updater":
                # a paired game's updater, as the panel's owner approved it; checked again here
                return self.send_json(*dep.updater_start(body))
            action, game = body.get("action"), body.get("game")
            if action not in ACTIONS:
                return self.send_json(400, {"error": "unknown action %r; one of %s"
                                            % (action, ", ".join(ACTIONS))})
            if action in PATCH_ACTIONS:
                code, obj = dep.patch_request(body)
                return self.send_json(code, obj)
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
            h = json.load(r)
    except Exception:
        return 1
    if h.get("problem"):
        print(h["problem"])
        return 1
    print("paths %s" % h.get("paths"))
    return 0 if h.get("ok") else 1


def channel_cli(args):
    """--channel: hand the running deployer a channel switch, as the panel does, and
    follow the job. The access code comes from stdin (a hidden prompt on a terminal),
    so it is in no argument; setup.sh runs this with docker compose exec -T."""
    token = os.environ.get("POL_DEPLOYER_TOKEN", "").strip()
    base = "http://127.0.0.1:%s" % (args.bind.rpartition(":")[2] or "8099")
    hdrs = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    body = {"channel": args.channel}
    if args.channel == "dev":
        if sys.stdin.isatty():
            code = getpass.getpass("Access code: ")
        else:
            code = sys.stdin.readline()
        body["code"] = code.strip()
        if args.registry.strip():
            body["registry"] = args.registry.strip()

    def call(method, path, data=None, timeout=60):
        req = urllib.request.Request(base + path, method=method, headers=hdrs,
                                     data=None if data is None else json.dumps(data).encode())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.load(e)
            except ValueError:
                return e.code, {}
        except (OSError, ValueError) as exc:
            return None, {"error": "the deployer is not answering (%s)" % exc}

    code, out = call("POST", "/api/channel", body)
    body.pop("code", None)
    if code != 202:
        print(out.get("error") or "the deployer answered HTTP %s" % code)
        return 1
    print("switching to the %s channel (job %s)..." % (args.channel, out["job"]), flush=True)
    t = time.time()
    while time.time() - t < 3 * 3600:
        st, j = call("GET", "/api/jobs/" + out["job"])
        if st == 200 and j.get("state") not in ACTIVE:
            print(j.get("summary") or j.get("state"))
            return 0 if j.get("state") == "ok" else 1
        time.sleep(2)
    print("the job is still running; Settings > Install games follows it")
    return 1


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.healthcheck:
        return healthcheck(args)
    if args.channel:
        return channel_cli(args)
    cfg = make_config(args)
    cfg.container = args.container or own_container()
    cfg.paths, cfg.paths_note = check_paths(cfg, cfg.container)
    say(("paths: " if cfg.paths != "wrong" else "") + cfg.paths_note)
    dep = Deployer(cfg)
    srv = ThreadingHTTPServer((cfg.host, cfg.port), make_handler(dep))
    srv.daemon_threads = True
    dep.updates.start()

    def stop(*_):
        threading.Thread(target=srv.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    ch = devchannel.read_channel(cfg.core)
    say("version %s listening on %s:%d; checkouts in %s, core %s%s%s%s"
        % (VERSION, cfg.host, srv.server_address[1], cfg.root, cfg.core,
           ", published images" if cfg.ghcr else "",
           ", client folders in %s" % cfg.clients if cfg.clients else "",
           ", on Dev: " + ", ".join(ch["dev"]) if ch["dev"] else ""))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
