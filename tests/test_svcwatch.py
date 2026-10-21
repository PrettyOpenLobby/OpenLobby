#!/usr/bin/env python3
"""The service watchdog posts once per outage and deletes the post on recovery.

    python tests/test_svcwatch.py

Discord, the meta store and the port checks are stand-ins; the last section
runs the real probe against a socket this test opens and one it has closed.
"""
import os
import socket
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))

import discordlink  # noqa: E402
import svcwatch  # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


class FakeDB:
    def close(self):
        pass


META = {}
CHANNELS = [{"guild_id": "1", "channel_id": "100", "role_id": "7"}]
CALLS = []
NEXT_ID = [500]
DELETE_STATUS = [204]

discordlink.connect = lambda path=None: FakeDB()
discordlink.get_meta = lambda conn, k, default=None: META.get(k, default)
discordlink.set_meta = lambda conn, k, v: META.__setitem__(k, str(v))
discordlink.gm_channel_rows = lambda conn: CHANNELS


def fake_api(method, path, payload=None):
    CALLS.append((method, path, payload))
    if method == "POST":
        NEXT_ID[0] += 1
        return 200, {"id": str(NEXT_ID[0])}
    return DELETE_STATUS[0], {}


svcwatch.wire(fake_api, logger=lambda s: None)
os.environ["POL_BRIDGE_WATCH"] = "Lobby=127.0.0.1:51220;Mail=127.0.0.1:51260"

DOWN = {}


def probe(host, port):
    return DOWN.get(port)


def tick(t):
    del CALLS[:]
    return svcwatch.tick(now=t, probe_fn=probe)


print("targets")
chk("parsed", svcwatch.parse_targets("A b=h:1; bad ;C=h:x;D=h:2"),
    [("A b", "h", 1), ("D", "h", 2)])
chk("default list is used when unset", len(svcwatch.parse_targets(svcwatch.DEFAULT_TARGETS)) > 0, True)

print("one failed check does not post")
DOWN[51220] = "connection refused"
chk("tick 1", tick(1000), {})
chk("no Discord call", CALLS, [])

print("the second failure posts once, pinging the GM role")
chk("tick 2", tick(1060), {"Lobby": "down"})
chk("one POST", [(m, p) for m, p, _ in CALLS], [("POST", "/channels/100/messages")])
body = CALLS[0][2]
chk("role pinged", body["content"].strip(), "<@&7>")
chk("only that role may be mentioned", body["allowed_mentions"], {"parse": [], "roles": ["7"]})
chk("title", body["embeds"][0]["title"], "Lobby is down")
chk("since = the first failure", "<t:1000:R>" in body["embeds"][0]["description"], True)

print("still down: no second post")
chk("tick 3", tick(1120), {})
chk("no Discord call", CALLS, [])

print("a restart forgets streaks but not the post")
svcwatch._streak.clear()
svcwatch._first_fail.clear()
chk("tick 4", tick(1180), {})
chk("tick 5", tick(1240), {})
chk("no repost", CALLS, [])

print("one good check does not clear")
DOWN.pop(51220)
chk("tick 6", tick(1300), {})
chk("no Discord call", CALLS, [])

print("the second good check deletes the post")
chk("tick 7", tick(1360), {"Lobby": "up"})
chk("one DELETE", [(m, p) for m, p, _ in CALLS], [("DELETE", "/channels/100/messages/501")])
chk("alert forgotten", META.get(svcwatch.META_KEY), "{}")

print("a delete that fails is retried")
DOWN[51260] = "timed out"
tick(1400)
tick(1460)
DOWN.pop(51260)
DELETE_STATUS[0] = 500
tick(1520)
chk("failed delete keeps the alert", tick(1580), {})
chk("still tracked", "Mail" in META[svcwatch.META_KEY], True)
DELETE_STATUS[0] = 404
chk("404 counts as gone", tick(1640), {"Mail": "up"})
DELETE_STATUS[0] = 204

print("no bound channel: nothing recorded, posted when one is bound")
CHANNELS_SAVED = CHANNELS[:]
del CHANNELS[:]
DOWN[51220] = "connection refused"
tick(2000)
chk("nothing to post to", tick(2060), {})
CHANNELS.extend(CHANNELS_SAVED)
chk("posted once a channel exists", tick(2120), {"Lobby": "down"})
DOWN.pop(51220)
tick(2180)
tick(2240)

print("off disables it")
os.environ["POL_BRIDGE_WATCH"] = "off"
chk("no targets", svcwatch.targets(), [])
os.environ["POL_BRIDGE_WATCH"] = "Lobby=127.0.0.1:51220;Mail=127.0.0.1:51260"

print("the real probe")
srv = socket.socket()
srv.bind(("127.0.0.1", 0))
srv.listen(1)
port = srv.getsockname()[1]
chk("listening port passes", svcwatch.probe("127.0.0.1", port, timeout=2), None)
srv.close()
chk("closed port fails", svcwatch.probe("127.0.0.1", port, timeout=2) is not None, True)

print("dns targets")
chk("dns: target parsed", svcwatch.parse_targets("DNS=dns:127.0.0.1:53"),
    [("DNS", "dns:127.0.0.1", 53)])
chk("default list has a dns target",
    any(h.startswith("dns:") for _, h, _ in svcwatch.parse_targets(svcwatch.DEFAULT_TARGETS)), True)
dbody = svcwatch._payload("DNS", "dns:127.0.0.1", 53, "nothing listening", 1000, "")
chk("dns alert wording", dbody["embeds"][0]["description"].split("\n")[0],
    "DNS on port 53 is not answering (nothing listening).")
tbody = svcwatch._payload("Lobby", "127.0.0.1", 51220, "timed out", 1000, "")
chk("tcp alert wording", tbody["embeds"][0]["description"].split("\n")[0],
    "Port 51220 is not accepting connections (timed out).")

print("the real dns probe")
import struct  # noqa: E402
import threading  # noqa: E402

MODE = ["ok"]
udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
udp.bind(("127.0.0.1", 0))
uport = udp.getsockname()[1]
SEEN = []


def dns_server():
    while True:
        try:
            q, addr = udp.recvfrom(512)
        except OSError:
            return
        SEEN.append(q)
        mode = MODE[0]
        if mode == "silent":
            continue
        qid = q[:2]
        question = q[12:]
        if mode == "ok":
            hdr = qid + struct.pack(">HHHHH", 0x8180, 1, 1, 0, 0)
            ans = b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) + bytes([192, 0, 2, 1])
            udp.sendto(hdr + question + ans, addr)
        elif mode == "wrongid":
            bad_id = struct.pack(">H", (struct.unpack(">H", qid)[0] + 1) & 0xFFFF)
            udp.sendto(bad_id + struct.pack(">HHHHH", 0x8180, 1, 1, 0, 0) + question, addr)
            hdr = qid + struct.pack(">HHHHH", 0x8180, 1, 1, 0, 0)
            ans = b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) + bytes([192, 0, 2, 1])
            udp.sendto(hdr + question + ans, addr)
        elif mode == "refused":
            udp.sendto(qid + struct.pack(">HHHHH", 0x8185, 1, 0, 0, 0) + question, addr)
        elif mode == "empty":
            udp.sendto(qid + struct.pack(">HHHHH", 0x8180, 1, 0, 0, 0) + question, addr)


threading.Thread(target=dns_server, daemon=True).start()
chk("answer passes", svcwatch.probe("dns:127.0.0.1", uport, timeout=2), None)
chk("query asks for the watch name", b"\x05pt008\x03pol\x03com\x00" in SEEN[-1], True)
MODE[0] = "wrongid"
chk("a reply to someone else's query is skipped", svcwatch.probe("dns:127.0.0.1", uport, timeout=2), None)
MODE[0] = "refused"
chk("REFUSED fails", svcwatch.probe("dns:127.0.0.1", uport, timeout=2), "answered REFUSED")
MODE[0] = "empty"
chk("no address fails", svcwatch.probe("dns:127.0.0.1", uport, timeout=2),
    "answered with no address for pt008.pol.com")
MODE[0] = "silent"
chk("silence fails", svcwatch.probe("dns:127.0.0.1", uport, timeout=1), "no DNS answer, timed out")
udp.close()
dead = svcwatch.probe("dns:127.0.0.1", uport, timeout=1)
chk("closed port fails", dead is not None, True)

print("FAIL: %d" % bad if bad else "all ok")
sys.exit(1 if bad else 0)
