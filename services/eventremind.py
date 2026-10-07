"""Discord reminders for the cups in the event calendar.

polbridge.py posts these into each guild's announcement channel (the one
`/playonline announce` binds), pinging the game's role. They are worked out
from `event-calendar.json` (eventcal.py) at every pass, never stored; the bridge
only remembers which ones it has already posted (discord_notified, one name per
guild and reminder).

A cup gets at most three kinds of reminder:

    week    a week before its first session (holiday and one-off cups only;
            the weekly cup would be announced while last week's is running)
    day     a day before its first session
    soon    a few hours before each RUN of sessions

Sessions close together are one run, so "tonight 19:00 and 02:00" is one post,
not two. The first run's post opens the cup; each later one says when the next
sessions are. A reminder is only good until the next one is due (the last one
until its run ends), so a bridge that was down posts the reminder that is
current, never a stale one and never a backlog.

Knobs (environment):

    POL_EVENT_REMIND_GAMES=tm,jan    calendar sections to remind for
    POL_EVENT_REMIND_SOON_H=3        hours before a run its post goes out
    POL_EVENT_REMIND_RUN_GAP_H=12    sessions with less than this between them
                                     are one run (24 makes a weekend one run)
    POL_EVENT_REMIND_WEEK_WEEKLY=0   1: the weekly cup gets a week post too

A calendar event with "remind": false gets none.

    python eventremind.py --show [unix time]   what is due, and the schedule
"""
import os
import sys
import time

import eventcal
import eventnews

STAGES = ("week", "day", "soon")

#: A window longer than this is shown as a span, not as a session.
LONG_S = 6 * 3600


def _env_num(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


def games():
    raw = os.environ.get("POL_EVENT_REMIND_GAMES", "tm,jan")
    return [g.strip() for g in raw.split(",") if g.strip() in eventnews.GAMES]


def soon_s():
    return max(0.0, _env_num("POL_EVENT_REMIND_SOON_H", 3)) * 3600


def run_gap_s():
    return max(0.0, _env_num("POL_EVENT_REMIND_RUN_GAP_H", 12)) * 3600


def week_for_weekly():
    return os.environ.get("POL_EVENT_REMIND_WEEK_WEEKLY", "0") == "1"


def runs(sessions, gap=None):
    """Sessions [(start, end), ...] grouped into runs: a session starting less
    than `gap` after the previous one ended joins its run."""
    gap = run_gap_s() if gap is None else gap
    out = []
    for s, e in sorted(sessions):
        if out and s - out[-1][-1][1] < gap:
            out[-1].append((s, e))
        else:
            out.append([(s, e)])
    return out


def _cups(game, now, cal):
    """The calendar's cups for `game` around `now`, sessions grouped by season
    (the same grouping as the news posts)."""
    lo = now - 8 * eventcal.DAY
    hi = now + 14 * eventcal.DAY
    groups = {}
    for w in eventcal.windows(game, lo, hi, cal):
        key = w.get("season") or w["id"]
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"game": game, "id": key,
                               "name": str(w.get("name") or "Cup"),
                               "sessions": [], "src": w}
        g["sessions"].append((int(w["start"]), int(w["end"])))
    return [g for g in groups.values() if g["src"].get("remind") is not False]


def schedule(cup):
    """Every reminder a cup gets, in order: dicts with `stage`, `at` (when it is
    due), `until` (when it stops being worth posting) and `run` (the sessions
    it is about). A weekly cup's reminders key on its season id, so each week
    is fresh."""
    rs = runs(cup["sessions"])
    if not rs:
        return []
    first = rs[0][0][0]
    plan = []
    if week_for_weekly() or not eventnews.is_weekly(cup["src"]):
        plan.append({"stage": "week", "at": first - 7 * eventcal.DAY,
                     "run": [s for r in rs for s in r]})
    plan.append({"stage": "day", "at": first - eventcal.DAY,
                 "run": [s for r in rs for s in r]})
    for i, r in enumerate(rs):
        plan.append({"stage": "soon", "at": r[0][0] - soon_s(), "run": r,
                     "index": i, "first": i == 0})
    plan.sort(key=lambda p: p["at"])
    for p, nxt in zip(plan, plan[1:] + [None]):
        p["until"] = nxt["at"] if nxt else p["run"][-1][1]
        if p["stage"] == "soon":
            p["until"] = min(p["until"], p["run"][-1][1])
        p["key"] = "%s:%s%s" % (cup["id"], p["stage"],
                                "" if p["stage"] != "soon" else p["index"])
    # A reminder squeezed out by the next one (a cup added late, a short lead)
    # never gets a turn: until <= at.
    return [p for p in plan if p["until"] > p["at"]]


def due(now=None, cal=None):
    """The reminders that should be up at `now`, one per cup at most, each
    with its cup under "cup"."""
    now = time.time() if now is None else now
    cal = eventcal.load() if cal is None else cal
    out = []
    for game in games():
        for cup in _cups(game, now, cal):
            for p in schedule(cup):
                if p["at"] <= now < p["until"]:
                    p["cup"] = cup
                    out.append(p)
    return sorted(out, key=lambda p: p["at"])


# --------------------------------------------------------------------------- #
# the post
# --------------------------------------------------------------------------- #

def _ts(t, style="F"):
    """A Discord timestamp: every reader sees it in their own time zone."""
    return "<t:%d:%s>" % (int(t), style)


def _in(delta):
    """`in 7 days`, `in 22 hours`, `in 40 minutes`: coarse, for a title, where
    Discord timestamps do not render."""
    if delta >= 2 * eventcal.DAY:
        n, unit = int(round(delta / eventcal.DAY)), "day"
    elif delta >= 2 * 3600:
        n, unit = int(round(delta / 3600)), "hour"
    else:
        n, unit = max(1, int(round(delta / 60))), "minute"
    return "in %d %s%s" % (n, unit, "" if n == 1 else "s")


def _money(n):
    return "{:,}".format(int(n))


def message(rem, now=None):
    """(title, description) for one reminder, as of `now`."""
    now = time.time() if now is None else now
    cup = rem["cup"]
    src = cup["src"]
    name = cup["name"]
    run = [(s, e) for s, e in rem["run"] if e > now]
    if not run:
        run = rem["run"][-1:]
    start = run[0][0]
    later = rem["stage"] == "soon" and not rem.get("first")
    if start <= now:
        title = ("%s session on now" if later else "%s is on now") % name
    elif later:
        title = "Next %s session%s %s" % (name, "s" if len(run) > 1 else "",
                                          "start " + _in(start - now)
                                          if len(run) > 1 else _in(start - now))
    else:
        title = "%s starts %s" % (name, _in(start - now))
    lines = []
    if not later and src.get("guide"):
        lines.append(" ".join(p.strip() for p in str(src["guide"]).split("|")))
        lines.append("")
    if len(run) == 1 and run[0][1] - run[0][0] > LONG_S:
        # One long window (a JongHoLow cup runs the whole weekend): its span,
        # not a session list.
        s, e = run[0]
        if s <= now:
            lines.append("Runs until %s (%s)." % (_ts(e), _ts(e, "R")))
        else:
            lines.append("Runs from %s (%s) to %s." % (_ts(s), _ts(s, "R"), _ts(e)))
    else:
        lines.append("**Sessions**" if len(run) > 1 else "**Session**")
        shown = run[:8]
        for s, e in shown:
            mins = (e - s) // 60
            lines.append("- %s (%s), %d minutes" % (_ts(s), _ts(s, "R"), mins))
        if len(run) > len(shown):
            lines.append("- and %d more" % (len(run) - len(shown)))
    if later:
        rest = [s for s in cup["sessions"] if s[0] > run[-1][0]]
        if rest:
            lines.append("")
            lines.append("After these: %d more session%s, the last %s."
                         % (len(rest), "" if len(rest) == 1 else "s",
                            _ts(rest[-1][0], "f")))
    lines.append("")
    if cup["game"] == "tm":
        lines.append("Enter from the Tournament Hall in the Tetra Master "
                     "lobby. It opens shortly before each session.")
        money = (src.get("prizes") or {}).get("money") or []
        if money and not later:
            lines.append("Prizes for the top three: %s gil."
                         % " / ".join(_money(m) for m in money[:3]))
    else:
        lines.append("Everything you win in JongHoLow during the cup counts "
                     "toward the Event Ranking. The standings are on the Event "
                     "Ranking tab of the Ranking screen.")
    return title[:256], "\n".join(lines).strip()


def _selftest():
    import calendar as _c
    fails = []

    def check(ok, label):
        print("  [%s] %s" % ("PASS" if ok else "FAIL", label))
        if not ok:
            fails.append(label)

    def utc(*a):
        return _c.timegm(a + (0,) * (6 - len(a)))

    cal = {"tm": {
        "weekly": {"key": "chocobo-cup", "name": "Chocobo Cup", "dow": 4,
                   "hour": 12, "hours": 72,
                   "sessions": {"hours": [19, 2], "minutes": 60,
                                "from": "2026-01-01"},
                   "guide": "Win matches!|Clear missions.",
                   "prizes": {"money": [50000, 30000, 10000]}},
        "events": [{"key": "halloween-cup", "name": "Halloween Cup",
                    "yearly": "10-31", "days": 1, "hour": 12,
                    "sessions": {"hours": [19, 2], "minutes": 60,
                                 "from": "2026-01-01"}}]}}
    for k in [k for k in os.environ if k.startswith("POL_EVENT_REMIND")]:
        os.environ.pop(k)

    def stages(now):
        return [(p["cup"]["name"], p["stage"], p.get("index")) for p in due(now, cal)]

    # Chocobo Cup W40: Fri 10-02 12:00 .. Mon 10-05 12:00, sessions 19 + 02.
    w40 = [c for c in _cups("tm", utc(2026, 10, 2), cal) if c["id"].endswith("W40")]
    check([p["stage"] for p in schedule(w40[0])] == ["day", "soon", "soon", "soon"],
          "weekly cup: no week post (the week before is still its own cup)")
    check(stages(utc(2026, 9, 28, 12, 0)) == [], "nothing due mid-week")
    check(stages(utc(2026, 10, 1, 19, 0)) == [("Chocobo Cup", "day", None)],
          "weekly cup: the day post is due 24 h before the first session")
    check(stages(utc(2026, 10, 2, 15, 59)) == [("Chocobo Cup", "day", None)],
          "...and stays current until the first run's post")
    check(stages(utc(2026, 10, 2, 16, 0)) == [("Chocobo Cup", "soon", 0)],
          "first run's post 3 h before 19:00")
    check(stages(utc(2026, 10, 3, 2, 30)) == [("Chocobo Cup", "soon", 0)],
          "19:00 and 02:00 are ONE run")
    check(stages(utc(2026, 10, 3, 3, 0)) == [],
          "nothing between runs")
    check(stages(utc(2026, 10, 3, 16, 0)) == [("Chocobo Cup", "soon", 1)],
          "second night: its own post")
    cups = [c for c in _cups("tm", utc(2026, 10, 2), cal) if c["id"].endswith("W40")]
    check(len(runs(cups[0]["sessions"])) == 3, "a 3-night season is 3 runs")
    os.environ["POL_EVENT_REMIND_RUN_GAP_H"] = "24"
    check(len(runs(cups[0]["sessions"])) == 1, "RUN_GAP_H=24: the weekend is one run")
    os.environ.pop("POL_EVENT_REMIND_RUN_GAP_H")

    # Halloween Cup 10-31 (a Saturday): it replaces that weekend's weekly cup.
    check(("Halloween Cup", "week", None) in stages(utc(2026, 10, 24, 19, 0)),
          "holiday cup: a week post 7 days before")
    check(("Halloween Cup", "week", None) in stages(utc(2026, 10, 29, 12, 0)),
          "...still current (catching up) until the day post")
    check(("Halloween Cup", "day", None) in stages(utc(2026, 10, 30, 19, 0))
          and ("Halloween Cup", "week", None) not in stages(utc(2026, 10, 30, 19, 0)),
          "the day post supersedes the week post")

    # Keys are stable and distinct.
    keys = [p["key"] for c in cups for p in schedule(c)]
    check(len(keys) == len(set(keys)) and all(k.startswith(cups[0]["id"]) for k in keys),
          "reminder keys are unique per cup  --  %s" % keys)

    # Messages.
    rem = due(utc(2026, 10, 2, 16, 0), cal)[0]
    title, body = message(rem, utc(2026, 10, 2, 16, 0))
    check(title == "Chocobo Cup starts in 3 hours", "first run title  --  %r" % title)
    check("<t:%d:F>" % utc(2026, 10, 2, 19) in body and "<t:%d:F>" % utc(2026, 10, 3, 2) in body
          and "<t:%d:F>" % utc(2026, 10, 3, 19) not in body,
          "first run lists tonight's two sessions only")
    check("50,000 / 30,000 / 10,000 gil" in body and "Win matches! Clear missions." in body,
          "first run carries the guide and the prizes")
    rem = due(utc(2026, 10, 3, 16, 0), cal)[0]
    title, body = message(rem, utc(2026, 10, 3, 16, 0))
    check(title == "Next Chocobo Cup sessions start in 3 hours", "later run title  --  %r" % title)
    check("After these: 2 more sessions" in body and "gil" not in body,
          "later run: what is left, no prize blurb  --  %r" % body[-120:])
    rem = due(utc(2026, 10, 1, 19, 0), cal)[0]
    title, body = message(rem, utc(2026, 10, 1, 19, 0))
    check(title == "Chocobo Cup starts in 24 hours" and body.count("<t:") == 12,
          "day post lists all six sessions  --  %r" % title)
    os.environ["POL_EVENT_REMIND_GAMES"] = "jan"
    check(due(utc(2026, 10, 2, 16, 0), cal) == [], "POL_EVENT_REMIND_GAMES picks the sections")
    os.environ.pop("POL_EVENT_REMIND_GAMES")

    # JongHoLow: one long window per cup, no sessions.
    cal["jan"] = {"weekly": {"key": "weekend-cup", "name": "Weekend Cup, week {week}",
                             "dow": 4, "hour": 0, "hours": 72}, "events": []}
    jan = [p for p in due(utc(2026, 10, 1, 3), cal) if p["cup"]["game"] == "jan"]
    check([p["stage"] for p in jan] == ["day"], "JongHoLow is reminded by default: the day post")
    title, body = message(jan[0], utc(2026, 10, 1, 3))
    check(title == "Weekend Cup, week 40 starts in 21 hours"
          and "Runs from <t:%d:F>" % utc(2026, 10, 2) in body
          and "minutes" not in body and "Event Ranking" in body,
          "a long window is shown as its span  --  %r / %r" % (title, body))
    jan = [p for p in due(utc(2026, 10, 3, 12), cal) if p["cup"]["game"] == "jan"]
    check(len(jan) == 1 and jan[0]["stage"] == "soon",
          "the cup's 'on' post stays current through the weekend")
    title, body = message(jan[0], utc(2026, 10, 3, 12))
    check(title == "Weekend Cup, week 40 is on now"
          and "Runs until <t:%d:F>" % utc(2026, 10, 5) in body,
          "...and says when it ends  --  %r" % body)
    print("\n%s" % ("all event reminder checks passed" if not fails
                    else "%d FAILED" % len(fails)))
    return 1 if fails else 0


def main(argv):
    if "--selftest" in argv:
        return _selftest()
    if "--show" in argv:
        rest = [a for a in argv if a != "--show"]
        now = float(rest[0]) if rest else time.time()
        cal = eventcal.load()
        print("due at %s UTC:" % time.strftime("%Y-%m-%d %H:%M", time.gmtime(now)))
        for rem in due(now, cal):
            title, body = message(rem, now)
            print("  %s\n    %s" % (rem["key"], title))
            for line in body.splitlines():
                print("      " + line)
        print("\nschedule:")
        for game in games():
            for cup in sorted(_cups(game, now, cal), key=lambda c: c["sessions"][0]):
                for p in schedule(cup):
                    print("  %s  ..  %s  %s" % (
                        time.strftime("%a %m-%d %H:%M", time.gmtime(p["at"])),
                        time.strftime("%a %m-%d %H:%M", time.gmtime(p["until"])),
                        p["key"]))
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
