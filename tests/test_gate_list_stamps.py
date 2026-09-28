#!/usr/bin/env python3
"""The login record's "last updated" stamps (0x14 mail, 0x24 friends, 0x28
handles) hold still until the list really changes.

    python tests/test_gate_list_stamps.py

Reported 2026-09-27: read messages and accepted friend requests came back
as NEW after every relog, with the server mailbox empty -- the client re-flagging
its own cache. We had served `now` in those fields on every login.
POL_GATE_LIST_STAMPS=1 serves real change times instead (default off).
"""
import os
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))

tmp = tempfile.mkdtemp(prefix="gatestamps-")
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import pgtest  # noqa: E402
pgtest.use_fresh_database()
os.environ.pop("POL_GATE_LIST_STAMPS", None)

import accounts  # noqa: E402
import responders  # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


db = accounts.connect()
acct = accounts.register_account(db, "Stampy", "abc12345", contents=(1,))
mid = acct["member_id"]

print("list_stamp")
chk("first sight stores now", accounts.list_stamp(db, mid, "friends", "A", now=1000), 1000)
chk("same list later -> same stamp", accounts.list_stamp(db, mid, "friends", "A", now=2000), 1000)
chk("changed list -> new stamp", accounts.list_stamp(db, mid, "friends", "B", now=3000), 3000)
chk("and it holds", accounts.list_stamp(db, mid, "friends", "B", now=4000), 3000)
chk("grow: first", accounts.list_stamp(db, mid, "mail", 500, grow=True, now=1000), 1000)
chk("grow: a message READ (newest drops) does not bump",
    accounts.list_stamp(db, mid, "mail", 400, grow=True, now=2000), 1000)
chk("grow: an empty box does not bump",
    accounts.list_stamp(db, mid, "mail", 0, grow=True, now=2500), 1000)
chk("grow: a NEWER message bumps",
    accounts.list_stamp(db, mid, "mail", 600, grow=True, now=3000), 3000)

print("fingerprints")
f1 = accounts.friend_list_fingerprint(db, mid)
h1 = accounts.handle_list_fingerprint(db, mid)
chk("friend fingerprint stable", accounts.friend_list_fingerprint(db, mid), f1)
hid = db.execute("SELECT id FROM handle WHERE member_id = %s", (mid,)).fetchone()["id"]
db.execute("INSERT INTO friend (handle_id, peer_name, created_at) VALUES (%s,%s,%s)",
           (hid, "Buddy", "2026-09-27T00:00:00Z"))
db.commit()
chk("friend fingerprint moves on a new friend",
    accounts.friend_list_fingerprint(db, mid) != f1, True)
accounts.set_handle(db, mid, "Stampy2", primary=False)
chk("handle fingerprint moves on a new handle",
    accounts.handle_list_fingerprint(db, mid) != h1, True)

print("gate record")


member = accounts.get_member(db, acct["polid"])
chk("knob off -> no stamps", responders.gate_list_stamps(db, member), None)
off_a = responders.build_gate_record("203.0.113.1", 51220, pad_to=0, unread=0)
off_b = responders.build_gate_record("203.0.113.1", 51220, pad_to=0, unread=0,
                                     stamps=None)
chk("stamps=None is the old record", off_a == off_b, True)

os.environ["POL_GATE_LIST_STAMPS"] = "1"
responders._mailbox = lambda m=None: [(1234, "O/m/x", {}), (999, "O/m/y", {})]
s1 = responders.gate_list_stamps(db, member)
chk("knob on -> three stamps", sorted(s1), [0x14, 0x24, 0x28])
s2 = responders.gate_list_stamps(db, member)
chk("a second login with nothing changed serves the SAME stamps", s2, s1)
responders._mailbox = lambda m=None: [(999, "O/m/y", {})]      # 1234 was read
chk("reading a message does not move the mail stamp",
    responders.gate_list_stamps(db, member)[0x14], s1[0x14])

rec = responders.build_gate_record("203.0.113.1", 51220, pad_to=0, unread=0,
                                   stamps={0x14: 0x11111111, 0x24: 0x22222222,
                                           0x28: 0x33333333})
raw = responders._b64decode(rec)
chk("0x14 written", struct.unpack_from("<I", raw, 0x14)[0], 0x11111111)
chk("0x24 written", struct.unpack_from("<I", raw, 0x24)[0], 0x22222222)
chk("0x28 written", struct.unpack_from("<I", raw, 0x28)[0], 0x33333333)

db.close()
print("FAILED: %d" % bad if bad else "all ok")
sys.exit(1 if bad else 0)
