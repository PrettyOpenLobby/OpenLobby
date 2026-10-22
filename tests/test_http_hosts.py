#!/usr/bin/env python3
"""Hosts other installs serve through this port 80 (stub.py http-hosts, docs/installs.md).

    python tests/test_http_hosts.py

A game's setup adds its host names and its upstream under its key; the web server passes
those requests to the game, whole, without a restart. Bad keys, upstreams and names are
refused and write nothing, and no install can take the PlayOnline names.
"""
import contextlib
import io
import os
import sys
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))

import stub  # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


def cli(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = stub.http_hosts_cli(list(args), folder=folder)
    return code, out.getvalue() + err.getvalue()


folder = os.path.join(tempfile.mkdtemp(prefix="httphosts-"), "http.d")
hosts = stub.HttpHosts(folder, every=0)

print("an install adds its hosts")
chk("nothing yet", hosts.routes(), {})
chk("add answers 0", cli("add", "kart", "http://kart:8080", "Patch.Kart.Example", "www.kart.example.")[0], 0)
chk("its hosts, lower case, to its upstream", hosts.routes(),
    {"patch.kart.example": ("kart", 8080), "www.kart.example": ("kart", 8080)})
chk("another install's hosts add to them",
    (cli("add", "bowl", "http://bowl:80/", "bowl.example")[0], sorted(hosts.routes())),
    (0, ["bowl.example", "patch.kart.example", "www.kart.example"]))
chk("adding again under the same key replaces that key's hosts",
    (cli("add", "kart", "http://kart:9090", "new.kart.example")[0], hosts.routes()),
    (0, {"bowl.example": ("bowl", 80), "new.kart.example": ("kart", 9090)}))
code, out = cli("list")
chk("list shows each key", (code, sorted(out.split("\n")[:2])),
    (0, ["bowl: bowl.example -> http://bowl:80", "kart: new.kart.example -> http://kart:9090"]))

print("refused, and nothing written")
before = sorted(os.listdir(folder))
chk("a key that is not one", cli("add", "../etc", "http://kart:80", "a.example")[0], 2)
chk("an upstream that is not http://host:port", cli("add", "kart", "kart:80", "a.example")[0], 2)
chk("an upstream with a path", cli("add", "kart", "http://kart:80/x", "a.example")[0], 2)
chk("a name that is not a host name", cli("add", "kart", "http://kart:80", "bad name")[0], 2)
chk("no names", cli("add", "kart", "http://kart:80")[0], 2)
chk("a PlayOnline name", cli("add", "kart", "http://kart:80", "wh000.pol.com")[0], 2)
chk("an unknown command", cli("wipe")[0], 2)
chk("...no file changed", sorted(os.listdir(folder)), before)

print("a hand-written file cannot take a PlayOnline name either")
with open(os.path.join(folder, "evil.txt"), "w", encoding="utf-8") as fh:
    fh.write("http://evil:80\nwww.playonline.com\nevil.example\n")
chk("only its own host", sorted(h for h, u in hosts.routes().items() if u[0] == "evil"),
    ["evil.example"])
os.remove(os.path.join(folder, "evil.txt"))


class Game(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        page = ("game saw %s %s from %s" % (self.headers["Host"], self.path,
                                             self.headers.get("X-Forwarded-For"))).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)


game = ThreadingHTTPServer(("127.0.0.1", 0), Game)
threading.Thread(target=game.serve_forever, daemon=True).start()
web = ThreadingHTTPServer(("127.0.0.1", 0), stub.StubHandler)
threading.Thread(target=web.serve_forever, daemon=True).start()
stub.StubHandler.install_hosts = hosts
cli("add", "kart", "http://127.0.0.1:%d" % game.server_address[1], "patch.kart.example")


def fetch(host, path="/"):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (web.server_address[1], path),
                                 headers={"Host": host})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, ""


print("the web server passes the game's hosts on")
chk("the game answers its host, path and all", fetch("patch.kart.example", "/v1/list.txt?x=1"),
    (200, "game saw patch.kart.example /v1/list.txt?x=1 from 127.0.0.1"))
chk("a host with a port still matches", fetch("patch.kart.example:80")[0], 200)
cli("add", "kart", "http://127.0.0.1:1", "patch.kart.example")
chk("a game that is down is a 502", fetch("patch.kart.example")[0], 502)

print("an install leaves")
chk("remove answers 0", cli("remove", "kart")[0], 0)
chk("its hosts are gone", "patch.kart.example" in hosts.routes(), False)
chk("removing a key that is not there is fine", cli("remove", "kart")[0], 0)

print("a missing folder is no hosts, not an error")
chk("nothing to read", stub.HttpHosts(os.path.join(folder, "nope"), every=0).routes(), {})

game.shutdown()
web.shutdown()
print("all checks passed" if not bad else "%d check(s) FAILED" % bad)
sys.exit(1 if bad else 0)
