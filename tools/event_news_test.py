#!/usr/bin/env python3
"""Event posts: the calendar's cups go up and come down in the news by themselves.

    python tools/event_news_test.py

Covers eventnews.py and its seam in newsgen.py, against a fixed calendar and
fixed clock, publishing into a temporary tree:

  * a Tetra Master season is ONE post listing its sessions; a JongHoLow
    weekend cup is one post;
  * a post appears POL_EVENT_NEWS_LEAD_H before the start and comes down
    POL_EVENT_NEWS_KEEP_MIN after the end, and its serial never changes;
  * the calendar's "news": false and "news_title" are honoured;
  * a publish writes the post into the ticker, SE's Events category of
    news<N>.pml and a detail page, and a later sync prunes it;
  * the timer republishes what was PUBLISHED, never a saved draft, and does
    nothing before it knows what that is.

NOT covered: that the Viewer re-reads the ticker when a post comes down.
"""
import calendar
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "services"))

TMP = tempfile.mkdtemp(prefix="event-news-")
WWW = os.path.join(TMP, "www")
os.makedirs(WWW)
os.environ["POL_NEWS_STORE"] = os.path.join(TMP, "announcements.yaml")
os.environ["POL_NEWS_PUBLISHED"] = os.path.join(TMP, "announcements.published.yaml")
for k in ("POL_EVENT_NEWS", "POL_EVENT_NEWS_LEAD_H", "POL_EVENT_NEWS_EVENT_LEAD_H",
          "POL_EVENT_NEWS_KEEP_MIN"):
    os.environ.pop(k, None)

import eventnews  # noqa: E402
import newsgen    # noqa: E402

FAILS = []
H = 3600


def check(ok, label, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  --  {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def utc(y, m, d, h=0):
    return calendar.timegm((y, m, d, h, 0, 0))


CAL = {
    "tm": {"weekly": {"key": "chocobo-cup", "name": "Chocobo Cup", "dow": 4,
                      "hour": 0, "hours": 72,
                      "sessions": {"hours": [19, 2], "minutes": 60,
                                   "from": "2026-09-28", "season_hour": 12,
                                   "season_hours": 72},
                      "guide": "Win matches!|Clear the missions."},
           "events": [{"key": "halloween-cup", "name": "Halloween Cup",
                       "yearly": "10-31", "days": 1, "hour": 12, "news": False,
                       "sessions": {"hours": [19, 2], "minutes": 60,
                                    "from": "2026-09-28"}}]},
    "jan": {"weekly": {"key": "weekend-cup", "name": "Weekend Cup, week {week}",
                       "dow": 4, "hour": 0, "hours": 72},
            "events": [{"key": "golden-week-cup", "name": "Golden Week Cup",
                        "yearly": "04-29", "days": 7,
                        "news_title": "Golden Week: a whole week of mahjong"}]},
}
TM_START = utc(2026, 10, 2, 12)      # Fri 12:00, 72 h season
TM_END = TM_START + 72 * H
JAN_START = utc(2026, 10, 2)          # Fri 00:00, 72 h


def detail_path(serial, loc="en-US"):
    return os.path.join(WWW, "wh000.pol.com", "pcd", "ntool", loc, f"{serial}.pml")


def read(rel):
    with open(os.path.join(WWW, *rel.split("/")), encoding="utf-8") as f:
        return f.read()


def main():
    # --- which posts, and when ---------------------------------------------
    now = TM_START + H
    evs = eventnews.events("tm", now, CAL)
    check(len(evs) == 1, "a Tetra Master season is ONE post", repr([e["id"] for e in evs]))
    if evs:
        check(len(evs[0]["sessions"]) == 6 and evs[0]["start"] == TM_START
              and evs[0]["end"] == TM_END,
              "...spanning the season, with its six sessions",
              "%d sessions" % len(evs[0]["sessions"]))
    items = eventnews.items(now, CAL)
    tm = [i for i in items if i["content"] == "tetra"]
    jan = [i for i in items if i["content"] == "jan"]
    check(len(tm) == 1 and len(jan) == 1, "one post per game while both cups run",
          repr([i["title"] for i in items]))
    if tm and jan:
        t, j = tm[0], jan[0]
        check(t["title"] == "Chocobo Cup: Oct. 2 to Oct. 5", "TM headline names the days",
              repr(t["title"]))
        check(j["title"] == "Weekend Cup, week 40: Oct. 2 to Oct. 4",
              "a cup ending Monday 00:00 ends on Sunday", repr(j["title"]))
        check(t["kind"] == j["kind"] == "event" and newsgen.KINDS["event"][1] == 3
              and newsgen.CATEGORIES[3] == "Events",
              "filed under SE's Events category")
        check("Fri. Oct. 2 19:00" in t["body"] and "Mon. Oct. 5 02:00" in t["body"]
              and "Win matches! Clear the missions." in t["body"],
              "TM body: the guide and every session", repr(t["body"][:80]))
        check(98_0000 <= t["serial"] < 99_0000 <= j["serial"] < 100_0000
              and newsgen.allowed(f"wh000.pol.com/pcd/ntool/en-US/{t['serial']}.pml"),
              "serials in 98xxxx (TM) / 99xxxx (Jan), writable detail paths",
              "%d %d" % (t["serial"], j["serial"]))
        check(re.fullmatch(r"[A-Z][a-z]{2}\.? \d{1,2}, \d{4} \d\d:\d\d \[UTC\]", t["date"])
              is not None, "date in newsgen's format", repr(t["date"]))
        later = [i for i in eventnews.items(TM_END - H, CAL) if i["content"] == "tetra"]
        check(bool(later) and later[0]["serial"] == t["serial"]
              and later[0]["date"] == t["date"],
              "the same serial and date for the whole life of the post")

    lead = 72 * H
    check(not [i for i in eventnews.items(TM_START - lead - 1, CAL) if i["content"] == "tetra"],
          "no TM post a second before the lead")
    check([i for i in eventnews.items(TM_START - lead, CAL) if i["content"] == "tetra"] != [],
          "the TM post appears 72 h before the start")
    check([i for i in eventnews.items(TM_END + 59 * 60, CAL) if i["content"] == "tetra"] != [],
          "still up 59 minutes after the end (results)")
    check(not [i for i in eventnews.items(TM_END + 60 * 60, CAL) if i["content"] == "tetra"],
          "down 60 minutes after the end")
    os.environ["POL_EVENT_NEWS_LEAD_H"] = "24"
    check(not [i for i in eventnews.items(TM_START - 25 * H, CAL) if i["content"] == "tetra"],
          "POL_EVENT_NEWS_LEAD_H=24: not up 25 h before")
    os.environ.pop("POL_EVENT_NEWS_LEAD_H")

    hallo = utc(2026, 10, 31, 20)
    check(not [i for i in eventnews.items(hallo, CAL) if i["content"] == "tetra"],
          "\"news\": false keeps an event out of the news")
    gw = [i for i in eventnews.items(utc(2027, 5, 1), CAL) if i["content"] == "jan"]
    check(bool(gw) and gw[0]["title"] == "Golden Week: a whole week of mahjong",
          "\"news_title\" replaces the headline", repr([i["title"] for i in gw]))
    os.environ["POL_EVENT_NEWS"] = "0"
    check(eventnews.items(now, CAL) == [], "POL_EVENT_NEWS=0: no posts")
    os.environ.pop("POL_EVENT_NEWS")

    # --- a holiday cup's week of lead, and a body that moves on -------------
    cal2 = {"tm": dict(CAL["tm"], events=[dict(CAL["tm"]["events"][0], news=True)])}
    h_start = utc(2026, 10, 31, 12)
    check([i for i in eventnews.items(h_start - 168 * H, cal2) if "Halloween" in i["title"]] != []
          and not [i for i in eventnews.items(h_start - 168 * H - 1, cal2)
                   if "Halloween" in i["title"]],
          "a holiday cup is up POL_EVENT_NEWS_EVENT_LEAD_H (168) before its start")
    first = [i for i in eventnews.items(TM_START + H, CAL) if i["content"] == "tetra"]
    check(bool(first) and "Next session: Fri. Oct. 2 19:00 UTC." in first[0]["body"],
          "the body leads with the next session",
          first and repr(first[0]["body"][:160]))
    mid = [i for i in eventnews.items(utc(2026, 10, 3, 2) + 1800, CAL) if i["content"] == "tetra"]
    check(bool(mid) and "A session is running now, until 03:00 UTC." in mid[0]["body"]
          and "Fri. Oct. 2 19:00" not in mid[0]["body"]
          and "Sessions still to come (UTC): Sat. Oct. 3 19:00" in mid[0]["body"]
          and mid[0]["serial"] == first[0]["serial"],
          "mid-cup: the running session, then only what is still to come; same serial",
          mid and repr(mid[0]["body"]))
    done = [i for i in eventnews.items(TM_END - 5 * H, CAL) if i["content"] == "tetra"]
    check(bool(done) and "The last session of the Chocobo Cup is over." in done[0]["body"],
          "after the last session it says so", done and repr(done[0]["body"][:120]))

    # --- publishing ---------------------------------------------------------
    manual = [{"date": "Sep. 27, 2026 20:00 [UTC]", "title": "Welcome back",
               "kind": "info", "content": "playonline", "body": "Hello."}]
    newsgen.save(manual)
    check(eventnews.sync(WWW, now, CAL).get("held") is not None,
          "before any publish, a store the tree does not match is NOT published")
    res = newsgen.publish(newsgen.load(), WWW, events_now=now, events_cal=CAL)
    ticker = read("wh000.pol.com/pcd/ntool/en-US/latestnews.pml")
    check("Chocobo Cup: Oct. 2 to Oct. 5" in ticker and "Weekend Cup, week 40" in ticker
          and "Welcome back" in ticker, "the ticker carries both event posts and the operator's",
          "%d written" % len(res["written"]))
    check(ticker.index("Chocobo Cup") < ticker.index("Welcome back"),
          "event posts come first on the ticker")
    serial_tm = tm[0]["serial"] if tm else 0
    check(os.path.isfile(detail_path(serial_tm)), "the TM post has a detail page")
    news3 = newsgen.parse_narray(read("wh000.pol.com/pcd/ntool/en-US/news3.pml"))
    check(any(r[5].startswith("Chocobo Cup") for r in news3[3]),
          "news3.pml (Tetra Master) lists it under Events")
    news4 = newsgen.parse_narray(read("wh000.pol.com/pcd/ntool/en-US/news4.pml"))
    check(not any(r[5].startswith("Chocobo Cup") for r in news4[3]),
          "...and news4.pml (JongHoLow) does not")
    pub = newsgen.load(os.environ["POL_NEWS_PUBLISHED"])
    check([i["title"] for i in pub] == ["Welcome back"],
          "the published record holds the operator's posts only",
          repr([i["title"] for i in pub]))

    # a draft saved but not published must not ride the timer's publish
    newsgen.save(manual + [{"date": "Sep. 28, 2026 20:00 [UTC]",
                            "title": "DRAFT not yet published", "kind": "info",
                            "content": "playonline"}])
    res = eventnews.sync(WWW, TM_END + 2 * H, CAL)
    ticker = read("wh000.pol.com/pcd/ntool/en-US/latestnews.pml")
    check("DRAFT" not in ticker, "the timer republishes the published set, not a draft")
    check("Chocobo Cup" not in ticker and "Welcome back" in ticker,
          "after the cup's results close, its post is gone from the ticker")
    check(not os.path.isfile(detail_path(serial_tm)) and
          any(str(serial_tm) in p for p in res.get("pruned", [])),
          "...and its detail page is pruned")
    news3 = newsgen.parse_narray(read("wh000.pol.com/pcd/ntool/en-US/news3.pml"))
    check(not any(r[5].startswith("Chocobo Cup") for r in news3[3]),
          "...and it is gone from news3.pml")

    # the bootstrap: no record yet, but the store is exactly what is served
    os.remove(os.environ["POL_NEWS_PUBLISHED"])
    newsgen.save(manual)
    newsgen.publish(newsgen.load(), WWW, events=False, record=False)
    res = eventnews.sync(WWW, now, CAL)
    check(not res.get("held") and "Chocobo Cup" in
          read("wh000.pol.com/pcd/ntool/en-US/latestnews.pml"),
          "with no record, a store that matches the tree is taken as published",
          repr(res.get("held")))

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s)")
        return 1
    print("all event news checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
