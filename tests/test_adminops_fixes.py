#!/usr/bin/env python3
"""The admin panel's side of the server fixes: it reads the host runner's
status and files a request only for a fix that status lists.

    python tests/test_adminops_fixes.py
"""
import glob
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))

import adminops  # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


data = tempfile.mkdtemp(prefix="ops-")
NOW = 1_800_000_000.0


def write_status(**over):
    st = {"at": NOW, "checks": [], "running": None,
          "actions": {"dns": {"name": "Restart DNS", "warning": "w"}}}
    st.update(over)
    os.makedirs(os.path.join(data, "ops"), exist_ok=True)
    with open(os.path.join(data, "ops", "status.json"), "w") as f:
        json.dump(st, f)


def requests():
    return sorted(glob.glob(os.path.join(data, "ops", "requests", "*.json")))


print("no runner yet")
chk("no status", adminops.ops_status(data, NOW), None)
chk("refused", adminops.ops_request(data, "dns", "owner", NOW)[0], False)

print("a fresh status")
write_status()
st = adminops.ops_status(data, NOW + 10)
chk("read", st["actions"]["dns"]["name"], "Restart DNS")
chk("not stale", st["stale"], False)

print("a listed fix is filed")
ok, msg = adminops.ops_request(data, "dns", "owner", NOW + 10)
chk("accepted", ok, True)
chk("message names the fix", msg.startswith("Restart DNS requested"), True)
files = requests()
chk("one request file", len(files), 1)
with open(files[0]) as f:
    req = json.load(f)
chk("request body", (req["action"], req["by"]), ("dns", "owner"))
chk("no temp file left", [p for p in os.listdir(os.path.join(data, "ops", "requests"))
                          if p.startswith(".")], [])
os.remove(files[0])

print("anything else is refused")
for what in ("edge", "../dns", "dns; reboot", None, 7, "DNS"):
    chk("refused %r" % (what,), adminops.ops_request(data, what, "owner", NOW + 10)[0], False)
chk("no files", requests(), [])

print("a fix already running blocks another")
write_status(running={"action": "dns", "by": "x", "at": NOW})
chk("refused while running", adminops.ops_request(data, "dns", "owner", NOW + 10)[0], False)

print("a stale runner turns the buttons off")
write_status()
chk("stale", adminops.ops_status(data, NOW + adminops.OPS_STALE + 1)["stale"], True)
ok, msg = adminops.ops_request(data, "dns", "owner", NOW + adminops.OPS_STALE + 1)
chk("refused when stale", ok, False)
chk("says why", "not running" in msg, True)

print("a corrupt status is treated as none")
with open(os.path.join(data, "ops", "status.json"), "w") as f:
    f.write("{nope")
chk("none", adminops.ops_status(data, NOW), None)

print("FAIL: %d" % bad if bad else "all ok")
sys.exit(1 if bad else 0)
