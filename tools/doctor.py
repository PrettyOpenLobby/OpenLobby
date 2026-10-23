#!/usr/bin/env python3
"""Check a self-hosted OpenLobby setup and say what to fix.

Run it from the checkout, on the machine that runs the stack:

    python tools/doctor.py
    python tools/doctor.py --json

or from another machine on the network, to see what a client there sees:

    python tools/doctor.py --host 192.0.2.10

Each check prints one line, OK, WARN or FAIL, and a WARN or FAIL is followed
by one line saying what to do about it. The exit code is 1 when anything
FAILed, 0 otherwise.

It only reads. The probes it sends are the ones a client sends anyway: a TCP
connect, a DNS question, an HTTP GET to the admin panel and an empty-version
POLP version check (the same question a Viewer asks on a re-login, which the
patch service does not record as a client build). Standard library only, so it
runs on the host's own Python with nothing installed.

With --host, the checks that only make sense on the server itself (.env, the
local interfaces, Docker, mirrors/, game data) are skipped.
"""
import argparse
import concurrent.futures
import glob
import ipaddress
import json
import os
import random
import re
import shutil
import socket
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

#: The name the Viewer asks for first (its version check), and one that the
#: DNS service forwards upstream instead of redirecting.
REDIRECTED_NAME = "pt008.pol.com"
FORWARDED_NAME = "example.com"

#: The ports that have a listener behind them in the default stack, by the
#: compose service that owns them. A published port not listed here (most of
#: the lobby band) is reserved for the lobby but has nothing listening on it
#: by default, so it is not probed. Taken from services/core/main.py
#: (directory 51240, lobby 51220, world 51330, mail 110/25/51260/51261),
#: services/authrelay.py (51241-51250), services/polserver2.py (54000 and
#: 53001-53015 with POLP_CURRENT_ALL=1) and docker-compose.yml.
LISTENERS = {
    80: "portal pages",
    443: "portal pages",
    25: "mail (SMTP)",
    110: "mail (POP3)",
    51220: "lobby",
    51240: "login directory",
    51260: "PlayOnline Mail (POP3)",
    51261: "PlayOnline Mail (SMTP)",
    51305: "sign-up wizard",
    51330: "world",
    54000: "patch version check",
    8080: "sign-up pages (local)",
    8090: "admin panel",
}
for _p in range(51241, 51251):
    LISTENERS[_p] = "auth"
TITLE_PATCH_PORTS = set(range(53001, 53016))
ADMIN_PORT = 8090


class Report:
    def __init__(self):
        self.rows = []

    def add(self, status, check, message, fix=""):
        self.rows.append({"status": status, "check": check,
                          "message": message, "fix": fix if status != "OK" else ""})

    def ok(self, check, message):
        self.add("OK", check, message)

    def warn(self, check, message, fix):
        self.add("WARN", check, message, fix)

    def fail(self, check, message, fix):
        self.add("FAIL", check, message, fix)

    def skip(self, check, message):
        self.add("SKIP", check, message)

    @property
    def failed(self):
        return any(r["status"] == "FAIL" for r in self.rows)

    def print_text(self):
        for r in self.rows:
            print(f"{r['status']:<5} {r['check']:<10} {r['message']}")
            if r["fix"]:
                print(f"{'':<16} -> {r['fix']}")
        counts = {s: sum(1 for r in self.rows if r["status"] == s)
                  for s in ("OK", "WARN", "FAIL", "SKIP")}
        print(f"\n{counts['OK']} ok, {counts['WARN']} warning(s), "
              f"{counts['FAIL']} failure(s), {counts['SKIP']} skipped")


# --------------------------------------------------------------------------- #
# parsers (offline, covered by tools/doctor_test.py)
# --------------------------------------------------------------------------- #
def parse_env(text):
    """KEY=VALUE pairs the way Docker Compose reads a .env file: blank lines
    and # comments skipped, an optional `export `, quotes stripped, and an
    unquoted value cut at ` #`."""
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not re.match(r"^[A-Za-z_][A-Za-z0-9_.-]*$", key):
            continue
        value = value.strip()
        if value[:1] in ("'", '"'):
            q = value[0]
            end = value.find(q, 1)
            value = value[1:end] if end > 0 else value[1:]
        else:
            m = re.search(r"\s#", value)
            if m:
                value = value[:m.start()].rstrip()
        out[key] = value
    return out


_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?])([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def substitute(text, env):
    """Compose variable interpolation: ${V}, $V, ${V:-default}, ${V-default}.
    ${V:-d} uses d when V is unset or empty; ${V-d} only when unset."""
    def one(m):
        if m.group(4):
            return env.get(m.group(4), "")
        name, op, default = m.group(1), m.group(2), m.group(3) or ""
        val = env.get(name)
        if op == ":-":
            return val if val else default
        if op == "-":
            return default if val is None else val
        return val or ""
    return _VAR.sub(one, text.replace("$$", "\0")).replace("\0", "$")


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def _unquote(s):
    s = s.strip()
    if s[:1] in ("'", '"'):
        end = s.find(s[0], 1)
        return s[1:end] if end > 0 else s[1:]
    if " #" in s:
        s = s[:s.index(" #")].rstrip()
    return s


def parse_compose(text):
    """{service: {"ports": [spec, ...], "profiles": [...]}} from a compose file,
    without a YAML library. Reads the top-level `services:` block only, the
    short port syntax ("host:container[/proto]") and the long one
    (target/published/protocol/host_ip items). Anchors merged into a service
    are not followed; the stack's shared block carries no ports."""
    services = {}
    lines = text.splitlines()
    i, n = 0, len(lines)
    while i < n and not re.match(r"^services:\s*(#.*)?$", lines[i]):
        i += 1
    i += 1
    svc_indent = None
    cur = None
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        ind = _indent(line)
        if ind == 0:
            break                                   # next top-level key
        if svc_indent is None:
            svc_indent = ind
        if ind == svc_indent:
            m = re.match(r"^([A-Za-z0-9_.-]+):\s*(#.*)?$", stripped)
            cur = m.group(1) if m else None
            if cur:
                services[cur] = {"ports": [], "profiles": []}
            i += 1
            continue
        if cur is None:
            i += 1
            continue
        m = re.match(r"^(ports|profiles):\s*(.*)$", stripped)
        if not m or ind <= svc_indent:
            i += 1
            continue
        key, rest = m.group(1), m.group(2).strip()
        target = services[cur][key]
        if rest.startswith("["):                    # inline list
            for item in rest.strip("[]").split(","):
                if item.strip():
                    target.append(_unquote(item))
            i += 1
            continue
        key_indent = ind
        i += 1
        long_item = None
        while i < n:
            line = lines[i]
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                i += 1
                continue
            if _indent(line) <= key_indent:
                break
            if stripped.startswith("- "):
                body = stripped[2:].strip()
                lm = re.match(r"^([a-z_]+):\s*(.*)$", body)
                if key == "ports" and lm and not body[:1] in ("'", '"'):
                    long_item = {lm.group(1): _unquote(lm.group(2))}
                    target.append(long_item)
                else:
                    long_item = None
                    target.append(_unquote(body))
            elif long_item is not None:
                lm = re.match(r"^([a-z_]+):\s*(.*)$", stripped)
                if lm:
                    long_item[lm.group(1)] = _unquote(lm.group(2))
            i += 1
    return services


def _port_range(text):
    a, sep, b = text.partition("-")
    a = int(a)
    b = int(b) if sep else a
    if not (0 < a <= b <= 65535):
        raise ValueError(text)
    return list(range(a, b + 1))


def parse_port_spec(spec):
    """[(host_ip, host_port, proto), ...] a port entry publishes on the host,
    after interpolation. host_ip is "" when it binds every address. An entry
    with no host side ("8080") publishes a random port and yields []."""
    if isinstance(spec, dict):
        pub = str(spec.get("published", "")).strip()
        if not pub:
            return []
        proto = (spec.get("protocol") or "tcp").lower()
        return [(spec.get("host_ip", "") or "", p, proto) for p in _port_range(pub)]
    spec = spec.strip()
    proto = "tcp"
    if "/" in spec:
        spec, proto = spec.rsplit("/", 1)
        proto = proto.lower()
    host_ip = ""
    if spec.startswith("["):                        # [v6]:host:container
        end = spec.index("]")
        host_ip, spec = spec[1:end], spec[end + 2:]
    parts = spec.split(":")
    if len(parts) == 3:
        host_ip, host, _container = parts
    elif len(parts) == 2:
        host, _container = parts
    else:
        return []
    if not host:
        return []
    if host_ip in ("0.0.0.0", "::"):
        host_ip = ""
    return [(host_ip, p, proto) for p in _port_range(host)]


def compose_ports(services, env):
    """[(service, host_ip, port, proto), ...] for every service, interpolated."""
    out = []
    for name, svc in services.items():
        for spec in svc["ports"]:
            if isinstance(spec, dict):
                spec = {k: substitute(v, env) for k, v in spec.items()}
            else:
                spec = substitute(spec, env)
            try:
                for host_ip, port, proto in parse_port_spec(spec):
                    out.append((name, host_ip, port, proto))
            except ValueError:
                continue
    return out


def dns_query(name, qid, qtype=1):
    """A standard recursive query for `name` (type A by default)."""
    header = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    q = b""
    for label in name.rstrip(".").split("."):
        raw = label.encode("ascii")
        if not 0 < len(raw) < 64:
            raise ValueError(f"bad label in {name!r}")
        q += bytes([len(raw)]) + raw
    return header + q + b"\x00" + struct.pack(">HH", qtype, 1)


def _skip_name(data, i):
    while True:
        n = data[i]
        if n == 0:
            return i + 1
        if n & 0xC0 == 0xC0:
            return i + 2
        i += 1 + n


def dns_parse_reply(data, qid):
    """(rcode, [IPv4 addresses]) from a reply to query `qid`. Raises
    ValueError when it is not a reply to that query."""
    if len(data) < 12:
        raise ValueError("short reply")
    rid, flags, qd, an, _ns, _ar = struct.unpack_from(">HHHHHH", data, 0)
    if rid != qid:
        raise ValueError("wrong transaction id")
    if not flags & 0x8000:
        raise ValueError("not a response")
    rcode = flags & 0x000F
    i = 12
    for _ in range(qd):
        i = _skip_name(data, i) + 4
    ips = []
    for _ in range(an):
        i = _skip_name(data, i)
        rtype, _rclass, _ttl, rdlen = struct.unpack_from(">HHIH", data, i)
        i += 10
        if rtype == 1 and rdlen == 4:
            ips.append(socket.inet_ntoa(data[i:i + 4]))
        i += rdlen
    return rcode, ips


def parse_enabled_games(text):
    """Game keys from openlobby/.games: a JSON list, a JSON object (keys whose
    value is truthy, or an "enabled" list), or one key per line / comma list
    with # comments, where "key=..." counts as the key."""
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, list):
        return [str(k) for k in data if isinstance(k, str) and k]
    if isinstance(data, dict):
        if isinstance(data.get("enabled"), list):
            return [str(k) for k in data["enabled"] if isinstance(k, str)]
        return [k for k, v in data.items() if v]
    keys = []
    for line in text.splitlines():
        line = line.split("#", 1)[0]
        for tok in re.split(r"[,\s]+", line):
            tok = re.split(r"[=:]", tok, 1)[0].strip()
            if tok:
                keys.append(tok)
    return keys


def config_stub_ip(text):
    m = re.search(r"^stub_ip:\s*['\"]?([^'\"\s#]+)", text, re.M)
    return m.group(1) if m else ""


# --------------------------------------------------------------------------- #
# probes
# --------------------------------------------------------------------------- #
def local_addresses():
    """IPv4 addresses this machine holds, as far as the standard library and
    `ip`/`hostname -I` (when present) can tell."""
    addrs = {"127.0.0.1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except OSError:
        pass
    for probe in ("192.0.2.1", "198.51.100.1"):
        # A UDP connect sends nothing; it only picks the source address the
        # routing table would use.
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((probe, 9))
            addrs.add(s.getsockname()[0])
            s.close()
        except OSError:
            pass
    for cmd in (["ip", "-o", "-4", "addr", "show"], ["hostname", "-I"]):
        if not shutil.which(cmd[0]):
            continue
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        addrs.update(re.findall(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", out))
    return addrs


def tcp_probe(host, port, timeout=3.5, listen=0.6):
    """'open' (accepted and waiting), 'greeting' (accepted and spoke first),
    'closed' (accepted then dropped at once), 'refused', 'timeout' or
    'unreachable: ...'."""
    try:
        s = socket.create_connection((host, port), timeout=timeout)
    except ConnectionRefusedError:
        return "refused"
    except (socket.timeout, TimeoutError):
        return "timeout"
    except OSError as exc:
        return "unreachable: " + (exc.strerror or str(exc))
    try:
        s.settimeout(listen)
        try:
            data = s.recv(1)
        except (socket.timeout, TimeoutError):
            return "open"
        except ConnectionResetError:
            return "closed"
        except OSError:
            return "closed"
        return "greeting" if data else "closed"
    finally:
        s.close()


def dns_ask(server, name, timeout=3.0):
    """(rcode, ips) or None when nothing answered."""
    qid = random.randrange(1, 0xFFFF)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(dns_query(name, qid), (server, 53))
        while True:
            data, _src = s.recvfrom(4096)
            try:
                return dns_parse_reply(data, qid)
            except ValueError:
                continue
    except (socket.timeout, TimeoutError):
        return None
    except OSError:
        return None
    finally:
        s.close()


def polp_version_check(host, port=54000, timeout=4.0):
    """Send cmd 7 for the PC Viewer (W2U/1000) with an empty version and
    return the reply's command number, or an error string."""
    import hashlib
    body = bytearray(0x48)
    body[0:3] = b"W2U"
    body[4:8] = b"1000"
    pkt = bytearray(16)
    pkt[8:12] = b"POLP"
    struct.pack_into("<I", pkt, 12, 7)
    pkt += body
    struct.pack_into("<I", pkt, 0, len(pkt))
    struct.pack_into("<I", pkt, 4, int.from_bytes(
        hashlib.md5(bytes(pkt[8:])).digest()[:4], "little"))
    try:
        s = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        return "no connection (" + (exc.strerror or type(exc).__name__) + ")"
    try:
        s.settimeout(timeout)
        s.sendall(bytes(pkt))
        buf = b""
        while len(buf) < 16:
            chunk = s.recv(4096)
            if not chunk:
                return "closed without a reply"
            buf += chunk
        if buf[8:12] != b"POLP":
            return "answered, but not with a POLP frame"
        return struct.unpack_from("<I", buf, 12)[0]
    except (socket.timeout, TimeoutError):
        return "no reply"
    except OSError as exc:
        return "error (" + (exc.strerror or type(exc).__name__) + ")"
    finally:
        s.close()


def http_status(host, port, timeout=3.0):
    try:
        s = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        return None, exc.strerror or type(exc).__name__
    try:
        s.settimeout(timeout)
        s.sendall(f"GET / HTTP/1.0\r\nHost: {host}:{port}\r\n\r\n".encode())
        data = s.recv(256)
    except OSError as exc:
        return None, exc.strerror or type(exc).__name__
    finally:
        s.close()
    m = re.match(rb"HTTP/1\.[01] (\d{3})", data or b"")
    if not m:
        return None, "answered, but not with HTTP"
    return int(m.group(1)), ""


def run(cmd, cwd=None, timeout=30, env=None):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return None, "", "not found"
    except subprocess.TimeoutExpired:
        return None, "", "timed out"
    except OSError as exc:
        return None, "", str(exc)


def parse_ps_json(text):
    """Rows from `docker compose ps --format json`: a JSON array on older
    Compose releases, one object per line on newer ones."""
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else [data]
    except ValueError:
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
        return rows


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #
def is_ipv4(text):
    try:
        ipaddress.IPv4Address(text)
        return True
    except ValueError:
        return False


def check_env(rep, env_path, env, local):
    if not os.path.isfile(env_path):
        rep.warn("env", ".env not found; the stack runs on built-in defaults "
                 "(clients are handed 127.0.0.1)",
                 "cp .env.example .env, set POL_ADVERTISE, then "
                 "docker compose up -d")
    else:
        rep.ok("env", ".env found")
    adv = env.get("POL_ADVERTISE", "").strip()
    if not adv:
        rep.warn("advertise", "POL_ADVERTISE is empty, so clients are handed "
                 "127.0.0.1 and only a client on this same machine can connect",
                 "for other machines, set POL_ADVERTISE in .env to the LAN, VPN "
                 "or public address they reach this host on, then "
                 "docker compose up -d")
        return
    if not is_ipv4(adv):
        rep.fail("advertise", f"POL_ADVERTISE={adv!r} is not an IPv4 address",
                 "use a dotted IPv4 address: clients dial it as a raw address "
                 "and the DNS service answers with it")
        return
    ip = ipaddress.IPv4Address(adv)
    if ip.is_unspecified:
        rep.fail("advertise", "POL_ADVERTISE is 0.0.0.0, which no client can dial",
                 "set it to the address clients reach this host on")
        return
    if ip.is_loopback:
        rep.warn("advertise", f"POL_ADVERTISE={adv} is a loopback address; only "
                 "a client on this machine can connect",
                 "for other machines, set it to this host's LAN, VPN or public "
                 "address")
        return
    if local is None:
        rep.ok("advertise", f"POL_ADVERTISE={adv}")
    elif adv in local:
        rep.ok("advertise", f"POL_ADVERTISE={adv} is an address of this machine")
    elif ip.is_global:
        rep.warn("advertise", f"POL_ADVERTISE={adv} is not on any interface of "
                 "this machine; fine if it is your router's public address",
                 "forward the published ports (listed under ports) from the "
                 "router to this machine, and test from outside with "
                 "tools/doctor.py --host " + adv)
    else:
        rep.warn("advertise", f"POL_ADVERTISE={adv} is not on any interface "
                 "this check can see",
                 "check it for a typo; if it belongs to a VPN or another "
                 "interface, run tools/doctor.py --host " + adv +
                 " from a client machine")


def check_docker(rep, services):
    core = [s for s, v in services.items() if not v["profiles"]]
    rc, out, err = run(["docker", "compose", "version"], timeout=20)
    if rc is None and "not found" in err:
        rep.skip("docker", "docker is not on PATH; skipping the container checks")
        return None
    if rc != 0:
        msg = (err or out).strip().splitlines()
        rep.fail("docker", "docker compose does not run"
                 + (": " + msg[-1] if msg else ""),
                 "install the Docker Compose plugin (the `docker compose` command)")
        return None
    rep.ok("docker", out.strip().splitlines()[0] if out.strip() else "docker compose works")
    rc, out, err = run(["docker", "compose", "ps", "--all", "--format", "json"],
                       cwd=ROOT, timeout=30)
    if rc != 0:
        msg = (err or out).strip().splitlines()
        rep.fail("services", "docker compose ps failed: "
                 + (msg[-1] if msg else "no output"),
                 "start Docker (on Windows and macOS, Docker Desktop) and run "
                 "this from the openlobby checkout")
        return None
    rows = parse_ps_json(out)
    state = {}
    for r in rows:
        name = r.get("Service") or r.get("Name") or ""
        state[name] = ((r.get("State") or "").lower(), (r.get("Health") or "").lower())
    if not rows:
        rep.fail("services", "no containers for this project",
                 "docker compose up -d --build (from the openlobby checkout)")
        return state
    down = [s for s in core if state.get(s, ("", ""))[0] != "running"]
    sick = [s for s in core if s not in down and state[s][1] == "unhealthy"]
    starting = [s for s in core if s not in down and state[s][1] == "starting"]
    if down:
        detail = ", ".join(f"{s} ({state[s][0]})" if s in state else f"{s} (missing)"
                           for s in down)
        rep.fail("services", f"not running: {detail}",
                 "docker compose up -d, then docker compose logs <service> "
                 "for the reason")
    if sick:
        rep.warn("services", "unhealthy: " + ", ".join(sick),
                 "docker compose logs " + sick[0])
    if starting:
        rep.warn("services", "still starting: " + ", ".join(starting),
                 "wait a minute and run this again")
    if not down and not sick and not starting:
        rep.ok("services", f"all {len(core)} core services running")
    return state


def is_running(state, svc):
    """True unless `docker compose ps` said the service is not running (or
    could not be asked, in which case everything is probed)."""
    return state is None or state.get(svc, ("", ""))[0] == "running"


def check_ports(rep, ports, target, remote, advertise, env, state=None):
    """Probe the published TCP ports that have a listener by default, and
    port 53 with a real DNS question. Ports of a service Docker reports as
    stopped are summarised on one line, unless something else answers there."""
    current_all = env.get("POLP_CURRENT_ALL", "1") or "1"
    jobs, skipped_ranges, loop_only = [], [], []
    seen = set()
    dns_rows = []
    for svc, host_ip, port, proto in ports:
        if proto == "udp":
            if port == 53:
                dns_rows.append((svc, host_ip))
            continue
        if (port, host_ip) in seen:
            continue
        seen.add((port, host_ip))
        loopback = host_ip.startswith("127.") or host_ip == "::1"
        if loopback:
            if port == ADMIN_PORT:
                continue                 # check_admin covers it
            if remote:
                loop_only.append(port)
                continue
            where = host_ip
        elif host_ip and not remote:
            where = host_ip
        else:
            where = target
        if port in LISTENERS:
            label = LISTENERS[port]
        elif port in TITLE_PATCH_PORTS:
            label = f"title patch {port}"
        else:
            skipped_ranges.append(port)
            continue
        jobs.append((svc, where, port, label))

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(lambda j: (j, tcp_probe(j[1], j[2])), jobs))

    title_bad = []
    stopped = {}
    for (svc, where, port, label), res in results:
        if not is_running(state, svc):
            if res in ("open", "greeting"):
                rep.warn("ports", f"{where}:{port} answers although {svc} is not "
                         "running: another program holds this port",
                         f"stop that program or move it off port {port} before "
                         f"starting {svc}")
            else:
                stopped.setdefault(svc, []).append(port)
            continue
        if port in TITLE_PATCH_PORTS:
            if res not in ("open", "greeting"):
                title_bad.append((port, res))
            continue
        name = f"{where}:{port}"
        if res in ("open", "greeting"):
            rep.ok("ports", f"{name} {label} ({svc}) answers")
        elif res == "closed":
            rep.warn("ports", f"{name} {label} ({svc}) accepts and drops the "
                     "connection at once: Docker holds the port but nothing "
                     "inside is listening",
                     f"docker compose logs {svc}")
        elif res == "refused":
            rep.fail("ports", f"{name} {label} ({svc}) refused",
                     f"docker compose ps {svc}; if it is up, another program "
                     f"on this host may own port {port}")
        else:
            fix = (f"a firewall between here and {where} is dropping port "
                   f"{port}; open it" if remote else
                   f"check the host firewall and that {svc} is running")
            rep.fail("ports", f"{name} {label} ({svc}): {res}", fix)
    if stopped:
        rep.skip("ports", "not probed, service not running: " + ", ".join(
            f"{svc} ({len(ps)} port{'s' if len(ps) > 1 else ''})"
            for svc, ps in stopped.items()))
    title_jobs = [j for j in jobs if j[2] in TITLE_PATCH_PORTS
                  and is_running(state, j[0])]
    if title_jobs:
        if not title_bad:
            rep.ok("ports", f"{target}:53001-53015 title patch ports answer")
        else:
            bad = ", ".join(f"{p} {r}" for p, r in title_bad)
            msg = f"title patch ports not answering: {bad}"
            if current_all == "0":
                rep.warn("ports", msg + " (POLP_CURRENT_ALL=0 listens only on "
                         "the trees under mirrors/ and POLP_CURRENT)",
                         "add the title's tree to mirrors/ or POLP_CURRENT, or "
                         "set POLP_CURRENT_ALL=1")
            else:
                rep.fail("ports", msg, "docker compose logs patch")
    if skipped_ranges:
        rep.ok("ports", f"{len(skipped_ranges)} more published lobby-band ports "
               "not probed (reserved, no listener by default)")
    if loop_only:
        rep.ok("ports", "published on loopback only, not probed from here: "
               + ", ".join(map(str, sorted(set(loop_only)))))

    for svc, host_ip in dns_rows[:1]:
        where = host_ip if (host_ip and not remote) else target
        if is_running(state, svc):
            check_dns(rep, where, advertise, svc)
        else:
            rep.skip("dns", f"not probed, {svc} is not running")
    if not dns_rows:
        rep.warn("dns", "docker-compose.yml publishes no udp/53",
                 "restore the dns service's port 53 entry")


def check_dns(rep, server, advertise, svc="dns"):
    ans = dns_ask(server, REDIRECTED_NAME)
    if ans is None:
        rep.fail("dns", f"{server}:53/udp did not answer {REDIRECTED_NAME}",
                 "the DNS service ignores clients outside its allowed ranges "
                 "(default: loopback and the private ranges); add yours as a "
                 "`clients:` CIDR list under `dns:` in config/server.yaml and "
                 "docker compose restart dns. Also check the firewall for udp/53 "
                 f"and docker compose logs {svc}")
        return
    rcode, ips = ans
    if rcode != 0 or not ips:
        rep.fail("dns", f"{server}:53 answered {REDIRECTED_NAME} with rcode "
                 f"{rcode} and no address",
                 "the asking address is outside the allowed ranges; add it as a "
                 "`clients:` CIDR list under `dns:` in config/server.yaml")
        return
    if advertise and advertise not in ips:
        rep.fail("dns", f"{server}:53 answers {REDIRECTED_NAME} with "
                 f"{', '.join(ips)}, expected {advertise}",
                 "the dns container has a different POL_ADVERTISE: "
                 "docker compose up -d dns after editing .env")
    else:
        rep.ok("dns", f"{server}:53 answers {REDIRECTED_NAME} -> {', '.join(ips)}")

    ans = dns_ask(server, FORWARDED_NAME, timeout=5.0)
    if ans is None:
        rep.warn("dns", f"{server}:53 did not answer {FORWARDED_NAME} (a name "
                 "it forwards upstream)",
                 "the container cannot reach its upstream resolver (dns.upstream "
                 "in config/server.yaml); a console that uses this box as its "
                 "only DNS server will lose other names")
    elif ans[0] == 5:
        rep.warn("dns", f"{server}:53 REFUSED {FORWARDED_NAME}; this address is "
                 "being treated as an internet client",
                 "add it to `clients:` under `dns:` in config/server.yaml if it "
                 "should get ordinary forwarding")
    elif ans[1] and advertise in ans[1]:
        rep.warn("dns", f"{server}:53 redirects {FORWARDED_NAME} too",
                 "check dns.zones and dns.redirect_only in config/server.yaml")
    else:
        rep.ok("dns", f"{server}:53 forwards {FORWARDED_NAME} upstream")


def check_patch(rep, target, env, remote, state=None):
    res = polp_version_check(target) if is_running(state, "patch") else None
    if res is None:
        rep.skip("patch", "version check not probed, patch is not running")
    elif res == 8:
        rep.ok("patch", f"{target}:54000 answers a Viewer version check")
    elif res == 5:
        rep.warn("patch", f"{target}:54000 rejects the PC Viewer version check: "
                 "no W2U-1000 tree under mirrors/ and POLP_CURRENT_ALL is off",
                 "set POLP_CURRENT_ALL=1 or add W2U/1000 to POLP_CURRENT in .env, "
                 "or supply the Viewer tree (docs/patching.md)")
    else:
        rep.fail("patch", f"{target}:54000 version check: {res}",
                 "the Viewer stops with POL-1160 without this; "
                 "docker compose logs patch")
    if remote:
        return
    mirrors = env.get("POL_MIRRORS") or "./mirrors"
    mdir = mirrors if os.path.isabs(mirrors) else os.path.join(ROOT, mirrors)
    current_all = (env.get("POLP_CURRENT_ALL") or "1") == "1"
    bundles = []
    if os.path.isdir(mdir):
        bundles = sorted(d for d in os.listdir(mdir)
                         if os.path.isfile(os.path.join(mdir, d, "patchlist.raw")))
    if not bundles:
        if current_all or env.get("POLP_CURRENT"):
            rep.ok("mirrors", "no patch trees under mirrors/; version checks "
                   "are answered as up to date")
        else:
            rep.fail("mirrors", "no patch trees under mirrors/ and "
                     "POLP_CURRENT_ALL=0 with no POLP_CURRENT: the patch "
                     "service exits at start",
                     "set POLP_CURRENT_ALL=1, or supply trees (docs/patching.md)")
        return
    cmd = [sys.executable, os.path.join(ROOT, "services", "polserver2.py"),
           mdir, "--check"]
    penv = dict(os.environ)
    for k in ("POLP_SLIM_LIST", "POLP_PIN", "POLP_LIST_CAP", "POLP_CURRENT"):
        if env.get(k):
            penv[k] = env[k]
    penv["POLP_CONSOLE_LIST_CAP"] = env.get("POLP_CONSOLE_LIST_CAP") or "20130601_E"
    rc, out, err = run(cmd, cwd=os.path.join(ROOT, "services"), timeout=600, env=penv)
    if rc == 0:
        rep.ok("mirrors", f"{len(bundles)} patch tree(s) complete: "
               + ", ".join(bundles))
    else:
        lines = [l.strip() for l in (out + err).splitlines() if l.strip()]
        bad = [l for l in lines if "missing" in l or "short" in l
               or "cannot verify" in l]
        rep.fail("mirrors", "polserver2.py --check: "
                 + ("; ".join(bad[:3]) if bad else (lines[-1] if lines else "failed")),
                 "python services/polserver2.py " + mirrors + " --check for the "
                 "full list; rebuild or re-copy the incomplete tree")


def check_admin(rep, ports, target, remote, state=None):
    published = [(h, p) for _s, h, p, proto in ports
                 if p == ADMIN_PORT and proto == "tcp"]
    exposed = [h for h, _p in published if not (h.startswith("127.") or h == "::1")]
    if remote:
        res = tcp_probe(target, ADMIN_PORT)
        if res in ("open", "greeting", "closed"):
            rep.warn("admin", f"the admin panel port {ADMIN_PORT} answers on "
                     f"{target} from this machine",
                     'publish it as "127.0.0.1:8090:8090" in docker-compose.yml '
                     "and reach it over an SSH tunnel")
        else:
            rep.ok("admin", f"admin panel not reachable on {target} (as intended)")
        return
    if exposed:
        rep.warn("admin", f"docker-compose.yml publishes the admin panel on "
                 f"{exposed[0] or 'every address'}:{ADMIN_PORT}",
                 'publish it as "127.0.0.1:8090:8090" and reach it over an SSH '
                 "tunnel")
    if not is_running(state, "admin"):
        rep.skip("admin", "not probed, admin is not running")
        return
    status, why = http_status("127.0.0.1", ADMIN_PORT)
    if status is None:
        rep.fail("admin", f"127.0.0.1:{ADMIN_PORT} admin panel: {why}",
                 "docker compose logs admin")
    else:
        rep.ok("admin", f"127.0.0.1:{ADMIN_PORT} admin panel answers HTTP {status}")


def check_games(rep, env):
    catalog = sorted(glob.glob(os.path.join(ROOT, "games", "*.json")))
    if not catalog:
        rep.skip("games", "no games catalog (games/*.json)")
        return
    enabled_path = os.path.join(ROOT, ".games")
    try:
        with open(enabled_path, encoding="utf-8") as f:
            enabled = parse_enabled_games(f.read())
    except OSError:
        rep.skip("games", "no titles enabled (.games not found)")
        return
    entries = {}
    for path in catalog:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        for item in (data if isinstance(data, list) else [data]):
            if isinstance(item, dict) and isinstance(item.get("key"), str):
                entries[item["key"]] = item
    for key in enabled:
        item = entries.get(key)
        if not item or not isinstance(item.get("data"), list):
            continue
        base = ROOT
        checkout = item.get("checkout")
        if isinstance(checkout, str) and checkout:
            checkout = substitute(checkout, env)
            # as tools/stack.py has it: the core is this checkout, a game's sits beside it
            base = (checkout if os.path.isabs(checkout)
                    else ROOT if item.get("kind") == "core"
                    else os.path.join(os.path.dirname(ROOT), checkout))
            if not os.path.isdir(base):
                rep.fail("games", f"{key}: checkout {checkout} not found",
                         f"clone the {key} repository next to openlobby, as "
                         "its README describes")
                continue
        missing = []
        for d in item["data"]:
            if not isinstance(d, dict) or not isinstance(d.get("path"), str):
                continue
            p = substitute(d["path"], env)
            full = p if os.path.isabs(p) else os.path.join(base, p)
            if not os.path.exists(full):
                missing.append((p, d.get("hint") if isinstance(d.get("hint"), str) else ""))
        if missing:
            p, hint = missing[0]
            more = f" (+{len(missing) - 1} more)" if len(missing) > 1 else ""
            rep.fail("games", f"{key}: missing {p}{more}",
                     hint or f"see the {key} README for where this comes from")
        else:
            rep.ok("games", f"{key}: game data present")


# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", help="probe this address instead of POL_ADVERTISE "
                    "(run from another machine to see what a client sees)")
    ap.add_argument("--json", action="store_true", help="print the results as JSON")
    ap.add_argument("--compose", default=os.path.join(ROOT, "docker-compose.yml"),
                    help="compose file to read the published ports from")
    args = ap.parse_args(argv)

    rep = Report()
    remote = bool(args.host)
    env_path = os.path.join(ROOT, ".env")
    dotenv = {}
    if os.path.isfile(env_path):
        with open(env_path, encoding="utf-8", errors="replace") as f:
            dotenv = parse_env(f.read())
    env = dict(dotenv)
    env.update(os.environ)                    # the shell wins, as in Compose

    stub_ip = ""
    try:
        with open(os.path.join(ROOT, "config", "server.yaml"), encoding="utf-8") as f:
            stub_ip = config_stub_ip(f.read())
    except OSError:
        pass
    adv_set = env.get("POL_ADVERTISE", "").strip()
    # What the dns service answers with: POL_ADVERTISE, else stub_ip
    # (services/stub.py advertise_ip).
    advertise = adv_set or stub_ip or "127.0.0.1"

    try:
        with open(args.compose, encoding="utf-8") as f:
            services = parse_compose(f.read())
    except OSError as exc:
        rep.fail("compose", f"cannot read {args.compose}: {exc.strerror}",
                 "run this from the openlobby checkout")
        services = {}
    ports = compose_ports(services, env)

    state = None
    if remote:
        target = args.host
        if adv_set and is_ipv4(adv_set) and adv_set != target:
            rep.ok("advertise", f"POL_ADVERTISE in this checkout's .env is "
                   f"{adv_set}; DNS answers are compared against it")
        elif not adv_set:
            advertise = target
        for name in ("env", "docker", "mirrors", "games"):
            rep.skip(name, "skipped with --host (run on the server itself)")
    else:
        local = local_addresses()
        check_env(rep, env_path, env, local)
        if adv_set and is_ipv4(adv_set) and adv_set in local:
            target = adv_set
        else:
            target = "127.0.0.1"
            if adv_set and is_ipv4(adv_set) and not ipaddress.IPv4Address(adv_set).is_loopback:
                rep.ok("ports", f"probing 127.0.0.1, since {adv_set} is not on "
                       f"this machine; run with --host {adv_set} from outside "
                       "to test that address")
        if services:
            state = check_docker(rep, services)

    if services:
        check_ports(rep, ports, target, remote, advertise, env, state)
    check_patch(rep, target, env, remote, state)
    if services:
        check_admin(rep, ports, target, remote, state)
    if not remote:
        check_games(rep, env)

    if args.json:
        print(json.dumps({"ok": not rep.failed, "target": target,
                          "checks": rep.rows}, indent=2))
    else:
        rep.print_text()
    return 1 if rep.failed else 0


if __name__ == "__main__":
    sys.exit(main())
