"""The event calendar both games read: holidays over a weekly default.

One JSON file, `services/event-calendar.json` beside this module (a
`/config/event-calendar.json` wins where mounted; POL_EVENT_CALENDAR overrides
both), one section per game:

    {"tm":  {"weekly": {...}, "events": [{...}, ...]},
     "jan": {"weekly": {...}, "events": [{...}, ...]}}

A WEEKLY default is `{"key", "name", "dow" (0 = Monday), "hour", "hours"}`;
its occurrence id carries the ISO week.

An EVENT is `{"key", "name", "days"}` plus exactly one of:

    "yearly": "MM-DD"                  every year from 00:00 UTC that day
    "dates":  {"2026": "09-25", ...}   a moving holiday, set per year
    "date":   "YYYY-MM-DD"             once

optional `"hour"` (start hour, UTC, default 0) and `"priority"` (higher wins
an overlap, default 10). Any other keys (guide, ticker, missions, prizes...)
are passed through untouched for the game to use.

SESSIONS (optional, on a weekly default or an event):

    "sessions": {"hours": [19, 2], "minutes": 60, "from": "2026-09-28",
                 "season_hour": 12, "season_hours": 72}

turn the occurrence into a SEASON that only decides which days run: the
windows the game sees are the short sessions inside it, each starting at one
of `hours` (UTC) and lasting `minutes`, wholly inside the season. Occurrences
that start before `from` keep their long single window. On a weekly default,
`season_hour`/`season_hours` replace `hour`/`hours` from `from` on. A session's id is
`<key>-<YYYYMMDD>-<HHMM>` and it carries `season` (the occurrence id). The
overlap rules below run on seasons, before they are split. Tetra Master's
client is built for this (its event list has one date plus a start and end
time of day, and leaving warns that your status is deleted); Janhourou does
not use it.

RULES, the same for both games:
  * One event at a time per game (Janhourou's client has ONE event flag).
  * An event that overlaps a weekly occurrence REPLACES that whole occurrence
    (a holiday that overlaps a weekend replaces that weekend's cup), so the
    weekly cup never runs in the leftover hours.
  * A window's id is `<key>-<YYYYMMDD of its start>`: stable across hourly
    re-runs and restarts, fresh for every occurrence.

    python eventcal.py --show tm       # current + the next few
    python eventcal.py --selftest
"""
import calendar
import datetime as _dt
import json
import os
import sys

DAY = 86400


def calendar_path():
    p = os.environ.get("POL_EVENT_CALENDAR")
    if p:
        return p
    # /config wins where it is mounted; the copy beside this module is the one
    # every container sees, including any without a /config mount.
    for cand in ("/config/event-calendar.json",
                 os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "event-calendar.json")):
        if os.path.exists(cand):
            return cand
    return "/config/event-calendar.json"


def load(path=None):
    try:
        with open(path or calendar_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _utc(y, m, d, h=0):
    return calendar.timegm((y, m, d, h, 0, 0))


def _occurrence(ev, start):
    out = dict(ev)
    out["start"] = int(start)
    out["end"] = int(start + max(1, float(ev.get("days", 1))) * DAY)
    out["id"] = "%s-%s" % (ev.get("key", "event"),
                           _dt.datetime.utcfromtimestamp(start).strftime("%Y%m%d"))
    out["kind"] = "event"
    return out


def _event_occurrences(ev, lo, hi):
    """Every occurrence of `ev` whose window touches [lo, hi)."""
    h = int(ev.get("hour", 0))
    starts = []
    y0 = _dt.datetime.utcfromtimestamp(lo).year - 1
    y1 = _dt.datetime.utcfromtimestamp(hi).year + 1
    try:
        if "yearly" in ev:
            m, d = (int(x) for x in str(ev["yearly"]).split("-"))
            starts = [_utc(y, m, d, h) for y in range(y0, y1 + 1)]
        elif "dates" in ev:
            for y, md in (ev.get("dates") or {}).items():
                m, d = (int(x) for x in str(md).split("-"))
                starts.append(_utc(int(y), m, d, h))
        elif "date" in ev:
            y, m, d = (int(x) for x in str(ev["date"]).split("-"))
            starts = [_utc(y, m, d, h)]
    except (ValueError, TypeError):
        return []
    out = [_occurrence(ev, s) for s in starts]
    return [o for o in out if o["end"] > lo and o["start"] < hi]


def _weekly_occurrences(wk, lo, hi):
    if not wk:
        return []
    dow, hour = int(wk.get("dow", 4)), int(wk.get("hour", 0))
    hours = float(wk.get("hours", 72))
    t = int(lo // DAY) * DAY - 8 * DAY
    out = []
    while t < hi:
        if _dt.datetime.utcfromtimestamp(t).weekday() == dow:
            s = t + hour * 3600
            e = int(s + hours * 3600)
            ses = wk.get("sessions")
            if isinstance(ses, dict) and ("season_hour" in ses or "season_hours" in ses):
                # From the sessions' `from` date the season may start at a
                # different hour (seasons already under way keep theirs).
                try:
                    fy, fm, fd = (int(x) for x in str(ses.get("from", "1970-01-01")).split("-"))
                    if s >= _utc(fy, fm, fd):
                        s = t + int(ses.get("season_hour", hour)) * 3600
                        e = int(s + float(ses.get("season_hours", hours)) * 3600)
                except (ValueError, TypeError):
                    pass
            if e > lo and s < hi:
                o = dict(wk)
                o.update(start=int(s), end=e, kind="weekly")
                iso = _dt.datetime.utcfromtimestamp(s).isocalendar()
                o["week"] = iso[1]
                o["id"] = "%s-%sW%02d" % (wk.get("key", "weekly"), iso[0], iso[1])
                o["name"] = str(wk.get("name", "Weekly Cup")).replace(
                    "{week}", str(iso[1]))
                out.append(o)
        t += DAY
    return out


def _sessions(o):
    """Split one occurrence into its sessions (see SESSIONS above), or [o]."""
    spec = o.get("sessions")
    if not isinstance(spec, dict):
        return [o]
    frm = spec.get("from")
    if frm:
        try:
            y, m, d = (int(x) for x in str(frm).split("-"))
            if o["start"] < _utc(y, m, d):
                return [o]
        except (ValueError, TypeError):
            pass
    try:
        hours = sorted({int(h) % 24 for h in spec.get("hours") or []})
        length = max(1, int(spec.get("minutes", 60))) * 60
    except (ValueError, TypeError):
        return [o]
    out = []
    day = int(o["start"] // DAY) * DAY
    while day < o["end"]:
        for h in hours:
            st = day + h * 3600
            if st >= o["start"] and st + length <= o["end"]:
                w = dict(o)
                w.update(start=int(st), end=int(st + length), kind="session",
                         season=o["id"], season_kind=o["kind"],
                         season_start=o["start"], season_end=o["end"])
                w["id"] = "%s-%s" % (o.get("key", "event"),
                                     _dt.datetime.utcfromtimestamp(st).strftime("%Y%m%d-%H%M"))
                out.append(w)
        day += DAY
    return out


def windows(game, lo, hi, cal=None):
    """Every window for `game` touching [lo, hi), after the one-at-a-time and
    replace-the-weekly rules, sorted by start."""
    sec = (cal if cal is not None else load()).get(game) or {}
    evs = []
    for ev in sec.get("events") or []:
        evs += _event_occurrences(ev, lo - 40 * DAY, hi + 40 * DAY)
    # one at a time: a higher-priority (then earlier) event wins an overlap
    evs.sort(key=lambda o: (-int(o.get("priority", 10)), o["start"]))
    kept = []
    for o in evs:
        if all(o["end"] <= k["start"] or o["start"] >= k["end"] for k in kept):
            kept.append(o)
    weekly = [w for w in _weekly_occurrences(sec.get("weekly"), lo - 8 * DAY,
                                             hi + 8 * DAY)
              if all(w["end"] <= k["start"] or w["start"] >= k["end"]
                     for k in kept)]
    out = [w for o in kept + weekly for w in _sessions(o)]
    out = [o for o in out if o["end"] > lo and o["start"] < hi]
    return sorted(out, key=lambda o: o["start"])


def current(game, now=None, cal=None):
    """The window running now for `game`, or None."""
    import time
    now = time.time() if now is None else now
    for o in windows(game, now, now + 1, cal):
        if o["start"] <= now < o["end"]:
            return o
    return None


def upcoming(game, now=None, n=5, cal=None, horizon_days=400):
    """The next `n` windows that have not ended (the running one first)."""
    import time
    now = time.time() if now is None else now
    return windows(game, now, now + horizon_days * DAY, cal)[:n]


def last_ended(game, now=None, cal=None, lookback_days=14):
    """The most recent window that has already ended (for results/prizes)."""
    import time
    now = time.time() if now is None else now
    past = [o for o in windows(game, now - lookback_days * DAY, now, cal)
            if o["end"] <= now]
    return past[-1] if past else None


def _selftest():
    ok = True
    def check(label, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + label)
        ok = ok and cond
    cal = {"g": {"weekly": {"key": "wk", "name": "Weekend Cup, week {week}",
                            "dow": 4, "hour": 0, "hours": 72},
                 "events": [{"key": "xmas", "name": "Holiday Cup",
                             "yearly": "12-24", "days": 3},
                            {"key": "ny", "name": "New Year", "yearly": "12-31",
                             "days": 3},
                            {"key": "moon", "name": "Tsukimi",
                             "dates": {"2026": "09-25"}, "days": 3},
                            {"key": "big", "name": "Big", "date": "2026-12-25",
                             "days": 1, "priority": 20}]}}
    t = _utc(2026, 10, 2, 12)                      # a Friday
    c = current("g", t, cal)
    check("weekly: Friday noon is in the weekend cup",
          c and c["kind"] == "weekly" and c["id"] == "wk-2026W40"
          and c["name"] == "Weekend Cup, week 40")
    check("weekly: Monday is not", current("g", _utc(2026, 10, 5, 12), cal) is None)
    c = current("g", _utc(2026, 9, 26, 12), cal)
    check("moving holiday (dates) preempts that weekend",
          c and c["key"] == "moon" and c["id"] == "moon-20260925")
    check("replaced weekend does not run in its leftover hours",
          current("g", _utc(2026, 9, 28, 12), cal) is None)
    c = current("g", _utc(2026, 12, 25, 12), cal)
    check("higher priority one-off wins an overlap", c and c["key"] == "big")
    check("the lower one loses the whole window, not a slice",
          current("g", _utc(2026, 12, 24, 12), cal) is None)
    c = current("g", _utc(2027, 1, 1, 12), cal)
    check("year-wrapping yearly event", c and c["key"] == "ny"
          and c["id"] == "ny-20261231")
    up = upcoming("g", t, 3, cal)
    check("upcoming: running one first, then later ones",
          up and up[0]["id"] == "wk-2026W40" and up[1]["start"] > up[0]["start"])
    check("stable ids across calls", current("g", t, cal)["id"] ==
          current("g", t + 3600, cal)["id"])
    le = last_ended("g", _utc(2026, 10, 6, 12), cal)
    check("last_ended: the weekend just gone", le and le["id"] == "wk-2026W40")
    check("empty calendar: nothing", current("zz", t, cal) is None)
    sc = {"g": {"weekly": {"key": "wk", "name": "Cup", "dow": 4, "hour": 0,
                           "hours": 72, "sessions": {"hours": [19, 2],
                                                     "minutes": 60,
                                                     "from": "2026-10-01",
                                                     "season_hour": 12,
                                                     "season_hours": 72}},
                "events": [{"key": "hw", "name": "Halloween", "yearly": "10-31",
                            "hour": 12, "days": 1,
                            "sessions": {"hours": [19, 2], "minutes": 60}}]}}
    wk = windows("g", _utc(2026, 10, 9), _utc(2026, 10, 13), sc)
    check("sessions: six one-hour sessions, Fri 19:00 .. Mon 02:00",
          [o["id"] for o in wk] == ["wk-20261009-1900", "wk-20261010-0200",
                                    "wk-20261010-1900", "wk-20261011-0200",
                                    "wk-20261011-1900", "wk-20261012-0200"]
          and all(o["end"] - o["start"] == 3600 for o in wk)
          and wk[0]["season"] == "wk-2026W41" and wk[0]["kind"] == "session")
    c = current("g", _utc(2026, 10, 9, 19) + 1800, sc)
    check("sessions: inside one, and nothing between them",
          c and c["id"] == "wk-20261009-1900"
          and current("g", _utc(2026, 10, 9, 21), sc) is None)
    c = current("g", _utc(2026, 9, 26, 12), sc)
    check("sessions: a season before 'from' keeps its long window",
          c and c["kind"] == "weekly" and c["id"] == "wk-2026W39"
          and c["start"] == _utc(2026, 9, 25) and c["end"] == _utc(2026, 9, 28))
    hw = [o["id"] for o in windows("g", _utc(2026, 10, 30), _utc(2026, 11, 3), sc)]
    check("sessions: Halloween replaces that weekend, two sessions",
          hw == ["hw-20261031-1900", "hw-20261101-0200"])
    le = last_ended("g", _utc(2026, 10, 9, 20, ) + 60, sc)
    check("sessions: last_ended is the session just gone",
          le and le["id"] == "wk-20261009-1900")
    real = load()
    check("the shipped calendar parses and has both games",
          "tm" in real and "jan" in real)
    return ok


def main(argv):
    if "--selftest" in argv:
        return 0 if _selftest() else 1
    if "--show" in argv:
        import time
        game = argv[argv.index("--show") + 1] if len(argv) > argv.index("--show") + 1 else "tm"
        now = time.time()
        fmt = lambda t: _dt.datetime.utcfromtimestamp(t).strftime("%Y-%m-%d %H:%M")
        c = current(game, now)
        print("now:", c["id"] if c else "-", c["name"] if c else "")
        for o in upcoming(game, now, 8):
            print("  %s  %s -> %s  %s" % (o["id"], fmt(o["start"]), fmt(o["end"]),
                                         o["name"]))
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
