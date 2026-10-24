#!/usr/bin/env python3
"""The admin panel's Bots tab (Discord and Fluxer), driven in headless Chrome on loopback.

Seeds one board with a Fluxer bot already checked, then checks that:
  * the tab is called Bots, a switch picks Discord or Fluxer (remembered), and a card shows
    only that platform: what needs doing, its passing checks and its fields folded away;
  * the Fluxer side names the channel it posts in and lists the board's feeds;
  * the PlayOnline bridge's Fluxer side has its news channel and per-game ping roles;
  * a channel typed into a feed and saved from the page is what the registry holds
    afterwards, and the token is never in the page;
  * the page raises no errors, and the cards fit a phone's width.

Fluxer itself is a dead loopback port here, so a save checks nothing and keeps the channels.

    python tools/admin_bots_browser.py [--shots DIR]

Needs Chrome and websocket-client; uses a throwaway PostgreSQL (pgtest).
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.abspath(os.path.join(HERE, os.pardir, "services"))
LEGACY_TOOLS = os.path.abspath(os.path.join(HERE, os.pardir, os.pardir, os.pardir,
                                            "legacy", "tools"))

TMP = tempfile.mkdtemp(prefix="adminbots-")

sys.path.insert(0, LEGACY_TOOLS)
try:
    from fe_panel_browser import Browser, CHROME_CANDIDATES  # noqa: E402
except ImportError:
    # a headless-Chrome check for a full checkout; the helper is not in every one
    print("SKIP: needs fe_panel_browser")
    sys.exit(0)
# the browser helper puts its own services directory first; this panel's code wins.
sys.path.insert(0, HERE)
sys.path.insert(0, SERVICES)

import pgtest  # noqa: E402

pgtest.use_fresh_database()
for _k in ("POL_VALKEY_URL", "POL_BOT_EXTRA"):
    os.environ.pop(_k, None)
os.environ["POL_BOT_KEY"] = "bots-browser-key"

import accounts  # noqa: E402
from admin_http import free_port, start_panel, stop  # noqa: E402
from polcore import botcreds, db  # noqa: E402

OWNER, OWNER_PW = "op", "correct horse 1"
FTOKEN = "flx.fake.token-tm-0002"
CHAN, CHAN2 = "1563415603597807616", "1563415603597807999"
bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


def seed():
    d = accounts.connect()
    accounts.set_admin_cred(d, OWNER, OWNER_PW)
    d.commit()
    d.close()
    db.migrate()
    info = {"user_id": "1563413322722713600", "username": "Tetra_Master", "app_name": "Tetra Master",
            "public": True, "guilds": ["OpenLobby"], "checked_at": 1.0,
            "channels": {CHAN: {"name": "game-status", "guild": "OpenLobby"}}}
    botcreds.save("tm", OWNER, fluxer_token=FTOKEN, fluxer_channels=json.dumps({"tm": CHAN}),
                  fluxer_info=json.dumps(info))


def main():
    if not any(os.path.exists(c) for c in CHROME_CANDIDATES):
        print("SKIP admin_bots_browser: no Chrome/Edge on this box")
        return 0
    shots = None
    if "--shots" in sys.argv:
        shots = sys.argv[sys.argv.index("--shots") + 1]
        os.makedirs(shots, exist_ok=True)
    seed()
    env = {"POL_BOT_KEY": os.environ["POL_BOT_KEY"],
           "POL_BOARDS_FLUXER_API": "http://127.0.0.1:%d/v1" % free_port()}
    proc, url = start_panel(TMP, SERVICES, free_port(), env_extra=env)
    b = None
    try:
        b = Browser(width=1400, height=1000)
        b.goto(url, settle=1.2)
        b.js("document.querySelector('#u').value=%s;document.querySelector('#p').value=%s"
             % (json.dumps(OWNER), json.dumps(OWNER_PW)))
        b.click_el("#go")
        b.pump(3.0)
        b.errors.clear()
        b.goto(url + "#bots", settle=2.5)
        if shots:
            b.call("Emulation.setDeviceMetricsOverride", width=1400,
                   height=int(b.js("document.body.scrollHeight")), deviceScaleFactor=1, mobile=False)
            b.screenshot(os.path.join(shots, "bots.png"))
            b.call("Emulation.setDeviceMetricsOverride", width=1400, height=1000,
                   deviceScaleFactor=1, mobile=False)
        chk("the tab is called Bots",
            b.js("document.querySelector('button[data-tab=\"bots\"]').innerText.trim()"), "Bots")
        chk("and so is its heading", b.js("document.querySelector('#tab-bots h2').innerText.trim()"), "Bots")
        cards = "[...document.querySelectorAll('#botsList [data-bot]')]"
        tm = "document.querySelector('[data-bot=\"tm\"]')"
        view = "document.querySelector('#botsView .on').dataset.view"
        chk("it opens on the Discord view", b.js(view), "discord")
        chk("  one platform per card: no Fluxer fields anywhere",
            b.js("document.querySelectorAll('#botsList .fluxChan, #botsList .fluxToken').length"), 0)
        chk("  the switch counts the bots set up on each",
            b.js("[...document.querySelectorAll('#botsView .cnt')].map(e=>e.textContent)"),
            ["0/%d" % b.js("%s.length" % cards), "1/%d" % b.js("%s.length" % cards)])
        b.click_el('[data-bot="tm"] .bc-edit > summary')
        b.pump(0.5)
        chk("a news board's Discord settings have a Game news field",
            b.js("!!%s.querySelector('.botNews')" % tm), True)
        chk("  the bridge's do not (it posts the news itself)",
            b.js("!!document.querySelector('[data-bot=\"bridge\"] .botNews')"), False)
        if shots:
            b.js("%s.scrollIntoView()" % tm)
            b.screenshot(os.path.join(shots, "bots-discord-news.png"))
        b.click_el('[data-bot="tm"] .bc-edit > summary')
        b.pump(0.3)
        b.click_el('#botsView button[data-view="fluxer"]')
        b.pump(0.8)
        if shots:
            b.call("Emulation.setDeviceMetricsOverride", width=1400,
                   height=int(b.js("document.body.scrollHeight")), deviceScaleFactor=1, mobile=False)
            b.screenshot(os.path.join(shots, "bots-fluxer.png"))
            b.call("Emulation.setDeviceMetricsOverride", width=1400, height=1000,
                   deviceScaleFactor=1, mobile=False)
        chk("the switch shows the Fluxer view", (b.js(view), b.js("%s.querySelector('.pill').innerText" % tm)),
            ("fluxer", "Restarting onto the new values"))
        chk("  with its own how-to, the Discord one hidden",
            b.js("[...document.querySelectorAll('#tab-bots [data-for]')].map(e=>e.dataset.for+':'+e.hidden)"),
            ["discord:true", "fluxer:false"])
        chk("  no Discord fields now", b.js("document.querySelectorAll('#botsList .botToken').length"), 0)
        text = b.js("%s.innerText" % tm) or ""
        chk("tm's card says who it signs in as", "Tetra_Master" in text, True)
        chk("  its passing checks fold into one line naming how many",
            b.js("%s.querySelector('.bc-ok summary').innerText.trim().split(',')[0]" % tm), "3 checks pass")
        chk("  set up, so its Settings are folded", b.js("%s.querySelector('.bc-edit').open" % tm), False)
        chk("  every card's Settings start folded",
            b.js("document.querySelectorAll('#botsList .bc-edit[open]').length"), 0)
        b.click_el('[data-bot="tm"] .bc-edit > summary')
        b.pump(0.5)
        chk("Settings opens on a click", b.js("%s.querySelector('.bc-edit').open" % tm), True)
        b.click_el('[data-bot="tm"] .bc-ok > summary')
        b.pump(0.5)
        chk("  and the passing checks unfold to name the channel",
            "#game-status in OpenLobby" in (b.js("%s.innerText" % tm) or ""), True)
        chk("  one channel field per feed",
            b.js("[...%s.querySelectorAll('.fluxChan')].map(e=>e.dataset.key)" % tm),
            ["tm", "tm_auction", "tm_live", "tm_news"])
        chk("  the stored channel is filled in",
            b.js("%s.querySelector('.fluxChan[data-key=\"tm\"]').value" % tm), CHAN)
        br = "document.querySelector('[data-bot=\"bridge\"]')"
        keys = b.js("[...%s.querySelectorAll('.fluxChan')].map(e=>e.dataset.key)" % br) or []
        chk("the bridge's Fluxer side has its news channel and a ping role per game",
            (keys[:1], "role:tetra" in keys), (["announce"], True))
        b.click_el('[data-bot="bridge"] .bc-edit > summary')
        b.pump(0.5)
        chk("  its ping roles sit in a grid, several to a row",
            b.js("(g=>getComputedStyle(g).gridTemplateColumns.split(' ').length)"
                 "(document.querySelector('[data-bot=\"bridge\"] .bc-roles'))") >= 2, True)
        if shots:
            b.call("Emulation.setDeviceMetricsOverride", width=1400,
                   height=int(b.js("document.body.scrollHeight")), deviceScaleFactor=1, mobile=False)
            b.screenshot(os.path.join(shots, "bots-bridge.png"))
            b.call("Emulation.setDeviceMetricsOverride", width=1400, height=1000,
                   deviceScaleFactor=1, mobile=False)
        chk("  and names its .env fallbacks",
            "POL_BRIDGE_FLUXER_ANNOUNCE_ROLES" in (b.js("%s.innerHTML" % br) or ""), True)
        chk("the token is nowhere in the page", FTOKEN in (b.js("document.body.innerHTML") or ""), False)

        b.js("%s.querySelector('.fluxChan[data-key=\"tm_auction\"]').value=%s" % (tm, json.dumps(CHAN2)))
        b.click_el('[data-bot="tm"] button[data-act="fluxer-save"]')
        b.pump(3.0)
        row = botcreds.get("tm")
        chk("Save from the page: the registry holds the new feed's channel, the rest kept",
            botcreds.fluxer_channels(row), {"tm": CHAN, "tm_auction": CHAN2})
        chk("  and the token is unchanged", row["fluxer_token"], FTOKEN)
        chk("  the card shows it after the reload",
            b.js("document.querySelector('[data-bot=\"tm\"] .fluxChan[data-key=\"tm_auction\"]').value"), CHAN2)
        chk("  still on the Fluxer view, its Settings still open",
            (b.js(view), b.js("%s.querySelector('.bc-edit').open" % tm)), ("fluxer", True))
        b.goto(url + "#bots", settle=2.5)
        chk("the view is remembered after a page load", b.js(view), "fluxer")

        b.call("Emulation.setDeviceMetricsOverride", width=390, height=900, deviceScaleFactor=1, mobile=True)
        b.pump(1.0)
        chk("on a phone nothing is wider than the screen",
            b.js("document.documentElement.scrollWidth <= window.innerWidth"), True)
        if shots:
            b.call("Emulation.setDeviceMetricsOverride", width=390,
                   height=int(b.js("document.body.scrollHeight")), deviceScaleFactor=1, mobile=True)
            b.screenshot(os.path.join(shots, "bots-phone.png"))
        chk("the page raised no errors", b.errors, [])
    finally:
        if b:
            b.close()
        stop(proc)
        shutil.rmtree(TMP, ignore_errors=True)
    print("admin_bots_browser: %d failed" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
