"""svcwatch.py -- tell the GM channel when a service stops taking connections.

Once a minute polbridge calls `tick()`, which opens a TCP connection to every
watched port and closes it again. A service that refuses or times out
FAIL_AFTER checks in a row gets one post in every bound GM alerts channel
(the same channels GM calls go to, `/playonline gm` binds them). Once it
answers OK_AFTER checks in a row the post is deleted.

What a post points at is kept in discord_meta under META_KEY, so a bridge
restart neither posts a second copy nor forgets to clean up. The bridge posts
as itself, so it can always delete its own message without Manage Messages.

A plain target is a TCP connect check. A port that binds but has a broken
handler still passes; this catches the case where nothing is listening at all,
which is what a crash loop or a lost bind looks like from a player's side
(POL-0011).

A target written "dns:host:port" is a DNS server instead. It is sent an A
query for POL_BRIDGE_WATCH_DNS_NAME and passes only on a NOERROR reply with at
least one answer. DNS is UDP, so there is no connection to open. If another
resolver on the host takes port 53 first, the DNS stub cannot bind and
restart-loops, and players see POL-0020; this is the check for that.

    POL_BRIDGE_WATCH          "Name=host:port;Name=dns:host:port" (default
                              below), "off" to disable
    POL_BRIDGE_WATCH_POLL     seconds between checks (60)
    POL_BRIDGE_WATCH_DNS_NAME the name a dns: target is asked for
                              (pt008.pol.com, which the stub always redirects)
"""
import json
import os
import random
import socket
import struct
import time

import discordlink

DEFAULT_TARGETS = ";".join([
    "PlayOnline lobby=127.0.0.1:51220",
    "PlayOnline sign-in=127.0.0.1:51241",
    "PlayOnline Mail=127.0.0.1:51260",
    "Sign-up wizard=127.0.0.1:51305",
    "Patch server=127.0.0.1:54000",
    "Web site=127.0.0.1:443",
    "Front Mission Online=127.0.0.1:61300",
    "Fantasy Earth=127.0.0.1:54848",
    "PlayOnline DNS=dns:127.0.0.1:53",
])

DNS_PREFIX = "dns:"
DNS_RCODES = {1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN", 4: "NOTIMP", 5: "REFUSED"}

META_KEY = "svcwatch-alerts"
FAIL_AFTER = 2
OK_AFTER = 2
CONNECT_TIMEOUT = 5.0
COLOR = 0xD08020

_api = None
_log = print
# name -> consecutive failures (positive) or successes (negative), and the
# time of the first failure in the current run. In memory only: after a
# restart a service needs FAIL_AFTER fresh failures again.
_streak = {}
_first_fail = {}


def wire(api_fn, logger=None):
    global _api, _log
    _api = api_fn
    if logger is not None:
        _log = logger


def parse_targets(spec):
    """'Name=host:port;...' -> [(name, host, port)]. Bad entries are skipped
    and logged."""
    out = []
    for part in (spec or "").split(";"):
        part = part.strip()
        if not part:
            continue
        name, sep, addr = part.rpartition("=")
        host, sep2, port = addr.rpartition(":")
        try:
            port = int(port)
        except ValueError:
            port = 0
        if not (sep and sep2 and name.strip() and host and 0 < port < 65536):
            _log("svcwatch: ignoring target %r" % part)
            continue
        out.append((name.strip(), host, port))
    return out


def targets():
    spec = os.environ.get("POL_BRIDGE_WATCH", "").strip()
    if spec.lower() in ("off", "0", "no", "false"):
        return []
    return parse_targets(spec or DEFAULT_TARGETS)


def dns_name():
    return os.environ.get("POL_BRIDGE_WATCH_DNS_NAME", "").strip() or "pt008.pol.com"


def dns_probe(host, port, qname, timeout=CONNECT_TIMEOUT):
    """None when the server answers an A query for qname with NOERROR and at
    least one record, else a short reason."""
    qid = random.randrange(0x10000)
    query = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    for label in qname.rstrip(".").split("."):
        query += bytes([len(label)]) + label.encode("ascii")
    query += b"\x00" + struct.pack(">HH", 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(timeout)
        s.connect((host, port))
        s.send(query)
        while True:
            reply = s.recv(4096)
            if len(reply) >= 12 and struct.unpack(">H", reply[:2])[0] == qid:
                break
    except socket.timeout:
        return "no DNS answer, timed out"
    except ConnectionRefusedError:
        return "nothing listening"
    except OSError as e:
        return e.strerror or str(e)
    finally:
        s.close()
    flags, ancount = struct.unpack(">H", reply[2:4])[0], struct.unpack(">H", reply[6:8])[0]
    rcode = flags & 0xF
    if rcode:
        return "answered %s" % DNS_RCODES.get(rcode, "rcode %d" % rcode)
    if not ancount:
        return "answered with no address for %s" % qname
    return None


def probe(host, port, timeout=CONNECT_TIMEOUT):
    """None when a connection opens (for a dns: target, when the server
    answers), else a short reason."""
    if host.startswith(DNS_PREFIX):
        return dns_probe(host[len(DNS_PREFIX):], port, dns_name(), timeout)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except socket.timeout:
        return "timed out"
    except ConnectionRefusedError:
        return "connection refused"
    except OSError as e:
        return e.strerror or str(e)


def _load(ldb):
    try:
        v = json.loads(discordlink.get_meta(ldb, META_KEY, "{}") or "{}")
    except ValueError:
        v = {}
    return v if isinstance(v, dict) else {}


def _save(ldb, alerts):
    discordlink.set_meta(ldb, META_KEY, json.dumps(alerts, sort_keys=True))


def _payload(name, host, port, reason, since, role_id):
    ping = ("<@&%s> " % role_id) if role_id else ""
    return {
        "content": ping,
        "embeds": [{
            "title": "%s is down" % name,
            "description": ((("DNS on port %d is not answering (%s).\n"
                              if host.startswith(DNS_PREFIX) else
                              "Port %d is not accepting connections (%s).\n")
                             % (port, reason))
                            + ("Down since <t:%d:R>. This message is removed "
                               "when it is back." % int(since))),
            "color": COLOR,
        }],
        "allowed_mentions": {"parse": [], "roles": [role_id] if role_id else []},
    }


def _post(ldb, name, host, port, reason, since):
    """One post per bound GM channel -> [{channel, id}] of the ones that went."""
    posts = []
    for row in discordlink.gm_channel_rows(ldb):
        st, data = _api("POST", "/channels/%s/messages" % row["channel_id"],
                        _payload(name, host, port, reason, since, row["role_id"]))
        if st in (200, 201) and data.get("id"):
            posts.append({"channel": str(row["channel_id"]), "id": str(data["id"])})
        else:
            _log("svcwatch: could not post %s down to channel %s (%s %s)"
                 % (name, row["channel_id"], st, str(data)[:160]))
    return posts


def _delete(post):
    """True when the message is gone or can never be deleted (404, 403)."""
    st, data = _api("DELETE", "/channels/%s/messages/%s" % (post["channel"], post["id"]))
    if st in (200, 204, 403, 404):
        return True
    _log("svcwatch: could not delete %s in channel %s (%s %s), will retry"
         % (post["id"], post["channel"], st, str(data)[:160]))
    return False


def tick(now=None, probe_fn=None):
    """One round of checks. Returns {name: "down" | "up"} for the services
    whose alert was posted or cleared on this round."""
    if _api is None:
        return {}
    now = time.time() if now is None else now
    probe_fn = probe_fn or probe
    results = {name: (host, port, probe_fn(host, port))
               for name, host, port in targets()}
    changed = {}
    ldb = discordlink.connect()
    try:
        alerts = _load(ldb)
        dirty = False
        for name, (host, port, reason) in results.items():
            s = _streak.get(name, 0)
            s = max(s, 0) + 1 if reason else min(s, 0) - 1
            _streak[name] = s
            if s == 1:
                _first_fail[name] = now
            alert = alerts.get(name)
            if reason and s >= FAIL_AFTER and alert is None:
                since = _first_fail.get(name, now)
                _log("svcwatch: %s (%s:%d) down: %s" % (name, host, port, reason))
                posts = _post(ldb, name, host, port, reason, since)
                if posts:
                    alerts[name] = {"since": since, "posts": posts}
                    dirty = True
                    changed[name] = "down"
            elif not reason and -s >= OK_AFTER and alert is not None:
                _log("svcwatch: %s (%s:%d) back after %ds"
                     % (name, host, port, int(now - float(alert.get("since") or now))))
                left = [p for p in alert.get("posts") or [] if not _delete(p)]
                if left:
                    alert["posts"] = left
                else:
                    del alerts[name]
                    changed[name] = "up"
                dirty = True
        # A target removed from the list while its alert stood: clean it up.
        for name in [n for n in alerts if n not in results]:
            left = [p for p in alerts[name].get("posts") or [] if not _delete(p)]
            if left:
                alerts[name]["posts"] = left
            else:
                del alerts[name]
            dirty = True
        if dirty:
            _save(ldb, alerts)
        return changed
    finally:
        ldb.close()


def watch():
    poll = max(15.0, float(os.environ.get("POL_BRIDGE_WATCH_POLL", "60")))
    _log("svcwatch: watching %s" % ", ".join("%s %d" % (n, p) for n, _, p in targets()))
    while True:
        try:
            tick()
        except Exception as e:                   # noqa: BLE001 -- never die
            _log("svcwatch error (%r) -- still running" % e)
        time.sleep(poll)
