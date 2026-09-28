#!/usr/bin/env python3
"""Report status on the admin panel's Issues and Reports tabs, over HTTP.

    python tools/admin_triage_test.py

Seeds three tester issue bundles and two user reports, then checks that:
  * every report starts open; the owner marks one resolved and the status
    and note stick, with who set it;
  * a moderator with the Reports permission may set a status and one without
    it may not; an unknown id, a path in the id and a made-up status are
    refused;
  * user reports carry their status too, and the activity log records both
    changes.

The browser side (the Open/Closed/All filters, the title picker and search,
the detail view's buttons) is not driven here.

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


def seed(tmp):
    issues = os.path.join(tmp, "issues")
    for rid, when, handle, title, desc in (
            ("20260927T100000Z-DECK-a1", "2026-09-27T10:00:00Z", "Amara",
             "Front Mission Online", "black screen after the hangar"),
            ("20260927T110000Z-PC-b2", "2026-09-27T11:00:00Z", "Birdie",
             "Fantasy Earth", "pointer stuck in the top band"),
            ("20260927T120000Z-PC-c3", "2026-09-27T12:00:00Z", "Otter",
             "Front Mission Online", "voice chat error")):
        for sub, name in (("client", "polshim.log"), ("client", "diag.txt"),
                          ("server", "correlated.log")):
            os.makedirs(os.path.join(issues, rid, sub), exist_ok=True)
            with open(os.path.join(issues, rid, sub, name), "w") as f:
                f.write("line\n")
        with open(os.path.join(issues, rid, "manifest.json"), "w") as f:
            json.dump({"id": rid, "received_at": when, "handle": handle, "host": "BOX",
                       "title": title, "description": desc,
                       "client_files": [{"name": "polshim.log", "bytes": 1048566},
                                        {"name": "diag.txt", "bytes": 4387}],
                       "server_files": ["correlated.log"], "window": {}}, f)
    reports = os.path.join(tmp, "reports")
    os.makedirs(reports, exist_ok=True)
    for rid, suspect, app, expl in (
            ("20260927T090000-1", "Griefer", "FINAL FANTASY XI", "spam in say"),
            ("20260927T091500-2", "Loud", "Tetra Master", "rude in chat")):
        with open(os.path.join(reports, rid + ".json"), "w") as f:
            json.dump({"received_at": "2026-09-27T09:00:00Z", "from": "someone@pol",
                       "subject": "report", "fields": {"suspect": suspect,
                                                       "application": app,
                                                       "explanation": expl}}, f)
    return issues, reports


def main():
    tmp = tempfile.mkdtemp(prefix="admintriage-")
    issues, reports = seed(tmp)
    db = accounts.connect()
    accounts.set_admin_cred(db, OWNER, OWNER_PW)
    db.commit()
    adminusers.create_mod(db, "helper", "helper pass 1", ["reports"], by=OWNER)
    adminusers.create_mod(db, "gmonly", "gmonly pass 1", ["gm"], by=OWNER)
    db.close()
    proc, url = start_panel(tmp, SERVICES, free_port(),
                            {"POL_ISSUE_DIR": issues, "POL_REPORT_DIR": reports})
    try:
        owner = Client(url)
        chk("the owner signs in", owner.login(OWNER, OWNER_PW), 200)
        code, rows = owner.call("api/issues")
        chk("issue reports list, each open to begin with",
            sorted(str(r.get("status")) for r in rows), ["open"] * 3)
        code, _res = owner.call("api/triage", {
            "kind": "issues", "id": "20260927T110000Z-PC-b2",
            "status": "resolved", "note": "fixed in the next build"})
        chk("the owner marks an issue resolved", code, 200)
        got = {r["id"]: r for r in owner.call("api/issues")[1]}["20260927T110000Z-PC-b2"]
        chk("the status and note stick",
            (got["status"], got["status_note"], got["status_by"]),
            ("resolved", "fixed in the next build", OWNER))

        helper, gm = Client(url), Client(url)
        chk("a Reports moderator signs in", helper.login("helper", "helper pass 1"), 200)
        chk("a GM-only moderator signs in", gm.login("gmonly", "gmonly pass 1"), 200)
        chk("the Reports moderator closes a user report",
            helper.call("api/triage", {"kind": "reports", "id": "20260927T090000-1",
                                       "status": "wontfix", "note": ""})[0], 200)
        chk("the GM-only moderator may not",
            gm.call("api/triage", {"kind": "reports", "id": "20260927T091500-2",
                                   "status": "resolved"})[0], 403)
        chk("an unknown id is refused",
            owner.call("api/triage", {"kind": "issues", "id": "nope",
                                      "status": "resolved"})[0], 404)
        chk("a path in the id is refused",
            owner.call("api/triage", {"kind": "reports", "id": "../x",
                                      "status": "resolved"})[0], 404)
        chk("an unknown status is refused",
            owner.call("api/triage", {"kind": "issues", "id": "20260927T100000Z-DECK-a1",
                                      "status": "maybe"})[0], 400)
        rep = {r["id"]: r for r in owner.call("api/reports")[1]}
        chk("user reports carry their status",
            (rep["20260927T090000-1"]["status"], rep["20260927T091500-2"]["status"]),
            ("wontfix", "open"))
        rows = owner.call("api/audit")[1].get("rows", [])
        acts = [(r["actor"], r["action"], r["ok"]) for r in rows]
        chk("the activity log records both changes",
            ((OWNER, "set a report's status", 1) in acts,
             ("helper", "set a report's status", 1) in acts), (True, True))
        owner.call("api/triage", {"kind": "issues", "id": "20260927T110000Z-PC-b2",
                                  "status": "open"})
        got = {r["id"]: r for r in owner.call("api/issues")[1]}["20260927T110000Z-PC-b2"]
        chk("reopening puts it back", got["status"], "open")
    finally:
        stop(proc)
    print("all checks passed" if not bad else "%d check(s) FAILED" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
