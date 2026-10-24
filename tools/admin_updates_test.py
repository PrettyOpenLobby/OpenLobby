#!/usr/bin/env python3
"""The admin panel's update notices (services/adminupdates.py).

    python tools/admin_updates_test.py

Two made-up games serve their status through olport, one with a good update report and one
with a broken one; a stand-in GitHub API and a stand-in deployer run on loopback. Checks that
the panel's own check of the core reads behind, current and unknown (no revision, a revision
that is not a commit, GitHub rate limited or out of reach), keeps GitHub's answers an hour and
forces a fresh check at most once a minute; that a game's own report is shown with its how,
and a broken one as unknown; that the deployer's items win over a game's own report and over
the panel's own check; and that clean_item() trims, caps and refuses what does not fit. No
database, no Docker, no network.
"""
import hashlib
import json
import os
import socket
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.abspath(os.path.join(HERE, os.pardir, "services"))
sys.path.insert(0, HERE)
sys.path.insert(0, SERVICES)
for k in [k for k in os.environ if k.startswith(("POL_ADMIN_", "POL_DEPLOYER_", "OPENLOBBY_"))]:
    del os.environ[k]
os.environ["POL_ADMIN_LIVE_TTL"] = "0"
os.environ["POL_ADMIN_MODULES"] = "kart,derby"

import olport  # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got, "" if ok else "  (want %r)" % (want,)))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def sha(n):
    return hashlib.sha1(("kart-%d" % n).encode()).hexdigest()


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


class Reply(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, obj, headers=()):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# --- a stand-in GitHub: one repository, 70 commits in a line, commit 30 a merge ---------------- #
MAIN = [{"sha": sha(i), "subject": "Merge branch side" if i == 30 else "Change %d" % i,
         "date": "2026-10-%02dT%02d:00:00+02:00" % (1 + i // 24, i % 24), "merge": i == 30}
        for i in range(70)]
GH = {"calls": [], "mode": "ok"}


def api(c):
    return {"sha": c["sha"], "parents": [{"sha": "0" * 40}] * (2 if c["merge"] else 1),
            "commit": {"message": c["subject"] + "\n\nmore", "committer": {"date": c["date"]}}}


def index(ref):
    if ref == "main":
        return len(MAIN) - 1
    for i, c in enumerate(MAIN):
        if len(ref) >= 7 and c["sha"].startswith(ref):
            return i
    return None


class GitHub(Reply):
    def do_GET(self):
        GH["calls"].append(self.path)
        if GH["mode"] == "limit":
            return self.reply(403, {"message": "API rate limit exceeded"}, [("X-RateLimit-Remaining", "0")])
        u = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(u.query)
        pre = "/repos/PrettyOpenLobby/OpenLobby/"
        rest = u.path[len(pre):] if u.path.startswith(pre) else ""
        if rest == "commits":
            i = index((q.get("sha") or ["main"])[0])
            if i is None:
                return self.reply(404, {"message": "No commit found"})
            return self.reply(200, [api(c) for c in reversed(MAIN[max(0, i - 49):i + 1])])
        if rest.startswith("compare/"):
            base, _, head = rest[len("compare/"):].partition("...")
            i, j = index(base), index(head)
            if i is None or j is None:
                return self.reply(404, {"message": "Not Found"})
            return self.reply(200, {"ahead_by": max(0, j - i), "behind_by": max(0, i - j),
                                    "commits": [api(c) for c in MAIN[i + 1:j + 1]]})
        return self.reply(404, {"message": "Not Found"})


# --- a stand-in deployer ------------------------------------------------------------------------ #
DTOKEN = "deployer-token-0123456789"
DEP = {"asked": [], "items": []}


class Deployer(Reply):
    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer " + DTOKEN:
            return self.reply(401, {"error": "unauthorized"})
        DEP["asked"].append(self.path)
        if urllib.parse.urlsplit(self.path).path == "/api/updates":
            return self.reply(200, {"checked_at": "2026-10-21T12:00:00Z", "checking": False,
                                    "items": DEP["items"], "channel": DEP.get("channel", "stable")})
        return self.reply(404, {"error": "not found"})


gh_srv, GH_URL = serve(GitHub)
dep_srv, DEP_URL = serve(Deployer)
DEAD = "http://127.0.0.1:%d" % free_port()

# --- two made-up games --------------------------------------------------------------------------- #
KART_UPDATE = {"running": {"commit": sha(60), "built_at": "2026-10-20T08:30:00Z"},
               "latest": {"commit": sha(62), "date": "2026-10-21T09:12:00+02:00"},
               "behind": 2,
               "changes": [{"commit": sha(62), "date": "2026-10-21T09:12:00Z",
                            "subject": "  Show lap times\n in the lobby  "},
                           {"commit": sha(61), "date": "2026-10-21T08:00:00Z", "subject": "Faster karts"}],
               "update": "available", "why": None, "how": "Pull the Kart Club image and restart it."}
GAMES = {"kart": {"up": True, "online": 1, "update": KART_UPDATE},
         "derby": {"up": True, "online": 0, "update": {"running": "yesterday", "update": "available"}}}
ports = {}
for key, title in (("kart", "Kart Club"), ("derby", "Derby Night")):
    p = olport.AdminPort(key, title, token="tok-" + key + "-0123456789")
    p.status((lambda k: lambda: GAMES[k])(key))
    host, pnum = p.start()
    ports[key] = p
    os.environ["POL_ADMIN_GAME_%s_URL" % key.upper()] = "http://127.0.0.1:%d" % pnum
    os.environ["POL_ADMIN_GAME_%s_TOKEN" % key.upper()] = "tok-" + key + "-0123456789"

import adminmodules  # noqa: E402
from adminmodules import sources  # noqa: E402
import adminupdates as U  # noqa: E402

ITEM_KEYS = ["behind", "changes", "how", "key", "latest", "name", "running", "update", "why"]


def items(out):
    return {i["key"]: i for i in out["items"]}


os.environ["POL_ADMIN_GITHUB_API"] = GH_URL
os.environ["OPENLOBBY_REVISION"] = sha(5)
os.environ["OPENLOBBY_BUILT_AT"] = "2026-10-20T08:30:00+00:00"

print("no deployer: the panel checks the core itself")
out = U.overview()
it = items(out)
core = it.get("core") or {}
keys = [i["key"] for i in out["items"]]
chk("the core, then the games that report", (keys[0], sorted(keys[1:])), ("core", ["derby", "kart"]))
chk("every item has exactly the agreed fields", all(sorted(i) == ITEM_KEYS for i in out["items"]), True)
chk("checked_at is ISO UTC", out["checked_at"].endswith("Z") and len(out["checked_at"]) == 20, True)
chk("core: available, behind 64",
    (core.get("update"), core.get("behind"), core["running"], core["latest"]["commit"]),
    ("available", 64, {"commit": sha(5), "built_at": "2026-10-20T08:30:00Z"}, sha(69)))
subjects = [c["subject"] for c in core["changes"]]
chk("  50 changes, newest first, no merges",
    (len(subjects), subjects[0], "Merge branch side" in subjects), (50, "Change 69", False))
chk("  dates in UTC", core["changes"][0]["date"], "2026-10-03T19:00:00Z")
chk("  how: the default", core["how"], "Run docker compose pull and docker compose up -d.")
kart = it.get("kart") or {}
chk("a game's own report, with its name and how",
    (kart.get("name"), kart.get("update"), kart.get("behind"), kart.get("how")),
    ("Kart Club", "available", 2, "Pull the Kart Club image and restart it."))
chk("  its strings trimmed and times in UTC",
    (kart["changes"][0]["subject"], kart["latest"]["date"]), ("Show lap times in the lobby", "2026-10-21T07:12:00Z"))
derby = it.get("derby") or {}
chk("a broken report: unknown, with a sentence", (derby.get("update"), "expected shape" in (derby.get("why") or "")),
    ("unknown", True))
chk("  and nothing of it shown", (derby["running"]["commit"], derby["changes"], derby["behind"]), (None, [], None))

print("the hour's cache and the forced check")
n = len(GH["calls"])
U.overview()
chk("a second look asks GitHub nothing", len(GH["calls"]), n)
U.overview(refresh=True)
m = len(GH["calls"])
chk("a forced check asks again", m > n, True)
U.overview(refresh=True)
chk("a second forced check within the minute asks nothing", len(GH["calls"]), m)

print("current and unknown")
os.environ["OPENLOBBY_REVISION"] = sha(69)[:12]
core = items(U.overview())["core"]
chk("at main's head (a 12-digit revision): current", (core["update"], core["behind"], core["changes"], core["how"]),
    ("current", 0, [], None))
os.environ["OPENLOBBY_REVISION"] = ""
core = items(U.overview())["core"]
chk("built without a revision: unknown, the newest still shown",
    (core["update"], core["latest"]["commit"], "without OPENLOBBY_REVISION" in core["why"]), ("unknown", sha(69), True))
os.environ["OPENLOBBY_REVISION"] = "release-candidate"
core = items(U.overview())["core"]
chk("a revision that is not a commit: unknown", (core["update"], "is not a commit" in core["why"]), ("unknown", True))
os.environ["OPENLOBBY_REVISION"] = "abcdef0123456789abcdef0123456789abcdef01"
core = items(U.overview())["core"]
chk("a commit the repository does not have: unknown", (core["update"], "is not in" in core["why"]), ("unknown", True))
os.environ["OPENLOBBY_REVISION"] = sha(5)
U.REFRESH_GAP = 0
GH["mode"] = "limit"
core = items(U.overview(refresh=True))["core"]
chk("GitHub rate limited: unknown, and why", (core["update"], "limit" in core["why"], core["changes"]),
    ("unknown", True, []))
GH["mode"] = "ok"
os.environ["POL_ADMIN_GITHUB_API"] = DEAD
core = items(U.overview())["core"]
chk("GitHub out of reach: unknown, and why", (core["update"], "could not be reached" in core["why"]),
    ("unknown", True))
os.environ["POL_ADMIN_GITHUB_API"] = GH_URL
os.environ["POL_ADMIN_UPDATE_HOW"] = "Run ./update.sh on the host."
core = items(U.overview(refresh=True))["core"]
chk("POL_ADMIN_UPDATE_HOW", (core["update"], core["how"]), ("available", "Run ./update.sh on the host."))
os.environ["POL_ADMIN_UPDATE_REPO"] = "off"
n = len(GH["calls"])
chk("POL_ADMIN_UPDATE_REPO=off: no core, and GitHub is not asked",
    ("core" in items(U.overview(refresh=True)), len(GH["calls"])), (False, n))
os.environ.pop("POL_ADMIN_UPDATE_REPO")

print("with a deployer")
os.environ["POL_DEPLOYER_URL"] = DEP_URL
os.environ["POL_DEPLOYER_TOKEN"] = DTOKEN
DEP["items"] = [
    {"key": "core", "name": "OpenLobby core (PlayOnline Viewer services)",
     "running": {"commit": sha(69), "built_at": None}, "latest": {"commit": sha(69), "date": "2026-10-03T21:00:00Z"},
     "behind": 0, "changes": [], "update": "current", "why": None, "how": None},
    {"key": "kartclub", "module": "kart", "name": "Kart Club",
     "running": {"commit": sha(61), "built_at": None}, "latest": {"commit": sha(62), "date": "2026-10-21T09:12:00Z"},
     "behind": 1, "changes": [{"commit": sha(62), "date": "2026-10-21T09:12:00Z", "subject": "Show lap times"}],
     "update": "available", "why": None, "how": "Update it from Settings > Install games."},
    {"key": "BAD KEY", "name": "dropped"},
]
n = len(GH["calls"])
out = U.overview()
it = items(out)
chk("the deployer's items, then the reports of games it has none for", [i["key"] for i in out["items"]],
    ["core", "kartclub", "derby"])
chk("  the deployer's core wins over the panel's own check, which asks GitHub nothing",
    (it["core"]["update"], len(GH["calls"]) - n), ("current", 0))
chk("  the deployer's item wins over the game's own report (matched by module)",
    (it["kartclub"]["behind"], it["kartclub"]["how"]), (1, "Update it from Settings > Install games."))
chk("  its checked_at, and no module field passed on", (out["checked_at"], "module" in it["kartclub"]),
    ("2026-10-21T12:00:00Z", False))
chk("  the stable channel by default", (out["channel"], "channel" in it["kartclub"]), ("stable", False))
os.environ["POL_ADMIN_CORE"] = "none"
it = items(U.overview())
chk("one game's own panel (POL_ADMIN_CORE=none): the core item is this panel, kept and renamed",
    (it["core"]["name"], it["core"]["update"]), ("OpenLobby admin panel", "current"))
os.environ.pop("POL_ADMIN_CORE")
chk("  with the core on, the deployer's own name", items(U.overview())["core"]["name"],
    "OpenLobby core (PlayOnline Viewer services)")
DEP["channel"] = "dev"
DEP["items"][1] = dict(DEP["items"][1], channel="dev", behind=None, changes=[],
                       why="A newer dev build is published: kart:main.")
DEP["items"][0] = dict(DEP["items"][0], channel="beta")
out = U.overview()
it = items(out)
chk("the dev channel: said for the list and for the item that follows a dev image",
    (out["channel"], it["kartclub"].get("channel"), it["kartclub"]["update"], it["kartclub"]["why"]),
    ("dev", "dev", "available", "A newer dev build is published: kart:main."))
chk("  a channel word it does not know marks nothing", "channel" in it["core"], False)
DEP["channel"] = "stable"
U.overview(refresh=True)
chk("refresh is passed on", DEP["asked"][-1], "/api/updates?refresh=1")
os.environ["POL_DEPLOYER_URL"] = DEAD
out = U.overview()
chk("a deployer that does not answer: the panel's own check and the games' reports",
    sorted((i["key"], i["update"]) for i in out["items"]),
    [("core", "available"), ("derby", "unknown"), ("kart", "available")])
chk("  and the stable channel", out["channel"], "stable")
del os.environ["POL_DEPLOYER_URL"]

print("the host's ops runner")
OPS = {"at": 1.0, "stale": False, "history": [],
       "actions": {"update-kart": {}, "auto-on-kart": {}, "auto-off-kart": {}, "update-derby": {}, "update-ghost": {}},
       "updates": {
           "kart": {"action": "update-kart", "timer": {"enabled": True, "active": True, "next": 1792806468.7,
                                                       "last": 1792806288.7, "on": "auto-on-kart", "off": "auto-off-kart"},
                    "running": None,
                    "last": {"ok": False, "result": "kart-sync.service failed (exit status 1, exit-code).",
                             "by": "owner", "at": 1792806000.0, "took": 3.5, "log": ["a  b", 7, "c" * 400]}},
           "derby": {"action": "update-derby", "timer": {"enabled": True, "active": True, "on": "auto-on-derby",
                                                         "off": "auto-off-derby"}},
           "ghost": {"action": "update-ghost"},
           "../x": {"action": "update-kart"},
           "nope": {"action": "update-nope"}}}
it = items(U.overview(ops=OPS))
k = it["kart"].get("ops") or {}
chk("a game with a report keeps it, and gets the runner's controls",
    (it["kart"]["update"], k.get("action"), k.get("auto"), k.get("next"), k.get("on")),
    ("available", "update-kart", True, 1792806468.7, "auto-on-kart"))
chk("  its last update as the runner said, the log kept as lines", (k["last"]["ok"], k["last"]["result"], k["last"]["log"]),
    (False, "kart-sync.service failed (exit status 1, exit-code).", ["a  b", "c" * 300]))
d = it["derby"].get("ops") or {}
chk("a timer whose actions the runner does not offer: no setting", (d.get("action"), d.get("auto"), d.get("on")),
    ("update-derby", None, None))
chk("a game this panel does not show, a bad key, an action not offered: left out",
    ("ghost" in it, "../x" in it, "nope" in it), (False, False, False))
chk("a stale runner: no controls", "ops" in items(U.overview(ops=dict(OPS, stale=True)))["kart"], False)
chk("no runner: no controls", "ops" in items(U.overview())["kart"], False)

print("cleaning what comes in")
good = {"running": {"commit": "ABCDEF1", "built_at": None}, "latest": {"commit": "abcdef2", "date": None},
        "behind": 60, "update": "available", "why": " a  b ", "how": "x" * 400,
        "changes": [{"commit": "%07x" % (0xabc0000 + i), "date": "2026-10-21T12:00:00Z", "subject": "c%d" % i}
                    for i in range(80)] + [{"commit": "zzz", "subject": "not hex"}]}
c = U.clean_item(good, key="kart", name="Kart Club")
chk("hex lower-cased, at most 50 changes, strings trimmed and capped",
    (c["running"]["commit"], len(c["changes"]), c["why"], len(c["how"])), ("abcdef1", 50, "a b", 300))
chk("a change that is not hex is dropped",
    any(x["subject"] == "not hex" for x in U.clean_item(dict(good, changes=good["changes"][-2:]), key="kart")["changes"]),
    False)
for what, raw in (("not an object", ["available"]),
                  ("an update word it does not know", dict(good, update="yes")),
                  ("running not an object", dict(good, running="abc1234")),
                  ("a commit that is not hex", dict(good, running={"commit": "HEAD"})),
                  ("a time that is not ISO", dict(good, latest={"commit": "abcdef2", "date": "tuesday"})),
                  ("a negative behind", dict(good, behind=-1)),
                  ("behind true", dict(good, behind=True)),
                  ("available but behind 0", dict(good, behind=0)),
                  ("changes not a list", dict(good, changes="lots"))):
    c = U.clean_item(raw, key="kart", name="Kart Club")
    chk("%s: unknown with why" % what, (c["update"], "expected shape" in (c["why"] or ""), c["changes"]),
        ("unknown", True, []))
chk("no usable key: dropped", (U.clean_item(good), U.clean_item(dict(good, key="../x"))), (None, None))
c = U.clean_item({"running": {}, "latest": {}, "update": "unknown"}, key="kart", name="Kart Club")
chk("unknown without a why gets one", (c["update"], c["why"]), ("unknown", "Kart Club cannot tell whether it is up to date."))
for what, raw in (("null running and latest", {"update": "unknown", "running": None, "latest": None,
                                                "why": "GitHub could not be reached."}),
                  ("no running or latest at all", {"update": "unknown", "why": "GitHub could not be reached."})):
    c = U.clean_item(raw, key="kart", name="Kart Club")
    chk("unknown with %s keeps its own why" % what, (c["update"], c["why"]),
        ("unknown", "GitHub could not be reached."))
c = U.clean_item({"update": "available", "running": None, "latest": None}, key="kart", name="Kart Club")
chk("available still needs both objects", "expected shape" in (c["why"] or ""), True)

print("a game that applies its own update (path B) keeps its fields")
c = U.clean_item(dict(good, apply=True, state="updating", message="  Building  the new image. " + "x" * 400),
                 key="kart", name="Kart Club")
chk("apply, state updating and its message (trimmed, capped)",
    (c.get("apply"), c.get("state"), c["message"][:24], len(c["message"]), c["update"]),
    (True, "updating", "Building the new image. ", 300, "available"))
c = U.clean_item(dict(good, apply=True, state="failed", why="The build failed."), key="kart", name="Kart Club")
chk("state failed with its why", (c.get("apply"), c.get("state"), c["why"]), (True, "failed", "The build failed."))
c = U.clean_item(dict(good, apply=True, state="failed", why=None), key="kart", name="Kart Club")
chk("  failed without a why gets one", c["why"], "Kart Club says its update failed, without saying why.")
c = U.clean_item(dict(good, running="abc1234", apply=True, state="updating", message="Restarting."),
                 key="kart", name="Kart Club")
chk("  kept on a report that does not fit the shape", (c["update"], c.get("apply"), c.get("state"), c.get("message")),
    ("unknown", True, "updating", "Restarting."))
c = U.clean_item({"running": {}, "latest": {}, "update": "unknown", "apply": True, "state": "updating"},
                 key="kart", name="Kart Club")
chk("  and on an unknown report", (c.get("apply"), c.get("state"), c.get("message")), (True, "updating", None))
c = U.clean_item(dict(good, apply="yes", state="busy", message="m"), key="kart", name="Kart Club")
chk("apply not true, a state it does not know: left out", ("apply" in c, "state" in c, "message" in c),
    (False, False, False))
chk("the status JSON passes them on", sources.generic({"update": {"update": "current", "apply": True,
                                                                  "state": "updating", "message": "m"}}).get("update"),
    {"update": "current", "apply": True, "state": "updating", "message": "m"})

print("the status JSON carries the report as it came")
chk("an object", sources.generic({"update": {"update": "current"}}).get("update"), {"update": "current"})
chk("not an object: left out", "update" in sources.generic({"update": "current"}), False)
chk("too large: left out", "update" in sources.generic({"update": {"x": "y" * (70 << 10)}}), False)
chk("read through a remote module's live status", adminmodules.live("kart", fresh=True).get("update"), KART_UPDATE)

for p in ports.values():
    p.stop()
gh_srv.shutdown()
dep_srv.shutdown()
print("admin_updates_test: %d failed" % bad)
sys.exit(1 if bad else 0)
