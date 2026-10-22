#!/usr/bin/env python3
"""A game's server logs in the admin panel (admin.py _logs, adminops.logs_*), against the files
the host's ops runner (not part of this repository) reads and writes.

    python tools/admin_logs_test.py

Checks that the owner can ask for any group's logs and the request lands where pol-ops looks,
that a moderator needs the game's serverlogs permission and sees players' addresses masked
while the owner sees them, that a moderator never reaches the platform's own groups, that a
group the runner did not list or a stale runner is refused, and that asking is in the activity
log. Throwaway PostgreSQL (tools/pgtest.py); the panel runs as a subprocess on a loopback port.
"""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.abspath(os.path.join(HERE, os.pardir, "services"))
sys.path.insert(0, HERE)
sys.path.insert(0, SERVICES)

import pgtest  # noqa: E402

pgtest.use_fresh_database()

# the test process must know Kart Club too, or its permissions are not ones a moderator can hold
TMP = tempfile.mkdtemp(prefix="adminlogs-")
MODS = os.path.join(TMP, "mods")
os.makedirs(MODS)
with open(os.path.join(MODS, "kart.py"), "w") as f:
    f.write("from adminmodules import Module, Service\n"
            "MODULE = Module('kart', 'Kart Club', services=[Service(['kart-game-1'], {7000: 'game'})])\n")
os.environ["POL_ADMIN_MODULES_DIR"] = MODS
os.environ["POL_ADMIN_MODULES"] = "kart"

import accounts  # noqa: E402
import adminusers  # noqa: E402
from admin_http import Client, free_port, start_panel, stop  # noqa: E402

OWNER, OWNER_PW = "op", "correct horse 1"
bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got, "" if ok else "  (want %r)" % (want,)))


def status(tmp, at):
    os.makedirs(os.path.join(tmp, "ops"), exist_ok=True)
    with open(os.path.join(tmp, "ops", "status.json"), "w") as f:
        json.dump({"at": at, "checks": [], "actions": {}, "logs": ["login", "kart"]}, f)


def main():
    tmp, mods = TMP, MODS
    status(tmp, time.time())
    os.makedirs(os.path.join(tmp, "ops", "logs"))
    with open(os.path.join(tmp, "ops", "logs", "kart.json"), "w") as f:
        json.dump({"group": "kart", "name": "Kart Club", "at": time.time(), "by": "op", "containers": [
            {"name": "kart-game-1", "rc": 0, "lines": [
                "2026-10-21T10:00:01Z Arden joined from 198.51.100.7:50123",
                "2026-10-21T10:00:02Z peer [2001:db8::17]:9100 lap 12:30:45"]}]}, f)
    db = accounts.connect()
    accounts.set_admin_cred(db, OWNER, OWNER_PW)
    db.commit()
    adminusers.create_mod(db, "gm_logs", "pass for gm_logs", ["game.kart", "game.kart.serverlogs"], by=OWNER)
    adminusers.create_mod(db, "gm_kart", "pass for gm_kart", ["game.kart"], by=OWNER)
    db.close()
    env = {"POL_ADMIN_MODULES_DIR": mods, "POL_ADMIN_MODULES": "kart"}
    proc, url = start_panel(tmp, SERVICES, free_port(), env_extra=env)
    try:
        owner, gm_logs, gm_kart = Client(url), Client(url), Client(url)
        owner.login(OWNER, OWNER_PW)
        gm_logs.login("gm_logs", "pass for gm_logs")
        gm_kart.login("gm_kart", "pass for gm_kart")

        chk("a game with services has a serverlogs permission to give",
            "game.kart.serverlogs" in (owner.call("api/session")[1].get("perm_labels") or {}), True)
        code, d = owner.call("api/logs?group=kart")
        lines = d["logs"]["containers"][0]["lines"] if code == 200 else []
        chk("the owner reads a game's logs, addresses and all",
            (code, d.get("available"), "198.51.100.7" in lines[0], "2001:db8::17" in lines[1]), (200, True, True, True))
        code, d = gm_logs.call("api/logs?group=kart")
        lines = d["logs"]["containers"][0]["lines"] if code == 200 else []
        chk("a moderator with serverlogs reads them with the addresses masked",
            (code, lines), (200, ["2026-10-21T10:00:01Z Arden joined from <address>:50123",
                                  "2026-10-21T10:00:02Z peer [<address>]:9100 lap 12:30:45"]))
        chk("a moderator without it cannot", (gm_kart.call("api/logs?group=kart")[0],
                                              gm_kart.call("api/logs", {"group": "kart"})[0]), (403, 403))
        chk("nor reach the platform's own logs", (gm_logs.call("api/logs?group=login")[0],
                                                  gm_logs.call("api/logs", {"group": "login"})[0]), (403, 403))

        code, out = gm_logs.call("api/logs", {"group": "kart"})
        reqdir = os.path.join(tmp, "ops", "logreq")
        reqs = [json.load(open(os.path.join(reqdir, n))) for n in sorted(os.listdir(reqdir))]
        chk("asking leaves a request where pol-ops looks, with who asked",
            (code, [(r["group"], r["by"]) for r in reqs]), (200, [("kart", "gm_logs")]))
        chk("the owner can ask for the platform's logs", owner.call("api/logs", {"group": "login"})[0], 200)
        chk("a group the runner did not list is refused", owner.call("api/logs", {"group": "dns"})[0], 409)
        chk("...and so is one that is not a name", owner.call("api/logs", {"group": "../../etc"})[0], 409)
        chk("...and nothing is read for it", owner.call("api/logs?group=../../etc")[1].get("logs"), None)
        rows = owner.call("api/audit")[1].get("rows") or []
        chk("asking is in the activity log", sum("read server logs" in json.dumps(r) for r in rows) >= 2, True)
        status(tmp, time.time() - 600)
        code, out = owner.call("api/logs", {"group": "kart"})
        chk("a runner that stopped reporting is not asked", (code, "not running" in out.get("error", "")), (409, True))
        chk("...and the page says the logs are not available", owner.call("api/logs?group=kart")[1].get("available"), False)
    finally:
        stop(proc)
    print("all checks passed" if not bad else "%d check(s) FAILED" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
