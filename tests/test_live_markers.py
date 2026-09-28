#!/usr/bin/env python3
"""The live-session markers (live_sessions.py) and the client-build record
(clientbuilds.py): what the deploy gate, the admin panel's Overview, the board
bots and the portal read from other containers.

    python tests/test_live_markers.py

Runs against the in-process store, then against a throwaway Valkey
(tools/pgtest.py), where the writer is a separate process -- the way a game
container publishes and the admin container reads.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.abspath(os.path.join(HERE, "..", "services"))
sys.path.insert(0, SERVICES)
sys.path.insert(0, os.path.join(HERE, "..", "tools"))

TMP = tempfile.mkdtemp(prefix="livemark-")
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_LOG_DIR"] = TMP

import pgtest  # noqa: E402
from polcore import kv  # noqa: E402
import live_sessions  # noqa: E402
import clientbuilds  # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


def cli(*args):
    p = subprocess.run([sys.executable, os.path.join(SERVICES, "live_sessions.py")]
                       + list(args), capture_output=True, text=True, timeout=60,
                       env=dict(os.environ))
    return p.returncode, p.stdout.strip()


def marker_suite():
    print(" markers")
    chk("no marker reads as unknown", live_sessions.read_count("fmo"), None)
    live_sessions.write_marker("fmo", 3)
    chk("a fresh marker reads its count", live_sessions.read_count("fmo"), 3)
    rec = live_sessions.read_marker("fmo")
    chk("the record carries a stamp", abs(rec["stamp"] - time.time()) < 5, True)
    live_sessions.write_marker("tm", 2, extra={"tables": ["#TM0R001"]})
    chk("extra fields ride along", live_sessions.read_marker("tm")["tables"],
        ["#TM0R001"])
    chk("the store key", live_sessions.marker_key("fmo"), "live:fmo")
    chk("the marker expires on its own", 0 < kv.ttl("live:fmo") <= live_sessions.MARKER_TTL,
        True)
    # backdate the stamp: a service that stopped publishing is not "0 live"
    old = dict(rec, stamp=time.time() - 600)
    kv.set("live:fmo", json.dumps(old))
    chk("a stale marker reads as unknown", live_sessions.read_count("fmo", stale=180), None)
    chk("...unless the reader accepts any age", live_sessions.read_count("fmo", stale=0), 3)
    chk("every marker is listed", sorted(live_sessions.live_markers()), ["fmo", "tm"])
    live_sessions.write_marker("fmo", 0)
    chk("zero is a count, not unknown", live_sessions.read_count("fmo"), 0)

    print(" the admin panel's Overview")
    import adminops
    live_sessions.write_marker("felobby", 5)
    kv.set("live:feworld", json.dumps({"count": 9, "stamp": time.time() - 3600}))
    rows = {r["label"]: r for r in adminops.live_counts()}
    chk("a fresh game shows its count", (rows["Front Mission Online"]["fresh"],
                                         rows["Front Mission Online"]["count"]), (True, 0))
    chk("with its own unit", rows["Tetra Master"]["unit"], "matches")
    chk("a stale one shows no count, and its age",
        (rows["Fantasy Earth (world)"]["fresh"], rows["Fantasy Earth (world)"]["count"],
         rows["Fantasy Earth (world)"]["age"] >= 3600), (False, None, True))
    chk("a service that never published is not listed", "Janhourou" in rows, False)

    print(" client builds")
    chk("an address with no record", clientbuilds.for_address("1.2.3.4"), {})
    chk("record", clientbuilds.record("1.2.3.4", "W2U", 1000, b"20061212_3"), True)
    clientbuilds.record("1.2.3.4", "PS2", 1, "20040520_1")
    got = clientbuilds.for_address("1.2.3.4")
    chk("both products, by region/product", sorted(got), ["PS2/1", "W2U/1000"])
    chk("the version as text", got["W2U/1000"]["version"], "20061212_3")
    chk("and when it was seen", got["W2U/1000"]["seen"].endswith("Z"), True)
    chk("another address is separate", clientbuilds.for_address("5.6.7.8"), {})
    chk("all addresses", sorted(clientbuilds.all_addresses()), ["1.2.3.4"])
    chk("the record expires on its own",
        0 < kv.ttl("clientbuild:1.2.3.4") <= clientbuilds.TTL, True)


print("in-process store")
kv.reset(kv.MemoryKV(prefix="livetest:"))
marker_suite()

print("\nValkey, the publisher in another process")
pgtest.use_fresh_valkey()
chk("the live-state store is Valkey", kv.default().backend, "valkey")
marker_suite()
WRITER = ("import sys; sys.path.insert(0, %r)\n"
          "import live_sessions, clientbuilds, threading\n"
          "live_sessions.write_marker('authsess-jan', 4)\n"
          "clientbuilds.record('9.9.9.9', 'W2U', 1000, '20110101_1')\n" % SERVICES)
p = subprocess.run([sys.executable, "-c", WRITER], env=dict(os.environ),
                   capture_output=True, text=True, timeout=60)
chk("the publisher ran", p.returncode, 0)
chk("this process reads its count", live_sessions.read_count("authsess-jan"), 4)
chk("and its client build", clientbuilds.for_address("9.9.9.9")["W2U/1000"]["version"],
    "20110101_1")
chk("the deploy gate's command line reads it", cli("count", "authsess-jan"), (0, "4"))
chk("...and says nothing for a service with no marker", cli("count", "nothing"), (0, ""))
code, out = cli("list")
chk("the list names every marker", (code, sorted(ln.split()[0] for ln in out.splitlines())),
    (0, ["authsess-jan", "felobby", "feworld", "fmo", "tm"]))

print("\n%s" % ("all passed" if not bad else "%d FAILED" % bad))
sys.exit(1 if bad else 0)
