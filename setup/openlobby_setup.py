"""OpenLobby Setup: the setup program for an OpenLobby server.

    python setup/openlobby_setup.py            (or the built "OpenLobby Setup" program)
    python setup/openlobby_setup.py --help

The first run checks Docker, asks a few questions (where to install, who will
play, the address players connect to, the admin panel's user name), gets
OpenLobby, makes the shared `openlobby` Docker network and runs the setup
script that comes with OpenLobby (setup.ps1 on Windows, setup.sh elsewhere)
with those answers. Later runs show a menu: update, change settings, stop or
start, what to tell players, a check that consoles can reach the server, start
over, uninstall.

The work itself stays in setup.sh and setup.ps1, which a checkout already has
and docs/setup.md describes; this program asks the questions, runs the script
without questions, and turns its output into one line per step. The script runs
on its own, writing to a log in the install folder, so closing this window does
not stop it; the next run picks up its result.

Standard library only (Python 3.8+). Built into one file with PyInstaller
(setup/build.py). docs/installs.md, "A setup program like the others".
"""
import argparse
import datetime
import getpass
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

NAME = "OpenLobby Setup"
REPO_URL = "https://github.com/PrettyOpenLobby/OpenLobby.git"
ZIP_URL = "https://github.com/PrettyOpenLobby/OpenLobby/archive/refs/heads/main.zip"
RELEASES = "https://github.com/PrettyOpenLobby/OpenLobby/releases"
DOCKER_DESKTOP = "https://www.docker.com/products/docker-desktop/"
DOCKER_ENGINE = "https://docs.docker.com/engine/install/"
LOOKUP_URL = "https://api.ipify.org"
PANEL_URL = "http://127.0.0.1:8090"
NETWORK = "openlobby"
ROLE_LABEL = "org.openlobby.role"
FROZEN = getattr(sys, "frozen", False)
# False once Docker is found not answering (a dry run goes on without it)
DOCKER_UP = {"ok": True}

CHECKOUT_NAME = "openlobby"
STATE_FILE = "setup-state.json"
PLAYERS_FILE = "What to tell players.txt"
RUNNING_FILE = os.path.join("logs", "running.json")

REACH_TEXT = {"lan": "people on this network only",
              "vpn": "friends over a VPN (WireGuard, Tailscale, ZeroTier and the like)",
              "internet": "anyone over the internet"}

# The script's section headings, as the plain step names shown here.
STEPS = {
    "OpenLobby setup": None,
    "Checking Docker and git": "Checking Docker",
    "Checking git and Docker Desktop": "Checking Docker",
    "Getting OpenLobby": "Getting OpenLobby",
    "Who will play on this server?": None,
    "Settings (.env)": "Writing the settings",
    "Game data": "Getting the games' data (the first time can download several hundred MB)",
    "Starting the stack": "Starting OpenLobby (the first start downloads it, a few minutes)",
    "Admin panel password": "Setting the admin panel's password",
    "Windows Firewall": "Letting players through Windows Firewall (Windows may ask for administrator rights)",
    "Checking the setup (tools/doctor.py)": "Checking the setup",
    "This PC as a player": "Pointing this PC at the server",
    "Done": None,
}


def osname():
    """"windows", "mac" or "linux"."""
    if os.name == "nt":
        return "windows"
    return "mac" if sys.platform == "darwin" else "linux"


def version():
    """The release this program was built from ("dev" when run as a script)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(base, "setup-version.txt"), encoding="utf-8") as f:
            return f.read().strip() or "dev"
    except OSError:
        return "dev"


# --- terminal ----------------------------------------------------------------------------

class Answers:
    """--yes takes every default without asking."""
    yes = False
    closed = False


#: The update channel the setup script is run with (docs/deployer.md, "Dev channel"):
#: {"channel", "registry", "code_file"} ("registry" only for a plain token, advanced: an
#: access code carries its own). The access code is in code_file, a private
#: temporary file the script reads and this program removes; never on a command line.
CHANNEL = {}
REGISTRY_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]{1,5})?(/[a-z0-9]([a-z0-9._-]*[a-z0-9])?)*$")


def channel_argv(windows):
    """The setup script's channel options, from CHANNEL."""
    ch = CHANNEL.get("channel")
    if not ch:
        return []
    names = ("-Channel", "-Registry", "-CodeFile") if windows else ("--channel", "--registry", "--code-file")
    out = [names[0], ch]
    if ch == "dev":
        if CHANNEL.get("registry"):
            out += [names[1], CHANNEL["registry"]]
        if CHANNEL.get("code_file"):
            out += [names[2], CHANNEL["code_file"]]
        # the deployer switches the channel; an install from before it was offered has none yet
        out.append("-Deployer" if windows else "--deployer")
    return out


def prepare_channel(args):
    """Ask what the dev channel needs (the access code, hidden) and keep it in a private
    temporary file for the script. False when it cannot go ahead."""
    CHANNEL.clear()
    if not args.channel:
        return True
    CHANNEL["channel"] = args.channel
    if args.channel != "dev":
        return True
    reg = (args.registry or "").strip()
    if reg and not REGISTRY_RE.match(reg):
        say("The dev registry looks like registry.example.org/team (got %s)." % reg)
        return False
    CHANNEL["registry"] = reg
    if args.dry_run:
        return True
    if args.code_file:
        try:
            with open(args.code_file, encoding="utf-8") as f:
                code = f.readline().strip()
        except OSError as exc:
            say("Cannot read the access code file: %s" % exc)
            return False
    elif Answers.yes:
        say("--channel dev with --yes needs --code-file (the access code is not taken on the command line).")
        return False
    else:
        try:
            code = getpass.getpass("Access code (not shown): ").strip()
        except EOFError:
            code = ""
    if not code:
        say("No access code was given.")
        return False
    keep_code(code)
    return True


def keep_code(code):
    """The access code into a private temporary file for the script (CHANNEL["code_file"])."""
    d = tempfile.mkdtemp(prefix="openlobby-channel-")
    path = os.path.join(d, "code")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(code + "\n")
    CHANNEL["code_file"] = path


def forget_channel_code():
    path = CHANNEL.pop("code_file", None)
    if path:
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)


def say(text=""):
    print(text, flush=True)


def heading(text):
    say()
    say(text)
    say("-" * len(text))


def ask(question, default=""):
    shown = " [%s]" % default if default else ""
    if Answers.yes:
        say("%s%s: %s" % (question, shown, default))
        return default
    try:
        answer = input("%s%s: " % (question, shown)).strip()
    except EOFError:
        # nobody to answer (input closed): the default, and no asking again
        if Answers.closed:
            raise SystemExit("%s: no answer to: %s" % (NAME, question))
        Answers.closed = True
        answer = ""
    return answer or default


def ask_yes(question, default=True):
    answer = ask(question + (" (Y/n)" if default else " (y/N)")).lower()
    return default if not answer else answer.startswith("y")


def ask_choice(question, options, default=1):
    """Numbered options from 1; returns the 1-based pick."""
    if question:
        say(question)
    for i, text in enumerate(options, 1):
        say("  %d. %s" % (i, text))
    while True:
        answer = ask("Type a number", str(default))
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer)
        if Answers.yes:
            return default
        say("  Type one of the numbers above.")


def confirm(question, requested):
    """A yes/no whose default is no; an action asked for on the command line
    together with --yes counts as the answer."""
    if Answers.yes and requested:
        say("%s (y/N): y" % question)
        return True
    return ask_yes(question, False)


def step(text):
    say("  %s..." % text)


# --- running things ----------------------------------------------------------------------

def tool(name):
    """The command line that runs `name`, or None when it is not installed."""
    if name == "docker":
        add_docker_to_path()
    found = shutil.which(name)
    return [found] if found else None


def add_docker_to_path():
    """Docker installed while this window was open is not on its PATH yet."""
    places = [os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Docker", "Docker",
                           "resources", "bin"),
              "/usr/local/bin", "/Applications/Docker.app/Contents/Resources/bin",
              os.path.join(os.path.expanduser("~"), ".docker", "bin")]
    path = os.environ.get("PATH", "").split(os.pathsep)
    for p in places:
        if os.path.isdir(p) and p not in path:
            path.append(p)
    os.environ["PATH"] = os.pathsep.join(path)


def run(name, *args, cwd=None, timeout=300):
    """(exit code, output) of a tool; 127 when it is not installed."""
    argv = tool(name)
    if not argv:
        return 127, "%s is not installed" % name
    try:
        p = subprocess.run(argv + [str(a) for a in args], cwd=cwd, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
                           text=True, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return 124, "%s did not answer in %d seconds" % (name, timeout)
    except OSError as e:
        return 126, str(e)
    return p.returncode, p.stdout or ""


def docker(*args, **kw):
    return run("docker", *args, **kw)


# --- Docker ------------------------------------------------------------------------------

def docker_state():
    """"ok", "missing", "stopped", "denied" (a Linux user not in the docker group)
    or "old" (no Compose v2). Never starts anything."""
    if not tool("docker"):
        return "missing"
    code, out = docker("info", timeout=60)
    if code != 0:
        return "denied" if "permission denied" in out.lower() else "stopped"
    if docker("compose", "version")[0] != 0:
        return "old"
    return "ok"


def docker_help(state):
    """What to do about a Docker that is not ready, in plain sentences."""
    system = osname()
    if state == "missing":
        if system == "linux":
            return ["Docker is not installed. OpenLobby runs inside Docker.",
                    "Install Docker Engine and its compose plugin: %s" % DOCKER_ENGINE]
        return ["Docker Desktop is not installed. OpenLobby runs inside it.",
                "Download it here, install it and start it: %s" % DOCKER_DESKTOP,
                "If it asks you to sign in, you can skip that."]
    if state == "stopped":
        if system == "linux":
            return ["Docker is installed but not running. Start it with: sudo systemctl start docker"]
        return ["Docker Desktop is installed but not running. Open it and wait until it says",
                "it is running. This program does not start it for you."]
    if state == "denied":
        return ["Your user may not use Docker yet. Run this, then log out and back in:",
                "  sudo usermod -aG docker $USER",
                "(Members of the docker group can do anything on this machine.)"]
    return ["This Docker is too old: it has no 'docker compose'. Update it: %s"
            % (DOCKER_ENGINE if system == "linux" else DOCKER_DESKTOP)]


def wait_for_docker():
    """True once Docker answers. Explains what is missing and lets the owner
    check again; False when they stop (or nobody is there to ask)."""
    while True:
        state = docker_state()
        if state == "ok":
            return True
        say()
        for line in docker_help(state):
            say(line)
        if Answers.yes or not sys.stdin or not sys.stdin.isatty():
            return False
        try:
            answer = input("Press Enter to check again, or type q to stop: ").strip().lower()
        except EOFError:
            return False
        if answer.startswith("q"):
            return False


def ensure_network(dry_run=False):
    """Make the shared network when it is missing. Never removes it: every
    install on this machine uses it (docs/installs.md)."""
    if dry_run:
        if not DOCKER_UP["ok"] or docker("network", "inspect", NETWORK, timeout=60)[0] != 0:
            say("    would run: docker network create %s" % NETWORK)
        return True
    if docker("network", "inspect", NETWORK)[0] == 0:
        return True
    if docker("network", "create", NETWORK)[0] == 0:
        return True
    # another setup program may have made it in the meantime
    return docker("network", "inspect", NETWORK)[0] == 0


LABEL_FORMAT = "\t".join([
    "{{.ID}}", "{{.State}}",
    '{{.Label "org.openlobby.role"}}',
    '{{.Label "org.openlobby.game.title"}}',
    '{{.Label "org.openlobby.game.key"}}',
    '{{.Label "org.openlobby.panel.url"}}',
    '{{.Label "com.docker.compose.project"}}',
    '{{.Label "com.docker.compose.project.working_dir"}}',
    '{{.Label "com.docker.compose.service"}}',
])
LABEL_FIELDS = ("id", "state", "role", "title", "key", "panel_url", "project", "workdir", "service")


def containers(*filters):
    """Containers (running or not) matching the docker ps filters, as dicts."""
    if not DOCKER_UP["ok"]:
        return []
    args = ["ps", "-a"]
    for f in filters:
        args += ["--filter", f]
    code, out = docker(*(args + ["--format", LABEL_FORMAT]), timeout=60)
    if code != 0:
        return []
    found = []
    for line in out.splitlines():
        parts = line.rstrip("\r").split("\t")
        if len(parts) >= 2 and parts[0]:
            parts += [""] * (len(LABEL_FIELDS) - len(parts))
            found.append(dict(zip(LABEL_FIELDS, parts)))
    return found


def labelled():
    """Every container of an install that follows docs/installs.md."""
    return containers("label=" + ROLE_LABEL)


def other_games(project):
    """Game servers from other installs on this machine, one per key."""
    seen, games = set(), []
    for c in labelled():
        if c["role"] == "game" and c["project"] != project:
            key = c["key"] or c["title"] or c["id"]
            if key not in seen:
                seen.add(key)
                games.append(c)
    return games


def tell_about_games(project):
    for g in other_games(project):
        title = g["title"] or g["key"] or "a game server"
        say("A game server here, %s, can be managed from this panel: open Settings > Modules >"
            % title)
        say("Pair with a setup program, then run %s's own setup program and type the code it shows."
            % title)


# --- the install ---------------------------------------------------------------------------

def default_folder():
    """Where an install goes when the owner does not say: the folder the
    setup scripts use too."""
    return os.path.join(os.path.expanduser("~"), "openlobby-server")


class Install:
    """The install folder: the OpenLobby checkout in `openlobby`, beside it the
    log folder, the saved answers, and what to tell players."""

    def __init__(self, folder):
        self.folder = os.path.abspath(os.path.expanduser(folder))
        self.checkout = os.path.join(self.folder, CHECKOUT_NAME)
        self.env_path = os.path.join(self.checkout, ".env")
        self.state_path = os.path.join(self.folder, STATE_FILE)
        self.players_path = os.path.join(self.folder, PLAYERS_FILE)
        self.logs = os.path.join(self.folder, "logs")
        self.running_path = os.path.join(self.folder, RUNNING_FILE)

    def has_checkout(self):
        return all(os.path.exists(os.path.join(self.checkout, n))
                   for n in ("docker-compose.yml", ".env.example"))

    def installed(self):
        return os.path.isfile(self.env_path)

    def env(self):
        return read_env(self.env_path)

    def project(self):
        name = self.env().get("COMPOSE_PROJECT_NAME", "")
        if not name:
            name = re.sub(r"[^a-z0-9_-]", "", os.path.basename(self.checkout).lower())
        return name

    def state(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                got = json.load(f)
            return got if isinstance(got, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_state(self, **values):
        got = self.state()
        got.update(values)
        os.makedirs(self.folder, exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(got, f, indent=2, sort_keys=True)
            f.write("\n")


def read_env(path):
    values = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line.rstrip("\r\n"))
                if m:
                    values[m.group(1)] = m.group(2).strip().strip("\"'")
    except OSError:
        pass
    return values


def find_install(given=None):
    """The install to work on: the folder given, else the one a panel on this
    machine was started from, else the default folder."""
    if given:
        return Install(given)
    for c in containers("label=%s=panel" % ROLE_LABEL):
        if c["workdir"] and os.path.basename(c["workdir"].rstrip("/\\")) == CHECKOUT_NAME:
            return Install(os.path.dirname(c["workdir"].rstrip("/\\")))
    return Install(default_folder())


def project_containers(inst):
    return containers("label=com.docker.compose.project=" + inst.project())


def status_word(inst):
    if not inst.installed():
        return "not set up yet"
    mine = project_containers(inst)
    if not mine:
        return "not started"
    up = [c for c in mine if c["state"] == "running"]
    admin = [c for c in mine if c["service"] == "admin" and c["state"] == "running"]
    if admin:
        return "running (%d of %d parts up)" % (len(up), len(mine))
    return "stopped"


def panel_url(inst, override=None):
    if override:
        return override.rstrip("/")
    for c in project_containers(inst):
        if c["role"] == "panel" and c["panel_url"].startswith("http"):
            return c["panel_url"].rstrip("/")
    return PANEL_URL


def show_status(inst, url):
    """The block shown at the start of every run."""
    env = inst.env()
    heading("OpenLobby: %s" % status_word(inst))
    say("  Folder:        %s" % inst.folder)
    if inst.installed():
        say("  Players use:   %s" % (env.get("POL_ADVERTISE") or "127.0.0.1"))
        if env.get("POL_ADVERTISE_LAN"):
            say("  Consoles use:  %s (as their DNS server)" % env["POL_ADVERTISE_LAN"])
        say("  Admin panel:   %s (on this computer)" % url)
        if os.path.isfile(inst.players_path):
            say("  For players:   %s" % inst.players_path)


# --- the address --------------------------------------------------------------------------

def lan_address():
    """This machine's address on its local network. Nothing is sent: a UDP
    socket only picks the interface its route would use."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("198.51.100.1", 9))
            ip = s.getsockname()[0]
        finally:
            s.close()
        if ip and not ip.startswith(("127.", "0.")):
            return ip
    except OSError:
        pass
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return ""


def is_ipv4(text):
    parts = text.split(".")
    return len(parts) == 4 and all(p.isdigit() and int(p) <= 255 for p in parts)


def ask_settings(previous=None, given=""):
    """Who plays, the address they connect to, consoles, as a dict. `given`
    (--address) is the proposed address whoever plays."""
    prev = previous or {}
    heading("Who will play on this server?")
    order = ["lan", "vpn", "internet"]
    default = order.index(prev["reach"]) + 1 if prev.get("reach") in order else 1
    reach = order[ask_choice("", [REACH_TEXT[r] for r in order], default) - 1]
    lookup = False
    if reach == "lan":
        detected = lan_address()
        guess = given or (prev.get("address") if prev.get("reach") == "lan" and prev.get("address") else detected)
        address = ask_address("The address players connect to (this computer on your network)", guess)
    elif reach == "vpn":
        guess = given or (prev.get("address") if prev.get("reach") == "vpn" else "")
        if osname() == "mac":
            address = ask_address("This computer's address on the VPN", guess, required=True)
        else:
            say("Press Enter and setup finds this computer's VPN address itself.")
            address = ask_address("This computer's address on the VPN", guess, required=False)
    else:
        guess = given or (prev.get("address") if prev.get("reach") == "internet" else "")
        say("Your public address is the one your router has on the internet.")
        address = ask_address("The public address players connect to (Enter if you do not know it)",
                              guess, required=False)
        if not address:
            say("Setup can look it up by asking %s (one request; nothing else is sent)." % LOOKUP_URL)
            lookup = ask_yes("Look it up there?", False)
            if not lookup:
                address = ask_address("The public address players connect to", "", required=True)
    if reach == "lan":
        ps2 = False      # a console on this network uses this server's DNS anyway
    elif reach == "vpn":
        ps2 = ask_yes("Will a PlayStation 2 on this computer's own network play too?",
                      bool(prev.get("ps2")))
    else:
        ps2 = ask_yes("Will PlayStation 2 consoles play over the internet? (they need this server's"
                      " DNS open to the internet)", bool(prev.get("ps2")))
    return {"reach": reach, "address": address, "lookup": lookup, "ps2": ps2}


def ask_address(question, default, required=True):
    while True:
        answer = ask(question, default)
        if not answer and not required:
            return ""
        if is_ipv4(answer):
            return answer
        if Answers.yes:
            raise SystemExit("%s: give the address with --address (an IPv4 address like 203.0.113.10)"
                             % NAME)
        say("  Type an IPv4 address, four numbers with dots, like 203.0.113.10.")


def ask_admin_user(default="admin"):
    while True:
        name = ask("A user name for the admin panel", default)
        if re.match(r"^[A-Za-z0-9._-]{1,32}$", name):
            return name
        if Answers.yes:
            return "admin"
        say("  Letters, digits, dot, dash and underscore only.")


# --- getting OpenLobby --------------------------------------------------------------------

def fetch(inst, repo=REPO_URL, zip_url=ZIP_URL, dry_run=False):
    """Clone with git when it is installed, else unpack the GitHub zip of main.
    True when the checkout is there."""
    if inst.has_checkout():
        return True
    if os.path.isdir(inst.checkout) and os.listdir(inst.checkout):
        say("%s is there but holds no OpenLobby. Move it away or pick another folder." % inst.checkout)
        return False
    if tool("git"):
        if dry_run:
            say("    would run: git clone %s %s" % (repo, inst.checkout))
            return False
        os.makedirs(inst.folder, exist_ok=True)
        code, out = run("git", "clone", repo, inst.checkout, timeout=1800)
        if code != 0:
            say("Getting OpenLobby with git failed:")
            say(tail(out, 8))
            return False
        return inst.has_checkout()
    if dry_run:
        say("    would download %s into %s" % (zip_url, inst.checkout))
        return False
    return unpack_zip(inst, zip_url)


def unpack_zip(inst, zip_url):
    """Download the zip and unpack its one top folder into the checkout. Files
    already there and not in the zip (.env, .games) are kept."""
    os.makedirs(inst.logs, exist_ok=True)
    fd, archive = tempfile.mkstemp(prefix="openlobby-", suffix=".zip", dir=inst.logs)
    os.close(fd)
    try:
        try:
            with urllib.request.urlopen(zip_url, timeout=120) as r, open(archive, "wb") as f:
                shutil.copyfileobj(r, f)
        except (OSError, urllib.error.URLError) as e:
            say("The download failed (%s). Check the internet connection and try again." % e)
            return False
        try:
            with zipfile.ZipFile(archive) as z:
                extract(z, inst.checkout)
        except (zipfile.BadZipFile, OSError, ValueError) as e:
            say("The download is not a usable zip (%s)." % e)
            return False
    finally:
        try:
            os.remove(archive)
        except OSError:
            pass
    return inst.has_checkout()


def extract(z, dest):
    dest = os.path.abspath(dest)
    for info in z.infolist():
        parts = info.filename.replace("\\", "/").split("/")[1:]     # drop OpenLobby-main/
        if not parts or not any(parts):
            continue
        target = os.path.abspath(os.path.join(dest, *parts))
        if not target.startswith(dest + os.sep):
            raise ValueError("a path outside the folder: %s" % info.filename)
        if info.is_dir():
            os.makedirs(target, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with z.open(info) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)
        mode = (info.external_attr >> 16) & 0o777
        if mode and os.name != "nt":
            os.chmod(target, mode)


# --- the setup script ---------------------------------------------------------------------

def script_command(inst, settings=None, admin_user="", client=None, dry_run=False, deployer=False):
    """The command that runs the checkout's own setup script without questions.
    settings None keeps the address settings in .env as they are."""
    if osname() == "windows":
        argv = tool("powershell") or ["powershell"]
        cmd = argv + ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                      os.path.join(inst.checkout, "setup.ps1"), "-Yes", "-Images", "published"]
        if settings:
            cmd += ["-Reach", settings["reach"]]
            if settings.get("address"):
                cmd += ["-PublicAddress" if settings["reach"] == "internet" else "-Address",
                        settings["address"]]
            if settings.get("lookup"):
                cmd.append("-LookupPublic")
            cmd.append("-Ps2" if settings.get("ps2") else "-NoPs2")
        if admin_user:
            cmd += ["-AdminUser", admin_user]
        cmd.append("-InstallClient" if client else "-NoClient")
        cmd += channel_argv(True)
        if deployer and "-Deployer" not in cmd:
            cmd.append("-Deployer")
        if dry_run:
            cmd.append("-DryRun")
        return cmd
    argv = tool("bash") or ["bash"]
    cmd = argv + [os.path.join(inst.checkout, "setup.sh"), "--yes", "--images", "published"]
    if settings:
        cmd += ["--reach", settings["reach"]]
        if settings.get("address"):
            cmd += ["--public-address" if settings["reach"] == "internet" else "--address",
                    settings["address"]]
        if settings.get("lookup"):
            cmd.append("--lookup-public")
        cmd.append("--ps2" if settings.get("ps2") else "--no-ps2")
    if admin_user:
        cmd += ["--admin-user", admin_user]
    cmd += channel_argv(False)
    if deployer and "--deployer" not in cmd:
        cmd.append("--deployer")
    if dry_run:
        cmd.append("--dry-run")
    return cmd


def run_attached(inst, cmd):
    """A dry run: the script in this window, its whole output shown."""
    say()
    say("Dry run: running %s" % " ".join(cmd))
    try:
        p = subprocess.run(cmd, cwd=inst.checkout, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                           timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        say("Could not run the setup script: %s" % e)
        return 1
    for line in (p.stdout or "").splitlines():
        say("    " + line)
    return 0 if p.returncode == 0 else 1


def pid_alive(pid):
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))    # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            ok = k.GetExitCodeProcess(h, ctypes.byref(code))
            return bool(ok) and code.value == 259     # STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def run_script(inst, cmd, what):
    """Run the setup script on its own (closing this window does not stop it),
    its output going to a log in the install folder, and show its steps.
    Returns (exit code, log path); the code is None when it is still running."""
    os.makedirs(inst.logs, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    log = os.path.join(inst.logs, "setup-%s.log" % stamp)
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("# %s %s: %s\n" % (NAME, version(), what))
        f.flush()
        kw = {}
        if os.name == "nt":
            # CREATE_NO_WINDOW, NEW_PROCESS_GROUP: a hidden console of its own, so closing
            # this window does not stop it. Not DETACHED_PROCESS: PowerShell with no console
            # at all exits 0 at once and runs nothing.
            kw["creationflags"] = 0x08000000 | 0x00000200
        else:
            kw["start_new_session"] = True
        try:
            proc = subprocess.Popen(cmd, cwd=inst.checkout, stdin=subprocess.DEVNULL, stdout=f,
                                    stderr=subprocess.STDOUT, **kw)
        except OSError as e:
            say("Could not run the setup script: %s" % e)
            return 126, log
    with open(inst.running_path, "w", encoding="utf-8") as f:
        json.dump({"pid": proc.pid, "log": log, "what": what,
                   "started": datetime.datetime.now().isoformat(timespec="seconds")}, f)
    say("Details of every step go to %s" % log)
    code = follow(proc, log)
    return code, log


def follow(proc, log):
    """Show the script's steps as it writes them; return its exit code."""
    pos, partial = 0, ""
    while True:
        done = proc.poll() is not None
        with open(log, encoding="utf-8", errors="replace") as f:
            f.seek(pos)
            chunk = f.read()
            pos = f.tell()
        if chunk:
            lines = (partial + chunk).split("\n")
            partial = lines.pop()
            for line in lines:
                show_line(line.rstrip("\r"))
        elif done:
            if partial:
                show_line(partial.rstrip("\r"))
            return proc.returncode
        else:
            time.sleep(0.3)


#: the script steps whose every line is shown, not only their heading and warnings
SHOW_ALL = {"Game data", "Windows Firewall"}
_current = {"step": None}


def show_line(line):
    if line.startswith("== "):
        title = line[3:].strip()
        _current["step"] = title
        shown = STEPS.get(title, title)
        if shown:
            step(shown)
    elif re.match(r"^(Warning|WARNING): ", line):
        say("    Note: " + line.split(": ", 1)[1])
    elif line.startswith("Dry run:") or line.startswith("  would run"):
        say("    " + line.strip())
    elif _current["step"] in SHOW_ALL and line.strip():
        say("    " + line.strip())


def summary_of(log):
    """The script's closing block (after "== Done"), or None."""
    try:
        with open(log, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    if "\n== Done" not in text:
        return None
    return text.split("\n== Done", 1)[1].split("\n", 1)[-1].rstrip()


def tail(text, n=20):
    lines = [l for l in (text or "").splitlines() if l.strip()]
    return "\n".join("    " + l for l in lines[-n:])


def log_tail(log, n=20):
    try:
        with open(log, encoding="utf-8", errors="replace") as f:
            return tail(f.read(), n)
    except OSError:
        return ""


def hide_password(log):
    """The admin password is shown once; the log keeps no copy."""
    try:
        with open(log, encoding="utf-8", errors="replace") as f:
            text = f.read()
        text = re.sub(r"(?m)^(\s*Password: ).*$", r"\1(shown once when setup finished)", text)
        with open(log, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass


def players_text(inst, summary):
    """What to tell players: the script's Players section, with the address."""
    env = inst.env()
    lines = ["OpenLobby: what to tell players",
             "Written by %s on %s." % (NAME, datetime.date.today().isoformat()), "",
             "The server's address: %s" % (env.get("POL_ADVERTISE") or "127.0.0.1")]
    if env.get("POL_ADVERTISE_LAN"):
        lines.append("Consoles on this network use %s as their DNS server." % env["POL_ADVERTISE_LAN"])
    lines.append("")
    take = False
    for line in (summary or "").splitlines():
        if line.startswith("Players:"):
            take = True
        elif take and line and not line.startswith(" "):
            break
        if take:
            lines.append(line)
    return "\n".join(lines).rstrip() + "\n"


_PORT_BUSY = re.compile(r"ports are not available: exposing port (UDP|TCP) ([0-9.]+):(\d+)"
                        r"|Bind for ([0-9.]+):(\d+) failed: port is already allocated"
                        r"|listen (udp|tcp)4? ([0-9.]+):(\d+): bind: address already in use", re.I)

_PORT_WHAT = {53: "Consoles use this computer as their DNS server, so OpenLobby needs UDP port 53.",
              80: "The PlayOnline web pages need TCP port 80.",
              443: "The PlayOnline secure pages need TCP port 443."}


def port_holder(proto, addr, port):
    """The name of the program holding proto addr:port on this computer, or None."""
    try:
        if os.name == "nt":
            out = subprocess.run(["netstat", "-ano", "-p", proto], capture_output=True,
                                 text=True, timeout=20).stdout
            pids = {ln.split()[-1] for ln in out.splitlines()
                    if re.search(r"\s(%s|0\.0\.0\.0):%d\s" % (re.escape(addr), port), ln)}
            for pid in sorted(p for p in pids if p.isdigit() and p != "0"):
                row = subprocess.run(["tasklist", "/FI", "PID eq %s" % pid, "/FO", "CSV", "/NH"],
                                     capture_output=True, text=True, timeout=20).stdout.strip()
                if row.startswith('"'):
                    return "%s (process %s)" % (row.split('","')[0].strip('"'), pid)
        elif shutil.which("ss"):
            out = subprocess.run(["ss", "-lnp", "-u" if proto == "UDP" else "-t"],
                                 capture_output=True, text=True, timeout=20).stdout
            for ln in out.splitlines():
                if re.search(r"[:.]%d\s" % port, ln):
                    m = re.search(r'users:\(\("([^"]+)",pid=(\d+)', ln)
                    return "%s (process %s)" % m.groups() if m else "another program"
        elif shutil.which("lsof"):
            out = subprocess.run(["lsof", "-nP", "-i", "%s:%d" % (proto, port)],
                                 capture_output=True, text=True, timeout=20).stdout.splitlines()
            if len(out) > 1:
                cols = out[1].split()
                return "%s (process %s)" % (cols[0], cols[1])
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def explain_busy_port(log):
    """When Docker could not take a port, say which, who has it and what to do. True if said."""
    try:
        with open(log, encoding="utf-8", errors="replace") as f:
            m = _PORT_BUSY.search(f.read())
    except OSError:
        return False
    if not m:
        return False
    g = m.groups()
    proto, addr, port = ((g[0], g[1], g[2]) if g[0] else ("TCP", g[3], g[4]) if g[3]
                         else (g[5], g[6], g[7]))
    proto, port = proto.upper(), int(port)
    holder = port_holder(proto, addr, port)
    say()
    say("OpenLobby could not start: %s port %d on %s is already in use%s."
        % (proto, port, addr, " by " + holder if holder else ""))
    if port in _PORT_WHAT:
        say(_PORT_WHAT[port])
    say("Stop %s or move it to another port, then run %s again; it carries on where it "
        "stopped." % ("that program" if holder else "the program using it", NAME))
    if holder and holder.lower().startswith("svchost") and port == 53:
        say("On Windows that is usually Internet Connection Sharing or the Mobile hotspot: "
            "turn it off in Settings, Network and internet.")
    if port == 53 and addr == "0.0.0.0":
        say("Or keep that program: choose Change settings in %s and keep the same answers. Setup "
            "then pins OpenLobby's DNS to this computer's network address, which works beside it."
            % NAME)
    return True


# --- can consoles reach this server? ------------------------------------------------------

#: A PlayOnline name every console asks for (the portal), and the rule group setup.ps1 makes
CHECK_NAME = "wh000.pol.com"
FW_GROUP = "OpenLobby"

# What Windows says about the console path, as one line of JSON: the OpenLobby rules,
# this computer's addresses with their network type, and inbound block rules on Docker
# Desktop's port forwarder (a block rule wins over every allow rule). Reads only.
FW_STATUS_PS = r"""
$r = [ordered]@{ rules = @(); addresses = @(); blocks = @(); allows = @() }
foreach ($x in @(Get-NetFirewallRule -Group 'OpenLobby' -ErrorAction SilentlyContinue)) {
    $pf = $x | Get-NetFirewallPortFilter
    $af = $x | Get-NetFirewallAddressFilter
    $r.rules += ,([ordered]@{ name = [string]$x.DisplayName; enabled = [string]$x.Enabled;
        profile = [string]$x.Profile; protocol = [string]$pf.Protocol;
        ports = (@($pf.LocalPort) -join ','); remote = (@($af.RemoteAddress) -join ',') })
}
foreach ($a in @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue)) {
    $ip = [string]$a.IPAddress
    if ($ip.StartsWith('127.') -or $ip.StartsWith('169.254.')) { continue }
    $cat = ''
    $p = Get-NetConnectionProfile -InterfaceIndex $a.InterfaceIndex -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($p) { $cat = [string]$p.NetworkCategory }
    $r.addresses += ,([ordered]@{ ip = $ip; alias = [string]$a.InterfaceAlias; profile = $cat })
}
foreach ($b in @(Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue |
        Where-Object { [string]$_.Program -match '\\(com\.docker\.backend|com\.docker\.vpnkit|vpnkit)\.exe$' } |
        Get-NetFirewallRule -ErrorAction SilentlyContinue |
        Where-Object { [string]$_.Direction -eq 'Inbound' -and [string]$_.Enabled -eq 'True' })) {
    if ([string]$b.Action -eq 'Block') { $r.blocks += ,([string]$b.DisplayName + ' (' + [string]$b.Profile + ')') }
    else { $r.allows += ,([string]$b.Profile) }
}
'OLJSON ' + ($r | ConvertTo-Json -Depth 4 -Compress)
"""


def dns_ask(server, name, port=53, timeout=3.0):
    """Ask `server` for the A record of `name` the way a console does: (rcode, [IPv4
    addresses]), or None when nothing answered."""
    qid = int.from_bytes(os.urandom(2), "big") or 1
    query = qid.to_bytes(2, "big") + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
    for label in name.rstrip(".").split("."):
        query += bytes([len(label)]) + label.encode("ascii")
    query += b"\x00\x00\x01\x00\x01"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(query, (server, port))
        while True:
            data = s.recv(4096)
            if len(data) >= 12 and int.from_bytes(data[:2], "big") == qid and data[2] & 0x80:
                return parse_dns_answer(data)
    except OSError:
        return None
    finally:
        s.close()


def parse_dns_answer(data):
    """(rcode, [IPv4 addresses]) from a DNS reply."""
    def skip_name(i):
        while i < len(data):
            n = data[i]
            if n == 0:
                return i + 1
            if n & 0xC0 == 0xC0:
                return i + 2
            i += 1 + n
        return i
    rcode = data[3] & 0x0F
    qd, an = int.from_bytes(data[4:6], "big"), int.from_bytes(data[6:8], "big")
    i, ips = 12, []
    for _ in range(qd):
        i = skip_name(i) + 4
    for _ in range(an):
        i = skip_name(i)
        if i + 10 > len(data):
            break
        rtype, rdlen = int.from_bytes(data[i:i + 2], "big"), int.from_bytes(data[i + 8:i + 10], "big")
        i += 10
        if rtype == 1 and rdlen == 4:
            ips.append(".".join(str(b) for b in data[i:i + 4]))
        i += rdlen
    return rcode, ips


def windows_network():
    """The firewall and network facts FW_STATUS_PS gathers, or None."""
    code, out = run("powershell", "-NoProfile", "-NonInteractive", "-Command", FW_STATUS_PS, timeout=180)
    for line in out.splitlines():
        if line.startswith("OLJSON "):
            try:
                got = json.loads(line[7:])
            except ValueError:
                return None
            if not isinstance(got, dict):
                return None
            for key in ("rules", "addresses", "blocks", "allows"):
                v = got.get(key) or []
                got[key] = v if isinstance(v, list) else [v]
            return got
    return None


def check_consoles(inst, args=None):
    """Check from this computer what a console needs: the DNS service running, a real
    DNS question to the address consoles are told to use answered with this server's
    address, and on Windows the firewall rules and the network type. Prints plain next
    steps; returns 0 when nothing needs fixing, else 1."""
    heading("Can consoles reach this server?")
    env = inst.env()
    adv = env.get("POL_ADVERTISE") or "127.0.0.1"
    console_dns = env.get("POL_ADVERTISE_LAN") or adv
    good_answers = {a for a in (adv, env.get("POL_ADVERTISE_LAN")) if a}
    bind = env.get("POL_DNS_BIND", "")
    problems = []

    def ok(text):
        say("  ok    " + text)

    def fix(text, *more):
        problems.append(text)
        say("  FIX   " + text)
        for line in more:
            say("        " + line)

    def note(text):
        say("  note  " + text)

    dns = [c for c in containers("label=%s=dns" % ROLE_LABEL)]
    running = [c for c in dns if c["state"] == "running"]
    if running:
        ok("The DNS service is running (%s)." % ", ".join(sorted({c["project"] or c["id"] for c in running})))
    else:
        # a program on the wildcard address is in the way only when the DNS service wants it
        # too; pinned to one address (POL_DNS_BIND), it starts beside one
        holder = port_holder("UDP", "0.0.0.0", 53) if bind in ("", "0.0.0.0") else None
        fix("The DNS service is not running, so no console finds this server.",
            ("UDP port 53 is taken by %s. " % holder if holder else "")
            + "Choose Update or Start in %s; if it stops on port 53, it says what holds it." % NAME)

    if console_dns.startswith("127."):
        fix("Players are given %s, which only this computer can use." % console_dns,
            "Choose Change settings and give this computer's network address.")

    asked = []
    for server, why in ((console_dns, "the address consoles use"), ("127.0.0.1", "through Docker on this computer")):
        if server in [a for a, _ in asked]:
            continue
        if server == "127.0.0.1" and bind and not bind.startswith("127.") and bind != "0.0.0.0":
            note("Not asked at 127.0.0.1: the DNS service listens on %s only (POL_DNS_BIND)." % bind)
            continue
        ans = dns_ask(server, CHECK_NAME)
        asked.append((server, ans))
        if ans is None:
            fix("No answer from %s port 53 (%s)." % (server, why))
        elif ans[0] != 0 or not ans[1]:
            fix("%s answered %s with no address (DNS error %d)." % (server, CHECK_NAME, ans[0]),
                "The DNS service may not take questions from this address: see docs/configuration.md, \"DNS\".")
        elif not good_answers & set(ans[1]):
            fix("%s answers %s with %s, not this server's address %s."
                % (server, CHECK_NAME, ", ".join(ans[1]), adv),
                "Another DNS service answers there, or OpenLobby's has old settings: choose Update.")
        else:
            ok("%s answers %s with %s (%s)." % (server, CHECK_NAME, ", ".join(ans[1]), why))
    lan_ans = asked[0][1] if asked and asked[0][0] == console_dns else None
    if lan_ans is None and any(a for s, a in asked[1:] if a):
        note("The DNS service answers on this computer but not at %s. If something else on this "
             "computer holds port 53 there, OpenLobby's DNS has to be pinned to %s (POL_DNS_BIND): "
             "choose Change settings and keep the same answers." % (console_dns, console_dns))

    alias = ""
    if osname() == "windows":
        net = windows_network()
        if net is None:
            note("Windows did not say how its firewall is set up, so that part is not checked.")
        else:
            mine = {a.get("ip"): a for a in net["addresses"] if isinstance(a, dict)}
            here = mine.get(console_dns)
            if console_dns.startswith("127."):
                pass
            elif not here:
                fix("%s is not an address of this computer right now." % console_dns,
                    "If the router gave this computer a new address, choose Change settings.")
            else:
                alias = here.get("alias", "")
                profile = here.get("profile", "")
                if profile == "Public":
                    fix("Windows treats the network on %s as Public, so it keeps other devices out." % alias,
                        "Make it Private: Settings > Network and internet > (the network) > Network "
                        "profile type > Private network. Or choose Update here: for players on this "
                        "network it switches it for you.")
                else:
                    ok("%s is on %s (network type: %s)." % (console_dns, alias, profile or "unknown"))
            # real networks only: not Hyper-V/WSL/Docker switches, not VPN interfaces
            others = [a for ip, a in sorted(mine.items())
                      if ip != console_dns and not re.search(
                          r"vEthernet|WSL|Loopback|docker|Bluetooth|^(wg|wt|tailscale|zt)", a.get("alias", ""), re.I)]
            if here and others:
                note("This computer is on more than one network: "
                     + ", ".join("%s (%s)" % (a["ip"], a.get("alias", "")) for a in [here] + others)
                     + ". A console must be on the same network as %s." % console_dns)
            rules = [r for r in net["rules"] if isinstance(r, dict)]
            for r in rules:
                say("        rule: %s, from %s%s" % (r.get("name", ""), r.get("remote", ""),
                                                   "" if r.get("enabled") == "True" else " (turned off)"))
            dns_rule = [r for r in rules if r.get("protocol") == "UDP" and r.get("enabled") == "True"
                        and "53" in r.get("ports", "").split(",")]
            profile = (here or {}).get("profile", "")
            docker_allows = [p for p in net["allows"] if isinstance(p, str)
                             and (p == "Any" or (profile and profile in p.split(", ")))]
            if not rules and docker_allows:
                note("No OpenLobby rules in Windows Firewall, but Docker Desktop's own allow rule "
                     "covers this network (%s). Update here adds OpenLobby's own rules." % docker_allows[0])
            elif not rules:
                fix("Windows Firewall has no OpenLobby rules, so it likely stops consoles.",
                    "Choose Update here: setup adds them (Windows asks for administrator rights once).")
            elif not dns_rule:
                fix("The OpenLobby rules do not let DNS (UDP 53) in.", "Choose Update here to put them right.")
            else:
                ok("Windows Firewall has the OpenLobby rules (%d), DNS included." % len(rules))
            for b in net["blocks"]:
                fix("Windows Firewall blocks Docker Desktop: %s." % b,
                    "Choose Update here: setup turns that rule off (Windows asks for administrator rights).")
        say()
        say("  This computer asked its own DNS service. Windows Firewall does not stop a")
        say("  computer's questions to itself the way it stops a console's, so the answers")
        say("  above show that the server answers; the network and firewall lines are what")
        say("  decide whether a console gets through.")
    say()
    say("  On the PlayStation 2: in its network settings, set the primary DNS server to")
    say("  %s and leave the secondary one empty. The console must be on the same" % console_dns)
    say("  network as this computer%s." % (" (%s)" % alias if alias else ""))
    say("  From another computer on that network, this shows what a console gets:")
    say("    nslookup %s %s     (it should answer %s)" % (CHECK_NAME, console_dns, adv))
    say()
    if problems:
        say("  %d thing%s to fix, listed above." % (len(problems), "s" if len(problems) > 1 else ""))
        return 1
    say("  Nothing to fix found.")
    return 0


def remove_firewall_rules(inst):
    """Windows: take setup.ps1's firewall rule group away, which needs administrator
    rights (one UAC prompt). Kept while another install uses this one's DNS and port 80."""
    if osname() != "windows":
        return
    code, out = run("powershell", "-NoProfile", "-NonInteractive", "-Command",
                    "@(Get-NetFirewallRule -Group %s -ErrorAction SilentlyContinue).Count" % FW_GROUP)
    count = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if code != 0 or not count.isdigit() or count == "0":
        return
    if shared_kept(inst):
        say("The Windows Firewall rules (group %s) stay: other game servers here use this server's "
            "DNS and port 80." % FW_GROUP)
        return
    step("Removing OpenLobby's Windows Firewall rules (Windows asks for administrator rights)")
    code, out = run("powershell", "-NoProfile", "-NonInteractive", "-Command",
                    "try { $p = Start-Process powershell.exe -Verb RunAs -WindowStyle Hidden -Wait -PassThru "
                    "-ArgumentList '-NoProfile -Command Remove-NetFirewallRule -Group %s'; exit $p.ExitCode } "
                    "catch { exit 5 }" % FW_GROUP)
    if code != 0:
        say("    The firewall rules were not removed (the UAC prompt was declined?). To remove them")
        say("    later, in PowerShell as administrator: Remove-NetFirewallRule -Group %s" % FW_GROUP)


def finish(inst, code, log, url, wait=120):
    """After the script: its result, the panel, the players file. True when it worked."""
    try:
        os.remove(inst.running_path)
    except OSError:
        pass
    summary = summary_of(log)
    if code != 0 or summary is None:
        if explain_busy_port(log):
            say("The whole log: %s" % log)
            return False
        say()
        say("Setup stopped before the end. Its last lines:")
        say(log_tail(log))
        say("The whole log: %s" % log)
        say("Fix what it says, then run %s again." % NAME)
        return False
    if url and wait:
        step("Waiting for the admin panel")
        if not wait_for_panel(url, wait):
            say("    Note: the panel has not answered yet at %s. Give it a minute." % url)
    with open(inst.players_path, "w", encoding="utf-8") as f:
        f.write(players_text(inst, summary))
    say()
    say("=" * 72)
    say("OpenLobby is running.")
    say(summary)
    say()
    say("Saved what to tell players in %s" % inst.players_path)
    say("OpenLobby keeps running when you close this window, as long as Docker runs.")
    say("Run %s again to update, change settings, stop it or uninstall it." % NAME)
    say("=" * 72)
    hide_password(log)
    tell_about_games(inst.project())
    check_consoles(inst)
    return True


def wait_for_panel(url, timeout):
    """True once the panel answers HTTP at all (a login page or 503 counts)."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(url + "/", timeout=5):
                return True
        except urllib.error.HTTPError:
            return True
        except (urllib.error.URLError, OSError):
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(2)


def unfinished(inst, url):
    """A script started by an earlier window: still working, or done while
    nobody watched. False when there is nothing to pick up."""
    try:
        with open(inst.running_path, encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError):
        return False
    log = info.get("log", "")
    if pid_alive(info.get("pid")):
        say()
        say("Setup is still working from an earlier window (%s, since %s)."
            % (info.get("what", "setup"), info.get("started", "?")))
        say("Its log: %s" % log)
        say("Wait until it finishes, then run %s again." % NAME)
        return True
    say()
    say("Setup finished while this window was closed. Here is how it went.")
    finish(inst, 0 if summary_of(log) is not None else 1, log, url, wait=10)
    return True


# --- first run ----------------------------------------------------------------------------

def first_run(inst, args):
    say()
    say("This sets up an OpenLobby server on this computer, in Docker. Answer a few")
    say("questions, then setup does the rest. It keeps everything in one folder.")
    say()
    folder = ask("The folder to install OpenLobby in", inst.folder)
    inst = Install(folder)
    if inst.installed():
        say("OpenLobby is already set up in %s." % inst.folder)
        return menu(inst, args)
    settings = ask_settings(inst.state(), args.address or "")
    admin_user = ask_admin_user(args.admin_user or "admin")
    client = None
    if osname() == "windows":
        client = ask_yes("Will you also play on this computer? (Windows asks for administrator "
                         "rights to point it at the server)", False)
    heading("Ready")
    say("  Folder:       %s" % inst.folder)
    say("  Players:      %s" % REACH_TEXT[settings["reach"]])
    say("  Address:      %s" % (settings["address"] or
                                ("looked up at " + LOOKUP_URL if settings["lookup"] else "found by setup")))
    say("  Admin panel:  user %s, password made by setup and shown at the end" % admin_user)
    say("  Images:       the published OpenLobby images")
    if args.dry_run:
        say()
        say("Dry run: nothing is changed.")
    elif not ask_yes("Start?"):
        say("Nothing was changed.")
        return 0
    return install(inst, settings, admin_user, client, args)


def install(inst, settings, admin_user, client, args):
    heading("Setting up")
    step("Getting OpenLobby")
    if not fetch(inst, args.repo, args.zip_url, args.dry_run):
        if args.dry_run:
            cmd = script_command(inst, settings, admin_user, client, dry_run=True)
            say("    then would run: %s" % " ".join(cmd))
            return 0
        return 1
    if args.dry_run:
        step("Making the shared Docker network (%s)" % NETWORK)
        ensure_network(dry_run=True)
        return run_attached(inst, script_command(inst, settings, admin_user, client, dry_run=True))
    inst.save_state(reach=settings["reach"], address=settings["address"], ps2=settings["ps2"],
                    admin_user=admin_user,
                    fetched_with="git" if os.path.isdir(os.path.join(inst.checkout, ".git")) else "zip")
    step("Making the shared Docker network (%s)" % NETWORK)
    if not ensure_network():
        say("Could not make the Docker network %s. Run: docker network create %s" % (NETWORK, NETWORK))
        return 1
    code, log = run_script(inst, script_command(inst, settings, admin_user, client), "install")
    return 0 if finish(inst, code, log, panel_url(inst, args.panel_url), args.panel_wait) else 1


# --- later runs ---------------------------------------------------------------------------

def get_new(inst, args):
    """The newest OpenLobby into the checkout. False (and says why) when it could not."""
    step("Getting the new OpenLobby")
    if os.path.isdir(os.path.join(inst.checkout, ".git")) and tool("git"):
        code, out = run("git", "-C", inst.checkout, "pull", "--ff-only", timeout=600)
        if code != 0:
            say("git pull did not work (changes made in the folder by hand?). Nothing was changed:")
            say(tail(out, 8))
            return False
        return True
    return bool(unpack_zip(inst, args.zip_url))


def update(inst, args):
    heading("Update")
    if not get_new(inst, args):
        return 1
    code, log = run_script(inst, script_command(inst, client=False), "update")
    ok = finish(inst, code, log, panel_url(inst, args.panel_url), args.panel_wait)
    if ok and FROZEN:
        say("A newer %s may be on %s" % (NAME, RELEASES))
    return 0 if ok else 1


def change_settings(inst, args):
    heading("Change settings")
    say("The admin panel's password and user are changed on the panel, Settings > Sign-in.")
    settings = ask_settings(inst.state(), args.address or "")
    if not ask_yes("Save these and restart OpenLobby?"):
        return 0
    inst.save_state(reach=settings["reach"], address=settings["address"], ps2=settings["ps2"])
    code, log = run_script(inst, script_command(inst, settings, client=False), "change settings")
    return 0 if finish(inst, code, log, panel_url(inst, args.panel_url), args.panel_wait) else 1


def has_deployer(inst):
    return bool(inst.env().get("POL_DEPLOYER_TOKEN"))


def add_deployer(inst, args):
    """Turn the deployer on for an install made without it (Setup offered it on Windows
    only later), and with an access code put it on the developers' builds as well."""
    heading("Add the deployer")
    if has_deployer(inst):
        say("The deployer is already set up here: Settings > Install games on the admin panel.")
        return 0
    say("The deployer lets the admin panel install, turn on and update games and OpenLobby")
    say("itself, and switch servers to the developers' builds with an access code. It runs")
    say("as one more container with control of Docker; it opens no port and needs a token.")
    say("This also updates OpenLobby. Your games, settings and the panel's password stay.")
    if not ask_yes("Add the deployer and restart OpenLobby?"):
        return 0
    # the setup script that can add it on Docker Desktop is newer than many such installs
    if not get_new(inst, args):
        return 1
    CHANNEL.clear()
    if not Answers.yes:
        try:
            code = getpass.getpass("Developers' access code, if you were given one "
                                   "(not shown; Enter for none): ").strip()
        except EOFError:
            code = ""
        if code:
            CHANNEL["channel"] = "dev"
            keep_code(code)
    try:
        rc, log = run_script(inst, script_command(inst, client=False, deployer=True), "add the deployer")
        ok = finish(inst, rc, log, panel_url(inst, args.panel_url), args.panel_wait)
    finally:
        dev = CHANNEL.get("channel") == "dev"
        forget_channel_code()
        CHANNEL.clear()
    if not ok:
        return 1
    if not has_deployer(inst):
        say()
        say("The deployer was left off; the setup log says why: %s" % log)
        return 1
    say()
    say("The deployer is on. Reload the admin panel: Settings now has an Install games tab%s."
        % (", with your access code already in" if dev else ""))
    return 0


def shared_kept(inst):
    """This install's dns and http containers stay when another install uses
    them (docs/installs.md: one DNS and one port 80 for every install)."""
    if not other_games(inst.project()):
        return []
    return [c for c in project_containers(inst) if c["role"] in ("dns", "http")]


def stop(inst, args):
    mine = [c for c in project_containers(inst) if c["state"] == "running"]
    if not mine:
        say("OpenLobby is not running.")
        return 0
    if not confirm("Stop OpenLobby? Players who are online are disconnected.", args.stop):
        return 0
    kept = {c["id"] for c in shared_kept(inst)}
    if kept:
        say("Other game servers here use this server's DNS and port 80, so those two keep running.")
    ids = [c["id"] for c in mine if c["id"] not in kept]
    step("Stopping OpenLobby")
    code, out = docker("stop", *ids, timeout=300) if ids else (0, "")
    if code != 0:
        say(tail(out, 8))
        return 1
    say("Stopped. Run %s again and choose Start to start it." % NAME)
    return 0


def start(inst, args):
    stopped = [c["id"] for c in project_containers(inst) if c["state"] != "running"]
    if not stopped:
        say("OpenLobby is running.")
        return 0
    step("Starting OpenLobby")
    if not ensure_network():
        say("Could not make the Docker network %s." % NETWORK)
        return 1
    code, out = docker("start", *stopped, timeout=300)
    if code != 0:
        say(tail(out, 8))
        return 1
    step("Waiting for the admin panel")
    url = panel_url(inst, args.panel_url)
    if wait_for_panel(url, args.panel_wait):
        say("OpenLobby is running. Admin panel: %s" % url)
    else:
        say("Started, but the panel has not answered yet at %s. Give it a minute." % url)
    return 0


def game_checkouts(inst):
    """The game checkouts the deployer cloned beside this install's checkout: each
    catalog entry's folder in the install folder, when it is a git checkout. Read
    from the catalog before the core checkout is deleted."""
    out = []
    folder = os.path.join(inst.checkout, "games")
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                entry = json.load(f)
        except (OSError, ValueError):
            continue
        sub = entry.get("checkout") if isinstance(entry, dict) else None
        if (entry.get("kind") == "core" or not isinstance(sub, str)
                or not re.match(r"^[A-Za-z0-9._-]+$", sub) or sub in (".", "..")):
            continue
        path = os.path.join(inst.folder, sub)
        if path != inst.checkout and os.path.isdir(os.path.join(path, ".git")) and path not in out:
            out.append(path)
    return out


def remove(inst, delete_data, games=False):
    """Take this install's containers away, and with delete_data its volumes and
    files, and with games the game checkouts the deployer cloned beside it. Never
    the openlobby network, never another install's containers, never the dns and
    http containers while another install uses them, never the client installs
    folder."""
    kept = shared_kept(inst)
    kept_ids = {c["id"] for c in kept}
    gone = [c["id"] for c in project_containers(inst) if c["id"] not in kept_ids]
    if kept:
        say("Other game servers here use this server's DNS and port 80, so those two stay.")
    step("Removing OpenLobby's containers")
    if gone:
        code, out = docker("rm", "-f", *gone, timeout=300)
        if code != 0:
            say(tail(out, 8))
            return False
    project = inst.project()
    code, out = docker("network", "ls", "--filter", "label=com.docker.compose.project=" + project,
                       "--format", "{{.Name}}")
    for net in out.split() if code == 0 else []:
        if net != NETWORK:
            docker("network", "rm", net)        # still in use by a kept container: left
    if not delete_data:
        return True
    step("Deleting OpenLobby's data")
    in_use = set()
    for c in kept:
        code, out = docker("inspect", "--format", "{{range .Mounts}}{{.Name}} {{end}}", c["id"])
        in_use.update(out.split() if code == 0 else [])
    code, out = docker("volume", "ls", "-q", "--filter", "label=com.docker.compose.project=" + project)
    for vol in out.split() if code == 0 else []:
        if vol in in_use:
            say("    Kept %s: the shared DNS and port 80 still use it." % vol)
        else:
            docker("volume", "rm", vol)
    for path in (game_checkouts(inst) if games else []) + [inst.checkout, inst.logs]:
        if os.path.isdir(path):
            shutil.rmtree(path, onerror=_force_remove)
    for path in (inst.state_path, inst.players_path):
        if os.path.exists(path):
            os.remove(path)
    return True


def _force_remove(func, path, _exc):
    """git keeps read-only files on Windows."""
    try:
        os.chmod(path, 0o700)
        func(path)
    except OSError:
        pass


def uninstall(inst, args):
    heading("Uninstall")
    say("This removes OpenLobby's containers from Docker. The shared Docker network")
    say("'%s' stays, since other installs use it." % NETWORK)
    delete = args.delete_data
    if not delete and not Answers.yes:
        delete = ask_yes("Also delete the data: accounts, settings, the database and the games"
                         " installed from the panel? This cannot be undone.", False)
    if delete and not Answers.yes:
        if ask("To delete everything, type delete") != "delete":
            say("Not deleting the data.")
            delete = False
    if not confirm("Uninstall OpenLobby now?", args.uninstall):
        return 0
    remove_firewall_rules(inst)
    if not remove(inst, delete, games=delete):
        return 1
    if delete:
        say("OpenLobby, its data and its games are gone from %s." % inst.folder)
        if os.path.isdir(os.path.join(inst.folder, "clients")):
            say("Your client files are kept in %s." % os.path.join(inst.folder, "clients"))
    else:
        say("OpenLobby is uninstalled. Its settings and data are kept in %s and in Docker;" % inst.folder)
        say("run %s again to start it with them." % NAME)
    return 0


def start_over(inst, args):
    heading("Start over")
    say("This deletes this server's accounts, settings and database, then sets it up anew.")
    say("A copy of the settings file goes to the backups folder first. The shared Docker")
    say("network, other game servers and your client installs folder are not touched.")
    if not Answers.yes and ask("To go on, type start over") != "start over":
        say("Nothing was changed.")
        return 0
    if os.path.isfile(inst.env_path):
        backups = os.path.join(inst.folder, "backups")
        os.makedirs(backups, exist_ok=True)
        copy = os.path.join(backups, "env-%s.txt" % datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(inst.env_path, copy)
        say("Saved the old settings in %s" % copy)
    if not remove(inst, True):
        return 1
    return first_run(inst, args)


def tell_players(inst, args):
    if os.path.isfile(inst.players_path):
        with open(inst.players_path, encoding="utf-8") as f:
            say()
            say(f.read().rstrip())
        say()
        say("(Saved in %s)" % inst.players_path)
    else:
        say("The address players use: %s" % (inst.env().get("POL_ADVERTISE") or "127.0.0.1"))
    return 0


def menu(inst, args):
    while True:
        running = any(c["service"] == "admin" and c["state"] == "running" for c in project_containers(inst))
        items = [("Update", update), ("Change settings", change_settings),
                 ("Stop OpenLobby", stop) if running else ("Start OpenLobby", start),
                 ("Show what to tell players", tell_players),
                 ("Check that consoles can reach this server", check_consoles),
                 ("Start over", start_over), ("Uninstall", uninstall)]
        say()
        if not has_deployer(inst):
            # last, so the numbers above stay what they always were
            items.append(("Add the deployer", add_deployer))
            say("This install cannot install games or take a developers' access code from the admin")
            say("panel yet. Pick %d, Add the deployer, to set that up; your games and settings stay."
                % len(items))
        for i, (title, _) in enumerate(items, 1):
            say("  %d. %s" % (i, title))
        say("  0. Quit")
        if Answers.closed:
            return 0
        pick = ask("Type a number", "0")
        if pick == "0" or Answers.yes:
            return 0
        if pick.isdigit() and 1 <= int(pick) <= len(items):
            try:
                items[int(pick) - 1][1](inst, args)
            except KeyboardInterrupt:
                say()
            if not inst.installed():
                return 0
            show_status(inst, panel_url(inst, args.panel_url))
        else:
            say("  Type one of the numbers above.")


# --- main ---------------------------------------------------------------------------------

def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog="openlobby-setup", description=(
        "Sets up an OpenLobby server in Docker on this computer. The first run asks a few "
        "questions; later runs offer update, change settings, stop, start over and uninstall."))
    ap.add_argument("--dir", help="the install folder (default: the one a running panel came from, "
                                  "else openlobby-server in your home folder)")
    ap.add_argument("--yes", action="store_true", help="take the default answer to every question")
    ap.add_argument("--dry-run", action="store_true",
                    help="ask the questions and show what would be done; changes nothing")
    ap.add_argument("--address", help="the address players connect to")
    ap.add_argument("--admin-user", default="", help="the admin panel's user name (default: admin)")
    do = ap.add_mutually_exclusive_group()
    do.add_argument("--update", action="store_true", help="update an install, no menu")
    do.add_argument("--change-settings", action="store_true", help="ask the address questions again")
    do.add_argument("--stop", action="store_true", help="stop OpenLobby")
    do.add_argument("--start", action="store_true", help="start OpenLobby again")
    do.add_argument("--add-deployer", action="store_true",
                    help="turn the deployer on for an install made without it")
    do.add_argument("--check", action="store_true",
                    help="check that consoles can reach this server (DNS, and on Windows the firewall)")
    do.add_argument("--uninstall", action="store_true",
                    help="remove the containers; the data stays unless --delete-data is given")
    ap.add_argument("--delete-data", action="store_true", help="with --uninstall: delete the data too")
    ap.add_argument("--channel", choices=("stable", "dev"),
                    help="the update channel: dev asks for an access code (not shown), unlocks the "
                         "developers' preview builds and puts everything that has one on Dev (the "
                         "panel then switches each server on its own); stable puts all back; "
                         "docs/deployer.md, \"Dev channel\"")
    ap.add_argument("--registry", default="",
                    help="advanced, with --channel dev and a plain token: its registry, host/path "
                         "(an access code carries its own)")
    ap.add_argument("--code-file", default="",
                    help="with --channel dev and --yes: read the access code from this file's first line")
    ap.add_argument("--repo", default=REPO_URL, help=argparse.SUPPRESS)
    ap.add_argument("--zip-url", default=ZIP_URL, help=argparse.SUPPRESS)
    ap.add_argument("--panel-url", default="", help=argparse.SUPPRESS)
    ap.add_argument("--panel-wait", type=int, default=120, help=argparse.SUPPRESS)
    ap.add_argument("--version", action="version", version="%s %s" % (NAME, version()))
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    Answers.yes = args.yes
    Answers.closed = False
    try:
        heading("%s %s" % (NAME, version()))
        if not prepare_channel(args):
            return 2
        step("Checking Docker")
        if args.dry_run:
            state = docker_state()
            say("    Docker: %s" % {"ok": "installed and running"}.get(state, state))
            DOCKER_UP["ok"] = state == "ok"
        elif not wait_for_docker():
            say()
            say("Run %s again once Docker is ready." % NAME)
            return 1
        inst = find_install(args.dir)
        url = panel_url(inst, args.panel_url)
        if not args.dry_run and unfinished(inst, url):
            return 0
        if not inst.installed():
            for line in other_installs_note(inst):
                say(line)
            if (args.update or args.change_settings or args.stop or args.start or args.uninstall
                    or args.check or args.add_deployer):
                say("OpenLobby is not set up in %s." % inst.folder)
                return 1
            return first_run(inst, args)
        show_status(inst, url)
        tell_about_games(inst.project())
        if args.dry_run:
            return run_attached(inst, script_command(inst, client=False, dry_run=True))
        for flag, action in ((args.update, update), (args.change_settings, change_settings),
                             (args.stop, stop), (args.start, start), (args.uninstall, uninstall),
                             (args.check, check_consoles), (args.add_deployer, add_deployer)):
            if flag:
                return action(inst, args)
        return menu(inst, args)
    except KeyboardInterrupt:
        say()
        return 1
    finally:
        forget_channel_code()


def other_installs_note(inst):
    """Before a first install: games already here, said once."""
    lines = []
    for g in other_games(inst.project()):
        lines.append("Found a game server here: %s. Once OpenLobby runs, it can be managed from the"
                     % (g["title"] or g["key"]))
        lines.append("admin panel (Settings > Modules > Pair with a setup program).")
    return lines


def own_window():
    """True when this program has a console window of its own (double-clicked),
    which closes the moment it ends."""
    if os.name == "nt":
        try:
            import ctypes
            ids = (ctypes.c_uint * 4)()
            # this program and its one-file launcher; a shell adds itself
            return ctypes.windll.kernel32.GetConsoleProcessList(ids, 4) <= 2
        except (OSError, AttributeError):
            return False
    return bool(sys.stdin and sys.stdin.isatty())


def entry():
    code = 1
    try:
        code = main()
    except SystemExit as e:
        if e.code is None or isinstance(e.code, int):
            code = e.code or 0
        else:
            say(str(e.code))
            code = 1
    finally:
        if FROZEN and own_window():
            # a double-clicked program's window closes when it ends: keep it open to be read
            try:
                input("Press Enter to close this window.")
            except (EOFError, KeyboardInterrupt):
                pass
    sys.exit(code)


if __name__ == "__main__":
    entry()
