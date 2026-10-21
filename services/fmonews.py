"""Front Mission Online's war phases in the Viewer's news.

FMO's war runs in phases of about two months: judged at 12:00 UTC on the 1st, a ceasefire until
the 5th, then the next phase (SE's rules, fmowar.py). The City Control board (hippaulfront's
boardfmo.py) publishes the phase clock, the score and the last judged phase to the live-state store
under KV_KEY every minute, from the same reads as the board, so this module keeps no clock of its
own. Discord already gets the phase news from the board itself; this is the Viewer's side.

One item at a time, on the FMO tab and the ticker:

  - from LEAD_H before a phase is judged: "Phase N ends <day>", with the score as it stands;
  - during the ceasefire: the result (or, until the war server has judged it, that the phase is
    over) and when the next phase begins;
  - for LEAD_H after the next phase begins: "Phase N+1 has begun", with the last result.

Knobs (environment):

    FMO_NEWS=0                 no FMO items
    FMO_NEWS_LEAD_H=72         hours before the judgement the item appears, and after a new
                               phase starts it stays
    FMO_NEWS_MAX_AGE_H=24      a clock older than this is ignored (the board is gone)

    python fmonews.py --show
"""
import calendar as _calendar
import os
import sys
import time

KV_KEY = "fmo:calendar"
CONTENT = "fmo"
HOUR = 3600
#: Viewer serials 960000-969999 are FMO's (ffxinews.py has 97xxxx, eventnews.py 98-99xxxx).
SERIAL_FLOOR = 960000
NATION = {1: "O.C.U.", 2: "U.S.N."}

_MONTHS = ["Jan.", "Feb.", "Mar.", "Apr.", "May", "Jun.",
           "Jul.", "Aug.", "Sep.", "Oct.", "Nov.", "Dec."]
_DAYS = ["Mon.", "Tue.", "Wed.", "Thu.", "Fri.", "Sat.", "Sun."]


def _env_num(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


def enabled():
    return os.environ.get("FMO_NEWS", "1") != "0"


def lead_s():
    return max(0.0, _env_num("FMO_NEWS_LEAD_H", 72)) * HOUR


def load(now=None, kv=None):
    """The clock the board last published, or None."""
    now = time.time() if now is None else now
    try:
        if kv is None:
            from polcore import kv as _kv
            kv = _kv.default()
        c = kv.get_json(KV_KEY)
    except Exception:                                # noqa: BLE001 -- no store, no items
        return None
    if not isinstance(c, dict) or now - float(c.get("at") or 0) > max(HOUR, _env_num("FMO_NEWS_MAX_AGE_H", 24) * HOUR):
        return None
    if not int(c.get("phase") or 0) or not c.get("judge") or not c.get("next"):
        return None
    return c


def _day(t):
    g = time.gmtime(t)
    return "%s %d" % (_MONTHS[g.tm_mon - 1], g.tm_mday)


def _clock(t):
    g = time.gmtime(t)
    return "%s %s %02d:%02d UTC" % (_DAYS[g.tm_wday], _day(t), g.tm_hour, g.tm_min)


def _stamp(t):
    g = time.gmtime(t)
    return "%s %d, %d %02d:%02d [UTC]" % (_MONTHS[g.tm_mon - 1], g.tm_mday, g.tm_year, g.tm_hour, g.tm_min)


def _score(ocu, usn):
    return "O.C.U. %d points, U.S.N. %d" % (ocu, usn)


def _result_lines(pn, r, next_n):
    """The paragraphs about a judged phase."""
    out = []
    if r["winner"] in NATION:
        won, lost = (r["ocu"], r["usn"]) if r["winner"] == 1 else (r["usn"], r["ocu"])
        out.append("%s won Phase %d, %d points to %d in economic-city ranks."
                   % (NATION[r["winner"]], pn, won, lost))
    else:
        out.append("Phase %d ended in a tie at %d points each, so both sides are rewarded." % (pn, r["ocu"]))
    for n, name in sorted((r.get("reward") or {}).items()):
        if name and int(n) in NATION:
            out.append("%s wins the %s series of wanzer parts, on sale from the start of Phase %d."
                       % (NATION[int(n)], name, next_n))
    if r.get("penalty") in NATION:
        out.append("%s's fortress opens Phase %d in Deadlock." % (NATION[r["penalty"]], next_n))
    return out


def item(now, c):
    """The one FMO item for `now`, or None."""
    n, judge, nxt, start = int(c["phase"]), int(c["judge"]), int(c["next"]), int(c.get("start") or 0)
    sc = c.get("score") or {}
    if judge - lead_s() <= now < judge:
        key, posted = "end-%d" % n, judge - lead_s()
        title = "Phase %d ends %s" % (n, _day(judge))
        paras = ["Phase %d of the war is judged on %s. The side holding more economic-city ranks wins."
                 % (n, _clock(judge)),
                 "The score so far: %s." % _score(int(sc.get("ocu") or 0), int(sc.get("usn") or 0)),
                 "A ceasefire follows until Phase %d begins on %s." % (n + 1, _clock(nxt))]
    elif judge <= now < nxt:
        key, posted = "result-%d" % n, judge
        r = c.get("judged")
        if r:
            who = NATION.get(r["winner"])
            title = "Phase %d: %s" % (n, "%s wins" % who if who else "a tie")
            paras = _result_lines(n, r, n + 1)
        else:
            title = "Phase %d is over" % n
            paras = ["Phase %d was judged on %s. The result is posted here once the war server records it."
                     % (n, _clock(judge))]
        paras.append("Ceasefire until Phase %d begins on %s.%s"
                     % (n + 1, _clock(nxt),
                        " The frontline goes back to its opening lines." if c.get("frontline_reset") else ""))
    elif n >= 2 and start and start <= now < start + lead_s():
        key, posted = "begun-%d" % n, start
        title = "Phase %d has begun" % n
        paras = ["Phase %d of the war began on %s and is judged on %s." % (n, _clock(start), _clock(judge))]
        if c.get("last"):
            paras += _result_lines(n - 1, c["last"], n)
    else:
        return None
    return {"serial": SERIAL_FLOOR + (int(posted) // HOUR) % 10000, "date": _stamp(int(posted) // HOUR * HOUR),
            "title": title, "kind": "event", "content": CONTENT, "body": "\n\n".join(paras),
            "status": False, "link": "", "auto": "fmo:" + key}


def items(now=None, clock=None):
    if not enabled():
        return []
    now = time.time() if now is None else now
    clock = load(now) if clock is None else clock
    it = item(now, clock) if clock else None
    return [it] if it else []


def _selftest():
    fails = []

    def check(ok, label):
        print("  [%s] %s" % ("PASS" if ok else "FAIL", label))
        if not ok:
            fails.append(label)

    def utc(*a):
        return _calendar.timegm(a + (0,) * (6 - len(a)))

    for k in [k for k in os.environ if k.startswith("FMO_NEWS")]:
        os.environ.pop(k)
    # What boardfmo.calendar() writes for phase 1 (2026-09-05 .. judged 11-01 12:00, phase 2 from 11-05).
    c = {"at": 0, "phase": 1, "start": utc(2026, 9, 5), "judge": utc(2026, 11, 1, 12), "next": utc(2026, 11, 5),
         "ceasefire": False, "judged": None, "last": None, "score": {"ocu": 12, "usn": 9},
         "present": True, "frontline_reset": True}
    check(items(utc(2026, 10, 15), c) == [], "mid-phase, no FMO item")
    it = items(utc(2026, 10, 30), c)
    check(len(it) == 1 and it[0]["title"] == "Phase 1 ends Nov. 1" and "O.C.U. 12 points, U.S.N. 9." in it[0]["body"]
          and "Thu. Nov. 5 00:00 UTC" in it[0]["body"], "3 days before the judgement  --  %r" % it)
    c2 = dict(c, ceasefire=True)
    it = items(utc(2026, 11, 1, 13), c2)
    check(it and it[0]["title"] == "Phase 1 is over" and "once the war server records it" in it[0]["body"],
          "judged time passed, nothing recorded yet  --  %r" % it)
    c3 = dict(c2, judged={"winner": 1, "ocu": 12, "usn": 9, "judged_at": utc(2026, 11, 1, 12),
                          "reward": {"1": "Zenith"}, "penalty": 2})
    it3 = items(utc(2026, 11, 2), c3)
    check(it3 and it3[0]["title"] == "Phase 1: O.C.U. wins" and "Zenith series" in it3[0]["body"]
          and "U.S.N.'s fortress opens Phase 2 in Deadlock" in it3[0]["body"]
          and "opening lines" in it3[0]["body"], "the result  --  %r" % it3)
    check(it3[0]["serial"] == it[0]["serial"] and it3[0]["auto"] == "fmo:result-1",
          "the result keeps the ceasefire item's serial (same post, now with the result)")
    tie = dict(c3, judged=dict(c3["judged"], winner=0, ocu=10, usn=10, reward={"1": "A", "2": "B"}, penalty=0))
    it = items(utc(2026, 11, 2), tie)
    check(it and it[0]["title"] == "Phase 1: a tie" and "tie at 10 points each" in it[0]["body"], "a tie")
    p2 = {"at": 0, "phase": 2, "start": utc(2026, 11, 5), "judge": utc(2027, 1, 1, 12), "next": utc(2027, 1, 5),
          "ceasefire": False, "judged": None, "last": c3["judged"], "score": {"ocu": 0, "usn": 0},
          "present": True, "frontline_reset": True}
    it = items(utc(2026, 11, 6), p2)
    check(it and it[0]["title"] == "Phase 2 has begun" and "O.C.U. won Phase 1, 12 points to 9" in it[0]["body"]
          and 960000 <= it[0]["serial"] <= 969999 and it[0]["content"] == "fmo", "the new phase  --  %r" % it)
    check(items(utc(2026, 11, 9), p2) == [], "three days on, it is gone")

    class KV:
        def __init__(self, v):
            self.v = v

        def get_json(self, k):
            return self.v if k == KV_KEY else None

    check(load(utc(2026, 10, 30), KV(dict(c, at=utc(2026, 10, 30) - 60)))["phase"] == 1, "load reads the clock")
    check(load(utc(2026, 10, 30), KV(dict(c, at=utc(2026, 10, 28)))) is None, "a stale clock is ignored")
    check(load(utc(2026, 10, 30), KV(dict(c, at=utc(2026, 10, 30), phase=0))) is None, "no war yet, no items")
    os.environ["FMO_NEWS"] = "0"
    check(items(utc(2026, 10, 30), c) == [], "FMO_NEWS=0")
    os.environ.pop("FMO_NEWS")
    print("\n%s" % ("all FMO news checks passed" if not fails else "%d FAILED" % len(fails)))
    return 1 if fails else 0


def main(argv):
    if "--selftest" in argv:
        return _selftest()
    if "--show" in argv:
        for it in items():
            print("%d  %s\n        %s\n%s" % (it["serial"], it["title"], it["date"], it["body"]))
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
