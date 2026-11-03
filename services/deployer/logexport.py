"""Log files to send with a report: one area of the server (the patch service, sign-in, a game)
as one text file, its services' recent logs with a header that says what runs, secrets blanked
(docs/deployer.md, "Exporting logs").

    GET /api/logs/areas                    the areas this server has
    GET /api/logs/export?area=<id>&lines=N&addresses=1

The areas come from the catalog: fixed ones for the core's services, then one per enabled game
(its catalog services). "all" is every area in one file. A service that is not running is
listed, not skipped, so the file says it.

Blanked before anything leaves: every value of a .env setting whose name says it is a secret
(PASSWORD, TOKEN, SECRET, KEY), the deployer's own token, and the session tokens, password
hashes and login digests the services log (as services/logtail.py). Players' public addresses
are shortened to their first two numbers unless addresses=1; LAN, loopback and VPN addresses, which
say how the server is wired, are kept.
"""
import ipaddress
import re
import time

LINES = 2000               # log lines per service, unless asked
MAX_LINES = 20000
SECRET_NAME = re.compile(r"(PASSWORD|PASSWD|TOKEN|SECRET|_KEY$|^KEY_|PRIVATE)", re.I)
MIN_SECRET = 4             # shorter values would blank ordinary words

#: the core's areas: (id, name, its services in the core catalog entry, what it is for)
CORE_AREAS = [
    ("patch", "Patch service", ["patch"],
     "What each console and PC asked when it checked for updates, and what it was given."),
    ("signin", "Sign-in and lobby", ["login", "authsess", "authrelay", "ucs", "ucs-plain", "dns",
                                     "sslterm", "ssl3ucs", "ssl3web", "http"],
     "Signing in, the Viewer's lobby, mail, friends, and the DNS that points consoles here."),
    ("panel", "Admin panel and deployer", ["admin", "deployer"],
     "This panel, and the deployer that installs and updates games."),
    ("data", "Database", ["postgres", "valkey"],
     "PostgreSQL and Valkey, which every service uses."),
]

#: what the services log that must not leave (as services/logtail.py REDACT)
REDACT = [
    (re.compile(r"(session=)[0-9a-f]{6,}", re.I), r"\1<blanked>"),
    (re.compile(r"(token[=:]\s*)[A-Za-z0-9+/=_.-]{8,}", re.I), r"\1<blanked>"),
    (re.compile(r"(Bearer\s+)\S+", re.I), r"\1<blanked>"),
    (re.compile(r"(NICK\s+\S+?:)[0-9a-f]{16,}", re.I), r"\1<blanked>"),
    (re.compile(r"(IV=)[0-9a-f]{8,}", re.I), r"\1<blanked>"),
    (re.compile(r"((?:pw_hash|password|passwd)\W+)\S+", re.I), r"\1<blanked>"),
    (re.compile(r"\bolx\d\.[A-Za-z0-9._~+/=-]{8,}"), "<access code>"),
]
IPV4 = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.])")


def areas(catalog, enabled):
    """[{"id", "name", "services", "about"}]: the core's areas, then each enabled game's."""
    core = set((catalog.get("core") or {}).get("services", [])) | {"deployer"}
    out = []
    for aid, name, svcs, about in CORE_AREAS:
        have = [s for s in svcs if s in core]
        if have:
            out.append({"id": aid, "name": name, "services": have, "about": about})
    for key in enabled:
        e = catalog.get(key) or {}
        if e.get("kind") == "game" and e.get("services"):
            out.append({"id": key, "name": e.get("name") or key, "services": list(e["services"]),
                        "about": "%s's own servers." % (e.get("name") or key)})
    return out


def env_secrets(env):
    """The values of the settings whose names say they are secrets, longest first."""
    vals = {v for k, v in env.items() if SECRET_NAME.search(k) and isinstance(v, str)
            and len(v) >= MIN_SECRET and v.lower() not in ("true", "false", "none")}
    return sorted(vals, key=len, reverse=True)


#: addresses that say how the server is wired, kept as they are: LAN ranges, loopback, link-local,
#: the carrier-grade range VPNs such as Tailscale use, and 0.0.0.0. Anything else is a player's.
WIRING = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",  # polcheck: allow (the LAN ranges, kept as they are)
    "127.0.0.0/8", "169.254.0.0/16",
    "100.64.0.0/10",  # polcheck: allow (the carrier-grade range VPNs use)
    "0.0.0.0/32")]


def _mask_ip(m):
    try:
        ip = ipaddress.ip_address(m.group(0))
    except ValueError:
        return m.group(0)
    if any(ip in n for n in WIRING):
        return m.group(0)
    return "%s.%s.x.x" % (m.group(1), m.group(2))


def clean(text, secrets=(), addresses=False):
    """`text` fit to send: secrets blanked, public addresses masked unless addresses."""
    for s in secrets:
        text = text.replace(s, "<blanked>")
    for rx, sub in REDACT:
        text = rx.sub(sub, text)
    if not addresses:
        text = IPV4.sub(_mask_ip, text)
    return text


def export(area_ids, all_areas, run, lines=LINES, addresses=False, secrets=(), header=None):
    """The file: a header, then for each area its services' state and their last `lines` log
    lines, oldest first. `run(args)` runs stack.py with `args` and returns its output; it is
    given ("compose", "ps", ...) and ("compose", "logs", ...) commands only."""
    lines = max(1, min(int(lines), MAX_LINES))
    by_id = {a["id"]: a for a in all_areas}
    want = all_areas if "all" in area_ids else [by_id[a] for a in area_ids if a in by_id]
    out = ["OpenLobby logs, %s" % time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())]
    for k, v in (header or {}).items():
        out.append("%s: %s" % (k, v))
    out.append("Areas: %s" % ", ".join(a["name"] for a in want))
    out.append("Last %d lines of each service. Secrets are blanked%s." % (
        lines, "" if addresses else ", and players' internet addresses shortened to their first two numbers"))
    known = set(run(["config", "--services"]).split())
    for a in want:
        here = [s for s in a["services"] if not known or s in known]
        gone = [s for s in a["services"] if s not in here]
        out += ["", "=" * 78, "%s (%s)" % (a["name"], ", ".join(a["services"])), "=" * 78, ""]
        if gone:
            out.append("Not set up here: %s" % ", ".join(gone))
        if not here:
            continue
        out += ["-- state", run(["ps", "--all", "--format",
                                 "table {{.Service}}\t{{.State}}\t{{.Status}}\t{{.Image}}"] + here).rstrip()]
        for s in here:
            body = run(["logs", "--no-color", "--timestamps", "--tail", str(lines), s]).rstrip()
            out += ["", "-- %s" % s, body or "(no log: the service has not run here)"]
    return clean("\n".join(out) + "\n", secrets, addresses)
