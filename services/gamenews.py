"""The games' own scheduled events in the news: the Viewer's Information page and the Discord
news channel.

A game that keeps a calendar of its own (Front Mission Online's event battles and Clash of Iron
seasons, Fantasy Earth's campaigns, Dirge of Cerberus's tournaments) publishes what is coming to
the live-state store under "<game>:events", in the shape its admin Calendar uses:

    {"at": <unix>, "events": [{"id", "title", "start", "end", "kind", "about", "times"}]}

where "times" (optional) are the sessions inside a long window, as [[start, end], ...]. Who writes
it: FMO's City Control board (hippaulfront boardfmo.py), Fantasy Earth's world (hippaulring). This
module keeps no calendar of its own, so the posts follow the game: an event a GM switches off
leaves the list and its post goes with it.

The Viewer gets one item per event on the game's tab and the ticker, from GAME_NEWS_LEAD_H
before it starts until it ends, never more than GAME_NEWS_MAX_ITEMS a game at once (the soonest
kept). Discord gets a post the day before an event starts and a "last days" post before a long
one ends. Posts less than a day apart are one post, and only the newest GAME_NEWS_DISCORD_KEEP a
game stay in the channel (polbridge.py deletes the rest).

Knobs (environment):

    GAME_NEWS=0                     no posts from any game here
    GAME_NEWS_GAMES=fmo,fe,doc      which games' lists are read
    GAME_NEWS_LEAD_H=168            hours before an event its Viewer item appears
    GAME_NEWS_MAX_ITEMS=2           Viewer items a game has up at once
    GAME_NEWS_DISCORD_KEEP=2        Discord posts a game keeps in the channel
    GAME_NEWS_SOON_H=24             hours before an event its Discord post goes out
    GAME_NEWS_CLOSING_H=48          hours before a long event ends its "last days" post goes out
    GAME_NEWS_MAX_AGE_H=24          a published list older than this is ignored (publisher gone)

    python gamenews.py --show       what would be up right now
    python gamenews.py --selftest
"""
import calendar as _calendar
import os
import sys
import time

DAY = 86400
HOUR = 3600
#: An event at least this long gets a "last days" post.
LONG_S = 6 * DAY
#: Posts closer than this are one post.
GROUP_S = DAY

#: game -> (newsgen content key, the game's name, serial slot). The content key picks the
#: Viewer tab and the Discord role and bot (newsgen.CONTENT_TABS, polbridge).
GAMES = {
    "fmo": ("fmo", "Front Mission Online", 0),
    "fe":  ("fe", "Fantasy Earth", 1),
    "doc": ("doc", "Dirge of Cerberus", 2),
}
#: Viewer serials 950000-959999 are these posts', 3000 a game (fmonews.py has 96xxxx,
#: ffxinews.py 97xxxx, eventnews.py 98-99xxxx). Derived from the event's start hour, so stable
#: for its life.
SERIAL_FLOOR = 950000
SERIAL_SPAN = 3000

#: Calendar kinds that are news. The rest ("arena" for a player's ordinary arena, "arena rule"
#: for a weekly rule, "war phase" which fmonews.py already posts) stay on the Calendar only.
NEWS_KINDS = ("event", "tournament", "shop", "campaign")

_MONTHS = ["Jan.", "Feb.", "Mar.", "Apr.", "May", "Jun.",
           "Jul.", "Aug.", "Sep.", "Oct.", "Nov.", "Dec."]
_DAYS = ["Mon.", "Tue.", "Wed.", "Thu.", "Fri.", "Sat.", "Sun."]


def _env_num(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


def enabled():
    return os.environ.get("GAME_NEWS", "1") != "0"


def games():
    raw = os.environ.get("GAME_NEWS_GAMES", ",".join(GAMES))
    return [g.strip() for g in raw.split(",") if g.strip() in GAMES]


def lead_s():
    return max(0.0, _env_num("GAME_NEWS_LEAD_H", 168)) * HOUR


def max_items():
    return max(0, int(_env_num("GAME_NEWS_MAX_ITEMS", 2)))


def discord_keep():
    return max(1, int(_env_num("GAME_NEWS_DISCORD_KEEP", 2)))


def soon_s():
    return max(0.0, _env_num("GAME_NEWS_SOON_H", 24)) * HOUR


def closing_s():
    return max(HOUR, _env_num("GAME_NEWS_CLOSING_H", 48) * HOUR)


def kv_key(game):
    return "%s:events" % game


# --------------------------------------------------------------------------- #
# the lists, as the games published them
# --------------------------------------------------------------------------- #

def load(game, now=None, kv=None):
    """The news-worthy events `game` last published, or [] when there is no fresh list."""
    now = time.time() if now is None else now
    try:
        if kv is None:
            from polcore import kv as _kv
            kv = _kv.default()
        data = kv.get_json(kv_key(game))
    except Exception:                                # noqa: BLE001 -- no store, no posts
        return []
    if not isinstance(data, dict):
        return []
    if now - float(data.get("at") or 0) > max(HOUR, _env_num("GAME_NEWS_MAX_AGE_H", 24) * HOUR):
        return []
    return parse(game, data.get("events"))


def parse(game, rows):
    """Calendar entries -> events this module posts: news kinds only, with sane times."""
    out = []
    for e in rows or []:
        try:
            ev = {"game": game, "id": str(e["id"]), "title": str(e.get("title") or "Event"),
                  "start": int(e["start"]), "end": int(e["end"]), "kind": str(e.get("kind") or ""),
                  "about": str(e.get("about") or ""),
                  "times": [[int(a), int(b)] for a, b in (e.get("times") or []) if int(b) > int(a)]}
        except (KeyError, TypeError, ValueError):
            continue
        if ev["end"] > ev["start"] and ev["kind"] in NEWS_KINDS:
            out.append(ev)
    return sorted(out, key=lambda ev: (ev["start"], ev["id"]))


def event_id(ev):
    return "%s:%s" % (ev["game"], ev["id"])


# --------------------------------------------------------------------------- #
# text
# --------------------------------------------------------------------------- #

def _day(t):
    g = time.gmtime(t)
    return "%s %d" % (_MONTHS[g.tm_mon - 1], g.tm_mday)


def _clock(t):
    g = time.gmtime(t)
    return "%s %s %02d:%02d" % (_DAYS[g.tm_wday], _day(t), g.tm_hour, g.tm_min)


def _span(start, end):
    first, last = _day(start), _day(end - 1)
    return first if first == last else "%s to %s" % (first, last)


def _stamp(t):
    g = time.gmtime(t)
    return "%s %d, %d %02d:%02d [UTC]" % (_MONTHS[g.tm_mon - 1], g.tm_mday, g.tm_year, g.tm_hour, g.tm_min)


def _ts(t, style="F"):
    return "<t:%d:%s>" % (int(t), style)


def lines(ev, when, now=None):
    """The paragraphs about one event. `when` formats a time (Discord timestamps or UTC text).
    Sessions already over are left out once the event has begun."""
    out = []
    times = ev["times"]
    if len(times) > 1:
        left = [t for t in times if now is None or t[1] > now]
        out.append("Sessions:\n" + "\n".join("- %s to %s" % (when(a), when(b)) for a, b in left))
    else:
        out.append("From %s to %s." % (when(ev["start"]), when(ev["end"])))
    if ev["about"]:
        out.append(ev["about"])
    return out


# --------------------------------------------------------------------------- #
# the Viewer's news items
# --------------------------------------------------------------------------- #

def serial(ev):
    slot = GAMES[ev["game"]][2]
    return SERIAL_FLOOR + slot * SERIAL_SPAN + (ev["start"] // HOUR) % SERIAL_SPAN


def item(ev, now=None):
    def when(t):
        return "%s UTC" % _clock(t)

    posted = int(max(0, ev["start"] - lead_s()) // HOUR) * HOUR
    return {"serial": serial(ev), "date": _stamp(posted),
            "title": "%s: %s" % (ev["title"], _span(ev["start"], ev["end"])), "kind": "event",
            "content": GAMES[ev["game"]][0],
            "body": "\n\n".join(p for p in lines(ev, when, now) if p), "status": False,
            "link": "", "auto": "game:" + event_id(ev)}


def game_items(game, now=None, events=None):
    """`game`'s items that should be up at `now`, soonest first."""
    now = time.time() if now is None else now
    events = load(game, now) if events is None else events
    up = [ev for ev in events if ev["start"] - lead_s() <= now < ev["end"]]
    return [item(ev, now) for ev in up[:max_items()]]


def items(now=None, lists=None):
    """Every game's items that should be up at `now`. `lists` = {game: events} for a test."""
    if not enabled():
        return []
    out = []
    for game in games():
        out += game_items(game, now, None if lists is None else lists.get(game, []))
    return out


# --------------------------------------------------------------------------- #
# the Discord posts
# --------------------------------------------------------------------------- #

def beats(events):
    """Every moment an event is worth a post: (at, kind, event); kind is soon or closing."""
    out = []
    for ev in events:
        out.append((ev["start"] - soon_s(), "soon", ev))
        if ev["end"] - ev["start"] >= LONG_S:
            out.append((ev["end"] - closing_s(), "closing", ev))
    return sorted(out, key=lambda b: (b[0], b[1], event_id(b[2])))


def posts(game, events):
    """`game`'s Discord posts, in order: dicts with `key`, `at`, `until` (when it stops being
    worth posting), `end` (when it can be deleted) and `beats`."""
    out = []
    for b in beats(events):
        if out and b[0] - out[-1]["at"] < GROUP_S:
            out[-1]["beats"].append(b)
        else:
            out.append({"at": b[0], "beats": [b]})
    for p, nxt in zip(out, out[1:] + [None]):
        p["end"] = max(b[2]["end"] for b in p["beats"])
        p["until"] = min(nxt["at"], p["end"]) if nxt else p["end"]
        p["key"] = "%s:%d:%s" % (game, p["at"], "+".join("%s/%s" % (k, event_id(ev))
                                                         for _a, k, ev in p["beats"]))
    return [p for p in out if p["until"] > p["at"]]


def due(game, now=None, events=None):
    """The one post of `game` that should be in the channel at `now`, as a list (or [])."""
    if not enabled() or game not in games():
        return []
    now = time.time() if now is None else now
    events = load(game, now) if events is None else events
    return [p for p in posts(game, events) if p["at"] <= now < p["until"]][-1:]


def _beat_title(kind, ev, now):
    if kind == "closing":
        return "%s: last days" % ev["title"]
    return "%s %s" % (ev["title"], "is on" if ev["start"] <= now else "starts soon")


def message(post, now=None):
    """One post as a list of {title, description}, one per event."""
    now = time.time() if now is None else now
    embeds = []
    for _at, kind, ev in post["beats"]:
        def when(t):
            return "%s (%s)" % (_ts(t), _ts(t, "R"))
        body = lines(ev, when, now)
        if kind == "closing":
            body = ["Ends %s." % when(ev["end"])] + body[1:]
        embeds.append({"title": _beat_title(kind, ev, now)[:256],
                       "description": "\n\n".join(p for p in body if p)[:4000]})
    return embeds


# --------------------------------------------------------------------------- #

def _selftest():
    fails = []

    def check(ok, label):
        print("  [%s] %s" % ("PASS" if ok else "FAIL", label))
        if not ok:
            fails.append(label)

    def utc(*a):
        return _calendar.timegm(a + (0,) * (6 - len(a)))

    for k in [k for k in os.environ if k.startswith("GAME_NEWS")]:
        os.environ.pop(k)
    # FMO's list as the board would publish it for late 2026
    nights = [[utc(2026, 11, d, 12), utc(2026, 11, d, 14)] for d in (12, 13, 14, 19, 20, 21)]
    rows = [
        {"id": "coi-S01", "title": "Clash of Iron S01: Two-Guns Shootout", "kind": "tournament",
         "start": nights[0][0], "end": utc(2026, 11, 28, 15),
         "times": nights + [[utc(2026, 11, 28, 12), utc(2026, 11, 28, 15)]],
         "about": "6 preliminary nights in the official 1 vs 1 arena."},
        {"id": "event-operation-holy-night-2026", "title": "Operation Holy Night", "kind": "event",
         "start": utc(2026, 12, 22, 3), "end": utc(2026, 12, 28, 3),
         "times": [[utc(2026, 12, 22, 3), utc(2026, 12, 28, 3)]],
         "about": "Event battles on 6 sector(s); a win there pays a bonus."},
        {"id": "heavy-1", "title": "Heavy Mobile Weapon Orders", "kind": "arena rule",
         "start": utc(2026, 11, 14, 7), "end": utc(2026, 11, 14, 15)},
        {"id": "phase-3", "title": "Phase 3", "kind": "war phase",
         "start": utc(2026, 11, 5), "end": utc(2027, 1, 1)},
        {"id": "bad", "title": "x", "kind": "event", "start": 10, "end": 5},
    ]
    evs = parse("fmo", rows)
    check([e["id"] for e in evs] == ["coi-S01", "event-operation-holy-night-2026"],
          "news kinds only: no arena rule, no war phase (fmonews has it), no backwards window")

    class _KV:
        def __init__(self, data):
            self.data = data

        def get_json(self, key):
            return self.data.get(key)

    kv = _KV({"fmo:events": {"at": utc(2026, 11, 1), "events": rows}})
    check(len(load("fmo", utc(2026, 11, 1, 6), kv)) == 2, "load reads <game>:events")
    check(load("fmo", utc(2026, 11, 3), kv) == [], "a list older than a day is ignored")
    check(load("fe", utc(2026, 11, 1, 6), kv) == [], "a game with no list has none")

    its = game_items("fmo", utc(2026, 11, 6), evs)
    check([it["title"] for it in its] == ["Clash of Iron S01: Two-Guns Shootout: Nov. 12 to Nov. 28"],
          "Nov 6: the season's item is up a week ahead  --  %r" % [it["title"] for it in its])
    check(game_items("fmo", utc(2026, 11, 4), evs) == [], "Nov 4: not yet")
    its = game_items("fmo", utc(2026, 11, 20, 13), evs)
    check(its and "- Thu. Nov. 12" not in its[0]["body"] and "- Sat. Nov. 21 12:00 UTC" in its[0]["body"],
          "a running season lists only the sessions still to come")
    its = game_items("fmo", utc(2026, 12, 23), evs)
    check([it["title"] for it in its] == ["Operation Holy Night: Dec. 22 to Dec. 28"]
          and its[0]["content"] == "fmo" and its[0]["kind"] == "event",
          "Dec 23: Holy Night, on the FMO tab  --  %r" % [it["title"] for it in its])
    check(950000 <= its[0]["serial"] < 953000, "FMO serials in 950000-952999")
    check(serial(dict(evs[0], game="fe")) - serial(evs[0]) == SERIAL_SPAN, "each game has its own block")
    os.environ["GAME_NEWS_MAX_ITEMS"] = "1"
    many = [dict(evs[1], id="e%d" % i, start=evs[1]["start"] + i * HOUR) for i in range(3)]
    check(len(game_items("fmo", utc(2026, 12, 23), many)) == 1, "GAME_NEWS_MAX_ITEMS caps a game's items")
    os.environ.pop("GAME_NEWS_MAX_ITEMS")
    check(len(items(utc(2026, 12, 23), {"fmo": evs, "fe": []})) == 1, "items() covers every game")
    os.environ["GAME_NEWS"] = "0"
    check(items(utc(2026, 12, 23), {"fmo": evs}) == [], "GAME_NEWS=0 posts nothing")
    os.environ.pop("GAME_NEWS")

    # Discord
    ps = posts("fmo", evs)
    check([[(k, e["id"][:6]) for _a, k, e in p["beats"]] for p in ps]
          == [[("soon", "coi-S0")], [("closing", "coi-S0")], [("soon", "event-")],
              [("closing", "event-")]],
          "a day before each start, and last days before each event of six days or more")
    check(due("fmo", utc(2026, 11, 11, 13), evs)[0]["beats"][0][2]["id"] == "coi-S01",
          "Nov 11 13:00: the season's post is due")
    check(due("fmo", utc(2026, 11, 10), evs) == [], "Nov 10: nothing yet")
    m = message(due("fmo", utc(2026, 11, 11, 13), evs)[0], utc(2026, 11, 11, 13))
    check(m[0]["title"] == "Clash of Iron S01: Two-Guns Shootout starts soon" and "<t:" in m[0]["description"],
          "the post names the event and carries Discord timestamps  --  %r" % m[0]["title"])
    m = message(due("fmo", utc(2026, 11, 27), evs)[0], utc(2026, 11, 27))
    check(m[0]["title"].endswith("last days") and m[0]["description"].startswith("Ends <t:"),
          "the closing post leads with the end")
    check(due("doc", utc(2026, 11, 11, 13), []) == [], "a game with nothing scheduled posts nothing")

    print("%d check(s) failed" % len(fails) if fails else "gamenews selftest: ALL PASS")
    return 1 if fails else 0


def main(argv):
    if "--selftest" in argv:
        return _selftest()
    if "--show" in argv:
        now = time.time()
        for game in games():
            evs = load(game, now)
            print("%s: %d event(s) published" % (game, len(evs)))
            for it in game_items(game, now, evs):
                print("  item %d  %s" % (it["serial"], it["title"]))
            for p in due(game, now, evs):
                print("  discord %s" % p["key"])
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
