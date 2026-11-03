#!/usr/bin/env python3
"""The deployer's log export (services/deployer/logexport.py), offline.

    python tools/logexport_test.py

A stand-in for stack.py answers compose ps and logs with lines that carry what must not leave
the server (a .env password, the deployer's token, a session token, a login digest, an access
code) and addresses of every kind. Checks the areas the catalog gives, what each file holds,
and what is blanked or shortened.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "services", "deployer"))

import deployer  # noqa: E402
import logexport  # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got, "" if ok else "  (want %r)" % (want,)))


PW, TOKEN = "hunter2-db-pass", "deployer-token-0123456789"
LOGS = {
    "patch": ["2026-10-25T07:00:01Z [203.0.113.45] cmd7 PS2/0001 ver='20160203_0' -> registered, latest=20160203_0",
              "2026-10-25T07:00:02Z [192.168.1.20] cmd7 P2U/1000 ver='20130107_0' -> current"],  # polcheck: allow (a LAN address the export must keep)
    "login": ["2026-10-25T07:01:00Z login from 198.51.100.7 session=abcdef0123456789 NICK Kart:0123456789abcdef0123",
              "2026-10-25T07:01:01Z redeem olx2.ZXhhbXBsZS1jb2RlLXRoYXQtaXMtbG9uZw ok"],
    "world": ["!!! Failed to connect to database, terminating server !!!",
              "Access denied for user 'xiadmin'@'172.20.0.16' (using password: YES) pw=%s" % PW,  # polcheck: allow (a LAN address the export must keep)
              "Authorization: Bearer %s" % TOKEN],
}
CALLS = []


def run(args):
    CALLS.append(args)
    if args[0] == "config":
        return "\n".join(["patch", "login", "authsess", "admin", "postgres", "valkey", "db", "world", "map"]) + "\n"
    if args[0] == "ps":
        return "SERVICE  STATE  STATUS  IMAGE\n" + "\n".join("%s  running  Up 2 hours  img" % s for s in args[4:])
    if args[0] == "logs":
        return "\n".join(LOGS.get(args[-1], [])) + "\n"
    return ""


catalog = deployer.load_catalog(os.path.join(HERE, os.pardir))
print("areas")
areas = logexport.areas(catalog, ["ffxi"])
chk("the core's areas, then each enabled game's", [a["id"] for a in areas], ["patch", "signin", "panel", "data", "ffxi"])
chk("  the patch service area is the patch service", areas[0]["services"], ["patch"])
chk("  a game that is not on has none", "fe" in [a["id"] for a in areas], False)

print("the patch service's file")
secrets = logexport.env_secrets({"LSB_DB_PASSWORD": PW, "LSB_DB_NAME": "xidb", "POL_DEPLOYER_TOKEN": TOKEN,
                                 "FFXI_REQUIRE_SIGNED_IN": "true", "SHORT_KEY": "ab"})
chk("secrets: the values of secret-named settings only, long enough to matter",
    sorted(secrets), sorted([PW, TOKEN]))
text = logexport.export(["patch"], areas, run, 500, False, secrets, {"Deployer": "3.0"})
chk("  the header says what this is", text.splitlines()[1:3], ["Deployer: 3.0", "Areas: Patch service"])
chk("  only the patch service's logs, its last 500 lines", [c for c in CALLS if c[0] == "logs"],
    [["logs", "--no-color", "--timestamps", "--tail", "500", "patch"]])
chk("  its state is listed", "patch  running  Up 2 hours" in text, True)
chk("  a player's address is shortened", ("203.0.113.45" in text, "[203.0.x.x] cmd7 PS2/0001" in text), (False, True))
chk("  a private address is kept (it says how the server is wired)", "192.168.1.20" in text, True)  # polcheck: allow (a LAN address the export must keep)
chk("  the version check itself is there", "ver='20160203_0' -> registered" in text, True)

print("everything, and what is blanked")
del CALLS[:]
text = logexport.export(["all"], areas, run, 99999, False, secrets)
chk("lines are capped", {c[4] for c in CALLS if c[0] == "logs"}, {str(logexport.MAX_LINES)})
chk("a service that is not set up here is listed, not asked", ("Not set up here: authrelay" in text,
                                                                any("authrelay" in c for c in CALLS)), (True, False))
for what, s in (("the .env database password", PW), ("the deployer's token", TOKEN),
                ("the session token", "abcdef0123456789"), ("the login digest", "0123456789abcdef0123"),
                ("the access code", "olx2.ZXhhbXBsZS1jb2RlLXRoYXQtaXMtbG9uZw")):
    chk("  %s is blanked" % what, s in text, False)
chk("  the FFXI database error is still readable", "Access denied for user 'xiadmin'@'172.20.0.16'" in text, True)  # polcheck: allow (a LAN address the export must keep)
text = logexport.export(["signin"], areas, run, 100, True, secrets)
chk("addresses=1 keeps them whole", "198.51.100.7" in text, True)

print("logexport_test: %d failed" % bad)
sys.exit(1 if bad else 0)
