#!/usr/bin/env python3
"""Offline tests for the first-run scripts, setup.sh and setup.ps1.

    python tools/setup_test.py

setup.sh runs in a scratch directory with stand-ins for docker, git, ip, ss,
python3, sleep and chmod first on PATH, so nothing reaches Docker, the
network or the real machine's settings. The stand-ins log every call, and
the tests check the .env the script writes for each answer to "who plays",
that the secrets are random and survive a second run, that .env is made
private, and that the script never removes containers or volumes. Needs
bash (Git Bash on Windows); without it those tests are skipped.

setup.ps1 is parse-checked with Windows PowerShell, then run with -DryRun and
-NoStart against PowerShell functions standing in for docker, git and the
address cmdlets. Skipped where powershell.exe is missing.

shellcheck runs on setup.sh when it is installed.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SETUP_SH = os.path.join(ROOT, "setup.sh")
SETUP_PS1 = os.path.join(ROOT, "setup.ps1")

LAN = "192.0.2.20"
VPN = "198.51.100.5"
PUBLIC = "203.0.113.10"
# Tailscale hands out /32 addresses in the shared (RFC 6598) range, which the
# repository scanners would otherwise read as a leaked private address.
TS_ADDR = "100.101.102.103"  # polcheck: allow (test data in the shared range)
TS_NET = "100.64.0.0/10"  # polcheck: allow (the shared range itself)

IP_ADDR = """\
1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever
2: eth0    inet {lan}/24 brd 192.0.2.255 scope global dynamic eth0\\       valid_lft 80000sec
3: docker0    inet 198.18.0.1/16 brd 198.18.255.255 scope global docker0\\       valid_lft forever
{vpn}"""

STUB_DOCKER = r"""#!/bin/sh
printf '%s\n' "docker $*" >> "$STUB_LOG"
case "$*" in
  "--version") echo "Docker version 27.0.0, build test" ;;
  "compose version") echo "Docker Compose version v2.29.1" ;;
  "compose version --short") echo "2.29.1" ;;
  "info")
    if [ -n "${STUB_DOCKER_DENIED:-}" ]; then
      echo "permission denied while trying to connect to the Docker daemon socket" >&2; exit 1
    fi
    echo "Server: test" ;;
  "info --format "*) echo "Ubuntu 24.04 LTS" ;;
  "volume inspect "*) exit 1 ;;
  "network inspect "*) exit 1 ;;
  "compose ps "*) printf 'admin running healthy\ndns running healthy\npostgres running healthy\n' ;;
  *"--show-admin"*)
    if [ -f "$STUB_STATE/admin-password" ]; then echo "[admin] user='admin' set 2026-01-01"
    else echo "[admin] no stored credential; the panel is OPEN"; fi ;;
  *"--set-password"*)
    IFS= read -r pw
    printf '%s' "$pw" > "$STUB_STATE/admin-password"
    echo "[admin] credential set for 'admin'; all sessions revoked" ;;
esac
exit 0
"""

STUB_GIT = r"""#!/bin/sh
printf '%s\n' "git $*" >> "$STUB_LOG"
case "$1" in
  --version) echo "git version 2.45.0" ;;
  clone) mkdir -p "$3" && cp -R "$STUB_FIXTURE/." "$3/" ;;
esac
exit 0
"""

STUB_IP = r"""#!/bin/sh
printf '%s\n' "ip $*" >> "$STUB_LOG"
case "$*" in
  "-4 route show default") echo "default via 192.0.2.1 dev eth0 proto dhcp metric 100" ;;
  "-o -4 addr show") cat "$STUB_IPADDR" ;;
esac
exit 0
"""

STUB_SS = r"""#!/bin/sh
printf '%s\n' "ss $*" >> "$STUB_LOG"
[ -n "${STUB_PORT53_BUSY:-}" ] && echo "UNCONN 0 0 127.0.0.53%lo:53 0.0.0.0:*"
exit 0
"""

# tools/stack.py compose ... is handed to the docker stand-in
STUB_PYTHON = r"""#!/bin/sh
printf '%s\n' "python3 $*" >> "$STUB_LOG"
if [ "$1" = tools/stack.py ] && [ "$2" = compose ]; then
  shift 2
  [ "$1" = --ghcr ] && shift
  exec docker compose "$@"
fi
exit 0
"""

STUB_SLEEP = "#!/bin/sh\nexit 0\n"
STUB_CURL = "#!/bin/sh\nprintf '%s\\n' \"curl $*\" >> \"$STUB_LOG\"\necho 203.0.113.99\n"
STUB_CHMOD = "#!/bin/sh\nprintf '%s\\n' \"chmod $*\" >> \"$STUB_LOG\"\nexec {real} \"$@\"\n"


def find_bash():
    """Git Bash on Windows (never the WSL launcher in System32), else bash.
    Git's usr/bin/bash.exe, since its bin/bash.exe wrapper puts Git's own
    tools ahead of the stand-ins on PATH."""
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            base = os.path.dirname(os.path.dirname(os.path.realpath(git)))
            for cand in (os.path.join(base, "usr", "bin", "bash.exe"),
                         os.path.join(os.path.dirname(base), "usr", "bin", "bash.exe")):
                if os.path.isfile(cand):
                    return cand
        b = shutil.which("bash")
        if b and "system32" not in b.lower():
            return b
        return None
    return shutil.which("bash")


BASH = find_bash()
POWERSHELL = shutil.which("powershell") if os.name == "nt" else None


def bash_path(bash, p):
    """The path as bash spells it (an MSYS path on Windows)."""
    if os.name != "nt":
        return p
    out = subprocess.run([bash, "-c", 'cygpath -u "$1"', "_", p],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def parse_env(text):
    env = {}
    for line in text.splitlines():
        m = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line)
        if m:
            env[m.group(1)] = m.group(2)
    return env


@unittest.skipUnless(BASH, "bash not found")
class SetupShTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="olsetup-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = os.path.join(self.tmp, "bin")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.bin)
        os.makedirs(self.state)
        self.log = os.path.join(self.tmp, "calls.log")
        open(self.log, "w").close()
        # A minimal checkout: the files setup.sh looks for, and the real
        # setup.sh and .env.example.
        self.fixture = os.path.join(self.tmp, "fixture")
        os.makedirs(os.path.join(self.fixture, "tools"))
        for name in ("setup.sh", ".env.example", "docker-compose.yml"):
            shutil.copy(os.path.join(ROOT, name), os.path.join(self.fixture, name))
        for name in ("stack.py", "doctor.py"):
            with open(os.path.join(self.fixture, "tools", name), "w") as f:
                f.write("# stand-in\n")
        self.srv = os.path.join(self.tmp, "srv")
        self.checkout = os.path.join(self.srv, "openlobby")
        shutil.copytree(self.fixture, self.checkout)
        self.ipaddr = os.path.join(self.tmp, "ipaddr.txt")
        self.set_vpn("    4: wg0    inet %s/24 scope global wg0\\       valid_lft forever" % VPN)
        real_chmod = subprocess.run([BASH, "-c", "command -v chmod"], capture_output=True,
                                    text=True).stdout.strip() or "/bin/chmod"
        stubs = {"docker": STUB_DOCKER, "git": STUB_GIT, "ip": STUB_IP, "ss": STUB_SS,
                 "python3": STUB_PYTHON, "sleep": STUB_SLEEP, "curl": STUB_CURL,
                 "chmod": STUB_CHMOD.format(real=real_chmod)}
        for name, body in stubs.items():
            p = os.path.join(self.bin, name)
            with open(p, "w", newline="\n") as f:
                f.write(body)
            os.chmod(p, 0o755)

    def set_vpn(self, line):
        with open(self.ipaddr, "w", newline="\n") as f:
            f.write(IP_ADDR.format(lan=LAN, vpn=line.strip()) + "\n")

    def run_sh(self, *args, script=None, extra_env=None, expect=0):
        env = dict(os.environ)
        for k in list(env):
            if k.startswith("OPENLOBBY_") or k in ("SSH_CONNECTION", "COMPOSE_FILE"):
                del env[k]
        env.update({
            "PATH": self.bin + os.pathsep + env.get("PATH", ""),
            "HOME": self.tmp,
            "STUB_LOG": self.log.replace("\\", "/"),
            "STUB_STATE": self.state.replace("\\", "/"),
            "STUB_FIXTURE": self.fixture.replace("\\", "/"),
            "STUB_IPADDR": self.ipaddr.replace("\\", "/"),
        })
        env.update(extra_env or {})
        script = script or os.path.join(self.checkout, "setup.sh")
        p = subprocess.run([BASH, script] + list(args), env=env, capture_output=True,
                           text=True, stdin=subprocess.DEVNULL, timeout=120)
        out = p.stdout + p.stderr
        if expect is not None:
            self.assertEqual(p.returncode, expect, out)
        return out

    def calls(self):
        with open(self.log) as f:
            return f.read().splitlines()

    def env(self, checkout=None):
        with open(os.path.join(checkout or self.checkout, ".env"), encoding="utf-8") as f:
            text = f.read()
        return text, parse_env(text)

    def assert_never_removes(self):
        for c in self.calls():
            self.assertNotRegex(c, r"\bdown\b|\brm\b|volume rm|prune|-v\b(?!ersion)", c)

    # ---------------------------------------------------------------- cases

    def test_lan_writes_env_with_random_secrets(self):
        out = self.run_sh("--yes", "--reach", "lan", "--no-ps2")
        text, env = self.env()
        self.assertEqual(env["POL_ADVERTISE"], LAN)
        self.assertEqual(env.get("POL_DNS_BIND", ""), "")
        self.assertNotIn("POL_DNS_CLIENTS", env)
        self.assertEqual(env["COMPOSE_FILE"], "docker-compose.yml:docker-compose.ghcr.yml")
        self.assertRegex(env["POL_DB_PASSWORD"], r"^[A-Za-z0-9]{32}$")
        self.assertRegex(env["POL_DEPLOYER_TOKEN"], r"^[A-Za-z0-9_-]{43}$")
        self.assertEqual(env["POL_DEPLOY_ROOT"], bash_path(BASH, self.srv))
        self.assertEqual(env["POL_DEPLOY_GHCR"], "1")
        self.assertEqual(env["POL_DEPLOY_CLIENTS"], bash_path(BASH, os.path.join(self.srv, "clients")))
        self.assertTrue(os.path.isdir(os.path.join(self.srv, "clients")))
        self.assertNotIn("COMPOSE_PROFILES", env)      # docs/deployer.md: jobs refuse it
        self.assertEqual(env.get("POL_ADMIN_PASSWORD", ""), "")
        # the example's comments are kept
        self.assertIn("# Copy to .env and edit.", text)
        # made private
        self.assertTrue(any(c.startswith("chmod 600") for c in self.calls()), self.calls())
        if os.name != "nt":
            self.assertEqual(os.stat(os.path.join(self.checkout, ".env")).st_mode & 0o777, 0o600)
        calls = self.calls()
        self.assertIn("docker compose pull", calls)
        self.assertIn("docker compose up -d", calls)
        self.assertIn("docker compose --profile deployer up -d deployer", calls)
        self.assertIn("python3 tools/doctor.py", calls)
        # the admin password went over stdin, never on a command line
        with open(os.path.join(self.state, "admin-password")) as f:
            pw = f.read()
        self.assertRegex(pw, r"^[A-Za-z0-9]{20}$")
        self.assertIn("Password: " + pw, out)
        self.assertFalse([c for c in calls if pw in c])
        self.assertIn("-Server " + LAN, out)
        self.assertIn("http://127.0.0.1:8090", out)
        self.assertIn("Settings > Install games", out)
        self.assert_never_removes()
        self.assertNotIn("curl", "\n".join(calls))       # no lookup unless asked

    def test_secrets_differ_between_installs(self):
        self.run_sh("--yes", "--reach", "lan", "--no-start")
        _, a = self.env()
        os.remove(os.path.join(self.checkout, ".env"))
        self.run_sh("--yes", "--reach", "lan", "--no-start")
        _, b = self.env()
        self.assertNotEqual(a["POL_DB_PASSWORD"], b["POL_DB_PASSWORD"])
        self.assertNotEqual(a["POL_DEPLOYER_TOKEN"], b["POL_DEPLOYER_TOKEN"])

    def test_second_run_keeps_everything(self):
        self.run_sh("--yes", "--reach", "lan")
        before_text, before = self.env()
        with open(os.path.join(self.state, "admin-password")) as f:
            pw = f.read()
        open(self.log, "w").close()
        out = self.run_sh("--yes")
        after_text, after = self.env()
        self.assertEqual(before_text, after_text)
        self.assertIn("keeping its values", out)
        self.assertIn("password unchanged", out)
        self.assertNotIn(pw, out)
        self.assertFalse([c for c in self.calls() if "--set-password" in c])
        with open(os.path.join(self.state, "admin-password")) as f:
            self.assertEqual(f.read(), pw)
        self.assert_never_removes()

    def test_rerun_changes_only_the_address(self):
        self.run_sh("--yes", "--reach", "lan", "--no-start")
        _, before = self.env()
        out = self.run_sh("--yes", "--reach", "vpn", "--no-ps2", "--no-start")
        _, after = self.env()
        self.assertEqual(after["POL_ADVERTISE"], VPN)
        self.assertEqual(after["POL_DNS_BIND"], VPN)
        self.assertEqual(after["POL_DNS_CLIENTS"], "127.0.0.0/8,198.51.100.0/24")
        for k in ("POL_DB_PASSWORD", "POL_DEPLOYER_TOKEN", "POL_DEPLOY_ROOT", "COMPOSE_FILE"):
            self.assertEqual(before[k], after[k], k)
        self.assertTrue(os.path.isfile(os.path.join(self.checkout, ".env.bak")))
        self.assertIn("Wrote", out)

    def test_vpn_with_console_on_the_lan(self):
        self.run_sh("--yes", "--reach", "vpn", "--ps2", "--no-start")
        _, env = self.env()
        self.assertEqual(env["POL_ADVERTISE"], VPN)
        self.assertEqual(env["POL_ADVERTISE_LAN"], LAN)
        self.assertEqual(env.get("POL_DNS_BIND", ""), "")
        self.assertEqual(env["POL_DNS_CLIENTS"], "127.0.0.0/8,198.51.100.0/24,192.0.2.0/24")

    def test_vpn_tailscale_32(self):
        self.set_vpn("4: tailscale0    inet %s/32 scope global tailscale0\\       valid_lft forever" % TS_ADDR)
        self.run_sh("--yes", "--reach", "vpn", "--no-ps2", "--no-start")
        _, env = self.env()
        self.assertEqual(env["POL_ADVERTISE"], TS_ADDR)
        self.assertEqual(env["POL_DNS_CLIENTS"], "127.0.0.0/8," + TS_NET)

    def test_internet_pc_only(self):
        out = self.run_sh("--yes", "--reach", "internet", "--public-address", PUBLIC,
                          "--no-ps2", "--no-start")
        _, env = self.env()
        self.assertEqual(env["POL_ADVERTISE"], PUBLIC)
        self.assertEqual(env.get("POL_ADVERTISE_PUBLIC", ""), "")
        self.assertEqual(env.get("POL_DNS_PUBLIC", ""), "")
        # the test LAN is not a private range, so the resolver goes to loopback
        self.assertEqual(env["POL_DNS_BIND"], "127.0.0.1")
        self.assertNotIn("curl", "\n".join(self.calls()))
        self.assertIn("Not starting", out)

    def test_internet_with_consoles(self):
        out = self.run_sh("--yes", "--reach", "internet", "--public-address", PUBLIC, "--ps2")
        _, env = self.env()
        self.assertEqual(env["POL_ADVERTISE"], PUBLIC)
        self.assertEqual(env["POL_ADVERTISE_PUBLIC"], PUBLIC)
        self.assertEqual(env["POL_DNS_PUBLIC"], "1")
        self.assertEqual(env.get("POL_DNS_BIND", ""), "")
        self.assertIn("54000", out)
        self.assertIn("53/udp too", out)

    def test_internet_lookup_only_when_asked(self):
        # no address of its own and no --public-address: the lookup runs only
        # with --lookup-public
        with open(self.ipaddr, "w", newline="\n") as f:
            f.write("1: lo    inet 127.0.0.1/8 scope host lo\n")
        self.run_sh("--yes", "--reach", "internet", "--lookup-public", "--no-ps2", "--no-start")
        self.assertIn("curl -fsS --max-time 10 https://api.ipify.org", self.calls())
        _, env = self.env()
        self.assertEqual(env["POL_ADVERTISE"], "203.0.113.99")
        self.assertEqual(env["POL_DNS_BIND"], "127.0.0.1")

    def test_port_53_taken(self):
        self.run_sh("--yes", "--reach", "lan", "--no-start", extra_env={"STUB_PORT53_BUSY": "1"})
        _, env = self.env()
        self.assertEqual(env["POL_DNS_BIND"], LAN)

    def test_build_locally_without_deployer(self):
        out = self.run_sh("--yes", "--reach", "lan", "--images", "build", "--no-deployer")
        _, env = self.env()
        self.assertNotIn("COMPOSE_FILE", env)
        self.assertNotIn("POL_DEPLOYER_TOKEN", env)
        self.assertNotIn("POL_DEPLOY_ROOT", env)
        calls = self.calls()
        self.assertIn("docker compose up -d --build", calls)
        self.assertNotIn("docker compose pull", calls)
        self.assertFalse([c for c in calls if "deployer" in c])
        self.assertIn("python3 tools/stack.py", out)

    def test_clients_none(self):
        self.run_sh("--yes", "--reach", "lan", "--clients", "none", "--no-start")
        _, env = self.env()
        self.assertNotIn("POL_DEPLOY_CLIENTS", env)
        self.assertFalse(os.path.exists(os.path.join(self.srv, "clients")))

    def test_enabled_games_start_through_stack_py(self):
        with open(os.path.join(self.checkout, ".games"), "w") as f:
            f.write("tm\n")
        self.run_sh("--yes", "--reach", "lan")
        calls = self.calls()
        self.assertIn("python3 tools/stack.py compose --ghcr up -d", calls)
        self.assertNotIn("docker compose pull", calls)

    def test_dry_run_changes_nothing(self):
        out = self.run_sh("--yes", "--dry-run", "--reach", "vpn", "--no-ps2")
        self.assertFalse(os.path.exists(os.path.join(self.checkout, ".env")))
        self.assertFalse(os.path.exists(os.path.join(self.srv, "clients")))
        self.assertIn("POL_ADVERTISE=" + VPN, out)
        self.assertIn("POL_DB_PASSWORD=<secret>", out)
        self.assertIn("would run: docker compose pull", out)
        self.assertIn("would run: docker compose --profile deployer up -d deployer", out)
        self.assertIn("would run: docker network create openlobby", out)
        for c in self.calls():
            if c.startswith("docker "):
                self.assertRegex(c, r"^docker (--version|compose version|info|volume inspect|network inspect)", c)
        self.assertFalse([c for c in self.calls() if c.startswith("chmod")])

    def test_clone_when_not_in_a_checkout(self):
        loose = os.path.join(self.tmp, "download", "setup.sh")
        os.makedirs(os.path.dirname(loose))
        shutil.copy(SETUP_SH, loose)
        target = os.path.join(self.tmp, "home", "ol-server", "openlobby")
        self.run_sh("--yes", "--dir", bash_path(BASH, target), "--reach", "lan",
                    "--repo", "https://example.com/OpenLobby.git", "--no-start", script=loose)
        self.assertIn("git clone https://example.com/OpenLobby.git " + bash_path(BASH, target),
                      self.calls())
        _, env = self.env(target)
        self.assertEqual(env["POL_DEPLOY_ROOT"], bash_path(BASH, os.path.dirname(target)))
        self.assertNotIn("POL_DEPLOY_CORE", env)

    def test_checkout_with_another_name_sets_core(self):
        other = os.path.join(self.srv, "ol-main")
        shutil.copytree(self.fixture, other)
        self.run_sh("--yes", "--reach", "lan", "--no-start", script=os.path.join(other, "setup.sh"))
        _, env = self.env(other)
        self.assertEqual(env["POL_DEPLOY_CORE"], bash_path(BASH, other))

    def test_docker_group_missing(self):
        out = self.run_sh("--yes", "--reach", "lan", extra_env={"STUB_DOCKER_DENIED": "1"}, expect=1)
        self.assertIn("docker group", out)
        self.assertFalse(os.path.exists(os.path.join(self.checkout, ".env")))
        self.assertFalse([c for c in self.calls() if c.startswith("docker compose up")])

    def test_ssh_tunnel_line(self):
        out = self.run_sh("--yes", "--reach", "lan",
                          extra_env={"SSH_CONNECTION": "198.51.100.9 50000 %s 2222" % LAN})
        self.assertRegex(out, r"ssh -N -p 2222 -L 8090:127\.0\.0\.1:8090 \S+@" + re.escape(LAN))

    def test_no_terminal_without_yes(self):
        out = self.run_sh("--reach", "lan", expect=1)
        self.assertIn("--yes", out)

    def test_admin_user_on_a_new_env_only(self):
        self.run_sh("--yes", "--reach", "lan", "--admin-user", "host", "--no-start")
        _, env = self.env()
        self.assertEqual(env["POL_ADMIN_USER"], "host")
        self.run_sh("--yes", "--admin-user", "other", "--no-start")
        _, env = self.env()
        self.assertEqual(env["POL_ADMIN_USER"], "host")
        out = self.run_sh("--yes", "--admin-user", "two words", expect=2)
        self.assertIn("--admin-user", out)

    def test_checkout_runs_without_git(self):
        # an OpenLobby unpacked from the GitHub zip (the setup program does that
        # when git is missing): only bash's own tools and the stand-ins on PATH
        os.remove(os.path.join(self.bin, "git"))
        path = self.bin + os.pathsep + os.path.dirname(BASH)
        if shutil.which("git", path=path):
            self.skipTest("git sits beside bash here")
        self.run_sh("--yes", "--reach", "lan", "--no-start", extra_env={"PATH": path})
        _, env = self.env()
        self.assertEqual(env["POL_ADVERTISE"], LAN)

    def test_help(self):
        out = self.run_sh("--help")
        self.assertIn("--dry-run", out)
        self.assertEqual(self.calls(), [])

    def test_shellcheck(self):
        sc = shutil.which("shellcheck")
        if not sc:
            self.skipTest("shellcheck is not installed")
        p = subprocess.run([sc, "-S", "warning", SETUP_SH], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


PS_STUBS = r"""
$global:StubLog = New-Object System.Collections.Generic.List[string]
function docker {
    $line = 'docker ' + ($args -join ' ')
    $global:StubLog.Add($line)
    $global:LASTEXITCODE = 0
    switch -Wildcard ($line) {
        'docker --version' { 'Docker version 27.0.0, build test' }
        'docker compose version --short' { '2.29.1' }
        'docker compose version' { 'Docker Compose version v2.29.1' }
        'docker info*' { 'ok' }
        'docker volume inspect*' { $global:LASTEXITCODE = 1 }
        'docker network inspect*' { $global:LASTEXITCODE = 1 }
    }
}
function git { $global:StubLog.Add('git ' + ($args -join ' ')); $global:LASTEXITCODE = 0; 'git version 2.45.0' }
function winget { $global:StubLog.Add('winget ' + ($args -join ' ')); $global:LASTEXITCODE = 0 }
function Get-NetIPAddress {
    [pscustomobject]@{ IPAddress = '127.0.0.1'; PrefixLength = 8; InterfaceAlias = 'Loopback Pseudo-Interface 1'; InterfaceIndex = 1 }
    [pscustomobject]@{ IPAddress = '192.0.2.20'; PrefixLength = 24; InterfaceAlias = 'Ethernet'; InterfaceIndex = 5 }
    [pscustomobject]@{ IPAddress = '198.51.100.5'; PrefixLength = 24; InterfaceAlias = 'wg0'; InterfaceIndex = 9 }
}
function Get-NetRoute { [pscustomobject]@{ InterfaceIndex = 5; RouteMetric = 0 } }
function Get-NetAdapter { [pscustomobject]@{ InterfaceDescription = 'Test adapter' } }
function Get-NetUDPEndpoint { throw 'none' }
function Invoke-RestMethod { $global:StubLog.Add('lookup'); '203.0.113.99' }
function Start-Process { $global:StubLog.Add('start-process ' + ($args -join ' ')) }
"""


@unittest.skipUnless(POWERSHELL, "Windows PowerShell not found")
class SetupPs1Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="olsetup-ps-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.checkout = os.path.join(self.tmp, "srv", "openlobby")
        os.makedirs(os.path.join(self.checkout, "tools"))
        for name in ("setup.ps1", ".env.example", "docker-compose.yml"):
            shutil.copy(os.path.join(ROOT, name), os.path.join(self.checkout, name))
        with open(os.path.join(self.checkout, "tools", "stack.py"), "w") as f:
            f.write("# stand-in\n")

    def run_ps(self, *args, expect=0):
        script = os.path.join(self.checkout, "setup.ps1")
        cmd = PS_STUBS + "\n& '%s' %s\n'STUBLOG:'; $global:StubLog\nexit $LASTEXITCODE\n" % (
            script.replace("'", "''"), " ".join(args))
        p = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                            "Bypass", "-Command", "-"], input=cmd, capture_output=True,
                           text=True, timeout=120)
        out = p.stdout + p.stderr
        if expect is not None:
            self.assertEqual(p.returncode, expect, out)
        return out

    def env(self):
        with open(os.path.join(self.checkout, ".env"), "rb") as f:
            raw = f.read()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "BOM in .env")
        return parse_env(raw.decode("utf-8"))

    def test_parses_and_is_ascii(self):
        with open(SETUP_PS1, "rb") as f:
            raw = f.read()
        self.assertTrue(all(b < 128 for b in raw), "setup.ps1 must be ASCII")
        ps = ("$t = $null; $e = $null; "
              "[void][System.Management.Automation.Language.Parser]::ParseFile('%s', [ref]$t, [ref]$e); "
              "$e | ForEach-Object { $_.Message }; exit $e.Count" % SETUP_PS1.replace("'", "''"))
        p = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_dry_run(self):
        out = self.run_ps("-Yes", "-DryRun", "-Reach", "vpn", "-NoPs2")
        self.assertFalse(os.path.exists(os.path.join(self.checkout, ".env")))
        self.assertIn("POL_ADVERTISE=" + VPN, out)
        self.assertIn("POL_DNS_CLIENTS=127.0.0.0/8,198.51.100.0/24", out)
        self.assertIn("COMPOSE_FILE=docker-compose.yml;docker-compose.ghcr.yml", out)
        self.assertIn("POL_DB_PASSWORD=<secret>", out)
        self.assertIn("would run: docker compose pull", out)
        self.assertIn("would run: docker network create openlobby", out)
        self.assertIn("Linux", out)          # the deployer note
        log = out.split("STUBLOG:", 1)[1]
        self.assertNotIn("compose up", log)
        self.assertNotIn("compose down", log)

    def test_writes_env_and_keeps_it(self):
        self.run_ps("-Yes", "-Reach", "lan", "-NoStart")
        a = self.env()
        self.assertEqual(a["POL_ADVERTISE"], LAN)
        self.assertEqual(a["COMPOSE_FILE"], "docker-compose.yml;docker-compose.ghcr.yml")
        self.assertRegex(a["POL_DB_PASSWORD"], r"^[A-Za-z0-9]{32}$")
        self.assertNotIn("POL_DEPLOYER_TOKEN", a)
        out = self.run_ps("-Yes", "-NoStart")
        self.assertIn("keeping its values", out)
        self.assertEqual(self.env(), a)
        self.run_ps("-Yes", "-Reach", "internet", "-PublicAddress", PUBLIC, "-NoPs2", "-NoStart")
        b = self.env()
        self.assertEqual(b["POL_ADVERTISE"], PUBLIC)
        self.assertEqual(b["POL_DNS_BIND"], "127.0.0.1")
        self.assertEqual(b["POL_DB_PASSWORD"], a["POL_DB_PASSWORD"])

    def test_admin_user(self):
        self.run_ps("-Yes", "-Reach", "lan", "-AdminUser", "host", "-NoStart")
        self.assertEqual(self.env()["POL_ADMIN_USER"], "host")


if __name__ == "__main__":
    unittest.main()
