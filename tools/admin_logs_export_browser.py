#!/usr/bin/env python3
"""Settings > Logs (adminlogs.py), driven in headless Chrome on loopback, from both sources.

With a deployer (a stand-in answering /api/logs/areas and /api/logs/export, as
deployer/logexport.py does): the tab lists its areas under Everything, offers the line count,
and a download is the deployer's text with the area, lines and addresses choice passed on. A
moderator gets neither route.

Without one (prod): the host runner's status lists its log groups, the patch service among
them, and a stand-in for pol-ops answers log requests by writing the group's logs. The tab hides
the line count; a download asks for each group, waits for it, and masks addresses unless asked.

    python tools/admin_logs_export_browser.py [--shots DIR]

Needs Chrome and websocket-client; uses a throwaway PostgreSQL (pgtest).
"""
import glob
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.abspath(os.path.join(HERE, os.pardir, "services"))
LEGACY_TOOLS = os.path.abspath(os.path.join(HERE, os.pardir, os.pardir, os.pardir, "legacy", "tools"))

sys.path.insert(0, LEGACY_TOOLS)
try:
    from fe_panel_browser import Browser, CHROME_CANDIDATES  # noqa: E402
except ImportError:
    print("SKIP: needs fe_panel_browser")
    sys.exit(0)
sys.path.insert(0, HERE)
sys.path.insert(0, SERVICES)

import pgtest  # noqa: E402

pgtest.use_fresh_database()
for _k in ("POL_VALKEY_URL", "POL_DEPLOYER_URL", "POL_DEPLOYER_TOKEN"):
    os.environ.pop(_k, None)

import accounts  # noqa: E402
import adminusers  # noqa: E402
from admin_http import Client, free_port, start_panel, stop  # noqa: E402

OWNER, OWNER_PW = "op", "correct horse 1"
DTOKEN = "deployer-token-for-logs-0001"
bad = 0
ASKED = []


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got, "" if ok else "  (want %r)" % (want,)))


class Deployer(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer " + DTOKEN:
            return self.reply(401, {"error": "token"})
        from urllib.parse import urlsplit, parse_qs
        u = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/api/health":
            return self.reply(200, {"ok": True, "version": "3.0", "busy": False, "paths": "ok"})
        if u.path == "/api/logs/areas":
            return self.reply(200, {"areas": [{"id": "patch", "name": "Patch service", "services": ["patch"]},
                                              {"id": "ffxi", "name": "Final Fantasy XI", "services": ["world"]}]})
        if u.path == "/api/logs/export":
            ASKED.append(q)
            return self.reply(200, ("OpenLobby logs\n-- patch\ncmd7 PS2/0001 ver='20160203_0' area=%s lines=%s addresses=%s\n"
                                    % (q.get("area"), q.get("lines"), q.get("addresses"))).encode(), "text/plain")
        self.reply(404, {"error": "not found"})


def ops_runner(data_dir, stop_ev):
    """pol-ops, as far as the logs go: answer each request in data/ops/logreq with its group's logs."""
    ops = os.path.join(data_dir, "ops")
    while not stop_ev.is_set():
        with open(os.path.join(ops, "status.json"), "w") as f:
            json.dump({"at": time.time(), "checks": [{"id": "login", "name": "PlayOnline sign-in and lobby",
                                                      "state": "ok"}],
                       "logs": ["login", "patch"], "actions": {}, "updates": {}, "history": []}, f)
        for p in glob.glob(os.path.join(ops, "logreq", "*.json")):
            with open(p) as f:
                g = json.load(f)["group"]
            os.remove(p)
            os.makedirs(os.path.join(ops, "logs"), exist_ok=True)
            with open(os.path.join(ops, "logs", g + ".json"), "w") as f:
                json.dump({"group": g, "at": time.time(), "by": "op", "containers": [
                    {"name": "hippaul-%s-1" % g, "lines": ["2026-10-25T08:00:00Z %s ok from 198.51.100.7" % g]}]}, f)
        time.sleep(0.3)


def run_panel(env_extra, tmp, label, shots, ops=False):
    proc, url = start_panel(tmp, SERVICES, free_port(), env_extra=env_extra)
    b = None
    try:
        mod = Client(url)
        chk("%s: a moderator signs in" % label, mod.login("mod_all", "pass for mod_all"), 200)
        chk("  and gets neither route", (mod.call("api/logexport/areas")[0], mod.call("api/logexport?area=all")[0]),
            (403, 403))
        b = Browser(width=1300, height=800)
        b.goto(url, settle=1.2)
        b.js("document.querySelector('#u').value=%s;document.querySelector('#p').value=%s"
             % (json.dumps(OWNER), json.dumps(OWNER_PW)))
        b.click_el("#go")
        b.pump(3.0)
        b.errors.clear()
        b.goto(url + "#logs", settle=2.5)
        if shots:
            b.screenshot(os.path.join(shots, "logs-%s.png" % ("ops" if ops else "deployer")))
        opts = b.js("[...document.querySelectorAll('#logArea option')].map(o=>o.textContent)")
        lines_shown = b.js("!document.querySelector('#logLines').hidden")
        b.js("document.querySelector('#logArea').value='patch'")
        if not ops:
            b.js("document.querySelector('#logLines').value='10000'")
        b.js("window.__dl=null;const o=URL.createObjectURL;URL.createObjectURL=(blob)=>{blob.text().then(t=>window.__dl=t);return o(blob)};"
             "HTMLAnchorElement.prototype.click=function(){window.__name=this.download}")
        b.click_el("#logGet")
        t0 = time.time()
        while time.time() - t0 < 30 and not b.js("window.__dl"):
            b.pump(0.3)
        text, name = b.js("window.__dl") or "", b.js("window.__name") or ""
        chk("  no page errors", b.errors, [])
        return opts, lines_shown, text, name
    finally:
        if b:
            b.close()
        stop(proc)


def main():
    if not any(os.path.exists(c) for c in CHROME_CANDIDATES):
        print("SKIP admin_logs_export_browser: no Chrome/Edge on this box")
        return 0
    shots = None
    if "--shots" in sys.argv:
        shots = sys.argv[sys.argv.index("--shots") + 1]
        os.makedirs(shots, exist_ok=True)
    d = accounts.connect()
    accounts.set_admin_cred(d, OWNER, OWNER_PW)
    d.commit()
    adminusers.create_mod(d, "mod_all", "pass for mod_all", ["audit", "health", "settings"], by=OWNER)
    d.close()

    print("with a deployer")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Deployer)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tmp = tempfile.mkdtemp(prefix="adminlogs-dep-")
    try:
        opts, lines_shown, text, name = run_panel(
            {"POL_DEPLOYER_URL": "http://127.0.0.1:%d" % srv.server_address[1], "POL_DEPLOYER_TOKEN": DTOKEN},
            tmp, "deployer", shots)
        chk("  the deployer's areas under Everything", opts, ["Everything", "Patch service", "Final Fantasy XI"])
        chk("  the line count is offered", lines_shown, True)
        chk("  the download is the deployer's text, with the choices passed on",
            "area=patch lines=10000 addresses=0" in text, True)
        chk("  saved under a file name that says what it is", name.startswith("openlobby-logs-patch-"), True)
    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print("without one: the host's ops runner")
    tmp = tempfile.mkdtemp(prefix="adminlogs-ops-")
    os.makedirs(os.path.join(tmp, "ops"))
    stop_ev = threading.Event()
    threading.Thread(target=ops_runner, args=(tmp, stop_ev), daemon=True).start()
    time.sleep(0.5)
    try:
        opts, lines_shown, text, name = run_panel({}, tmp, "ops", shots, ops=True)
        chk("  the runner's groups, the patch service named", opts,
            ["Everything", "PlayOnline sign-in and lobby", "Patch service"])
        chk("  no line count (the runner keeps a fixed tail)", lines_shown, False)
        chk("  the download has the patch service's lines", "patch ok from" in text, True)
        chk("  with the address hidden", ("198.51.100.7" in text, "<address>" in text), (False, True))
    finally:
        stop_ev.set()
        shutil.rmtree(tmp, ignore_errors=True)
    print("admin_logs_export_browser: %d failed" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
