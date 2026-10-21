"""FFXI's own events in the news: the Viewer's Information page and the Discord news channel.

What FFXI has scheduled is worked out by the game itself. The LSB world's calendar script publishes
the Adventurer Campaign windows, the Repeat Login Campaign and the seasonal festivals, the Mog
Bonanza keeps its runs, and HippaulBridge's lsb/ffxi_calendar.py hands the lot to the live-state
store under KV_KEY. Nothing here keeps a calendar of its own, so the posts follow the
game: a campaign a GM switches off, or a festival whose dates change, changes the posts with it.

Few posts at a time, so FFXI does not take over the channel or the ticker:

  - events that start within a day of each other are one post (October's campaigns and October's
    Login Campaign start the same morning);
  - Discord gets a post when such a group opens, a "last days" post two days before a long event
    ends, and the Bonanza's winning numbers. Posts less than a day apart are one post. Only the
    newest FFXI_NEWS_DISCORD_KEEP (2) stay in the channel; older ones are deleted (polbridge.py);
  - the Viewer gets one item per group, up FFXI_NEWS_LEAD_H before it starts until it ends, and
    never more than FFXI_NEWS_MAX_ITEMS (2) at once, the newest kept.

Knobs (environment):

    FFXI_NEWS=0                     no FFXI posts at all
    FFXI_NEWS_LEAD_H=72             hours before a group starts its Viewer item appears
    FFXI_NEWS_MAX_ITEMS=2           Viewer items up at once
    FFXI_NEWS_DISCORD_KEEP=2        Discord posts kept in the channel
    FFXI_NEWS_CLOSING_H=48          hours before a long event ends its "last days" post goes out
    FFXI_NEWS_MAX_AGE_H=24          a published calendar older than this is ignored (bridge gone)

    python ffxinews.py --show       what would be up right now
"""
import calendar as _calendar
import os
import sys
import time

KV_KEY = "ffxi:calendar"
CONTENT = "ffxi"
DAY = 86400
HOUR = 3600
#: An event at least this long gets a "last days" post.
LONG_S = 6 * DAY
#: Events (or posts) closer than this are one post.
GROUP_S = DAY

#: Viewer serials 970000-979999 are FFXI's (eventnews.py has 98xxxx and 99xxxx; hand-written
#: posts count up from 900001). Derived from the group's start hour, so stable for its life.
SERIAL_FLOOR = 970000

_MONTHS = ["Jan.", "Feb.", "Mar.", "Apr.", "May", "Jun.",
           "Jul.", "Aug.", "Sep.", "Oct.", "Nov.", "Dec."]
_DAYS = ["Mon.", "Tue.", "Wed.", "Thu.", "Fri.", "Sat.", "Sun."]

LOGIN_MOOGLES = "the Greeter Moogles in Port San d'Oria, Port Bastok and Windurst Walls"
BONANZA_MOOGLES = "the Bonanza Moogles in Port San d'Oria, Port Bastok and Port Windurst"


def _env_num(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


def enabled():
    return os.environ.get("FFXI_NEWS", "1") != "0"


def lead_s():
    return max(0.0, _env_num("FFXI_NEWS_LEAD_H", 72)) * HOUR


def max_items():
    return max(0, int(_env_num("FFXI_NEWS_MAX_ITEMS", 2)))


def discord_keep():
    return max(1, int(_env_num("FFXI_NEWS_DISCORD_KEEP", 2)))


def closing_s():
    return max(HOUR, _env_num("FFXI_NEWS_CLOSING_H", 48) * HOUR)


# --------------------------------------------------------------------------- #
# the calendar, as the bridge published it
# --------------------------------------------------------------------------- #

def load(now=None, kv=None):
    """The events the bridge last published, or [] when there is no fresh list."""
    now = time.time() if now is None else now
    try:
        if kv is None:
            from polcore import kv as _kv
            kv = _kv.default()
        data = kv.get_json(KV_KEY)
    except Exception:                                # noqa: BLE001 -- no store, no posts
        return []
    if not isinstance(data, dict):
        return []
    if now - float(data.get("at") or 0) > max(HOUR, _env_num("FFXI_NEWS_MAX_AGE_H", 24) * HOUR):
        return []
    out = []
    for e in data.get("events") or []:
        try:
            ev = {"source": str(e["source"]), "key": str(e["key"]), "name": str(e.get("name") or ""),
                  "start": int(e["start"]), "end": int(e["end"]), "detail": dict(e.get("detail") or {})}
        except (KeyError, TypeError, ValueError):
            continue
        if ev["end"] > ev["start"]:
            out.append(ev)
    return sorted(out, key=lambda ev: (ev["start"], ev["source"], ev["key"]))


def event_id(ev):
    return "%s:%s" % (ev["source"], ev["key"])


def groups(events):
    """Events whose starts are less than GROUP_S apart, as lists, in start order."""
    out = []
    for ev in sorted(events, key=lambda ev: (ev["start"], ev["source"], ev["key"])):
        if out and ev["start"] - out[-1][0]["start"] < GROUP_S:
            out[-1].append(ev)
        else:
            out.append([ev])
    return out


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


def _and(names):
    names = list(names)
    if len(names) <= 1:
        return "".join(names)
    return "%s and %s" % (", ".join(names[:-1]), names[-1])


def _month(ev):
    return str(ev["detail"].get("month") or time.strftime("%B", time.gmtime(ev["start"])))


def short_name(ev):
    """The event as it reads in a title."""
    if ev["source"] == "login":
        return "%s Login Campaign" % _month(ev)
    return ev["name"] or "Event"


def _numbers(ev):
    nums = ev["detail"].get("numbers")
    if not nums or len(nums) != 5:
        return None
    return ["Rank %d: %0*d" % (i + 1, 5 - i, int(n)) for i, n in enumerate(nums)]


def lines(ev, when, discord):
    """The paragraphs about one event. `when` formats a time (Discord timestamps or UTC text)."""
    d = ev["detail"]
    src = ev["source"]
    out = []
    if src == "campaigns":
        names = [str(n) for n in d.get("campaigns") or []]
        out.append("From %s to %s:" % (when(ev["start"]), when(ev["end"])))
        out.append("\n".join("- %s" % n for n in names))
        if d.get("plus"):
            out.append("The chain bonuses run as PLUS: Dedication and Commitment count double.")
        if d.get("fishing"):
            out.append("Fishing skill gains are doubled along with synthesis.")
    elif src == "login":
        earn = int(d.get("earnEnd") or ev["end"])
        out.append("Log in each day from %s to %s to earn login points, then trade them with %s "
                   "for this month's prizes until %s." % (when(ev["start"]), when(earn), LOGIN_MOOGLES,
                                                         when(ev["end"])))
    elif src == "bonanza":
        sales_end = int(d.get("salesEnd") or ev["start"])
        collect = int(d.get("collectStart") or sales_end)
        nums = _numbers(ev)
        if nums:
            out.append("The winning numbers are in. A marble wins a rank when its last digits match:")
            out.append("\n".join("- %s" % n for n in nums))
            out.append("Trade winning marbles to %s until %s." % (BONANZA_MOOGLES, when(ev["end"])))
        else:
            price = int(d.get("price") or 0)
            out.append("Marbles are on sale from %s%s from %s to %s. The winning numbers are drawn "
                       "after sales close, and prizes can be collected from %s to %s."
                       % (BONANZA_MOOGLES, " for %s gil each" % "{:,}".format(price) if price else "",
                          when(ev["start"]), when(sales_end), when(collect), when(ev["end"])))
    else:
        if d.get("openEnded"):
            out.append("From %s." % when(ev["start"]))
        else:
            out.append("From %s to %s." % (when(ev["start"]), when(ev["end"])))
        if d.get("blurb"):
            out.append(str(d["blurb"]))
    return out


# --------------------------------------------------------------------------- #
# the Viewer's news items
# --------------------------------------------------------------------------- #

def serial(group):
    return SERIAL_FLOOR + (group[0]["start"] // HOUR) % 10000


def group_title(group):
    """A month's campaigns and Login Campaign start together and read as one: "October
    campaigns". Anything else is named in full."""
    if len(group) > 1 and all(ev["source"] in ("campaigns", "login") for ev in group):
        return "%s campaigns" % time.strftime("%B", time.gmtime(group[0]["start"]))
    return _and(short_name(ev) for ev in group)


def item(group):
    start = group[0]["start"]
    end = max(ev["end"] for ev in group)
    title = "%s: %s" % (group_title(group), _span(start, end))

    def when(t):
        return "%s UTC" % _clock(t)

    paras = []
    for ev in group:
        if len(group) > 1:
            paras.append(short_name(ev))
        paras.extend(lines(ev, when, False))
    posted = int(max(0, start - lead_s()) // HOUR) * HOUR
    return {"serial": serial(group), "date": _stamp(posted), "title": title, "kind": "event",
            "content": CONTENT, "body": "\n\n".join(p for p in paras if p), "status": False,
            "link": "", "auto": "ffxi:" + "+".join(event_id(ev) for ev in group)}


def items(now=None, events=None):
    """The FFXI items that should be up at `now`, newest last."""
    if not enabled():
        return []
    now = time.time() if now is None else now
    events = load(now) if events is None else events
    up = [g for g in groups(events)
          if g[0]["start"] - lead_s() <= now < max(ev["end"] for ev in g)]
    up = up[-max_items():] if max_items() else []
    return [item(g) for g in up]


# --------------------------------------------------------------------------- #
# the Discord posts
# --------------------------------------------------------------------------- #

def beats(events):
    """Every moment an event is worth a post: (at, kind, event). kind is open, closing or results."""
    out = []
    for ev in events:
        out.append((ev["start"], "open", ev))
        d = ev["detail"]
        if ev["source"] == "bonanza":
            if d.get("drawnAt") and _numbers(ev):
                out.append((int(d["drawnAt"]), "results", ev))
            continue
        if ev["source"] == "login":
            earn = int(d.get("earnEnd") or ev["end"])
            out.append((earn - closing_s(), "closing", ev))
            continue
        if ev["end"] - ev["start"] >= LONG_S and not d.get("openEnded"):
            out.append((ev["end"] - closing_s(), "closing", ev))
    return sorted(out, key=lambda b: (b[0], b[1], event_id(b[2])))


def posts(events):
    """The Discord posts, in order: dicts with `key`, `at`, `until` (when it stops being worth
    posting), `end` (when it can be deleted) and `beats`."""
    out = []
    for b in beats(events):
        if out and b[0] - out[-1]["at"] < GROUP_S:
            out[-1]["beats"].append(b)
        else:
            out.append({"at": b[0], "beats": [b]})
    for p, nxt in zip(out, out[1:] + [None]):
        p["end"] = max(b[2]["end"] for b in p["beats"])
        p["until"] = min(nxt["at"], p["end"]) if nxt else p["end"]
        p["key"] = "ffxi:%d:%s" % (p["at"], "+".join("%s/%s" % (k, event_id(ev)) for _a, k, ev in p["beats"]))
    return [p for p in out if p["until"] > p["at"]]


def due(now=None, events=None):
    """The one post that should be in the channel at `now`, as a list (empty when none)."""
    if not enabled():
        return []
    now = time.time() if now is None else now
    events = load(now) if events is None else events
    return [p for p in posts(events) if p["at"] <= now < p["until"]][-1:]


def _beat_title(at, kind, ev, now):
    name = short_name(ev)
    if kind == "results":
        return "%s: the winning numbers" % name
    if kind == "closing":
        if ev["source"] == "login":
            return "%s: last days to earn points" % name
        return "%s: last days" % name
    if ev["source"] == "bonanza":
        return "%s: marbles on sale" % name
    return "%s %s" % (name, "is on" if ev["start"] <= now else "starts soon")


def message(post, now=None):
    """(payload embeds) for one post: one embed per event, as a list of {title, description}."""
    now = time.time() if now is None else now
    embeds = []
    for at, kind, ev in post["beats"]:
        def when(t):
            return "%s (%s)" % (_ts(t), _ts(t, "R"))
        body = lines(ev, when, True)
        if kind == "closing" and ev["source"] != "login":
            body = ["Ends %s." % when(ev["end"])] + body[1:]
        embeds.append({"title": _beat_title(at, kind, ev, now)[:256],
                       "description": "\n\n".join(p for p in body if p)[:4000]})
    return embeds


def _selftest():
    fails = []

    def check(ok, label):
        print("  [%s] %s" % ("PASS" if ok else "FAIL", label))
        if not ok:
            fails.append(label)

    def utc(*a):
        return _calendar.timegm(a + (0,) * (6 - len(a)))

    for k in [k for k in os.environ if k.startswith("FFXI_NEWS")]:
        os.environ.pop(k)
    # A calendar as the LSB world would publish it for October-November 2026.
    evs = [
        {"source": "campaigns", "key": "202610101500-202610311500", "name": "Adventurer Campaigns",
         "start": utc(2026, 10, 10, 15), "end": utc(2026, 10, 31, 15),
         "detail": {"campaigns": ["Mog Garden Campaign", "Alter Ego Expo", "Double Unity Accolade Campaign"],
                    "plus": False, "fishing": False}},
        {"source": "login", "key": "2026-10", "name": "Repeat Login Campaign",
         "start": utc(2026, 10, 10, 15), "end": utc(2026, 11, 9, 14),
         "detail": {"month": "October", "earnEnd": utc(2026, 11, 2, 14)}},
        {"source": "seasonal", "key": "harvest-festival-20261020", "name": "Harvest Festival",
         "start": utc(2026, 10, 19, 15), "end": utc(2026, 11, 1, 15),
         "detail": {"blurb": "Trade a sweet to the townsfolk.", "openEnded": False}},
        {"source": "campaigns", "key": "202611100700-202611201500", "name": "Adventurer Campaigns",
         "start": utc(2026, 11, 10, 7), "end": utc(2026, 11, 20, 15),
         "detail": {"campaigns": ["Chain Experience Bonus Campaign"], "plus": True, "fishing": False}},
        {"source": "login", "key": "2026-11", "name": "Repeat Login Campaign",
         "start": utc(2026, 11, 10, 15), "end": utc(2026, 12, 9, 14),
         "detail": {"month": "November", "earnEnd": utc(2026, 12, 2, 14)}},
    ]
    g = groups(evs)
    check([len(x) for x in g] == [2, 1, 2], "October's campaigns and login are one group, so are November's")

    # Viewer items.
    its = items(utc(2026, 10, 15), evs)
    check([it["title"] for it in its] == ["October campaigns: Oct. 10 to Nov. 9"],
          "Oct 15: one item, the October group  --  %r" % [it["title"] for it in its])
    its = items(utc(2026, 10, 17), evs)
    check(len(its) == 2 and its[1]["title"].startswith("Harvest Festival: Oct. 19 to Nov. 1"),
          "Oct 17: the Harvest Festival item is up 72 h ahead  --  %r" % [it["title"] for it in its])
    its = items(utc(2026, 11, 8), evs)
    check(len(its) == 2 and its[0]["title"].startswith("October campaigns")
          and its[1]["title"].startswith("November campaigns"),
          "Nov 8: never more than two, the newest kept  --  %r" % [it["title"] for it in its])
    check(all(970000 <= it["serial"] <= 979999 for it in its) and len({it["serial"] for it in its}) == 2,
          "serials in 97xxxx and distinct")
    body = items(utc(2026, 10, 15), evs)[0]["body"]
    check("- Alter Ego Expo" in body and "Greeter Moogles" in body and "Mon. Nov. 2 14:00 UTC" in body,
          "the item lists the campaigns and the login dates  --  %r" % body[:200])
    check(all(it["content"] == "ffxi" and it["kind"] == "event" for it in its), "content ffxi, kind event")
    os.environ["FFXI_NEWS_MAX_ITEMS"] = "1"
    check(len(items(utc(2026, 11, 8), evs)) == 1, "FFXI_NEWS_MAX_ITEMS caps the items")
    os.environ.pop("FFXI_NEWS_MAX_ITEMS")

    # Discord posts.
    ps = posts(evs)
    kinds = [[k + ":" + ev["source"] for _a, k, ev in p["beats"]] for p in ps]
    check(kinds[0] == ["open:campaigns", "open:login"], "the first post opens October's group  --  %r" % kinds[0])
    check(["open:seasonal"] in kinds and ["closing:campaigns"] in kinds,
          "Harvest Festival opens on its own; the campaigns' last days on their own  --  %r" % kinds)
    check(all(b["at"] - a["at"] >= GROUP_S for a, b in zip(ps, ps[1:])), "posts are at least a day apart")
    d = due(utc(2026, 10, 15), evs)
    check(len(d) == 1 and d[0] is not None and d[0]["beats"][0][1] == "open",
          "Oct 15: the current post is October's opening")
    d = due(utc(2026, 10, 19, 16), evs)
    check(len(d) == 1 and d[0]["beats"][0][2]["source"] == "seasonal", "Oct 19 16:00: the Harvest Festival post")
    emb = message(d[0], utc(2026, 10, 19, 16))
    check(emb[0]["title"] == "Harvest Festival is on" and "<t:%d:F>" % utc(2026, 10, 19, 15) in emb[0]["description"],
          "its embed  --  %r" % emb)
    d = due(utc(2026, 10, 29, 16), evs)
    check(len(d) == 1 and [(b[1], b[2]["source"]) for b in d[0]["beats"]] == [("closing", "campaigns")],
          "Oct 29 15:00: the campaigns' last days")
    emb = message(d[0], utc(2026, 10, 29, 16))
    check(emb[0]["title"] == "Adventurer Campaigns: last days" and emb[0]["description"].startswith("Ends <t:"),
          "a last-days embed leads with the end  --  %r" % emb[0])
    d = due(utc(2026, 10, 30, 16), evs)
    check(len(d) == 1 and [(b[1], b[2]["source"]) for b in d[0]["beats"]]
          == [("closing", "seasonal"), ("closing", "login")],
          "Oct 30 15:00: Harvest Festival's last days and the login points' last days are one post")
    check(message(d[0], utc(2026, 10, 30, 16))[1]["title"] == "October Login Campaign: last days to earn points",
          "the login embed's title")
    check(len({p["key"] for p in ps}) == len(ps), "post keys are unique")

    # Bonanza.
    bon = {"source": "bonanza", "key": "v13", "name": "13th Vana'versary Mog Bonanza",
           "start": utc(2027, 5, 1, 15), "end": utc(2027, 6, 30, 15),
           "detail": {"salesEnd": utc(2027, 5, 31, 15), "collectStart": utc(2027, 6, 7, 15), "price": 2000,
                      "drawnAt": utc(2027, 6, 1, 0), "numbers": [12345, 678, 90, 1, 2]}}
    ps = posts([bon])
    check([[k for _a, k, _e in p["beats"]] for p in ps] == [["open"], ["results"]],
          "a Bonanza run: sales, then the winning numbers")
    emb = message(ps[1], utc(2027, 6, 2))
    check("Rank 1: 12345" in emb[0]["description"] and "Rank 2: 0678" in emb[0]["description"]
          and "Rank 5: 2" in emb[0]["description"], "the numbers, padded to each rank's digits  --  %r" % emb[0])
    undrawn = dict(bon, detail=dict(bon["detail"], drawnAt=None, numbers=None))
    check([[k for _a, k, _e in p["beats"]] for p in posts([undrawn])] == [["open"]], "no results before the draw")
    check("2,000 gil each" in message(posts([undrawn])[0], utc(2027, 5, 2))[0]["description"], "the price")

    # The stored calendar.
    class KV:
        def __init__(self, v):
            self.v = v

        def get_json(self, k):
            return self.v if k == KV_KEY else None

    check(load(utc(2026, 10, 15), KV({"at": utc(2026, 10, 15) - 60, "events": evs}))[0]["key"] == evs[0]["key"],
          "load reads the bridge's list")
    check(load(utc(2026, 10, 15), KV({"at": utc(2026, 10, 13), "events": evs})) == [],
          "a list older than a day is ignored")
    check(load(utc(2026, 10, 15), KV(None)) == [], "nothing published, no posts")
    os.environ["FFXI_NEWS"] = "0"
    check(items(utc(2026, 10, 15), evs) == [] and due(utc(2026, 10, 15), evs) == [], "FFXI_NEWS=0")
    os.environ.pop("FFXI_NEWS")
    print("\n%s" % ("all FFXI news checks passed" if not fails else "%d FAILED" % len(fails)))
    return 1 if fails else 0


def main(argv):
    if "--selftest" in argv:
        return _selftest()
    if "--show" in argv:
        now = time.time()
        for it in items(now):
            print("%d  %s\n        %s" % (it["serial"], it["title"], it["date"]))
        for p in due(now):
            print("discord: %s" % p["key"])
            for e in message(p, now):
                print("  %s\n    %s" % (e["title"], e["description"].replace("\n", "\n    ")))
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
