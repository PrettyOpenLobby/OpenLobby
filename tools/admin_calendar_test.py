#!/usr/bin/env python3
"""The admin panel's Calendar (services/admincalendar.py) end to end.

    python tools/admin_calendar_test.py

Tetra Master's and JongHoLow's entries come from the event calendar they read (eventcal.py); a
made-up game, Kart Club, serves its own through tools/olport.py's @port.calendar; Front Mission
Online and Fantasy Earth read calendar.json off their tool ports (sources.tool_calendar), here a
stand-in port that checks the token and serves a few made-up war phases and arenas. Checks that the
owner sees all of it with a cup's sessions folded into one entry, that a moderator sees only the
games they have a permission for, that the panel's own entries are added, changed and deleted by
calendar_edit only and land in the activity log, that bad entries and ranges are refused, and
that a game whose calendar does not answer is named while the rest still shows. Throwaway
PostgreSQL (tools/pgtest.py); the panel runs as a subprocess on a loopback port.
"""
import calendar
import http.server
import json
import os
import sys
import tempfile
import threading
import urllib.parse

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


# What the two war games' tool ports serve: an FMO war phase with its ceasefire, a Clash of Iron
# season with its nights, an entry outside the asked range (the module drops it), and an FE war.
PH3 = calendar.timegm((2026, 11, 5, 0, 0, 0))
JUDGE3 = calendar.timegm((2027, 1, 1, 12, 0, 0))
COI = [[calendar.timegm((2026, 11, d, 12, 0, 0)), calendar.timegm((2026, 11, d, 14, 0, 0))]
       for d in (12, 13, 14)]
WAR = calendar.timegm((2026, 11, 8, 20, 0, 0))
TOOL_EVENTS = {
    "fmo": [{"id": "ceasefire-2", "title": "Ceasefire after phase 2", "start": NOV - 4 * DAY + 43200,
             "end": PH3, "kind": "ceasefire", "about": "Battles count for nothing."},
            {"id": "phase-3", "title": "War phase 3", "start": PH3, "end": JUDGE3, "kind": "war phase"},
            {"id": "coi-S01", "title": "Clash of Iron S01: Two-Guns Shootout", "start": COI[0][0],
             "end": COI[-1][1], "kind": "tournament", "times": COI},
            {"id": "old", "title": "Long gone", "start": NOV - 90 * DAY, "end": NOV - 80 * DAY}],
    "fe": [{"id": "war-17-%d" % WAR, "title": "War: Wren Hills", "start": WAR, "end": WAR + 3600,
            "kind": "war", "about": "Elsord attacks Holdein's Wren Hills."}],
}
ASKED = []


class ToolPort(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        game = self.server.game
        u = urllib.parse.urlparse(self.path)
        if self.headers.get("X-Devtool-Token") != TOKEN + game:
            return self.reply(403, {"error": "forbidden"})
        if u.path != "/calendar.json":
            return self.reply(404, {"error": "no such thing"})
        q = urllib.parse.parse_qs(u.query)
        ASKED.append((game, int(q["from"][0]), int(q["to"][0])))
        return self.reply(200, {"ok": True, "events": TOOL_EVENTS[game]})

    def reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def tool_port(game):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ToolPort)
    srv.game = game
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


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
    tools = {g: tool_port(g) for g in TOOL_EVENTS}
    env = {"POL_EVENT_CALENDAR": calpath, "POL_ADMIN_MODULES": "tm,jan,kart,fmo,fe",
           "POL_ADMIN_GAME_KART_URL": "http://127.0.0.1:%d" % kport, "POL_ADMIN_GAME_KART_TOKEN": TOKEN}
    for g, srv in tools.items():
        env["POL_ADMIN_GAME_%s_URL" % g.upper()] = "http://127.0.0.1:%d" % srv.server_address[1]
        env["POL_ADMIN_GAME_%s_TOKEN" % g.upper()] = TOKEN + g
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
        chk("the owner sees every game's calendar", (code, sorted(by_game)),
            (200, ["fe", "fmo", "jan", "kart", "tm"]))
        cups = [e for e in by_game.get("tm", []) if e["title"] == "Test Cup"]
        chk("a TM cup season is one entry, its sessions inside it",
            (len(cups) >= 4, [len(c["times"]) for c in cups] == [6] * len(cups), cups and cups[0]["kind"]), (True, True, "cup"))
        chk("...a holiday cup replaces that week's cup",
            [e["kind"] for e in by_game["tm"] if e["title"] == "Harvest Cup"], ["holiday cup"])
        gp = (by_game.get("kart") or [{}])[0]
        chk("a protocol game's own calendar, through olport",
            (gp.get("id"), gp.get("title"), gp.get("start"), gp.get("about"), gp.get("editable")),
            ("kart:gp", "Grand Prix", GP, "Pumpkin karts only.", False))
        fmo = {e["id"]: e for e in by_game.get("fmo", [])}
        chk("FMO's war phases off its tool port, with their times and kinds",
            [(i, fmo.get(i, {}).get("start"), fmo.get(i, {}).get("end"), fmo.get(i, {}).get("kind"))
             for i in ("fmo:ceasefire-2", "fmo:phase-3")],
            [("fmo:ceasefire-2", NOV - 4 * DAY + 43200, PH3, "ceasefire"),
             ("fmo:phase-3", PH3, JUDGE3, "war phase")])
        chk("...a Clash of Iron season with its nights inside it",
            (fmo.get("fmo:coi-S01", {}).get("times"), fmo.get("fmo:coi-S01", {}).get("kind")),
            (COI, "tournament"))
        chk("...and nothing outside the range", "fmo:old" in fmo, False)
        fe = by_game.get("fe") or [{}]
        chk("FE's war off its tool port",
            [(e.get("id"), e.get("title"), e.get("start"), e.get("end"), e.get("kind"), e.get("about"))
             for e in fe],
            [("fe:war-17-%d" % WAR, "War: Wren Hills", WAR, WAR + 3600, "war",
              "Elsord attacks Holdein's Wren Hills.")])
        chk("the tool ports were asked for the calendar's own range, with their tokens",
            sorted({a for a in ASKED}), [("fe", NOV, NOV + 30 * DAY), ("fmo", NOV, NOV + 30 * DAY)])
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
            (code, sorted({e["game"] for e in d["entries"]})), (200, ["fe", "fmo", "jan", "tm"]))
        chk("...and the panel says which game is missing",
            any("Kart Club" in p for p in d.get("problems", [])), True)
        tools["fe"].shutdown()
        tools["fe"].server_close()
        code, d = owner.call(rng)
        chk("a war game whose tool port is down is named too",
            (code, sorted({e["game"] for e in d["entries"]}),
             any("Fantasy Earth" in p for p in d.get("problems", []))), (200, ["fmo", "jan", "tm"], True))
    finally:
        stop(proc)
        port.stop()
        for srv in tools.values():
            srv.shutdown()
    print("all checks passed" if not bad else "%d check(s) FAILED" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
