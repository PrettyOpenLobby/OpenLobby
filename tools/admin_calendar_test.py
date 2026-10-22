#!/usr/bin/env python3
"""The admin panel's Calendar (services/admincalendar.py) end to end.

    python tools/admin_calendar_test.py

Tetra Master's and JongHoLow's entries come from the event calendar they read (eventcal.py); a
made-up game, Kart Club, serves its own through tools/olport.py's @port.calendar. Checks that the
owner sees all of it with a cup's sessions folded into one entry, that a moderator sees only the
games they have a permission for, that the panel's own entries are added, changed and deleted by
calendar_edit only and land in the activity log, that bad entries and ranges are refused, and
that a game whose calendar does not answer is named while the rest still shows. Throwaway
PostgreSQL (tools/pgtest.py); the panel runs as a subprocess on a loopback port.
"""
import calendar
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
import olport  # noqa: E402
from admin_http import Client, free_port, start_panel, stop  # noqa: E402

OWNER, OWNER_PW = "op", "correct horse 1"
TOKEN = "kart-token-0123456789"
DAY = 86400
bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got, "" if ok else "  (want %r)" % (want,)))


# A calendar of our own, so the test does not depend on the shipped one's dates.
CAL = {"tm": {"weekly": {"key": "cup", "name": "Test Cup", "dow": 4, "hour": 12, "hours": 72,
                         "sessions": {"hours": [19, 2], "minutes": 60, "from": "2026-01-01"}},
              "events": [{"key": "fest", "name": "Harvest Cup", "date": "2026-11-20", "days": 1}]},
       "jan": {"weekly": {"key": "jcup", "name": "Weekend Cup", "dow": 5, "hour": 0, "hours": 48}}}
NOV = calendar.timegm((2026, 11, 1, 0, 0, 0))
GP = calendar.timegm((2026, 11, 14, 18, 0, 0))

port = olport.AdminPort("kart", "Kart Club", token=TOKEN)


@port.calendar
def kart_calendar(lo, hi):
    return [{"id": "gp", "title": "Grand Prix", "start": GP, "end": GP + 7200, "kind": "cup",
             "about": "Pumpkin karts only."}] if GP < hi and GP + 7200 > lo else []


def main():
    tmp = tempfile.mkdtemp(prefix="admincal-")
    calpath = os.path.join(tmp, "event-calendar.json")
    with open(calpath, "w") as f:
        json.dump(CAL, f)
    _h, kport = port.start()
    db = accounts.connect()
    accounts.set_admin_cred(db, OWNER, OWNER_PW)
    db.commit()
    adminusers.create_mod(db, "gm_tm", "pass for gm_tm", ["calendar", "game.tm"], by=OWNER)
    adminusers.create_mod(db, "gm_ed", "pass for gm_ed", ["calendar", "calendar_edit", "game.kart"], by=OWNER)
    adminusers.create_mod(db, "gm_none", "pass for gm_none", ["codes"], by=OWNER)
    db.close()
    env = {"POL_EVENT_CALENDAR": calpath, "POL_ADMIN_MODULES": "tm,jan,kart",
           "POL_ADMIN_GAME_KART_URL": "http://127.0.0.1:%d" % kport, "POL_ADMIN_GAME_KART_TOKEN": TOKEN}
    proc, url = start_panel(tmp, SERVICES, free_port(), env_extra=env)
    rng = "api/calendar?from=%d&to=%d" % (NOV, NOV + 30 * DAY)
    try:
        owner, gm_tm, gm_ed, gm_none = Client(url), Client(url), Client(url), Client(url)
        owner.login(OWNER, OWNER_PW)
        gm_tm.login("gm_tm", "pass for gm_tm")
        gm_ed.login("gm_ed", "pass for gm_ed")
        gm_none.login("gm_none", "pass for gm_none")

        print("what the games have scheduled")
        code, d = owner.call(rng)
        ents = d.get("entries", [])
        by_game = {}
        for e in ents:
            by_game.setdefault(e["game"], []).append(e)
        chk("the owner sees every game's calendar", (code, sorted(by_game)), (200, ["jan", "kart", "tm"]))
        cups = [e for e in by_game.get("tm", []) if e["title"] == "Test Cup"]
        chk("a TM cup season is one entry, its sessions inside it",
            (len(cups) >= 4, [len(c["times"]) for c in cups] == [6] * len(cups), cups and cups[0]["kind"]), (True, True, "cup"))
        chk("...a holiday cup replaces that week's cup",
            [e["kind"] for e in by_game["tm"] if e["title"] == "Harvest Cup"], ["holiday cup"])
        gp = (by_game.get("kart") or [{}])[0]
        chk("a protocol game's own calendar, through olport",
            (gp.get("id"), gp.get("title"), gp.get("start"), gp.get("about"), gp.get("editable")),
            ("kart:gp", "Grand Prix", GP, "Pumpkin karts only.", False))
        chk("no problems", d.get("problems"), [])

        print("who sees what")
        chk("a moderator sees only the games they have a permission for",
            sorted({e["game"] for e in gm_tm.call(rng)[1].get("entries", [])}), ["tm"])
        chk("without the calendar permission there is no calendar", gm_none.call(rng)[0], 403)
        chk("a range that is too long is refused",
            owner.call("api/calendar?from=%d&to=%d" % (NOV, NOV + 200 * DAY))[0], 400)

        print("the panel's own entries")
        new = {"action": "add", "title": "Ballista: Ronfaure", "game": "kart", "kind": "match",
               "start": NOV + 10 * DAY, "end": NOV + 10 * DAY + 3600, "about": "Official match."}
        chk("seeing the calendar is not editing it", gm_tm.call("api/calendar", new)[0], 403)
        code, out = gm_ed.call("api/calendar", new)
        chk("calendar_edit adds an entry", (code, out.get("ok"), out.get("id", "").startswith("panel:")), (200, True, True))
        eid = out.get("id")
        mine = [e for e in owner.call(rng)[1]["entries"] if e["id"] == eid]
        chk("...it shows, editable, with who added it",
            [(e["title"], e["kind"], e["editable"], e["by"]) for e in mine], [("Ballista: Ronfaure", "match", True, "gm_ed")])
        chk("...and its game's moderator does not need the game's permission to see it",
            any(e["id"] == eid for e in gm_tm.call(rng)[1]["entries"]), True)
        code, out = gm_ed.call("api/calendar", dict(new, action="edit", id=eid, title="Ballista: Jugner"))
        chk("changing it", (code, [e["title"] for e in owner.call(rng)[1]["entries"] if e["id"] == eid]),
            (200, ["Ballista: Jugner"]))
        for what, extra in (("no title", {"title": "  "}), ("ending before it starts", {"end": NOV}),
                            ("a game that is not here", {"game": "nope"}), ("an unknown kind", {"kind": "party"}),
                            ("no start", {"start": "soon"})):
            chk("refused: %s" % what, gm_ed.call("api/calendar", dict(new, **extra))[0], 400)
        chk("refused: an entry id that is not one",
            gm_ed.call("api/calendar", {"action": "delete", "id": "panel:../x"})[0], 400)
        rows = owner.call("api/audit")[1].get("rows") or []
        chk("the changes are in the activity log",
            sum("calendar:" in json.dumps(r) for r in rows) >= 2, True)
        chk("deleting it", gm_ed.call("api/calendar", {"action": "delete", "id": eid})[0], 200)
        chk("...it is gone", any(e["id"] == eid for e in owner.call(rng)[1]["entries"]), False)

        print("a game that does not answer")
        port.stop()
        code, d = owner.call(rng)
        chk("the rest of the calendar still shows",
            (code, sorted({e["game"] for e in d["entries"]})), (200, ["jan", "tm"]))
        chk("...and the panel says which game is missing",
            any("Kart Club" in p for p in d.get("problems", [])), True)
    finally:
        stop(proc)
        port.stop()
    print("all checks passed" if not bad else "%d check(s) FAILED" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
