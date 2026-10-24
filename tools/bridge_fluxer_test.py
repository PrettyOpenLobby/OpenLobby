#!/usr/bin/env python3
"""bridge_fluxer_test.py -- the bridge's news posts on Fluxer, beside Discord.

    python tools/bridge_fluxer_test.py

Fluxer has no slash commands, so its announcement channels are config
(POL_BRIDGE_FLUXER_ANNOUNCE) rather than /playonline announce rows. This pins
that a Fluxer channel is posted to through Fluxer's API and its own webhook,
under the game's FLUXER bot, never dumps the backlog on its first pass, and
that the Discord rows go on exactly as before. No database: discordlink's
store is replaced by a dict, the network by a recorder.
"""
import json
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "services"))
for extra in (os.path.abspath(os.path.join(HERE, "..", "..", "hippaulmaster", "services")),
              os.path.abspath(os.path.join(HERE, "..", "..", "..", "private", "services"))):
    if os.path.isdir(extra):
        sys.path.insert(0, extra)

os.environ["POL_BRIDGE_FLUXER_ANNOUNCE"] = "F1"
os.environ["POL_BRIDGE_FLUXER_ANNOUNCE_ROLES"] = "tetra=FR"
os.environ["POL_BRIDGE_FLUXER_GAME_BOTS"] = "tetra=FTM"
os.environ["POL_LOG_DIR"] = os.devnull

import polbridge                                        # noqa: E402
import discordlink                                      # noqa: E402

CHECKS = []


def check(label, cond, detail=""):
    CHECKS.append(label)
    print("  %-70s %s%s" % (label, "PASS" if cond else "FAIL",
                            ("  " + str(detail)) if detail and not cond else ""), flush=True)
    if not cond:
        raise AssertionError(label)


class Store:
    """discordlink's tables, as far as the news scans use them."""

    def __init__(self):
        self.meta, self.notified = {}, set()
        self.rows = [{"guild_id": "G1", "channel_id": "D1", "last_serial": 1, "bound_at": 0}]

    def install(self):
        s = self
        fake = {
            "connect": lambda *a, **k: types.SimpleNamespace(close=lambda: None),
            "announce_rows": lambda ldb: [dict(r) for r in s.rows],
            "announce_roles": lambda ldb, g: {"tetra": "DR"},
            "bump_announce_serial": lambda ldb, g, n: [r.update(last_serial=max(r["last_serial"], n))
                                                     for r in s.rows if r["guild_id"] == g],
            "get_meta": lambda ldb, k, d=None: s.meta.get(k, d),
            "set_meta": lambda ldb, k, v: s.meta.__setitem__(k, v),
            "is_notified": lambda ldb, n: n in s.notified,
            "mark_notified": lambda ldb, n, now=None: s.notified.add(n),
        }
        for k, v in fake.items():
            setattr(discordlink, k, v)


class Net:
    """Answers like both APIs: users, webhooks, posts."""

    def __init__(self):
        self.calls = []
        self.n = 0

    def __call__(self, req, timeout=None):
        url, m = req.full_url, req.get_method()
        body = json.loads(req.data) if req.data else None
        self.calls.append((m, url, body, req.get_header("Authorization")))
        if m == "GET" and "/users/" in url:
            uid = url.rsplit("/", 1)[1]
            data = {"id": uid if uid != "@me" else "BR", "username": "bot-" + uid, "avatar": "abc"}
        elif m == "POST" and url.endswith("/webhooks"):
            data = {"id": "H" + url.split("/channels/")[1].split("/")[0], "token": "T"}
        elif m == "DELETE":
            return Resp(204, b"")
        else:
            self.n += 1
            data = {"id": "m%d" % self.n}
        return Resp(200, json.dumps(data).encode())


class Resp:
    def __init__(self, status, raw):
        self.status, self.raw = status, raw

    def read(self):
        return self.raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


ITEMS = [{"serial": 1, "title": "old", "content": "tetra"},
         {"serial": 2, "title": "new", "content": "tetra"}]


def setup():
    store, net = Store(), Net()
    store.install()
    polbridge.OPENER = net
    polbridge.ARGS = polbridge.build_parser().parse_args([])
    polbridge.ARGS.token, polbridge.ARGS.fluxer_token = "DTOK", "FTOK"
    polbridge._load_announcements = lambda: list(ITEMS)
    polbridge._PROFILES.clear()
    return store, net


def posts(net, host):
    return [(u, b, a) for m, u, b, a in net.calls
            if m == "POST" and host in u and "/webhooks/H" in u]


def test_announce():
    print("announcements")
    store, net = setup()
    n = polbridge.scan_announce()
    check("first pass: Discord gets serial 2, Fluxer only takes its baseline", n == 1, n)
    check("...the Fluxer channel starts at the newest serial, no backlog",
          store.meta.get("fluxer-announce-serial:F1") == "2", store.meta)
    d = posts(net, "discord.com")
    check("the Discord post went through Discord's webhook", len(d) == 1, net.calls)
    check("...pinging the Discord role", d[0][1].get("content") == "<@&DR>", d[0][1])

    ITEMS.append({"serial": 3, "title": "newer", "content": "tetra"})
    net.calls.clear()
    n = polbridge.scan_announce()
    f = posts(net, "api.fluxer.app")
    check("the next one goes to both", n == 2 and len(f) == 1, (n, net.calls))
    check("...the Fluxer one through Fluxer's own webhook in its channel",
          "/webhooks/HF1/T" in f[0][0], f[0][0])
    check("...pinging the Fluxer role from config", f[0][1].get("content") == "<@&FR>", f[0][1])
    check("...under the game's FLUXER bot and its Fluxer avatar",
          f[0][1].get("username") == "bot-FTM"
          and f[0][1].get("avatar_url", "").startswith(polbridge.FLUXER_MEDIA + "/avatars/FTM/"),
          f[0][1])
    fluxer_auth = {a for m, u, b, a in net.calls if "api.fluxer.app" in u and a}
    check("every Fluxer call carries the Fluxer token, never Discord's",
          fluxer_auth == {"Bot FTOK"}, fluxer_auth)
    check("the two webhooks are kept apart",
          "fluxer-news-webhook:F1" in store.meta and "news-webhook:D1" in store.meta, store.meta)
    check("the thread is back on Discord afterwards", not polbridge.on_fluxer())


def test_delete_on_its_platform():
    print("a reminder is deleted where it was posted")
    store, net = setup()
    store.meta["fluxer-news-webhook:F1"] = json.dumps({"id": "HF1", "token": "T"})
    polbridge._delete_event_post(None, {"plat": "fluxer", "channel": "F1", "id": "m9",
                                        "hook": "HF1"}, "test")
    check("a Fluxer post is deleted through Fluxer's webhook",
          net.calls[-1][:2] == ("DELETE", polbridge.FLUXER_API + "/webhooks/HF1/T/messages/m9"),
          net.calls[-1])
    polbridge._delete_event_post(None, {"channel": "D1", "id": "m8"}, "test")
    check("a post from before Fluxer existed (no plat) is a Discord one",
          net.calls[-1][1] == polbridge.DISCORD_API + "/channels/D1/messages/m8", net.calls[-1])


def test_game_news():
    print("a game's own news channels")
    store, net = setup()
    polbridge.GAME_NEWS[:] = [
        {"plat": "fluxer", "channel": "G1", "content": "tetra", "bot": "tm", "token": "TMTOK"},
        {"plat": "discord", "channel": "GD", "content": "jan", "bot": "jan", "token": "JANTOK"}]
    try:
        polbridge.scan_announce()                       # first pass: baselines only
        ITEMS.append({"serial": 4, "title": "tetra news", "content": "tetra"})
        ITEMS.append({"serial": 5, "title": "jan news", "content": "jan"})
        net.calls.clear()
        polbridge.scan_announce()
        g1 = [(u, b, a) for m, u, b, a in net.calls if m == "POST" and "/channels/G1/" in u]
        gd = [(u, b, a) for m, u, b, a in net.calls if m == "POST" and "/channels/GD/" in u]
        check("the Tetra channel gets the Tetra post only",
              [b["embeds"][0]["title"] for u, b, a in g1] == ["tetra news"], g1)
        check("...posted straight into the channel by the game's own bot, no webhook",
              g1[0][0] == polbridge.FLUXER_API + "/channels/G1/messages" and g1[0][2] == "Bot TMTOK",
              g1[0][:1] + g1[0][2:])
        check("...pinging nobody", "content" not in g1[0][1], g1[0][1])
        check("the Jan channel on Discord gets the Jan post only, as the Jan bot",
              [(b["embeds"][0]["title"], a) for u, b, a in gd] == [("jan news", "Bot JANTOK")]
              and gd[0][0].startswith(polbridge.DISCORD_API), gd)
        check("both serials moved past what they skipped",
              store.meta.get("game-news-serial:fluxer:G1") == "5"
              and store.meta.get("game-news-serial:discord:GD") == "5", store.meta)
        polbridge._delete_event_post(None, {"plat": "fluxer", "channel": "G1", "id": "m7",
                                            "bot": "tm"}, "test")
        check("a game-news post is deleted as the game's bot",
              net.calls[-1][1].endswith("/channels/G1/messages/m7") and net.calls[-1][3] == "Bot TMTOK",
              net.calls[-1])
        check("...and the thread is back to the bridge afterwards",
              not polbridge.on_fluxer() and getattr(polbridge._PLAT, "token", None) is None)
    finally:
        polbridge.GAME_NEWS[:] = []
        del ITEMS[3:]


def test_no_fluxer_token():
    print("no Fluxer bot")
    store, net = setup()
    polbridge.ARGS.fluxer_token = ""
    check("no Fluxer targets at all", [r["plat"] for r in polbridge.announce_targets(None)]
          == ["discord"])


def main():
    print("the bridge on Fluxer\n")
    test_announce()
    test_delete_on_its_platform()
    test_game_news()
    test_no_fluxer_token()
    print("\n%d checks, all PASS" % len(CHECKS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
