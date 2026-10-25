#!/usr/bin/env python3
"""A single-game panel with a deployer for paired games only (POL_DEPLOY_MODE=updaters), driven
in headless Chrome on loopback.

The panel runs without the PlayOnline core (POL_ADMIN_CORE=none), as a game's own setup program
starts it, with a real deployer in updaters mode in front of the stand-in docker of
deployer_test. A game pairs with an updater that offers stable and dev, and the owner approves
it. Checks that:
  * Settings has an Updates tab, not Install games, with no install list and no OpenLobby row;
  * the game's Update button runs its updater through the deployer, and the list says so;
  * the Update channel section shows the game's row and the access code box, without the
    Unlock and Forget buttons, and says the code goes with the game's switch;
  * the page raises no errors and fits a phone.

    python tools/admin_updaters_browser.py [--shots DIR]

Needs Chrome and websocket-client; uses a throwaway PostgreSQL (pgtest).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.abspath(os.path.join(HERE, os.pardir, "services"))
LEGACY_TOOLS = os.path.abspath(os.path.join(HERE, os.pardir, os.pardir, os.pardir,
                                            "legacy", "tools"))

sys.path.insert(0, LEGACY_TOOLS)
try:
    from fe_panel_browser import Browser, CHROME_CANDIDATES  # noqa: E402
except ImportError:
    # a headless-Chrome check for a full checkout; the helper is not in every one
    print("SKIP: needs fe_panel_browser")
    sys.exit(0)
sys.path.insert(0, HERE)
sys.path.insert(0, SERVICES)

# a fresh database, the game stand-in and the owner come from the paired-updates suite
import admin_gameupdate_test as gu  # noqa: E402
import deployer_test as dt  # noqa: E402
from admin_http import Client, free_port, start_panel, stop  # noqa: E402

TMP = tempfile.mkdtemp(prefix="adminupdaters-")
bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


def start_deployer(dock, d):
    port = free_port()
    state = os.path.join(TMP, "deployer-state")
    env = dict(os.environ, POL_DEPLOYER_TOKEN=gu.DEPLOYER_TOKEN, POL_DEPLOY_MODE="updaters",
               POL_DEV_ISSUER="off", FAKE_UPD_DIR=d)
    env.pop("POL_DEPLOY_ROOT", None)
    err = open(os.path.join(TMP, "deployer.log"), "w")
    proc = subprocess.Popen([sys.executable, dt.DEPLOYER, "--bind", "127.0.0.1:%d" % port,
                             "--state", state, "--container", "c0ffee", "--docker", dock,
                             "--minute", "0.2"], env=env, stdout=err, stderr=subprocess.STDOUT)
    c = dt.Client(port)
    t0 = time.time()
    while time.time() - t0 < 15:
        try:
            if c.call("GET", "/api/health", token=gu.DEPLOYER_TOKEN)[0] == 200:
                return proc, "http://127.0.0.1:%d" % port
        except OSError:
            pass
        time.sleep(0.1)
    raise RuntimeError("the deployer did not start")


def main():
    if not any(os.path.exists(c) for c in CHROME_CANDIDATES):
        print("SKIP admin_updaters_browser: no Chrome/Edge on this box")
        return 0
    shots = None
    if "--shots" in sys.argv:
        shots = sys.argv[sys.argv.index("--shots") + 1]
        os.makedirs(shots, exist_ok=True)
    dock, d, folder = dt.updater_tree(types.SimpleNamespace(tmp=TMP))
    dep, dep_url = start_deployer(dock, d)
    game = gu.Game()
    gu.seed()
    env = {"POL_ADMIN_BIND": "127.0.0.1", "POL_ADMIN_CORE": "none", "POL_ADMIN_UPDATER_FOLLOW": "0.2",
           "POL_ADMIN_LIVE_TTL": "0", "POL_ADMIN_GITHUB_API": "http://127.0.0.1:%d" % free_port(),
           "POL_ADMIN_PAIR_TRIES": "1000", "POL_ADMIN_UPDATE_REPO": "off",
           "POL_DEPLOYER_URL": dep_url, "POL_DEPLOYER_TOKEN": gu.DEPLOYER_TOKEN}
    proc, url = start_panel(TMP, SERVICES, free_port(), env_extra=env)
    b = None
    try:
        owner, anon = Client(url), Client(url)
        chk("the owner signs in", owner.login(gu.OWNER, gu.OWNER_PW), 200)
        pc = owner.call("api/pair/code", {})[1]["code"]
        updater = {"folder": folder, "command": ["python", "ok.py", "--yes"], "timeout_minutes": 30,
                   "channels": ["stable", "dev"]}
        code, r = anon.call("api/pair", {"code": pc, "key": "kart", "title": "Kart Club",
                                         "url": "http://127.0.0.1:%d" % game.port, "token": gu.TOKEN,
                                         "updater": updater})
        chk("Kart Club pairs with an updater", (code, r.get("updater")), (200, "requested"))
        added = {a["key"]: a for a in owner.call("api/modules?fresh=1")[1].get("added", [])}
        owner.call("api/modules/game", {"action": "approve_updater", "key": "kart",
                                        "id": added["kart"]["updater"]["id"]})
        game.update = dict(gu.report(), apply=False)
        chk("approved: the deployer runs it", (gu.item(owner) or {}).get("via"), "updater")

        b = Browser(width=1440, height=1000)
        b.goto(url, settle=1.2)
        b.js("document.querySelector('#u').value=%s;document.querySelector('#p').value=%s"
             % (json.dumps(gu.OWNER), json.dumps(gu.OWNER_PW)))
        b.click_el("#go")
        b.pump(3.0)
        b.errors.clear()
        b.js("window.confirm = () => true")
        b.goto(url + "#deploy", settle=3.0)
        b.js("window.confirm = () => true")
        chk("Settings has an Updates tab", b.js("document.querySelector('#deployTab').textContent"), "Updates")
        chk("  with no install list", b.js("document.querySelector('#deployPanel').hidden"), True)
        chk("  and no note about adding a deployer", b.js("document.querySelector('#deployOff').hidden"), True)
        chk("  no OpenLobby row offers a deployer update",
            b.js("!!document.querySelector('[data-upd-go=\"core\"]')"), False)
        chk("  Kart Club has an Update button", b.js("!!document.querySelector('[data-upd-run=\"kart\"]')"), True)
        chk("the Update channel section shows", b.js("document.querySelector('#chanPanel').hidden"), False)
        chk("  with Kart Club's row",
            b.js("!!document.querySelector('#chanBody [data-ck=\"kart\"]')"), True)
        chk("  the access code box, open",
            [b.js("document.querySelector('#chanForm').hidden"), b.js("document.querySelector('#chanCode').offsetParent !== null")],
            [False, True])
        chk("  and no Unlock, Forget or Use this code",
            [b.js("document.querySelector('%s').hidden" % s) for s in ("#chanUnlock", "#chanLock", "#chanGo")],
            [True, True, True])
        chk("  it says the code goes with the game's switch",
            "pick Dev on the game's row" in (b.js("document.querySelector('#chanHint').textContent") or ""), True)
        if shots:
            b.call("Emulation.setDeviceMetricsOverride", width=1440,
                   height=int(b.js("document.body.scrollHeight")), deviceScaleFactor=1, mobile=False)
            b.screenshot(os.path.join(shots, "updaters-desktop.png"))
            b.call("Emulation.setDeviceMetricsOverride", width=1440, height=1000,
                   deviceScaleFactor=1, mobile=False)
        b.click_el('[data-upd-run="kart"]')
        t0 = time.time()
        jobs = []
        while time.time() - t0 < 60:
            b.pump(0.5)
            jobs = owner.call("api/deploy")[1].get("jobs") or []
            if jobs and jobs[0].get("state") in ("ok", "failed"):
                break
        chk("Update runs the updater through the deployer",
            [(j.get("action"), j.get("game"), j.get("state")) for j in jobs[:1]], [("updater", "kart", "ok")])
        b.pump(3.0)
        b.call("Emulation.setDeviceMetricsOverride", width=390, height=900, deviceScaleFactor=1, mobile=True)
        b.pump(1.0)
        chk("on a phone nothing is wider than the screen",
            b.js("document.documentElement.scrollWidth <= window.innerWidth"), True)
        if shots:
            b.call("Emulation.setDeviceMetricsOverride", width=390,
                   height=int(b.js("document.body.scrollHeight")), deviceScaleFactor=1, mobile=True)
            b.screenshot(os.path.join(shots, "updaters-phone.png"))
        chk("the page raised no errors", b.errors, [])
    finally:
        if b:
            b.close()
        stop(proc)
        game.stop()
        dep.terminate()
        try:
            dep.wait(10)
        except subprocess.TimeoutExpired:
            dep.kill()
        shutil.rmtree(TMP, ignore_errors=True)
    print("admin_updaters_browser: %d failed" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
