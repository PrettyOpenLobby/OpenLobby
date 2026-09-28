#!/usr/bin/env python3
"""Moderator permissions for every tab of the admin panel, over HTTP.

    python tools/admin_perms_test.py

For each permission the owner can tick -- account management and deletion,
voiding codes, writing and publishing news, PML Preview, the activity log,
service status, alert/check settings -- a moderator holding ONLY that
permission may use its endpoints and is refused the others. Moderator
management and the owner sign-in stay the owner's whatever is ticked.

The browser side (which tabs each moderator sees) is not driven here.

Uses a throwaway PostgreSQL database (tools/pgtest.py) and a temporary data
directory; the panel runs as a subprocess on a loopback port.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.abspath(os.path.join(HERE, os.pardir, "services"))
sys.path.insert(0, HERE)
sys.path.insert(0, SERVICES)

import pgtest  # noqa: E402

pgtest.use_fresh_database()

import accounts  # noqa: E402
import adminusers  # noqa: E402
from admin_http import Client, free_port, start_panel, stop  # noqa: E402

OWNER, OWNER_PW = "op", "correct horse 1"
bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


#: One moderator per permission, and one request only that permission allows.
PROBES = {
    "accounts_manage": ("POST", "api/account-kick", {"polid": "NOPE"}),
    "accounts_delete": ("POST", "api/account-delete", {"polid": "NOPE"}),
    "codes_void": ("POST", "api/codes/void", {"code": "NOPE"}),
    "news_edit": ("GET", "api/news", None),
    "pml": ("GET", "api/pml-list", None),
    "audit": ("GET", "api/audit", None),
    "health": ("GET", "api/health", None),
    "settings": ("GET", "api/alerts", None),
}


def main():
    tmp = tempfile.mkdtemp(prefix="adminperms-")
    db = accounts.connect()
    accounts.set_admin_cred(db, OWNER, OWNER_PW)
    db.commit()
    for perm in list(PROBES) + ["news_publish"]:
        adminusers.create_mod(db, "m_" + perm, "pass for " + perm, [perm], by=OWNER)
    adminusers.create_mod(db, "m_news_both", "pass for both",
                          ["news_edit", "news_publish"], by=OWNER)
    db.close()
    proc, url = start_panel(tmp, SERVICES, free_port())
    try:
        mods = {}
        for perm in list(PROBES) + ["news_publish", "news_both"]:
            c = Client(url)
            code = c.login("m_" + perm,
                           "pass for " + ("both" if perm == "news_both" else perm))
            if code != 200:
                chk("moderator %s signs in" % perm, code, 200)
            mods[perm] = c

        def status(c, probe):
            method, path, body = probe
            return c.call(path, body if method == "POST" else None)[0]

        for perm, probe in PROBES.items():
            chk("%s may use %s" % (perm, probe[1]), status(mods[perm], probe) != 403, True)
            others = sorted(p for p, c in mods.items()
                            if p not in (perm, "news_both") and status(c, probe) != 403)
            chk("...and no other single permission may", others, [])

        chk("a news writer cannot publish",
            mods["news_edit"].call("api/news/publish", {"dry_run": False})[0], 403)
        chk("...but can see what a publish would change",
            mods["news_edit"].call("api/news/publish", {"dry_run": True})[0] != 403, True)
        chk("a writer who may publish gets past the permission check",
            mods["news_both"].call("api/news/publish", {"dry_run": False})[0] != 403, True)
        chk("the PML moderator can load a page's art path",
            mods["pml"].call("art/wh000.pol.com/pml/top/nothing.png")[0] != 403, True)
        chk("the news writer can too (the preview draws with it)",
            mods["news_edit"].call("art/wh000.pol.com/pml/top/nothing.png")[0] != 403, True)
        chk("the activity-log moderator cannot",
            mods["audit"].call("art/wh000.pol.com/pml/top/nothing.png")[0], 403)
        for perm, c in mods.items():
            if c.call("api/mods")[0] != 403 or c.call(
                    "api/mods", {"action": "create", "username": "x" + perm,
                                 "perms": ["pml"]})[0] != 403:
                chk("moderator management stays the owner's (%s)" % perm, False, True)
        chk("no moderator can change the owner sign-in",
            sorted(p for p, c in mods.items()
                   if c.call("api/credentials",
                             {"username": "x", "password": "y" * 12})[0] != 403), [])
        _code, sess = mods["pml"].call("api/session")
        chk("the session lists the new permission's label",
            "PML Preview" in json.dumps(sess.get("perm_labels", {})), True)
    finally:
        stop(proc)
    print("all checks passed" if not bad else "%d check(s) FAILED" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
