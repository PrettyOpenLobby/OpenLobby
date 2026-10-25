#!/usr/bin/env python3
"""A game's patches on its own page in the admin panel, driven in headless Chrome on loopback.

A made-up patch versions service (the shape of prod's patchinfo: GET versions.json with
polpatch_lib.provenance rows) stands behind a "patches" module's tool port, as on a server
with no deployer. Checks that:
  * Final Fantasy XI's page lists only its bundles (products 0001 and 0015), not another
    game's, each with the version it serves as newest and what that version is;
  * a row opens to the whole timeline: retail, ours and the retail versions not here;
  * the Viewer's page shows the Viewer and friend list bundles;
  * a game with no bundles shows no Patches section, and one the moderator cannot see
    answers 404;
  * the page raises no errors and fits a phone.

    python tools/admin_patches_browser.py [--shots DIR]

Needs Chrome and websocket-client; uses a throwaway PostgreSQL (pgtest).
"""
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.abspath(os.path.join(HERE, os.pardir, "services"))
LEGACY_TOOLS = os.path.abspath(os.path.join(HERE, os.pardir, os.pardir, os.pardir,
                                            "legacy", "tools"))

TMP = tempfile.mkdtemp(prefix="adminpatches-")

sys.path.insert(0, LEGACY_TOOLS)
try:
    from fe_panel_browser import Browser, CHROME_CANDIDATES  # noqa: E402
except ImportError:
    # a headless-Chrome check for a full checkout; the helper is not in every one
    print("SKIP: needs fe_panel_browser")
    sys.exit(0)
sys.path.insert(0, HERE)
sys.path.insert(0, SERVICES)

import pgtest  # noqa: E402

pgtest.use_fresh_database()
for _k in ("POL_VALKEY_URL", "POL_DEPLOYER_URL", "POL_DEPLOYER_TOKEN", "POL_ADMIN_MODULES"):
    os.environ.pop(_k, None)

import accounts  # noqa: E402
import adminusers  # noqa: E402
from admin_http import Client, free_port, start_panel, stop  # noqa: E402

OWNER, OWNER_PW = "op", "correct horse 1"
TOKEN = "patchinfo-token-0123456789"
WHY = {"se-final-list": "named by Square Enix's final patch list for it",
       "se-server": "listed by Square Enix's own patch server when it was mirrored"}


def v(ver, origin, src=None):
    return {"version": ver, "origin": origin, "source": src, "why": WHY.get(src)}


VERSIONS = {"at": "2026-10-24 20:00 UTC", "sources": WHY, "bundles": [
    {"region": "PS2", "product": "0001", "title": "FINAL FANTASY XI (PlayStation 2, Japan)",
     "latest": "20261009_14", "final_list": True, "mirrored": True, "note": None, "discs": [],
     "versions": [v("20151103_0", "retail", "se-final-list"), v("20160203_0", "retail", "se-final-list"),
                  v("20261009_14", "ours")],
     "missing": [v("20151204_0", "retail", "se-final-list")]},
    {"region": "W20", "product": "0001", "title": "FINAL FANTASY XI (Windows, Japan)",
     "latest": "20160203_0", "final_list": False, "mirrored": True, "note": None, "discs": [],
     "versions": [v("20160203_0", "retail", "se-server")], "missing": []},
    {"region": "W20", "product": "0015", "title": "FINAL FANTASY XI Test Server (Windows, Japan)",
     "latest": "20150101_0", "final_list": False, "mirrored": False, "note": None, "discs": [],
     "versions": [v("20150101_0", "unknown")], "missing": []},
    {"region": "PS2", "product": "1000", "title": "PlayOnline Viewer (PlayStation 2, Japan)",
     "latest": "20160301_0", "final_list": False, "mirrored": True, "note": None, "discs": [],
     "versions": [v("20160301_0", "retail", "se-server")], "missing": []},
    {"region": "W20", "product": "0014", "title": "PlayOnline Friend List (Windows, Japan)",
     "latest": "20120101_0", "final_list": False, "mirrored": True, "note": None, "discs": [],
     "versions": [v("20120101_0", "retail", "se-server")], "missing": []},
    {"region": "PS2", "product": "0002", "title": "Tetra Master (PlayStation 2, Japan)",
     "latest": "20050101_0", "final_list": False, "mirrored": True, "note": None, "discs": [],
     "versions": [v("20050101_0", "retail", "se-server")], "missing": []},
]}
bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


class Patchinfo(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.headers.get("X-Devtool-Token") != TOKEN:
            self.send_response(401)
            self.end_headers()
            return
        body = json.dumps(VERSIONS).encode() if self.path.split("?")[0].strip("/") == "versions.json" else b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    if not any(os.path.exists(c) for c in CHROME_CANDIDATES):
        print("SKIP admin_patches_browser: no Chrome/Edge on this box")
        return 0
    shots = None
    if "--shots" in sys.argv:
        shots = sys.argv[sys.argv.index("--shots") + 1]
        os.makedirs(shots, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Patchinfo)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    mods = os.path.join(TMP, "modules")
    os.makedirs(mods)
    with open(os.path.join(mods, "patches.py"), "w", encoding="utf-8") as f:
        f.write('from adminmodules import Module, Tool\n'
                'MODULE = Module("patches", "Patch versions", kind="platform", present=True,\n'
                '                tools=[Tool("versions", "Patch versions", "")])\n')
    d = accounts.connect()
    accounts.set_admin_cred(d, OWNER, OWNER_PW)
    d.commit()
    adminusers.create_mod(d, "mod_tm", "pass for mod_tm", ["audit"], by=OWNER)
    d.close()
    d = accounts.connect()
    d.execute("UPDATE admin_moderator SET perms = 'game.tm' WHERE username = 'mod_tm'")
    d.commit()
    d.close()
    env = {"POL_ADMIN_MODULES_DIR": mods, "POL_ADMIN_MODULES": "viewer,ffxi,tm,jan,patches",
           "POL_ADMIN_GAME_PATCHES_URL": "http://127.0.0.1:%d" % srv.server_address[1],
           "POL_ADMIN_GAME_PATCHES_TOKEN": TOKEN}
    proc, url = start_panel(TMP, SERVICES, free_port(), env_extra=env)
    b = None
    try:
        mod = Client(url)
        chk("a moderator signs in", mod.login("mod_tm", "pass for mod_tm"), 200)
        chk("  a game they cannot see: 404", mod.call("api/games/patches?key=ffxi")[0], 404)
        st, r = mod.call("api/games/patches?key=tm")
        chk("  their own game: its one bundle", (st, [x["product"] for x in r.get("bundles", [])]), (200, ["0002"]))

        b = Browser(width=1400, height=1000)
        b.goto(url, settle=1.2)
        b.js("document.querySelector('#u').value=%s;document.querySelector('#p').value=%s"
             % (json.dumps(OWNER), json.dumps(OWNER_PW)))
        b.click_el("#go")
        b.pump(3.0)
        b.errors.clear()
        b.goto(url + "#games/ffxi", settle=1.0)
        box = "document.querySelector('.gx-patches')"
        import time
        t0 = time.time()
        while time.time() - t0 < 15 and not b.js("!!%s" % box):
            b.pump(0.25)
        print("   (the section appeared after %.1f s)" % (time.time() - t0))
        chk("FFXI's page has a Patches section", b.js("!!%s" % box), True)
        chk("  one row per bundle of its products, none of another game's",
            b.js("[...%s.querySelectorAll('tr[data-gp]')].map(r=>r.dataset.gp)" % box),
            ["ffxi:PS2-0001", "ffxi:W20-0001", "ffxi:W20-0015"])
        ps2 = "%s.querySelector('tr[data-gp=\"ffxi:PS2-0001\"]')" % box
        chk("  the PS2 row: what it serves, that it is ours, and the retail version not here",
            [c.strip() for c in (b.js("[...%s.cells].map(c=>c.innerText)" % ps2) or [])],
            ["▸ FINAL FANTASY XI (PlayStation 2, Japan) PS2-0001", "20261009_14 Ours",
             "3 (2 retail, 1 ours)", "1 not here"])
        if shots:
            b.screenshot(os.path.join(shots, "ffxi-patches.png"))
        b.click_el('tr[data-gp="ffxi:PS2-0001"]')
        b.pump(0.6)
        body = "document.querySelector('[data-gpbody=\"ffxi:PS2-0001\"]')"
        chk("a click opens the timeline, newest first, the missing one in place",
            b.js("[...%s.querySelectorAll('table > tbody > tr')].map(r=>r.cells[0].innerText+' '+r.cells[1].innerText)" % body),
            ["20261009_14 Ours", "20160203_0 Retail", "20151204_0 Not here", "20151103_0 Retail"])
        b.pump(16.0)       # the page's own status redraw
        chk("  it stays open through the page's redraw", b.js("%s.hidden" % body), False)
        if shots:
            b.call("Emulation.setDeviceMetricsOverride", width=1400,
                   height=int(b.js("document.body.scrollHeight")), deviceScaleFactor=1, mobile=False)
            b.screenshot(os.path.join(shots, "ffxi-patches-open.png"))
            b.call("Emulation.setDeviceMetricsOverride", width=1400, height=1000,
                   deviceScaleFactor=1, mobile=False)
        b.goto(url + "#games/viewer", settle=3.0)
        chk("the Viewer's page: the Viewer and the friend list",
            b.js("[...document.querySelectorAll('.gx-patches tr[data-gp]')].map(r=>r.dataset.gp)"),
            ["viewer:PS2-1000", "viewer:W20-0014"])
        b.goto(url + "#games/jan", settle=3.0)
        chk("a game with no bundles has no Patches section", b.js("!!document.querySelector('.gx-patches')"), False)
        b.goto(url + "#games/ffxi", settle=3.0)
        b.call("Emulation.setDeviceMetricsOverride", width=390, height=900, deviceScaleFactor=1, mobile=True)
        b.pump(1.0)
        chk("on a phone nothing is wider than the screen",
            b.js("document.documentElement.scrollWidth <= window.innerWidth"), True)
        chk("the page raised no errors", b.errors, [])
    finally:
        if b:
            b.close()
        stop(proc)
        srv.shutdown()
        shutil.rmtree(TMP, ignore_errors=True)
    print("admin_patches_browser: %d failed" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
