"""Announcements for the game events in the event calendar.

Tetra Master's cups and JongHoLow's weekend cups are already scheduled in
`event-calendar.json` (see eventcal.py). This turns each one into a news item
for the login ticker and the Information section, from a few days before it
starts until its results have closed, so nobody has to post or retire them by
hand.

The items are computed, never stored: `newsgen.publish` adds them to whatever
the operator has published, so the admin panel, `tools/gen_news.py` and the
admin service's timer (`sync`) all write the same files, and an event post can
neither be edited by accident nor overwrite the operator's own posts.

One post per EVENT, not per session. Tetra Master splits a season into short
sessions (eventcal SESSIONS); the post names the season, leads with the next
session and lists the ones still to come, so the text moves on as each session
ends while the serial (and the detail page) stays put.

Knobs (environment):

    POL_EVENT_NEWS=0                    no event posts at all
    POL_EVENT_NEWS_LEAD_H=72            hours before a weekly cup a post appears
    POL_EVENT_NEWS_EVENT_LEAD_H=168     the same for a holiday or one-off cup
    POL_EVENT_NEWS_KEEP_MIN=60          minutes after the end it stays up (results)

A weekly cup gets the short lead because a week's lead would put it up while
the previous week's cup is still running.

Per event, in the calendar (weekly default or event entry):

    "news": false                 never post this one
    "news_title": "..."           replaces the generated headline
    "news_body": "..."            replaces the generated body ("|" = new paragraph)

    python eventnews.py --show        # what would be posted right now
"""
import os
import sys
import time

import eventcal

#: calendar section -> (newsgen content key, the game's name in the body text).
GAMES = {
    "tm":  ("tetra", "Tetra Master"),
    "jan": ("jan", "JongHoLow"),
}

#: Serials 980000-999999 are the event posts'; 98xxxx Tetra Master, 99xxxx
#: JongHoLow. A serial names `pcd/ntool/<loc>/<serial>.pml`, so it must be
#: stable for the life of a post: it is derived from the event's own start hour.
#: Hand-written posts count up from newsgen.SERIAL_BASE and never get near.
SERIAL_FLOOR = 980000
_GAME_SLOT = {"tm": 0, "jan": 1}

_MONTHS = ["Jan.", "Feb.", "Mar.", "Apr.", "May", "Jun.",
           "Jul.", "Aug.", "Sep.", "Oct.", "Nov.", "Dec."]
_DAYS = ["Mon.", "Tue.", "Wed.", "Thu.", "Fri.", "Sat.", "Sun."]


def _env_num(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


def enabled():
    return os.environ.get("POL_EVENT_NEWS", "1") != "0"


def lead_s(src=None):
    """How long before the start a post goes up. `src` is the event's calendar
    window: a weekly cup gets POL_EVENT_NEWS_LEAD_H, anything else
    POL_EVENT_NEWS_EVENT_LEAD_H. Without one, the longer of the two."""
    weekly = max(0.0, _env_num("POL_EVENT_NEWS_LEAD_H", 72)) * 3600
    other = max(0.0, _env_num("POL_EVENT_NEWS_EVENT_LEAD_H", 168)) * 3600
    if src is None:
        return max(weekly, other)
    return weekly if is_weekly(src) else other


def is_weekly(src):
    """True when a calendar window belongs to the weekly default cup."""
    return (src.get("season_kind") or src.get("kind")) == "weekly"


def keep_s():
    return max(0.0, _env_num("POL_EVENT_NEWS_KEEP_MIN", 60)) * 60


def _day(t):
    g = time.gmtime(t)
    return "%s %d" % (_MONTHS[g.tm_mon - 1], g.tm_mday)


def _stamp(t):
    """newsgen's date format: `Sep. 27, 2026 20:00 [UTC]`."""
    g = time.gmtime(t)
    return "%s %d, %d %02d:%02d [UTC]" % (_MONTHS[g.tm_mon - 1], g.tm_mday,
                                          g.tm_year, g.tm_hour, g.tm_min)


def _span(start, end):
    """`Oct. 2 to Oct. 5`, or one day. The last day is the one the event is
    still running on, so a cup ending Monday 00:00 ends on Sunday."""
    first, last = _day(start), _day(end - 1)
    return first if first == last else "%s to %s" % (first, last)


def _clock(t):
    g = time.gmtime(t)
    return "%s %s %02d:%02d" % (_DAYS[g.tm_wday], _day(t), g.tm_hour, g.tm_min)


def events(game, now=None, cal=None):
    """The events of one calendar section whose post is up at `now`.

    Each is {"game", "id", "name", "start", "end", "sessions", "src"}: sessions
    are grouped back into their season, so a Tetra Master cup is one event."""
    now = time.time() if now is None else now
    lo = now - keep_s() - 8 * eventcal.DAY
    hi = now + lead_s() + 8 * eventcal.DAY
    groups = {}
    for w in eventcal.windows(game, lo, hi, cal):
        key = w.get("season") or w["id"]
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "game": game, "id": key, "name": str(w.get("name") or "Cup"),
                "start": int(w.get("season_start", w["start"])),
                "end": int(w.get("season_end", w["end"])),
                "sessions": [], "src": w}
        if w.get("kind") == "session":
            g["sessions"].append((int(w["start"]), int(w["end"])))
    out = []
    for g in groups.values():
        if g["src"].get("news") is False:
            continue
        if g["start"] - lead_s(g["src"]) <= now < g["end"] + keep_s():
            g["sessions"].sort()
            g["now"] = now
            out.append(g)
    return sorted(out, key=lambda g: g["start"])


def serial(ev):
    return (SERIAL_FLOOR + _GAME_SLOT.get(ev["game"], 0) * 10000
            + (ev["start"] // 3600) % 10000)


def _body(ev):
    src = ev["src"]
    if src.get("news_body"):
        return "\n\n".join(p.strip() for p in str(src["news_body"]).split("|"))
    paras = []
    if ev["game"] == "tm":
        if src.get("guide"):
            paras.append(" ".join(p.strip() for p in str(src["guide"]).split("|")))
        if ev["sessions"]:
            now = ev.get("now", 0)
            n = len(ev["sessions"])
            mins = (ev["sessions"][0][1] - ev["sessions"][0][0]) // 60
            ahead = [(s, e) for s, e in ev["sessions"] if e > now]
            if not ahead:
                paras.append("The last session of the %s is over." % ev["name"])
            elif ahead[0][0] <= now:
                paras.append("A session is running now, until %02d:%02d UTC."
                             % (time.gmtime(ahead[0][1]).tm_hour,
                                time.gmtime(ahead[0][1]).tm_min))
            else:
                paras.append("Next session: %s UTC." % _clock(ahead[0][0]))
            paras.append(
                "The %s runs in %d session%s of %d minutes. Enter from the "
                "Tournament Hall in the Tetra Master lobby, which opens shortly "
                "before each session." % (ev["name"], n, "" if n == 1 else "s",
                                          mins))
            if len(ahead) > 1 or (ahead and ahead[0][0] > now):
                later = [s for s, _e in ahead if s > now]
                paras.append(("Sessions (UTC): " if len(later) == n else
                              "Sessions still to come (UTC): ")
                             + ", ".join(_clock(s) for s in later) + ".")
        else:
            paras.append("The %s runs from %s to %s (UTC). Enter from the "
                         "Tournament Hall in the Tetra Master lobby."
                         % (ev["name"], _clock(ev["start"]), _clock(ev["end"])))
        paras.append("Rankings and prizes are given out after each session.")
    else:
        paras.append("The %s runs from %s to %s (UTC)."
                     % (ev["name"], _clock(ev["start"]), _clock(ev["end"])))
        paras.append("Everything you win during the cup counts toward the "
                     "Event Ranking. The standings are on the Event Ranking tab "
                     "of the Ranking screen.")
    return "\n\n".join(paras)


def item(ev):
    """One event as a newsgen announcement (already validated shape)."""
    content, _label = GAMES[ev["game"]]
    src = ev["src"]
    title = str(src.get("news_title") or "%s: %s" % (ev["name"],
                                                    _span(ev["start"], ev["end"])))
    # The date is when the post went up (the lead before the start), floored to
    # the hour, so it is the same on every publish and never in the future.
    posted = int(max(0, ev["start"] - lead_s(src)) // 3600) * 3600
    return {"serial": serial(ev), "date": _stamp(posted), "title": title,
            "kind": "event", "content": content, "body": _body(ev),
            "status": False, "link": "", "auto": ev["id"]}


def items(now=None, cal=None):
    """Every event post that should be up at `now`, soonest first."""
    if not enabled():
        return []
    cal = eventcal.load() if cal is None else cal
    evs = []
    for game in GAMES:
        evs += events(game, now, cal)
    return [item(ev) for ev in sorted(evs, key=lambda ev: (ev["start"], ev["game"]))]


def signature(now=None, cal=None):
    """What changes when the set of posts (or their text) changes."""
    return tuple((it["serial"], it["title"], it["body"]) for it in items(now, cal))


def sync(www=None, now=None, cal=None):
    """Republish the last PUBLISHED announcements with the current event posts.

    Never the saved store: a draft the operator saved but did not publish must
    not go live because an event started. See newsgen.published_items."""
    import newsgen
    manual = newsgen.published_items(www)
    if manual is None:
        return {"held": "the saved announcements have not been published; "
                           "publish once from the News tab"}
    return newsgen.publish(manual, www, events_now=now, events_cal=cal)


def main(argv):
    if "--show" in argv:
        for it in items():
            print("%d  %-6s %s\n        %s" % (it["serial"], it["content"],
                                             it["title"], it["date"]))
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
