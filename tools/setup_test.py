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

setup.ps1 is parse-checked with Windows PowerShell, then run with -DryRun,
-NoStart and through a whole start against PowerShell functions standing in
for docker, git and the address cmdlets: the deployer's settings with the
checkouts as Docker Desktop's VM sees them (C:\\x is /run/desktop/mnt/host/c/x),
the deployer started and its own check reported, and enabled games started
through tools\\stack.py. Skipped where powershell.exe is missing.

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
  "info --format "*) echo "${STUB_DOCKER_OS:-Ubuntu 24.04 LTS}" ;;
  "volume inspect "*) exit 1 ;;
  "network inspect "*) exit 1 ;;
  "compose ps "*) printf 'admin running healthy\ndns running healthy\npostgres running healthy\n' ;;
  *"deployer.py --channel"*)
    # the deployer's channel switch: what came on stdin is kept to compare, never logged
    IFS= read -r code
    printf '%s' "$code" > "$STUB_STATE/channel-code"
    echo "On the dev channel: the stand-in says so." ;;
  *"--show-admin"*)
    # a slow first start: the first STUB_ADMIN_SLOW answers say the service is not running
    n=$(cat "$STUB_STATE/admin-asked" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$STUB_STATE/admin-asked"
    if [ "$n" -le "${STUB_ADMIN_SLOW:-0}" ]; then echo 'service "admin" is not running' >&2; exit 1; fi
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


def daemon_path(p):
    """A Windows folder as Docker Desktop's VM sees it: C:\\x\\y is /run/desktop/mnt/host/c/x/y."""
    drive, rest = os.path.splitdrive(os.path.abspath(p))
    return "/run/desktop/mnt/host/" + drive[0].lower() + rest.replace("\\", "/").rstrip("/")


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

    def test_admin_service_slow_to_start_still_gets_a_password(self):
        out = self.run_sh("--yes", "--reach", "lan", extra_env={"STUB_ADMIN_SLOW": "2"})
        with open(os.path.join(self.state, "admin-password")) as f:
            pw = f.read()
        self.assertIn("Password: " + pw, out)
        self.assertNotIn("No password is set yet", out)

    def test_no_password_set_says_so_with_the_command(self):
        out = self.run_sh("--yes", "--reach", "lan",
                          extra_env={"STUB_ADMIN_SLOW": "99", "OPENLOBBY_ADMIN_TRIES": "2"})
        self.assertFalse(os.path.exists(os.path.join(self.state, "admin-password")))
        self.assertNotIn("Password: ", out)
        self.assertIn("No password is set yet", out)
        self.assertIn("There is no default", out)
        self.assertIn("exec admin python admin.py --set-password", out)

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

    # the dev update channel (docs/deployer.md, "Dev channel")

    def test_dev_channel_hands_the_code_to_the_deployer_on_stdin(self):
        code = "kart-dev-7Qm2x9Lp4Vb8"
        cf = os.path.join(self.tmp, "code.txt")
        with open(cf, "w", newline="\n") as f:
            f.write("  " + code + "  \r\n")
        out = self.run_sh("--yes", "--reach", "lan", "--channel", "dev", "--registry",
                          "registry.example.org/kart", "--code-file", bash_path(BASH, cf))
        _, env = self.env()
        self.assertEqual(env["POL_DEV_REGISTRY"], "registry.example.org/kart")
        calls = self.calls()
        self.assertIn("docker compose --profile deployer exec -T deployer python /app/deployer.py --channel dev",
                      calls)
        with open(os.path.join(self.state, "channel-code")) as f:
            self.assertEqual(f.read(), code)         # trimmed, on stdin
        self.assertFalse([c for c in calls if code in c])
        self.assertNotIn(code, out)
        self.assertNotIn(code, self.env()[0])
        self.assertIn("On the dev channel", out)
        self.assertLess(calls.index("docker compose --profile deployer up -d deployer"),
                        calls.index("docker compose --profile deployer exec -T deployer python /app/deployer.py --channel dev"))

    def test_dev_channel_refusals(self):
        out = self.run_sh("--yes", "--channel", "beta", expect=2)
        self.assertIn("--channel is stable or dev", out)
        out = self.run_sh("--yes", "--channel", "dev", "--registry", "Not A Registry", expect=2)
        self.assertIn("--registry looks like", out)
        out = self.run_sh("--yes", "--reach", "lan", "--channel", "dev", "--registry",
                          "registry.example.org/kart", expect=1)
        self.assertIn("needs --code-file", out)
        out = self.run_sh("--yes", "--reach", "lan", "--channel", "dev", "--no-deployer", expect=1)
        self.assertIn("run with --deployer", out)
        out = self.run_sh("--yes", "--reach", "lan", "--channel", "dev", expect=1)
        self.assertIn("needs --registry", out)
        self.assertFalse([c for c in self.calls() if "--channel" in c])
        self.assertFalse(os.path.exists(os.path.join(self.checkout, ".env")) and
                         "POL_DEV_REGISTRY=registry" in self.env()[0])

    def test_dev_channel_dry_run(self):
        out = self.run_sh("--dry-run", "--yes", "--reach", "lan", "--channel", "dev", "--registry",
                          "registry.example.org/kart")
        self.assertIn("would run: docker compose --profile deployer exec -T deployer python "
                      "/app/deployer.py --channel dev (the access code on stdin)", out)
        self.assertFalse([c for c in self.calls() if "--channel" in c])

    def test_stable_switches_back_only_from_dev(self):
        self.run_sh("--yes", "--reach", "lan", "--channel", "stable")
        self.assertFalse([c for c in self.calls() if "--channel" in c])
        with open(os.path.join(self.checkout, ".channel"), "w", newline="\n") as f:
            f.write('{\n "channel": "dev",\n "registry": "registry.example.org/kart"\n}\n')
        self.run_sh("--yes", "--reach", "lan", "--channel", "stable")
        self.assertIn("docker compose --profile deployer exec -T deployer python /app/deployer.py --channel stable",
                      self.calls())

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

    def test_docker_desktop_leaves_the_deployer_off_unless_asked(self):
        out = self.run_sh("--yes", "--reach", "lan", "--no-start",
                          extra_env={"STUB_DOCKER_OS": "Docker Desktop"})
        self.assertIn("This Docker is Docker Desktop.", out)
        self.assertIn("--deployer turns it on", out)
        self.assertNotIn("POL_DEPLOYER_TOKEN", self.env()[1])

    def test_docker_desktop_with_deployer(self):
        out = self.run_sh("--yes", "--reach", "lan", "--deployer", "--clients", "none",
                          extra_env={"STUB_DOCKER_OS": "Docker Desktop"})
        env = self.env()[1]
        self.assertRegex(env["POL_DEPLOYER_TOKEN"], r"^[A-Za-z0-9_-]{43}$")
        self.assertEqual(env["POL_DEPLOY_ROOT"], bash_path(BASH, self.srv))
        self.assertIn("proven only on Windows", out)
        self.assertIn("docker compose --profile deployer up -d deployer", self.calls())

    def test_clients_none(self):
        self.run_sh("--yes", "--reach", "lan", "--clients", "none", "--no-start")
        _, env = self.env()
        self.assertNotIn("POL_DEPLOY_CLIENTS", env)
        self.assertFalse(os.path.exists(os.path.join(self.srv, "clients")))

    def test_enabled_games_start_through_stack_py(self):
        with open(os.path.join(self.checkout, ".games"), "w") as f:
            f.write("tm\n")
        out = self.run_sh("--yes", "--reach", "lan")
        calls = self.calls()
        self.assertIn("python3 tools/stack.py compose --ghcr up -d", calls)
        self.assertNotIn("docker compose pull", calls)
        # the games' data volumes are set up first, under a step of their own
        vol = [i for i, c in enumerate(calls) if c.startswith("python3 ") and c.endswith("/tools/stack.py volumes")]
        self.assertTrue(vol and vol[0] < calls.index("python3 tools/stack.py compose --ghcr up -d"), calls)
        self.assertLess(out.index("== Game data"), out.index("== Starting the stack"))

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
if (-not (Test-Path variable:global:StubDockerOs)) { $global:StubDockerOs = 'Docker Desktop' }
if (-not (Test-Path variable:global:StubDeployerHealth)) { $global:StubDeployerHealth = 'paths ok' }
if (-not (Test-Path variable:global:StubUpFails)) { $global:StubUpFails = $false }
if (-not (Test-Path variable:global:StubUdp53)) { $global:StubUdp53 = $false }
function docker {
    $piped = @($input)
    $line = 'docker ' + ($args -join ' ')
    $global:StubLog.Add($line)
    $global:LASTEXITCODE = 0
    switch -Wildcard ($line) {
        '*deployer.py --channel*' {
            # what came on stdin, as its sha256: compared by the test, never shown
            $b = [Text.Encoding]::UTF8.GetBytes(($piped -join ''))
            $h = [Security.Cryptography.SHA256]::Create().ComputeHash($b)
            'CHANNEL-STDIN ' + (($h | ForEach-Object { $_.ToString('x2') }) -join '')
            'On the dev channel: the stand-in says so.'
        }
        'docker --version' { 'Docker version 27.0.0, build test' }
        'docker compose version --short' { '2.29.1' }
        'docker compose version' { 'Docker Compose version v2.29.1' }
        'docker info --format*' { $global:StubDockerOs; break }
        'docker info*' { 'ok' }
        'docker volume inspect*' { $global:LASTEXITCODE = 1 }
        'docker network inspect*' { $global:LASTEXITCODE = 1 }
        'docker compose ps*' { 'admin running healthy'; 'dns running healthy' }
        'docker compose up*' { if ($global:StubUpFails) { 'Error response from daemon: ports are not available: exposing port UDP 0.0.0.0:53'; $global:LASTEXITCODE = 1 } }
        '*deployer.py --healthcheck' {
            $global:StubDeployerHealth
            if ($global:StubDeployerHealth -ne 'paths ok') { $global:LASTEXITCODE = 1 }
        }
        '*--show-admin' { "[admin] user='admin' set 2026-01-01" }
    }
}
function Start-Sleep { }
function git { $global:StubLog.Add('git ' + ($args -join ' ')); $global:LASTEXITCODE = 0; 'git version 2.45.0' }
function winget { $global:StubLog.Add('winget ' + ($args -join ' ')); $global:LASTEXITCODE = 0 }
function Get-NetIPAddress {
    [pscustomobject]@{ IPAddress = '127.0.0.1'; PrefixLength = 8; InterfaceAlias = 'Loopback Pseudo-Interface 1'; InterfaceIndex = 1 }
    [pscustomobject]@{ IPAddress = '192.0.2.20'; PrefixLength = 24; InterfaceAlias = 'Ethernet'; InterfaceIndex = 5 }
    [pscustomobject]@{ IPAddress = '198.51.100.5'; PrefixLength = 24; InterfaceAlias = 'wg0'; InterfaceIndex = 9 }
}
function Get-NetRoute { [pscustomobject]@{ InterfaceIndex = 5; RouteMetric = 0 } }
function Get-NetAdapter { [pscustomobject]@{ InterfaceDescription = 'Test adapter' } }
function Get-NetUDPEndpoint {
    if (-not $global:StubUdp53) { throw 'none' }
    [pscustomobject]@{ LocalAddress = '0.0.0.0'; LocalPort = 53; OwningProcess = 4242 }
}
function Get-NetTCPConnection { throw 'none' }
function Get-Process { [pscustomobject]@{ ProcessName = 'svchost' } }
function Invoke-RestMethod { $global:StubLog.Add('lookup'); '203.0.113.99' }
function Start-Process {
    $global:StubLog.Add('start-process ' + ($args -join ' '))
    [pscustomobject]@{ ExitCode = 0 }
}
# Windows Firewall as setup.ps1 reads it: the OpenLobby rules in place (their
# descriptions), the network type and Docker Desktop's block rules
if (-not (Test-Path variable:global:StubFwRules)) { $global:StubFwRules = @() }
if (-not (Test-Path variable:global:StubProfile)) { $global:StubProfile = 'Private' }
if (-not (Test-Path variable:global:StubDockerBlocks)) { $global:StubDockerBlocks = 0 }
function New-NetFirewallRule { $global:StubLog.Add('REAL FIREWALL CHANGE') }
function Get-NetFirewallRule {
    $global:StubLog.Add('get-netfirewallrule ' + ($args -join ' '))
    if ($args -contains '-Group') {
        if ($global:StubFwRules.Count -eq 0) { throw 'No MSFT_NetFirewallRule objects found' }
        foreach ($d in $global:StubFwRules) { [pscustomobject]@{ Description = $d } }
        return
    }
    foreach ($i in 1..$global:StubDockerBlocks) {
        if ($global:StubDockerBlocks -eq 0) { break }
        [pscustomobject]@{ DisplayName = 'Docker Desktop Backend'; Direction = 'Inbound'; Action = 'Block'; Enabled = 'True'; Profile = 'Public' }
    }
}
function Get-NetFirewallApplicationFilter {
    [pscustomobject]@{ Program = 'C:\Program Files\Docker\Docker\resources\com.docker.backend.exe' }
}
function Get-NetConnectionProfile { [pscustomobject]@{ NetworkCategory = $global:StubProfile } }
"""


@unittest.skipUnless(POWERSHELL, "Windows PowerShell not found")
class SetupPs1Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="olsetup-ps-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.srv = os.path.join(self.tmp, "srv")
        self.checkout = os.path.join(self.srv, "openlobby")
        os.makedirs(os.path.join(self.checkout, "tools"))
        for name in ("setup.ps1", ".env.example", "docker-compose.yml", os.path.join("tools", "firewall.ps1")):
            shutil.copy(os.path.join(ROOT, name), os.path.join(self.checkout, name))
        with open(os.path.join(self.checkout, "tools", "stack.py"), "w") as f:
            f.write("# stand-in\n")

    def run_ps(self, *args, expect=0, pre=""):
        script = os.path.join(self.checkout, "setup.ps1")
        cmd = pre + "\n" + PS_STUBS + "\n& '%s' %s\n'STUBLOG:'; $global:StubLog\nexit $LASTEXITCODE\n" % (
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

    def test_dev_channel_hands_the_code_to_the_deployer_on_stdin(self):
        import hashlib
        code = "kart-dev-7Qm2x9Lp4Vb8"
        cf = os.path.join(self.tmp, "code.txt")
        with open(cf, "w") as f:
            f.write(" " + code + " \n")
        out = self.run_ps("-Yes", "-Reach", "lan", "-NoClient", "-NoFirewall", "-Channel", "dev",
                          "-Registry", "registry.example.org/kart", "-CodeFile", "'%s'" % cf)
        self.assertEqual(self.env()["POL_DEV_REGISTRY"], "registry.example.org/kart")
        log = out.split("STUBLOG:", 1)[1]
        self.assertIn("docker compose --profile deployer exec -T deployer python /app/deployer.py --channel dev", log)
        self.assertIn("CHANNEL-STDIN " + hashlib.sha256(code.encode()).hexdigest(), out)
        self.assertNotIn(code, out)
        self.assertIn("On the dev channel", out)

    def test_dev_channel_refusals(self):
        out = self.run_ps("-Yes", "-Reach", "lan", "-NoClient", "-NoFirewall", "-Channel", "dev",
                          "-Registry", "registry.example.org/kart", expect=1)
        self.assertIn("needs -CodeFile", out)
        out = self.run_ps("-Yes", "-Reach", "lan", "-NoClient", "-NoFirewall", "-Channel", "dev",
                          "-Registry", "'Not A Registry'", expect=2)
        self.assertIn("-Registry looks like", out)
        out = self.run_ps("-Yes", "-DryRun", "-Reach", "lan", "-Channel", "dev", "-Registry",
                          "registry.example.org/kart")
        self.assertIn("would run: docker compose --profile deployer exec -T deployer python /app/deployer.py "
                      "--channel dev (the access code on stdin)", out)

    def test_dry_run(self):
        out = self.run_ps("-Yes", "-DryRun", "-Reach", "vpn", "-NoPs2")
        self.assertFalse(os.path.exists(os.path.join(self.checkout, ".env")))
        self.assertIn("POL_ADVERTISE=" + VPN, out)
        self.assertIn("POL_DNS_CLIENTS=127.0.0.0/8,198.51.100.0/24", out)
        self.assertIn("COMPOSE_FILE=docker-compose.yml;docker-compose.ghcr.yml", out)
        self.assertIn("POL_DB_PASSWORD=<secret>", out)
        self.assertIn("would run: docker compose pull", out)
        self.assertIn("would run: docker network create openlobby", out)
        # the deployer, with the checkouts as Docker Desktop's VM sees them
        self.assertIn("POL_DEPLOYER_TOKEN=<secret>", out)
        self.assertIn("POL_DEPLOY_ROOT=" + daemon_path(self.srv), out)
        self.assertIn("would run: docker compose --profile deployer up -d deployer", out)
        self.assertIn("would create " + os.path.join(self.srv, "clients"), out)
        self.assertNotIn("Linux only", out)
        log = out.split("STUBLOG:", 1)[1]
        self.assertNotIn("compose up", log)
        self.assertNotIn("compose down", log)

    def test_writes_env_and_keeps_it(self):
        self.run_ps("-Yes", "-Reach", "lan", "-NoStart")
        a = self.env()
        self.assertEqual(a["POL_ADVERTISE"], LAN)
        self.assertEqual(a["COMPOSE_FILE"], "docker-compose.yml;docker-compose.ghcr.yml")
        self.assertRegex(a["POL_DB_PASSWORD"], r"^[A-Za-z0-9]{32}$")
        self.assertRegex(a["POL_DEPLOYER_TOKEN"], r"^[A-Za-z0-9_-]{43}$")
        self.assertEqual(a["POL_DEPLOY_ROOT"], daemon_path(self.srv))
        self.assertTrue(a["POL_DEPLOY_ROOT"].startswith("/run/desktop/mnt/host/"), a["POL_DEPLOY_ROOT"])
        self.assertEqual(a["POL_DEPLOY_GHCR"], "1")
        self.assertEqual(a["POL_DEPLOY_CLIENTS"], daemon_path(os.path.join(self.srv, "clients")))
        self.assertTrue(os.path.isdir(os.path.join(self.srv, "clients")))
        self.assertNotIn("POL_DEPLOY_CORE", a)           # the checkout is named openlobby
        self.assertNotIn("COMPOSE_PROFILES", a)          # docs/deployer.md: jobs refuse it
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

    def test_no_deployer_and_no_clients(self):
        self.run_ps("-Yes", "-Reach", "lan", "-NoDeployer", "-NoStart")
        self.assertFalse([k for k in self.env() if k.startswith(("POL_DEPLOY", "POL_DEPLOYER"))])
        out = self.run_ps("-Yes", "-Deployer", "-Clients", "none", "-NoStart")
        a = self.env()
        self.assertRegex(a["POL_DEPLOYER_TOKEN"], r"^[A-Za-z0-9_-]{43}$")
        self.assertNotIn("POL_DEPLOY_CLIENTS", a)
        self.assertIn("Generated a deployer token", out)

    def test_not_docker_desktop_leaves_it_off(self):
        out = self.run_ps("-Yes", "-Reach", "lan", "-Deployer", "-NoStart",
                          pre="$global:StubDockerOs = 'Ubuntu 24.04 LTS'")
        self.assertIn("is left off: this Docker is not Docker Desktop (Ubuntu 24.04 LTS)", out)
        self.assertNotIn("POL_DEPLOYER_TOKEN", self.env())

    def test_checkout_with_another_name_sets_core(self):
        other = os.path.join(self.tmp, "srv", "kartclub-lobby")
        shutil.copytree(self.checkout, other)
        self.checkout = other
        self.run_ps("-Yes", "-Reach", "lan", "-NoStart")
        a = self.env()
        self.assertEqual(a["POL_DEPLOY_ROOT"], daemon_path(self.srv))
        self.assertEqual(a["POL_DEPLOY_CORE"], daemon_path(other))

    def test_daemon_path_mapping(self):
        ps = ("$t = $null; $e = $null; "
              "$ast = [System.Management.Automation.Language.Parser]::ParseFile('%s', [ref]$t, [ref]$e); "
              "$f = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] "
              "-and $n.Name -eq 'ConvertTo-DaemonPath' }, $true); "
              "Invoke-Expression $f.Extent.Text; "
              "foreach ($p in 'C:\\Users\\Kart Club\\openlobby-server', 'D:\\', 'e:\\srv\\pol\\', "
              "'\\\\server\\share\\x', 'relative\\x') { '[' + (ConvertTo-DaemonPath $p) + ']' }"
              % SETUP_PS1.replace("'", "''"))
        p = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.stdout.split(), ["[/run/desktop/mnt/host/c/Users/Kart", "Club/openlobby-server]",
                                            "[/run/desktop/mnt/host/d]", "[/run/desktop/mnt/host/e/srv/pol]",
                                            "[]", "[]"], p.stdout + p.stderr)

    def write_stack_standin(self):
        """tools/stack.py that records its arguments, for the runs that start the stack."""
        with open(os.path.join(self.checkout, "tools", "stack.py"), "w") as f:
            f.write("import os, sys\n"
                    "with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stack-calls.txt'),"
                    " 'a') as f:\n"
                    "    f.write(' '.join(sys.argv[1:]) + '\\n')\n")
        return os.path.join(self.checkout, "tools", "stack-calls.txt")

    def test_start_runs_the_deployer_and_reports_its_check(self):
        self.write_stack_standin()
        with open(os.path.join(self.checkout, ".games"), "w") as f:
            f.write("# Games tools/stack.py runs with the core, one key per line.\n")
        out = self.run_ps("-Yes", "-Reach", "lan", "-NoClient")
        log = out.split("STUBLOG:", 1)[1].splitlines()
        # a .games with comments only enables nothing: the core starts with docker compose
        self.assertIn("docker compose --profile deployer up -d deployer", log)
        self.assertIn("docker compose pull", log)
        self.assertIn("docker compose up -d", log)
        self.assertIn("docker compose --profile deployer exec -T deployer python /app/deployer.py --healthcheck", log)
        self.assertIn("The deployer is running, and Docker sees this folder where the deployer does.", out)
        self.assertIn("Games and updates: on the admin panel, Settings > Install games.", out)
        self.assertIn("== Done", out)
        self.assertNotIn("runs on Linux only", out)

    def test_start_says_when_the_deployer_check_fails(self):
        self.write_stack_standin()
        out = self.run_ps("-Yes", "-Reach", "lan", "-NoClient",
                          pre="$global:StubDeployerHealth = 'The Docker daemon does not see the OpenLobby "
                              "checkout; set POL_DEPLOY_ROOT'")
        self.assertIn("the deployer is not ready: The Docker daemon does not see", out)
        self.assertEqual(out.count("deployer.py --healthcheck"), 1, out)   # no retries on that answer
        self.assertIn("== Done", out)

    @unittest.skipUnless(shutil.which("python") or shutil.which("py"), "Python not found")
    def test_enabled_games_start_through_stack_py(self):
        calls = self.write_stack_standin()
        with open(os.path.join(self.checkout, ".games"), "w") as f:
            f.write("# Games tools/stack.py runs with the core, one key per line.\njan\n")
        out = self.run_ps("-Yes", "-Reach", "lan", "-NoClient")
        log = out.split("STUBLOG:", 1)[1].splitlines()
        self.assertIn("Enabled games: jan (started through tools\\stack.py)", out)
        self.assertNotIn("docker compose up -d", log)       # it would rebuild login without the game
        self.assertNotIn("docker compose pull", log)
        with open(calls) as f:
            self.assertEqual(f.read().splitlines(), [
                "volumes",
                "compose --ghcr pull --ignore-buildable --ignore-pull-failures",
                "compose --ghcr up -d"])
        # the games' data volumes are set up first, under a step of their own
        self.assertLess(out.index("== Game data"), out.index("== Starting the stack"))
        self.assertIn("== Done", out)

    # ------------------------------------------------------------ Windows Firewall

    LAN_TCP = "25,80,110,443,51200-51271,51273-51310,51330,53001-53015,54000"

    def firewall_call(self, out):
        """The elevated tools\\firewall.ps1 run, or None."""
        log = out.split("STUBLOG:", 1)[1].splitlines()
        self.assertNotIn("REAL FIREWALL CHANGE", log)
        runs = [l for l in log if l.startswith("start-process") and "firewall.ps1" in l]
        self.assertLessEqual(len(runs), 1, runs)
        return runs[0] if runs else None

    def test_firewall_lan_adds_the_players_ports_elevated(self):
        out = self.run_ps("-Yes", "-Reach", "lan", "-NoClient")
        call = self.firewall_call(out)
        self.assertIsNotNone(call, out)
        self.assertIn("-Verb RunAs", call)
        self.assertIn(os.path.join(self.checkout, "tools", "firewall.ps1"), call)
        self.assertIn("-Group OpenLobby", call)
        self.assertIn('-Rules "UDP:53:Private:LocalSubnet;TCP:%s:Private:LocalSubnet"' % self.LAN_TCP, call)
        self.assertIn("-Log ", call)
        self.assertNotIn("-PrivateInterface", call)
        self.assertNotIn("-UnblockDocker", call)
        self.assertIn("Consoles reach this PC at %s, on Ethernet (network type: Private)." % LAN, out)
        self.assertIn("Windows asks for administrator rights", out)
        self.assertIn("Windows Firewall lets players in now (rule group OpenLobby).", out)
        self.assertIn("set the primary DNS server", out)
        # before the doctor, so its checks see the rules
        self.assertLess(out.index("== Windows Firewall"), out.index("== Done"))

    def test_firewall_rules_in_place_are_left_alone(self):
        pre = ("$global:StubFwRules = @('TCP:%s:Private:LocalSubnet', 'UDP:53:Private:LocalSubnet')"
               % self.LAN_TCP)
        out = self.run_ps("-Yes", "-Reach", "lan", "-NoClient", pre=pre)
        self.assertIsNone(self.firewall_call(out))
        self.assertIn("are in place (rule group OpenLobby)", out)
        self.assertIn("Windows Firewall already lets players in", out)
        # a rule that differs (443 missing) is replaced
        pre = "$global:StubFwRules = @('TCP:80:Private:LocalSubnet', 'UDP:53:Private:LocalSubnet')"
        out = self.run_ps("-Yes", "-Reach", "lan", "-NoClient", pre=pre)
        self.assertIsNotNone(self.firewall_call(out))

    def test_firewall_public_network_is_switched_to_private_on_a_lan(self):
        out = self.run_ps("-Yes", "-Reach", "lan", "-NoClient", pre="$global:StubProfile = 'Public'")
        call = self.firewall_call(out)
        self.assertIn("-PrivateInterface 5", call)
        self.assertIn("Windows treats the network on Ethernet as Public", out)
        self.assertIn("Network and internet", out)
        self.assertIn("switch Ethernet to Private", out)

    def test_firewall_public_network_is_only_explained_for_internet_players(self):
        pre = ("$global:StubProfile = 'Public'; $global:StubFwRules = @('UDP:53:Private:LocalSubnet', "
               "'TCP:25,80,110,51200-51271,51273-51310,51330,53001-53015,54000:Private:Any')")
        out = self.run_ps("-Yes", "-Reach", "internet", "-PublicAddress", PUBLIC, "-NoPs2", "-NoClient", pre=pre)
        self.assertIsNone(self.firewall_call(out))
        self.assertIn("Windows treats the network on Ethernet as Public", out)

    def test_firewall_turns_off_docker_block_rules(self):
        out = self.run_ps("-Yes", "-Reach", "lan", "-NoClient", pre="$global:StubDockerBlocks = 2")
        call = self.firewall_call(out)
        self.assertIn("-UnblockDocker", call)
        self.assertIn('"Allow\naccess" question was cancelled', out.replace("\r", ""))
        self.assertIn("Docker Desktop Backend (Public)", out)

    def test_firewall_vpn_without_consoles_has_no_443(self):
        out = self.run_ps("-Yes", "-Reach", "vpn", "-NoPs2", "-NoClient")
        call = self.firewall_call(out)
        tcp = "25,80,110,51200-51271,51273-51310,51330,53001-53015,54000"
        net = "198.51.100.0/24"
        self.assertIn('-Rules "UDP:53:Private:LocalSubnet,{n};TCP:{t}:Private:LocalSubnet,{n};'
                      'UDP:53:Public:{n};TCP:{t}:Public:{n}"'.format(n=net, t=tcp), call)

    def test_firewall_dry_run_and_no_firewall(self):
        out = self.run_ps("-Yes", "-DryRun", "-Reach", "lan")
        self.assertIsNone(self.firewall_call(out))
        self.assertIn("would run (elevated): powershell.exe -NoProfile -ExecutionPolicy Bypass -File", out)
        self.assertIn('-Rules "UDP:53:Private:LocalSubnet;', out)
        out = self.run_ps("-Yes", "-Reach", "lan", "-NoClient", "-NoFirewall")
        self.assertIsNone(self.firewall_call(out))
        self.assertIn("Left alone (-NoFirewall)", out)
        self.assertNotIn("get-netfirewallrule", out.split("STUBLOG:", 1)[1])

    def test_failed_start_names_the_port_holder(self):
        out = self.run_ps("-Yes", "-Reach", "lan", "-NoClient", expect=1,
                          pre="$global:StubUpFails = $true; $global:StubUdp53 = $true")
        self.assertIn("UDP port 53 on 0.0.0.0 is taken by svchost (process 4242).", out)
        self.assertIn("Internet Connection Sharing or the Mobile hotspot", out)
        self.assertIn("-ChangeAddress", out)
        self.assertIn("docker compose up failed", out)


@unittest.skipUnless(POWERSHELL, "Windows PowerShell not found")
class FirewallPs1Test(unittest.TestCase):
    """tools\\firewall.ps1 against stand-ins for the NetSecurity cmdlets: what it
    would ask Windows Firewall to do. Nothing reaches the real firewall."""

    STUBS = r"""
$global:Calls = New-Object System.Collections.Generic.List[string]
function Get-NetFirewallRule {
    $global:Calls.Add('get ' + ($args -join ' '))
    if ($args -contains '-Group') { [pscustomobject]@{ Name = 'old' }; return }
    [pscustomobject]@{ DisplayName = 'Docker Desktop Backend'; Direction = 'Inbound'; Action = 'Block'; Enabled = 'True'; Profile = 'Public' }
    [pscustomobject]@{ DisplayName = 'Docker Desktop Backend'; Direction = 'Inbound'; Action = 'Allow'; Enabled = 'True'; Profile = 'Private' }
}
function Remove-NetFirewallRule { $global:Calls.Add('remove ' + ($args -join ' ')) }
function New-NetFirewallRule {
    param($Name, $DisplayName, $Group, $Description, $Direction, $Action, $Protocol, $LocalPort, $Profile,
          $RemoteAddress, $ErrorAction, [switch]$WhatIf)
    $global:Calls.Add("new $Name|$Group|$Description|$Direction|$Action|$Protocol|" + ($LocalPort -join ',') +
        "|$Profile|" + ($RemoteAddress -join ','))
}
function Set-NetConnectionProfile { $global:Calls.Add('profile ' + ($args -join ' ')) }
function Get-NetFirewallApplicationFilter {
    [pscustomobject]@{ Program = 'C:\Program Files\Docker\Docker\resources\com.docker.backend.exe' }
    [pscustomobject]@{ Program = 'C:\Program Files\Other\other.exe' }
}
function Disable-NetFirewallRule { $global:Calls.Add('disable ' + ($input | ForEach-Object { $_.Profile })) }
"""

    def run_fw(self, *args):
        script = os.path.join(ROOT, "tools", "firewall.ps1")
        cmd = self.STUBS + "\n& '%s' %s\n$rc = $LASTEXITCODE\n'CALLS:'; $global:Calls\nexit $rc\n" % (
            script.replace("'", "''"), " ".join(args))
        p = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-Command", "-"], input=cmd, capture_output=True, text=True, timeout=120)
        out = p.stdout + p.stderr
        return p.returncode, out, out.split("CALLS:", 1)[1].split() if "CALLS:" in out else []

    def test_parses_and_is_ascii(self):
        path = os.path.join(ROOT, "tools", "firewall.ps1")
        with open(path, "rb") as f:
            self.assertTrue(all(b < 128 for b in f.read()), "firewall.ps1 must be ASCII")

    def test_replaces_the_group_with_the_rules_given(self):
        code, out, _ = self.run_fw("-Rules", '"UDP:53:Private:LocalSubnet;TCP:80,51200-51271:Private:LocalSubnet,198.51.100.0/24"')
        self.assertEqual(code, 0, out)
        calls = out.split("CALLS:", 1)[1]
        self.assertIn("get -Group OpenLobby", calls)
        self.assertLess(calls.index("remove"), calls.index("new "))
        self.assertIn("new OpenLobby-1|OpenLobby|UDP:53:Private:LocalSubnet|Inbound|Allow|UDP|53|Private|LocalSubnet",
                      calls)
        self.assertIn("new OpenLobby-2|OpenLobby|TCP:80,51200-51271:Private:LocalSubnet,198.51.100.0/24|Inbound|"
                      "Allow|TCP|80,51200-51271|Private|LocalSubnet,198.51.100.0/24", calls)
        self.assertNotIn("profile", calls)
        self.assertNotIn("disable", calls)

    def test_profile_and_docker_blocks(self):
        code, out, _ = self.run_fw("-Rules", "UDP:53:Private:LocalSubnet", "-PrivateInterface", "7",
                                   "-UnblockDocker")
        self.assertEqual(code, 0, out)
        calls = out.split("CALLS:", 1)[1]
        self.assertIn("profile -InterfaceIndex 7 -NetworkCategory Private", calls)
        # only the block rule, only Docker Desktop's forwarder
        self.assertIn("disable Public", calls)
        self.assertNotIn("disable Private", calls)

    def test_remove_only_removes(self):
        code, out, _ = self.run_fw("-Remove")
        self.assertEqual(code, 0, out)
        calls = out.split("CALLS:", 1)[1]
        self.assertIn("remove", calls)
        self.assertNotIn("new ", calls)

    def test_a_bad_rule_changes_nothing(self):
        for rules in ("TCP:80:Private:somewhere", "ICMP:0:Private:Any", "TCP:80;x:Private:Any",
                      "TCP:80:Home:Any", "TCP:80:Private"):
            code, out, _ = self.run_fw("-Rules", '"%s"' % rules)
            self.assertEqual(code, 1, rules + out)
            calls = out.split("CALLS:", 1)[1]
            self.assertNotIn("remove", calls, rules)
            self.assertNotIn("new ", calls, rules)
            self.assertIn("error: bad", out, rules)


if __name__ == "__main__":
    unittest.main()
