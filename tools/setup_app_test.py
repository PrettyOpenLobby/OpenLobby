#!/usr/bin/env python3
"""Offline tests for the setup program, setup/openlobby_setup.py.

    python tools/setup_app_test.py

The program runs in this process with its tools swapped for stand-ins:
docker, git and the setup script (run as powershell on Windows, bash
elsewhere) are small Python scripts that log every call and keep their state
in a scratch folder, so nothing reaches Docker, GitHub or the real machine.
The admin panel is a local HTTP server. Covered: Docker missing and not
running, a fresh install with git and from the zip, the second run's menu,
update, stop, uninstall keeping the data and the shared network, uninstall
next to another install's game server, a failed script, and a run that
picks up a script the previous window left running. The flags the program
hands the scripts are checked against setup.sh and setup.ps1 themselves.
The dev channel: the access code reaches the script in a private temporary
file (never on its command line) that is gone afterwards, asked hidden when
there is a terminal and refused with --yes unless --code-file gives it.
"""
import builtins
import contextlib
import http.server
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
import zipfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROGRAM = os.path.join(ROOT, "setup", "openlobby_setup.py")

LAN = "192.0.2.20"
PUBLIC = "203.0.113.10"


def load():
    spec = importlib.util.spec_from_file_location("openlobby_setup_under_test", PROGRAM)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Each stand-in logs "<name> <args>" to $STUB_LOG and keeps state in $STUB_STATE.
STUB_COMMON = r'''
import json, os, sys
STATE = os.environ["STUB_STATE"]
def log(name):
    with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as f:
        f.write(name + " " + " ".join(sys.argv[1:]) + "\n")
def load(name, default):
    try:
        with open(os.path.join(STATE, name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default
def save(name, value):
    with open(os.path.join(STATE, name), "w", encoding="utf-8") as f:
        json.dump(value, f)
'''

STUB_DOCKER = STUB_COMMON + r'''
log("docker")
a = sys.argv[1:]
mode = load("docker_mode", "ok")
FIELDS = ("id", "state", "role", "title", "key", "panel_url", "project", "workdir", "service")
LABELS = {"org.openlobby.role": "role", "com.docker.compose.project": "project"}
if a[:1] == ["info"]:
    if mode == "stopped":
        print("Cannot connect to the Docker daemon. Is the docker daemon running?")
        sys.exit(1)
    if mode == "denied":
        print("permission denied while trying to connect to the Docker daemon socket")
        sys.exit(1)
    print("Server: stand-in")
elif a[:2] == ["compose", "version"]:
    print("Docker Compose version v2.29.1")
elif a[:2] == ["network", "inspect"]:
    sys.exit(0 if a[2] in load("networks", []) else 1)
elif a[:2] == ["network", "create"]:
    save("networks", load("networks", []) + [a[2]])
elif a[:2] == ["network", "rm"]:
    save("networks", [n for n in load("networks", []) if n != a[2]])
elif a[:2] == ["network", "ls"]:
    # every network, the shared one too: the program must leave that one alone
    for n in load("networks", []):
        print(n)
elif a[:1] == ["ps"]:
    rows = load("containers", [])
    i = 0
    while i < len(a):
        if a[i] == "--filter":
            f = a[i + 1][len("label="):]
            key, _, want = f.partition("=")
            field = LABELS.get(key)
            if field:
                rows = [r for r in rows if r.get(field) and (not want or r.get(field) == want)]
            i += 1
        i += 1
    for r in rows:
        print("\t".join(r.get(k, "") for k in FIELDS))
elif a[:1] in (["stop"], ["start"]):
    rows = load("containers", [])
    for r in rows:
        if r["id"] in a[1:]:
            r["state"] = "exited" if a[0] == "stop" else "running"
    save("containers", rows)
elif a[:2] == ["rm", "-f"]:
    save("containers", [r for r in load("containers", []) if r["id"] not in a[2:]])
elif a[:2] == ["volume", "ls"]:
    project = a[-1].rpartition("=")[2]
    for name, owner in sorted(load("volumes", {}).items()):
        if owner == project:
            print(name)
elif a[:2] == ["volume", "rm"]:
    vols = load("volumes", {})
    vols.pop(a[2], None)
    save("volumes", vols)
elif a[:1] == ["inspect"]:
    for r in load("containers", []):
        if r["id"] == a[-1]:
            print(" ".join(r.get("mounts", [])))
'''

STUB_GIT = STUB_COMMON + r'''
import shutil
log("git")
a = sys.argv[1:]
if a[:1] == ["clone"]:
    shutil.copytree(os.environ["STUB_FIXTURE"], a[2])
    os.makedirs(os.path.join(a[2], ".git"))
'''

# setup.ps1 or setup.sh: what the real ones leave behind, in a few lines. As powershell
# without a script, it answers the program's own Windows Firewall questions from the
# state: fw_rules (the OpenLobby group) and fw_status (what FW_STATUS_PS reports).
STUB_SCRIPT = STUB_COMMON + r'''
a = sys.argv[1:]
if not [x for x in a if x.endswith(("setup.sh", "setup.ps1"))]:
    cmd = a[-1] if a else ""
    def note(text):
        with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as f:
            f.write("powershell " + text + "\n")
    if "OLJSON" in cmd:
        note("firewall status")
        status = load("fw_status", {"addresses": [], "blocks": []})
        status["rules"] = load("fw_rules", [])
        print("OLJSON " + json.dumps(status))
    elif ".Count" in cmd:
        note("firewall count " + cmd)
        print(len(load("fw_rules", [])))
    elif "Remove-NetFirewallRule" in cmd:
        note(cmd)
        if not load("uac_ok", True):
            sys.exit(5)
        save("fw_rules", [])
    sys.exit(0)
log("script")
path = [x for x in a if x.endswith(("setup.sh", "setup.ps1"))][0]
checkout = os.path.dirname(path)
def opt(*names):
    for n in names:
        if n in a:
            return a[a.index(n) + 1]
    return ""
dry = "--dry-run" in a or "-DryRun" in a
cf = opt("--code-file", "-CodeFile")
if cf:
    with open(cf, encoding="utf-8") as f:
        save("channel_code", f.read().strip())
print("== OpenLobby setup")
print("== Checking Docker and git")
print("== Settings (.env)")
env_path = os.path.join(checkout, ".env")
address = opt("--address", "-Address", "--public-address", "-PublicAddress")
if dry:
    print("Dry run: nothing is changed, every change is printed instead.")
    print("  would run: docker compose pull")
    sys.exit(0)
if not os.path.exists(env_path):
    with open(env_path, "w", encoding="utf-8") as f:
        f.write("POL_ADVERTISE=%s\nPOL_ADMIN_USER=%s\n" % (address or "198.51.100.5", opt("--admin-user", "-AdminUser") or "admin"))
elif opt("--reach", "-Reach"):
    with open(env_path, encoding="utf-8") as f:
        lines = [l for l in f.read().splitlines() if not l.startswith("POL_ADVERTISE=")]
    with open(env_path, "w", encoding="utf-8") as f:
        f.write("\n".join(["POL_ADVERTISE=%s" % (address or "198.51.100.5")] + lines) + "\n")
print("== Starting the stack")
if os.environ.get("STUB_SCRIPT_FAIL"):
    print("setup.sh: docker compose up failed; see the output above")
    sys.exit(1)
rows = [r for r in load("containers", []) if r.get("project") != "openlobby"]
url = os.environ.get("STUB_PANEL_URL", "")
for name, role in (("admin", "panel"), ("dns", "dns"), ("http", "http"), ("postgres", "")):
    rows.append({"id": "c-" + name, "state": "running", "role": role, "project": "openlobby",
                 "workdir": checkout, "service": name, "panel_url": url if role == "panel" else "",
                 "mounts": ["openlobby_pol-data"] if role in ("dns", "http") else ["openlobby_pol-pgdata"]})
if os.environ.get("STUB_DEPLOYER"):
    # setup.ps1 on Docker Desktop and setup.sh on Docker Engine start the deployer too
    rows.append({"id": "c-deployer", "state": "running", "role": "", "project": "openlobby",
                 "workdir": checkout, "service": "deployer", "panel_url": "",
                 "mounts": ["openlobby_pol-deployer"]})
save("containers", rows)
vols = load("volumes", {})
if os.environ.get("STUB_DEPLOYER"):
    vols["openlobby_pol-deployer"] = "openlobby"
vols.update({"openlobby_pol-data": "openlobby", "openlobby_pol-pgdata": "openlobby"})
save("volumes", vols)
nets = load("networks", [])
if "openlobby_default" not in nets:
    save("networks", nets + ["openlobby_default"])
print("Warning: a stand-in warning")
if path.endswith("setup.ps1"):
    print("== Windows Firewall")
    print("Consoles reach this PC at %s, on Ethernet (network type: Private)." % (address or "198.51.100.5"))
    save("fw_rules", [{"name": "OpenLobby UDP 53 (Private)", "enabled": "True", "profile": "Private",
                       "protocol": "UDP", "ports": "53", "remote": "LocalSubnet"},
                      {"name": "OpenLobby TCP 80 (Private)", "enabled": "True", "profile": "Private",
                       "protocol": "TCP", "ports": "80", "remote": "LocalSubnet"}])
print("== Admin panel password")
print("== Done")
print("Admin panel:")
print("  http://127.0.0.1:8090 (on this machine only)")
print("  User: admin")
print("  Password: Pw0nlyOnce12345678ab")
print("")
print("Players:")
print("  PC: run Install-PolConnect.ps1 -Server %s" % (address or "198.51.100.5"))
print("  PlayStation 2: set the console's DNS server to %s." % (address or "198.51.100.5"))
print("")
print("Games: on the admin panel, Settings > Install games.")
'''


class Panel(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"panel")

    def log_message(self, *args):
        pass


class Feeder:
    """Answers for input(); EOF once they run out."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.asked = []

    def __call__(self, prompt=""):
        self.asked.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


class SetupAppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = http.server.HTTPServer(("127.0.0.1", 0), Panel)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.panel = "http://127.0.0.1:%d" % cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="olsetupapp-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = os.path.join(self.tmp, "state")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.state)
        os.makedirs(self.bin)
        self.log = os.path.join(self.tmp, "calls.log")
        open(self.log, "w").close()
        self.fixture = os.path.join(self.tmp, "fixture")
        os.makedirs(os.path.join(self.fixture, "tools"))
        for name in ("setup.sh", "setup.ps1", ".env.example", "docker-compose.yml"):
            shutil.copy(os.path.join(ROOT, name), os.path.join(self.fixture, name))
        with open(os.path.join(self.fixture, "tools", "stack.py"), "w") as f:
            f.write("# stand-in\n")
        self.stubs = {}
        for name, body in (("docker", STUB_DOCKER), ("git", STUB_GIT), ("script", STUB_SCRIPT)):
            p = os.path.join(self.bin, name + ".py")
            with open(p, "w", encoding="utf-8") as f:
                f.write(body)
            self.stubs[name] = [sys.executable, p]
        self.stubs["powershell"] = self.stubs["bash"] = self.stubs["script"]
        self.missing = set()
        self.folder = os.path.join(self.tmp, "OpenLobby Server")
        env = {"STUB_LOG": self.log, "STUB_STATE": self.state, "STUB_FIXTURE": self.fixture,
               "STUB_PANEL_URL": self.panel}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mod = load()
        self.mod.tool = lambda name: None if name in self.missing else self.stubs.get(name)
        self.system = "windows"
        self.mod.osname = lambda: self.system
        # a stand-in resolver: {server: (rcode, [addresses])}, nothing answers elsewhere
        self.dns = {}
        self.dns_asked = []

        def dns_ask(server, name, port=53, timeout=3.0):
            self.dns_asked.append((server, name))
            return self.dns.get(server)
        self.mod.dns_ask = dns_ask

    # ---------------------------------------------------------------- helpers

    def main(self, *args, answers=(), expect=0):
        feeder = Feeder(answers)
        out = io.StringIO()
        with mock.patch.object(builtins, "input", feeder), contextlib.redirect_stdout(out):
            code = self.mod.main(list(args))
        text = out.getvalue()
        if expect is not None:
            self.assertEqual(code, expect, text)
        self.asked = feeder.asked
        return text

    def install(self, *extra, expect=0):
        return self.main("--yes", "--dir", self.folder, "--address", LAN, "--panel-url", self.panel,
                         "--panel-wait", "5", *extra, expect=expect)

    def calls(self, prefix=""):
        with open(self.log, encoding="utf-8") as f:
            return [l for l in f.read().splitlines() if l.startswith(prefix)]

    def put(self, name, value):
        with open(os.path.join(self.state, name), "w", encoding="utf-8") as f:
            json.dump(value, f)

    def get(self, name, default=None):
        try:
            with open(os.path.join(self.state, name), encoding="utf-8") as f:
                return json.load(f)
        except OSError:
            return default

    def assert_network_kept(self):
        self.assertNotIn("docker network rm openlobby", self.calls("docker"))
        self.assertIn("openlobby", self.get("networks", []))

    def kart_club(self):
        return {"id": "k-1", "state": "running", "role": "game", "title": "Kart Club", "key": "kart",
                "project": "kart", "workdir": os.path.join(self.tmp, "kart"), "service": "game"}

    # ---------------------------------------------------------------- Docker

    def test_docker_missing(self):
        self.missing.add("docker")
        out = self.main("--yes", "--dir", self.folder, expect=1)
        self.assertIn("Docker Desktop is not installed", out)
        self.assertIn("https://www.docker.com/products/docker-desktop/", out)
        self.assertEqual(self.calls(), [])
        self.assertFalse(os.path.exists(self.folder))
        self.system = "linux"
        out = self.main("--yes", "--dir", self.folder, expect=1)
        self.assertIn("https://docs.docker.com/engine/install/", out)

    def test_docker_not_running(self):
        self.put("docker_mode", "stopped")
        out = self.main("--yes", "--dir", self.folder, expect=1)
        self.assertIn("not running", out)
        self.assertIn("does not start it", out)
        # it asked Docker, and did nothing else
        self.assertEqual(self.calls(), ["docker info"])
        self.assertFalse(os.path.exists(self.folder))

    def test_docker_denied_on_linux(self):
        self.system = "linux"
        self.put("docker_mode", "denied")
        out = self.main("--yes", "--dir", self.folder, expect=1)
        self.assertIn("usermod -aG docker", out)

    # ---------------------------------------------------------------- first run

    def test_fresh_install_windows(self):
        out = self.install("--admin-user", "host")
        checkout = os.path.join(self.folder, "openlobby")
        self.assertIn("git clone https://github.com/PrettyOpenLobby/OpenLobby.git " + checkout, self.calls("git"))
        self.assertIn("docker network create openlobby", self.calls("docker"))
        script = self.calls("script")
        self.assertEqual(len(script), 1)
        self.assertIn("-ExecutionPolicy Bypass -File " + os.path.join(checkout, "setup.ps1"), script[0])
        for flag in ("-Yes", "-Images published", "-Reach lan", "-Address " + LAN, "-NoPs2",
                     "-AdminUser host", "-NoClient"):
            self.assertIn(flag, script[0])
        self.assertNotIn("-DryRun", script[0])
        # one line per step, in plain words
        for line in ("Getting OpenLobby...", "Making the shared Docker network (openlobby)...",
                     "Writing the settings...", "Starting OpenLobby", "Setting the admin panel's password...",
                     "Waiting for the admin panel...", "Note: a stand-in warning"):
            self.assertIn(line, out)
        self.assertNotIn("== ", out)
        # the closing block, the password once, the players file
        self.assertIn("OpenLobby is running.", out)
        self.assertIn("Password: Pw0nlyOnce12345678ab", out)
        with open(os.path.join(self.folder, "What to tell players.txt"), encoding="utf-8") as f:
            players = f.read()
        self.assertIn("The server's address: " + LAN, players)
        self.assertIn("-Server " + LAN, players)
        self.assertNotIn("Pw0nlyOnce", players)
        logs = os.listdir(os.path.join(self.folder, "logs"))
        self.assertEqual([n for n in logs if n.endswith(".log")], logs)
        with open(os.path.join(self.folder, "logs", logs[0]), encoding="utf-8") as f:
            log = f.read()
        self.assertNotIn("Pw0nlyOnce", log)
        self.assertIn("Password: (shown once", log)
        with open(os.path.join(self.folder, "setup-state.json"), encoding="utf-8") as f:
            state = json.load(f)
        self.assertEqual((state["reach"], state["address"], state["admin_user"], state["fetched_with"]),
                         ("lan", LAN, "host", "git"))
        self.assertFalse(os.path.exists(os.path.join(self.folder, "logs", "running.json")))

    def test_fresh_install_linux_asks_numbered_questions(self):
        self.system = "linux"
        answers = ["",              # the folder: the default from --dir
                   "3",             # anyone over the internet
                   PUBLIC,          # the public address
                   "n",             # no consoles
                   "",              # admin user: admin
                   ""]              # Start? yes
        out = self.main("--dir", self.folder, "--panel-url", self.panel, "--panel-wait", "5",
                        answers=answers)
        self.assertIn("  3. anyone over the internet", out)
        self.assertTrue(any("[%s]" % self.folder in q for q in self.asked), self.asked)
        self.assertTrue(any(q.startswith("Type a number [1]") for q in self.asked), self.asked)
        self.assertTrue(any("(Y/n)" in q for q in self.asked), self.asked)
        script = self.calls("script")[0]
        self.assertIn(os.path.join(self.folder, "openlobby", "setup.sh") + " --yes --images published", script)
        self.assertIn("--reach internet --public-address " + PUBLIC + " --no-ps2", script)
        self.assertNotIn("--lookup-public", script)

    def test_fresh_install_from_the_zip_without_git(self):
        self.missing.add("git")
        archive = os.path.join(self.tmp, "main.zip")
        with zipfile.ZipFile(archive, "w") as z:
            for base, _, files in os.walk(self.fixture):
                for name in files:
                    full = os.path.join(base, name)
                    rel = os.path.relpath(full, self.fixture).replace(os.sep, "/")
                    z.write(full, "OpenLobby-main/" + rel)
        url = "file:" + urllib.request.pathname2url(archive)
        self.install("--zip-url", url)
        checkout = os.path.join(self.folder, "openlobby")
        self.assertTrue(os.path.isfile(os.path.join(checkout, "setup.ps1")))
        self.assertTrue(os.path.isfile(os.path.join(checkout, "tools", "stack.py")))
        self.assertEqual(self.calls("git"), [])
        self.assertEqual(len(self.calls("script")), 1)
        with open(os.path.join(self.folder, "setup-state.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["fetched_with"], "zip")

    def test_dry_run_changes_nothing(self):
        out = self.main("--yes", "--dry-run", "--dir", self.folder, "--address", LAN)
        self.assertIn("would run: git clone", out)
        self.assertIn("-DryRun", out)
        self.assertFalse(os.path.exists(self.folder))
        self.assertEqual(self.calls("git"), [])
        self.assertEqual(self.calls("script"), [])
        self.assertFalse([c for c in self.calls("docker") if "create" in c])

    def test_failed_script(self):
        os.environ["STUB_SCRIPT_FAIL"] = "1"
        out = self.install(expect=None)
        self.assertIn("Setup stopped before the end", out)
        self.assertIn("docker compose up failed", out)
        self.assertFalse(os.path.exists(os.path.join(self.folder, "What to tell players.txt")))
        self.assertNotIn("OpenLobby is running.", out)

    def test_busy_port_is_named_with_its_holder(self):
        mod = load()
        log = os.path.join(self.tmp, "busy.log")
        for line, want in (
                ("Error response from daemon: ports are not available: exposing port UDP "
                 "192.0.2.20:53 -> 127.0.0.1:0: listen udp4 192.0.2.20:53: bind: Only one usage "
                 "of each socket address", "UDP port 53 on 192.0.2.20 is already in use by dnsd (process 4242)"),
                ("Error response from daemon: driver failed programming external connectivity on "
                 "endpoint x: Bind for 0.0.0.0:80 failed: port is already allocated",
                 "TCP port 80 on 0.0.0.0 is already in use by dnsd (process 4242)")):
            with open(log, "w", encoding="utf-8") as f:
                f.write("  Container openlobby-login-1 Started\n" + line + "\n")
            buf = io.StringIO()
            with mock.patch.object(mod, "port_holder", return_value="dnsd (process 4242)"), \
                    contextlib.redirect_stdout(buf):
                self.assertTrue(mod.explain_busy_port(log))
            self.assertIn(want, buf.getvalue())
            self.assertIn("then run OpenLobby Setup again", buf.getvalue())
        with open(log, "w", encoding="utf-8") as f:
            f.write("setup.ps1: docker compose up failed\n")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(mod.explain_busy_port(log))

    def test_other_game_is_mentioned_not_changed(self):
        self.put("containers", [self.kart_club()])
        out = self.install()
        self.assertIn("A game server here, Kart Club, can be managed from this panel: open Settings > Modules >",
                      out)
        self.assertIn("Pair with a setup program", out)
        self.assertFalse([c for c in self.calls("docker") if "k-1" in c])
        self.assertIn(self.kart_club(), self.get("containers"))

    # ---------------------------------------------------------------- later runs

    def test_second_run_menu_and_status(self):
        self.install()
        open(self.log, "w").close()
        out = self.main("--dir", self.folder, "--panel-url", self.panel, answers=["0"])
        for line in ("OpenLobby: running", "Players use:   " + LAN, "Admin panel:   " + self.panel,
                     "What to tell players.txt", "1. Update", "2. Change settings", "3. Stop OpenLobby",
                     "4. Show what to tell players", "5. Check that consoles can reach this server",
                     "6. Start over", "7. Uninstall", "0. Quit"):
            self.assertIn(line, out)
        self.assertEqual(self.calls("script"), [])
        self.assertEqual(self.calls("git"), [])

    def test_second_run_finds_the_install_by_its_panel(self):
        self.install()
        out = self.main("--panel-url", self.panel, answers=["0"])
        self.assertIn("Folder:        " + self.folder, out)

    def test_update(self):
        self.install()
        open(self.log, "w").close()
        out = self.main("--dir", self.folder, "--panel-url", self.panel, "--panel-wait", "5", answers=["1"])
        self.assertIn("git -C %s pull --ff-only" % os.path.join(self.folder, "openlobby"), self.calls("git"))
        script = self.calls("script")
        self.assertEqual(len(script), 1)
        self.assertIn("-Yes -Images published -NoClient", script[0])
        self.assertNotIn("-Reach", script[0])
        self.assertIn("OpenLobby is running.", out)

    def test_change_settings(self):
        self.install()
        open(self.log, "w").close()
        self.main("--dir", self.folder, "--change-settings", "--yes", "--address", "198.51.100.7",
                  "--panel-url", self.panel, "--panel-wait", "5")
        script = self.calls("script")[0]
        self.assertIn("-Reach lan -Address 198.51.100.7", script)
        with open(os.path.join(self.folder, "openlobby", ".env"), encoding="utf-8") as f:
            self.assertIn("POL_ADVERTISE=198.51.100.7", f.read())

    def test_stop_and_start(self):
        self.install()
        self.main("--dir", self.folder, "--stop", "--yes")
        states = {r["id"]: r["state"] for r in self.get("containers")}
        self.assertEqual(set(states.values()), {"exited"})
        out = self.main("--dir", self.folder, "--panel-url", self.panel, answers=["0"])
        self.assertIn("3. Start OpenLobby", out)
        self.main("--dir", self.folder, "--start", "--panel-url", self.panel, "--panel-wait", "5")
        self.assertEqual({r["state"] for r in self.get("containers")}, {"running"})
        self.assert_network_kept()

    def test_stop_keeps_shared_dns_and_http_for_other_installs(self):
        self.install()
        rows = self.get("containers") + [self.kart_club()]
        self.put("containers", rows)
        out = self.main("--dir", self.folder, "--stop", "--yes")
        states = {r["id"]: r["state"] for r in self.get("containers")}
        self.assertEqual(states["c-admin"], "exited")
        self.assertEqual(states["c-dns"], "running")
        self.assertEqual(states["c-http"], "running")
        self.assertEqual(states["k-1"], "running")
        self.assertIn("keep running", out)

    def test_uninstall_keeps_data_and_network(self):
        self.install()
        self.main("--dir", self.folder, "--uninstall", "--yes")
        self.assertEqual(self.get("containers"), [])
        self.assertEqual(set(self.get("volumes")), {"openlobby_pol-data", "openlobby_pol-pgdata"})
        self.assertFalse([c for c in self.calls("docker") if c.startswith("docker volume rm")])
        self.assert_network_kept()
        self.assertTrue(os.path.isfile(os.path.join(self.folder, "openlobby", ".env")))

    def test_windows_install_runs_the_deployer_and_uninstall_takes_it(self):
        # the script runs with -Yes and never -NoDeployer, so setup.ps1 sets up the deployer
        with mock.patch.dict(os.environ, {"STUB_DEPLOYER": "1"}):
            self.install()
        script = self.calls("script")[0]
        self.assertIn("-Yes", script)
        self.assertNotIn("Deployer", script)
        with open(os.path.join(ROOT, "setup.ps1"), encoding="utf-8") as f:
            self.assertIn("YesNo 'Run the deployer?' $true", f.read())
        self.assertIn("c-deployer", {r["id"] for r in self.get("containers")})
        self.main("--dir", self.folder, "--uninstall", "--delete-data", "--yes")
        self.assertEqual(self.get("containers"), [])
        self.assertNotIn("openlobby_pol-deployer", self.get("volumes"))
        self.assert_network_kept()

    def test_uninstall_asks_and_defaults_to_no(self):
        self.install()
        self.main("--dir", self.folder, answers=["7", "", ""])     # Uninstall; keep data; not now
        self.assertEqual(len(self.get("containers")), 4)

    def test_uninstall_with_data_next_to_another_game(self):
        self.install()
        clients = os.path.join(self.folder, "clients")
        os.makedirs(clients)
        self.put("containers", self.get("containers") + [self.kart_club()])
        vols = self.get("volumes")
        vols["kart_data"] = "kart"
        self.put("volumes", vols)
        out = self.main("--dir", self.folder, "--uninstall", "--delete-data", "--yes")
        left = {r["id"] for r in self.get("containers")}
        self.assertEqual(left, {"c-dns", "c-http", "k-1"})
        # the volume the kept DNS and port 80 use stays, the database goes
        self.assertEqual(set(self.get("volumes")), {"openlobby_pol-data", "kart_data"})
        self.assertIn("Kept openlobby_pol-data", out)
        self.assert_network_kept()
        self.assertFalse(os.path.exists(os.path.join(self.folder, "openlobby")))
        self.assertTrue(os.path.isdir(clients))

    def _game_folders(self):
        games = os.path.join(self.folder, "openlobby", "games")
        os.makedirs(games, exist_ok=True)
        for key, sub in (("kart", "kartclub"), ("bowl", "bowlnight"), ("core", "openlobby")):
            with open(os.path.join(games, key + ".json"), "w", encoding="utf-8") as f:
                json.dump({"key": key, "kind": "core" if key == "core" else "game", "checkout": sub}, f)
        os.makedirs(os.path.join(self.folder, "kartclub", ".git"))       # cloned by the deployer
        os.makedirs(os.path.join(self.folder, "bowlnight"))              # same name, not a checkout
        os.makedirs(os.path.join(self.folder, "mine", ".git"))           # not in the catalog

    def test_uninstall_with_data_takes_the_games_the_panel_installed(self):
        self.install()
        self._game_folders()
        out = self.main("--dir", self.folder, "--uninstall", "--delete-data", "--yes")
        self.assertFalse(os.path.exists(os.path.join(self.folder, "kartclub")))
        self.assertTrue(os.path.isdir(os.path.join(self.folder, "bowlnight")))
        self.assertTrue(os.path.isdir(os.path.join(self.folder, "mine")))
        self.assertFalse(os.path.exists(os.path.join(self.folder, "openlobby")))
        self.assertIn("its data and its games are gone", out)

    def test_uninstall_keeping_data_keeps_the_games(self):
        self.install()
        self._game_folders()
        self.main("--dir", self.folder, "--uninstall", "--yes")
        self.assertTrue(os.path.isdir(os.path.join(self.folder, "kartclub", ".git")))

    def test_start_over_keeps_a_copy_of_the_settings(self):
        self.install()
        open(self.log, "w").close()
        self.main("--dir", self.folder, "--panel-url", self.panel, "--panel-wait", "5",
                  answers=["6", "start over", "", "1", LAN, "", "", ""])
        backups = os.listdir(os.path.join(self.folder, "backups"))
        self.assertEqual(len(backups), 1)
        self.assertIn("docker volume rm openlobby_pol-pgdata", self.calls("docker"))
        self.assertEqual(len(self.calls("script")), 1)
        self.assert_network_kept()

    def test_start_over_needs_the_words(self):
        self.install()
        self.main("--dir", self.folder, answers=["6", "yes"])
        self.assertEqual(len(self.get("containers")), 4)
        self.assertFalse(os.path.exists(os.path.join(self.folder, "backups")))

    def test_picks_up_a_script_left_running(self):
        self.install()
        logs = os.path.join(self.folder, "logs")
        log = os.path.join(logs, os.listdir(logs)[0])
        with open(log, "a", encoding="utf-8") as f:
            f.write("  Password: Later0nly\n")
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        with open(os.path.join(logs, "running.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": p.pid, "log": log, "what": "install", "started": "earlier"}, f)
        out = self.main("--dir", self.folder, "--panel-url", self.panel)
        self.assertIn("finished while this window was closed", out)
        self.assertIn("OpenLobby is running.", out)
        self.assertFalse(os.path.exists(os.path.join(logs, "running.json")))

    # ---------------------------------------------------------------- the scripts

    # ---------------------------------------------------------------- the dev channel

    def test_dev_channel_hands_the_code_over_in_a_private_file(self):
        code = "kart-dev-7Qm2x9Lp4Vb8"
        cf = os.path.join(self.tmp, "code.txt")
        with open(cf, "w", encoding="utf-8") as f:
            f.write(code + "\n")
        self.install("--channel", "dev", "--registry", "registry.example.org/kart", "--code-file", cf)
        script = self.calls("script")[0]
        self.assertIn("-Channel dev -Registry registry.example.org/kart -CodeFile ", script)
        self.assertNotIn(code, script)
        self.assertEqual(self.get("channel_code"), code)
        handed = script.split("-CodeFile ", 1)[1].split(" ")[0]
        self.assertNotEqual(os.path.normcase(handed), os.path.normcase(cf))
        self.assertFalse(os.path.exists(handed), "the temporary code file is removed")
        self.assertEqual(self.mod.CHANNEL, {"channel": "dev", "registry": "registry.example.org/kart"})

    def test_dev_channel_asks_the_code_hidden(self):
        self.system = "linux"
        code = "kart-dev-second-55Zr"
        asked = []
        with mock.patch.object(self.mod.getpass, "getpass", lambda prompt: asked.append(prompt) or code):
            self.main("--dir", self.folder, "--panel-url", self.panel, "--panel-wait", "5",
                      "--channel", "dev", answers=["", "1", LAN, "n", "", ""])
        self.assertEqual(asked, ["Access code (not shown): "])
        # only the code is asked for: an access code carries where the dev builds are
        self.assertFalse(any("registry" in q.lower() for q in self.asked), self.asked)
        script = self.calls("script")[0]
        self.assertIn("--channel dev --code-file ", script)
        self.assertNotIn("--registry", script)
        self.assertNotIn(code, script)
        self.assertEqual(self.get("channel_code"), code)

    def test_dev_channel_refusals(self):
        out = self.main("--yes", "--dir", self.folder, "--channel", "dev", "--registry",
                        "registry.example.org/kart", expect=2)
        self.assertIn("needs --code-file", out)
        out = self.main("--yes", "--dir", self.folder, "--channel", "dev", "--registry", "Not A Registry",
                        expect=2)
        self.assertIn("looks like registry.example.org/team", out)
        self.assertEqual(self.calls("script"), [])

    def test_flags_exist_in_the_scripts(self):
        self.mod.CHANNEL.update(channel="dev", registry="registry.example.org/kart",
                                code_file=os.path.join(self.tmp, "code"))
        self.addCleanup(self.mod.CHANNEL.clear)
        inst = self.mod.Install(self.folder)
        settings = {"reach": "internet", "address": PUBLIC, "lookup": True, "ps2": True}
        with open(os.path.join(ROOT, "setup.sh"), encoding="utf-8") as f:
            sh = f.read()
        with open(os.path.join(ROOT, "setup.ps1"), encoding="utf-8") as f:
            ps1 = f.read()
        self.system = "linux"
        flags = [a for a in self.mod.script_command(inst, settings, "host", dry_run=True) if a.startswith("--")]
        flags += ["--address", "--no-ps2"]
        for flag in flags:
            # a case label in the option loop, alone or among others: --yes|-y)
            self.assertRegex(sh, r"(?m)^\s+(?:[-\w]+\|)*%s(?:\|[-\w]+)*\)" % re.escape(flag), flag)
        self.system = "windows"
        cmd = self.mod.script_command(inst, settings, "host", client=True, dry_run=True)
        flags = [a for a in cmd[cmd.index("-File") + 2:] if a.startswith("-")] + ["-Address", "-NoPs2", "-NoClient"]
        for flag in flags:
            self.assertRegex(ps1, r"\]\$%s\b" % flag[1:], flag)

    def test_game_data_lines_are_shown(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for line in ("== Settings (.env)", "wrote .env", "", "== Game data",
                         "Downloading the track data for Kart Club: about 400 MB, only this once. "
                         "This can take a while...", "kart_tracks: 3 files, ready.",
                         "== Starting the stack", "Enabled games: kart"):
                self.mod.show_line(line)
        text = out.getvalue()
        self.assertIn("Getting the games' data", text)
        self.assertIn("    Downloading the track data for Kart Club", text)
        self.assertIn("    kart_tracks: 3 files, ready.", text)
        self.assertNotIn("wrote .env", text)
        self.assertNotIn("Enabled games", text)

    def test_scripts_set_up_game_data_before_starting(self):
        for name, call in (("setup.sh", '"$CHECKOUT/tools/stack.py" volumes'),
                           ("setup.ps1", "'tools\\stack.py'), 'volumes'")):
            with open(os.path.join(ROOT, name), encoding="utf-8") as f:
                text = f.read()
            self.assertIn(call, text, name)
            self.assertIn("Game data", self.mod.STEPS)
            self.assertLess(text.index("Game data"), text.index("Starting the stack"), name)
            self.assertLess(text.index(call), text.index("Starting the stack"), name)

    # ---------------------------------------------------------------- consoles and firewall

    def network(self, profile="Private", blocks=()):
        self.put("fw_status", {"addresses": [
            {"ip": LAN, "alias": "Ethernet", "profile": profile},
            {"ip": "192.0.2.77", "alias": "Wi-Fi", "profile": "Private"},
            {"ip": "198.18.5.1", "alias": "vEthernet (WSL)", "profile": ""}], "blocks": list(blocks)})

    def test_check_all_good(self):
        self.install()
        self.network()
        self.dns = {LAN: (0, [LAN]), "127.0.0.1": (0, [LAN])}
        out = self.main("--dir", self.folder, "--check")
        self.assertIn((LAN, "wh000.pol.com"), self.dns_asked)
        self.assertIn(("127.0.0.1", "wh000.pol.com"), self.dns_asked)
        for line in ("ok    The DNS service is running (openlobby).",
                     "ok    %s answers wh000.pol.com with %s (the address consoles use)." % (LAN, LAN),
                     "ok    %s is on Ethernet (network type: Private)." % LAN,
                     "rule: OpenLobby UDP 53 (Private), from LocalSubnet",
                     "ok    Windows Firewall has the OpenLobby rules (2), DNS included.",
                     "Windows Firewall does not stop a",
                     "set the primary DNS server to",
                     "nslookup wh000.pol.com %s" % LAN,
                     "Nothing to fix found."):
            self.assertIn(line, out)
        # another real network is named, the virtual one is not
        self.assertIn("more than one network: %s (Ethernet), 192.0.2.77 (Wi-Fi)" % LAN, out)
        self.assertNotIn("198.18.5.1", out)
        self.assertNotIn("FIX", out)

    def test_check_runs_after_install_and_from_the_menu(self):
        out = self.install()
        self.assertIn("Can consoles reach this server?", out)
        self.assertLess(out.index("OpenLobby is running."), out.index("Can consoles reach this server?"))
        self.assertIn("powershell firewall status", self.calls("powershell"))
        out = self.main("--dir", self.folder, answers=["5", "0"])
        self.assertEqual(out.count("Can consoles reach this server?"), 1)

    def test_check_names_what_to_fix(self):
        self.install()
        self.put("fw_rules", [])
        self.network(profile="Public", blocks=["Docker Desktop Backend (Public)"])
        self.dns = {"127.0.0.1": (0, ["203.0.113.50"])}
        rows = [r for r in self.get("containers") if r["service"] != "dns"]
        self.put("containers", rows)
        out = self.main("--dir", self.folder, "--check", expect=1)
        for line in ("FIX   The DNS service is not running",
                     "FIX   No answer from %s port 53 (the address consoles use)." % LAN,
                     "FIX   127.0.0.1 answers wh000.pol.com with 203.0.113.50, not this server's address",
                     "FIX   Windows treats the network on Ethernet as Public",
                     "Network profile type > Private network",
                     "FIX   Windows Firewall has no OpenLobby rules",
                     "FIX   Windows Firewall blocks Docker Desktop: Docker Desktop Backend (Public).",
                     "6 things to fix, listed above."):
            self.assertIn(line, out)

    def test_check_docker_allow_rule_is_a_note_not_a_fix(self):
        self.install()
        self.put("fw_rules", [])
        self.network()
        status = self.get("fw_status")
        status["allows"] = ["Private"]
        self.put("fw_status", status)
        self.dns = {LAN: (0, [LAN]), "127.0.0.1": (0, [LAN])}
        out = self.main("--dir", self.folder, "--check")
        self.assertIn("Docker Desktop's own allow rule covers this network (Private)", out)
        self.assertIn("Nothing to fix found.", out)

    def test_check_with_dns_pinned_skips_loopback(self):
        self.install()
        env = os.path.join(self.folder, "openlobby", ".env")
        with open(env, "a", encoding="utf-8") as f:
            f.write("POL_DNS_BIND=%s\n" % LAN)
        self.network()
        self.dns = {LAN: (0, [LAN])}
        self.dns_asked.clear()
        out = self.main("--dir", self.folder, "--check")
        self.assertNotIn(("127.0.0.1", "wh000.pol.com"), self.dns_asked)
        self.assertIn("listens on %s only (POL_DNS_BIND)" % LAN, out)
        self.assertIn("Nothing to fix found.", out)

    def test_check_address_not_on_this_computer(self):
        self.install()
        self.put("fw_status", {"addresses": [{"ip": "192.0.2.99", "alias": "Ethernet", "profile": "Private"}],
                               "blocks": []})
        self.dns = {LAN: (0, [LAN]), "127.0.0.1": (0, [LAN])}
        out = self.main("--dir", self.folder, "--check", expect=1)
        self.assertIn("FIX   %s is not an address of this computer right now." % LAN, out)

    def test_check_on_linux_has_no_firewall_part(self):
        self.system = "linux"
        self.install()
        open(self.log, "w").close()
        self.dns = {LAN: (0, [LAN]), "127.0.0.1": (0, [LAN])}
        out = self.main("--dir", self.folder, "--check")
        self.assertEqual(self.calls("powershell"), [])
        self.assertNotIn("Windows Firewall", out)
        self.assertIn("Nothing to fix found.", out)

    def test_uninstall_removes_the_firewall_rules(self):
        self.install()
        self.assertEqual(len(self.get("fw_rules")), 2)
        self.main("--dir", self.folder, "--uninstall", "--yes")
        removed = [c for c in self.calls("powershell") if "Remove-NetFirewallRule" in c]
        self.assertEqual(len(removed), 1, self.calls("powershell"))
        self.assertIn("Start-Process powershell.exe -Verb RunAs", removed[0])
        self.assertIn("Remove-NetFirewallRule -Group OpenLobby", removed[0])
        self.assertEqual(self.get("fw_rules"), [])
        # nothing left: no second UAC prompt
        open(self.log, "w").close()
        self.install()
        self.put("fw_rules", [])
        self.main("--dir", self.folder, "--uninstall", "--yes")
        self.assertFalse([c for c in self.calls("powershell") if "Remove-NetFirewallRule" in c])

    def test_uninstall_says_how_when_the_uac_prompt_is_declined(self):
        self.install()
        self.put("uac_ok", False)
        out = self.main("--dir", self.folder, "--uninstall", "--yes")
        self.assertIn("The firewall rules were not removed", out)
        self.assertIn("Remove-NetFirewallRule -Group OpenLobby", out)
        self.assertEqual(self.get("containers"), [])       # the uninstall itself went on

    def test_uninstall_keeps_the_rules_another_install_uses(self):
        self.install()
        self.put("containers", self.get("containers") + [self.kart_club()])
        out = self.main("--dir", self.folder, "--uninstall", "--yes")
        self.assertIn("The Windows Firewall rules (group OpenLobby) stay", out)
        self.assertEqual(len(self.get("fw_rules")), 2)

    def test_firewall_step_lines_are_shown(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for line in ("== Windows Firewall", "Windows treats the network on Wi-Fi as Public, a network it does not",
                         "== Admin panel password", "Set a new password for admin."):
                self.mod.show_line(line)
        text = out.getvalue()
        self.assertIn("Letting players through Windows Firewall", text)
        self.assertIn("    Windows treats the network on Wi-Fi as Public", text)
        self.assertNotIn("Set a new password", text)

    def test_dns_answer_parser(self):
        reply = bytes.fromhex("1234818000010001000000000377683003706f6c03636f6d0000010001"
                              "c00c000100010000003c0004c0000214")
        self.assertEqual(self.mod.parse_dns_answer(reply), (0, ["192.0.2.20"]))
        refused = bytes.fromhex("123481850001000000000000")
        self.assertEqual(self.mod.parse_dns_answer(refused), (5, []))

    def test_program_is_ascii(self):
        with open(PROGRAM, "rb") as f:
            raw = f.read()
        self.assertTrue(all(b < 128 for b in raw), "openlobby_setup.py must be ASCII")


if __name__ == "__main__":
    unittest.main()
